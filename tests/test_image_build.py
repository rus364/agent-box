"""Image freshness is about build inputs, never unrelated package changes."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from agent_box import image_build, runtime
from agent_box.config import Config
from agent_box.errors import AgentBoxError
from agent_box.identity import HostIdentity
from agent_box.workspace import Workspace
from test_runtime import IsolatedRuntimeTest


class ImageInputsTests(IsolatedRuntimeTest):
    def setUp(self):
        super().setUp()
        self.source = Path(self._state_tmp.name) / 'source'
        self.source.mkdir()
        for name in image_build.CONTEXT_FILES:
            (self.source / name).write_bytes((runtime.PACKAGE_ROOT / name).read_bytes())
        runtime.BUILD_STATE.mkdir(parents=True)
        runtime.ensure_refresh_token()
        self.identity = HostIdentity('tester', 1000, 1000, '/home/tester')
        self.config = Config()

    def fingerprint(self):
        args = runtime.image_build_arguments(self.config, self.identity)
        with image_build.context(self.source, runtime.BUILD_STATE, args) as (_, fingerprint, _):
            return fingerprint

    def test_only_build_inputs_change_the_fingerprint(self):
        """Python/skill changes must not rebuild; each consumed input must invalidate."""
        original = self.fingerprint()
        (self.source / 'runtime.py').write_text('unrelated launcher change')
        (self.source / 'skills').mkdir()
        (self.source / 'skills/help.md').write_text('a new hint')
        self.config.box.browser.enabled = True
        self.assertEqual(self.fingerprint(), original)
        for name in image_build.CONTEXT_FILES:
            path = self.source / name
            previous = path.read_bytes()
            path.write_bytes(previous + b'\n# changed\n')
            self.assertNotEqual(self.fingerprint(), original, name)
            path.write_bytes(previous)
        self.config.box.tools.groups['analysis'] = False
        self.assertNotEqual(self.fingerprint(), original)
        self.config = Config()
        runtime.TOOLS_REFRESH_FILE.write_text('new refresh\n')
        self.assertNotEqual(self.fingerprint(), original)
        runtime.TOOLS_REFRESH_FILE.write_text('initial\n')
        self.identity = HostIdentity('other', 1001, 1001, '/home/other')
        self.assertNotEqual(self.fingerprint(), original)

    def test_changed_nested_id_ranges_invalidate_the_image(self):
        """A new namespace mapping must rebuild instead of leaving broken newuidmap."""
        with patch.object(runtime, 'nested_podman_build_args', return_value=['--build-arg', 'PODMAN_UID_RANGES=[[1,2]]']):
            previous = self.fingerprint()
        with patch.object(runtime, 'nested_podman_build_args', return_value=['--build-arg', 'PODMAN_UID_RANGES=[[3,4]]']):
            self.assertNotEqual(self.fingerprint(), previous)

    def test_relocated_cache_is_provisioned_by_a_matching_image(self):
        """A cache outside the home needs image-owned permissions, not a missing host bind."""
        original = self.fingerprint()
        with patch.object(runtime, 'XDG_CACHE_HOME', Path('/srv/agent-cache')):
            arguments = runtime.image_build_arguments(self.config, self.identity)
            self.assertIn('USER_CACHE_HOME=/srv/agent-cache', arguments)
            self.assertNotEqual(self.fingerprint(), original)

    def test_podman_reads_the_bytes_that_were_hashed(self):
        """Replacing an editable/package source during a build cannot mislabel it."""
        before = (self.source / 'nested_podman.py').read_bytes()
        with image_build.context(self.source, runtime.BUILD_STATE, []) as (staged, _, _):
            (self.source / 'nested_podman.py').write_text('replacement')
            self.assertEqual((staged / 'nested_podman.py').read_bytes(), before)

    def test_missing_legacy_and_stale_images_build_but_current_image_reuses(self):
        """An unlabeled public predecessor needs one build, not assumed compatibility."""
        for current, count in ((None, 1), (('old', None), 1), (('old', 'stale'), 1),
                               (('selected', self.fingerprint()), 0)):
            with self.subTest(current=current), patch.object(runtime, 'detect_host_identity', return_value=self.identity), \
                 patch.object(image_build, 'current', return_value=current), \
                 patch.object(image_build, 'build', return_value=(0, 'selected')) as build:
                self.assertEqual(runtime.ensure_workspace_image(self.source, self.config), 'selected')
                self.assertEqual(build.call_count, count)

    def test_concurrent_launches_recheck_after_lock_and_build_once(self):
        """Two first launches must not spend time rebuilding the same inputs twice."""
        stored = None
        def build(_image, _directory, fingerprint, *_args):
            nonlocal stored
            time.sleep(.05)
            stored = ('selected', fingerprint)
            return 0, 'selected'
        with patch.object(runtime, 'detect_host_identity', return_value=self.identity), \
             patch.object(image_build, 'current', side_effect=lambda _: stored), \
             patch.object(image_build, 'build', side_effect=build) as builds:
            with ThreadPoolExecutor(2) as pool:
                futures = [pool.submit(runtime.ensure_workspace_image, self.source, self.config) for _ in range(2)]
                self.assertEqual([f.result() for f in futures], ['selected', 'selected'])
            self.assertEqual(builds.call_count, 1)

    def test_required_build_failure_aborts_and_next_launch_retries(self):
        """A failed refresh cannot silently launch stale tools or mark success."""
        with patch.object(runtime, 'detect_host_identity', return_value=self.identity), \
             patch.object(image_build, 'current', return_value=('old', None)), \
             patch.object(image_build, 'build', side_effect=[(42, ''), (0, 'new')]) as build:
            with self.assertRaisesRegex(AgentBoxError, 'previous image retained'):
                runtime.ensure_workspace_image(self.source, self.config)
            self.assertEqual(runtime.ensure_workspace_image(self.source, self.config), 'new')
            self.assertEqual(build.call_count, 2)

    def test_selected_image_survives_a_shared_tag_change_before_creation(self):
        """A parallel build for another config cannot change this launch's image."""
        with patch.object(runtime, 'IMAGE', 'localhost/changed-tag'):
            spec = runtime.workspace_container_spec(Workspace(self.source, self.source), [],
                                                    runtime.PACKAGE_ROOT, self.config, image='sha256:selected')
        self.assertIn('sha256:selected', spec.create_argv)
        self.assertNotIn('localhost/changed-tag', spec.create_argv)
        self.assertEqual(spec.image, 'sha256:selected')


class ImagePublicationTests(unittest.TestCase):
    def test_failed_build_never_moves_the_usable_tag(self):
        """Publishing is separate from building so failed builds preserve the last image."""
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(image_build.subprocess, 'call', return_value=9), \
             patch.object(image_build.subprocess, 'run') as run:
            self.assertEqual(image_build.build('image', Path(tmp), 'digest', Path(tmp)/'id', [], False), (9, ''))
            run.assert_not_called()

    def test_publication_uses_verified_iid_and_preserves_explicit_pull_policy(self):
        """A build's exact result, not a concurrent tag lookup, must be published."""
        identity = 'sha256:' + 'a'*64
        for no_cache in (False, True):
            with self.subTest(no_cache=no_cache), tempfile.TemporaryDirectory() as tmp:
                iid = Path(tmp)/'id'
                def build(argv):
                    self.assertNotIn('--tag', argv)
                    self.assertIn('--pull=always' if no_cache else '--pull=missing', argv)
                    self.assertIn('--layers', argv)
                    self.assertEqual('--no-cache' in argv, no_cache)
                    iid.write_text(identity)
                    return 0
                result = json.dumps([{'Id': identity, 'Config': {'Labels': {image_build.LABEL: 'digest'}}}])
                with patch.object(image_build.subprocess, 'call', side_effect=build), \
                     patch.object(image_build.subprocess, 'run', return_value=Mock(returncode=0, stdout=result)) as run:
                    self.assertEqual(image_build.build('published', Path(tmp), 'digest', iid, [], no_cache), (0, identity))
                    self.assertEqual(run.call_args.args[0], ['podman', 'tag', identity, 'published'])

    def test_inspection_failure_is_not_permission_to_build(self):
        """Podman storage errors must not become an expensive destructive guess."""
        with patch.object(image_build.subprocess, 'run', return_value=Mock(returncode=125, stderr='storage error')):
            with self.assertRaisesRegex(AgentBoxError, 'storage error'):
                image_build.current('image')

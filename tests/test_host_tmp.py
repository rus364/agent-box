"""Whole-host temporary sharing is host consent, bounded by the box lifetime."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_box import browser, config, k3s, launch_options, mounts, project_config, runtime
from agent_box.errors import AgentBoxError
from agent_box.launch_options import LaunchOptions
from agent_box.mounts import MountError, MountSpec
import test_launch_options as launch_tests
import test_workspace_container as workspace_tests
import test_k3s as k3s_tests


class HostTmpConfigTests(unittest.TestCase):
    def test_published_configuration_upgrades_without_granting_host_tmp(self):
        """An additive opt-in must not expose files on a direct old-version upgrade."""
        fixture = Path(__file__).parent / 'fixtures/compatibility/1.0.0/config.json'
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'config.json'
            path.write_bytes(fixture.read_bytes())
            original = path.read_bytes()
            loaded = config.load_config(path)
            self.assertFalse(loaded.box.host_tmp)
            self.assertEqual(path.read_bytes(), original)
            loaded.box.host_tmp = True
            config.save_config(loaded, path)
            self.assertTrue(config.load_config(path).box.host_tmp)
            loaded.box.host_tmp = False
            config.save_config(loaded, path)
            self.assertFalse(config.load_config(path).box.host_tmp)
        self.assertFalse(config.BoxConfig.from_dict({'host_tmp': 'yes'}).host_tmp)

    def test_a_repository_cannot_enable_or_disable_host_tmp_policy(self):
        """A checked-out project cannot grant itself unrelated host files and sockets."""
        for enabled in (False, True):
            with self.assertRaisesRegex(AgentBoxError, 'host_tmp is not a project field'):
                project_config.validate({'box': {'host_tmp': enabled}}, Path('/project/.agent-box.json'))

    def test_host_tmp_ignores_tmpdir_and_does_no_resolution_when_disabled(self):
        """Caller-controlled temporary locations must not redirect an opt-in bind."""
        with patch.dict(os.environ, {'TMPDIR': '/untrusted'}):
            self.assertEqual(mounts.host_tmp_mounts(True), [mounts.parse_mount_arg('/tmp')])
        with patch.object(mounts, 'parse_mount_arg') as parse:
            self.assertEqual(mounts.host_tmp_mounts(False), [])
            parse.assert_not_called()


class HostTmpSessionTests(launch_tests.IsolatedLaunchTest):
    def test_cli_choice_survives_snapshot_without_changing_saved_defaults(self):
        """Per-launch sharing must reach nested consumers without persisting consent."""
        for enabled in (True, False):
            with self.subTest(enabled=enabled):
                saved = config.Config()
                saved.box.host_tmp = not enabled
                launched, snapshot = self.launch(saved, {'host-tmp': enabled})
                self.assertEqual(saved.box.host_tmp, not enabled)
                self.assertEqual(launched.box.host_tmp, enabled)
                self.assertFalse(launched.box.browser.enabled)
                self.assertFalse(launched.box.k3s.enabled)
                with patch.dict(os.environ, {'AGENT_BOX_RUNTIME_CONFIG': str(snapshot)}):
                    self.assertEqual(config.effective_config().box.host_tmp, enabled)
                parsed, rest = launch_options.parse(['--host-tmp' if enabled else '--no-host-tmp', 'codex'])
                self.assertEqual(parsed.overrides, {'host-tmp': enabled})
                self.assertEqual(rest, ['codex'])


class HostTmpWorkspaceTests(unittest.TestCase):
    setUp = workspace_tests.WorkspaceContainerLifecycleTests.setUp
    _container = workspace_tests.WorkspaceContainerLifecycleTests._container
    _live_box = workspace_tests.WorkspaceContainerLifecycleTests._live_box

    def test_enabled_mount_is_structural_and_conflicting_read_only_mounts_fail(self):
        """The opt-in must use normal overlap rules and change the immutable box spec."""
        self.config.box.host_tmp = True
        enabled = runtime.workspace_container_spec(self.workspace, [], self.script_dir, self.config)
        self.assertNotEqual(self.spec.base_config_hash, enabled.base_config_hash)
        self.assertEqual(enabled.create_argv.count('type=bind,src=/tmp,dst=/tmp'), 1)
        duplicate = runtime.workspace_container_spec(
            self.workspace, [MountSpec(Path('/tmp'), False)], self.script_dir, self.config)
        self.assertEqual(duplicate.create_argv.count('type=bind,src=/tmp,dst=/tmp'), 1)
        for path in (Path('/tmp'), self.workspace.host_root):
            with self.subTest(path=path), self.assertRaisesRegex(MountError, 'conflicting mount modes'):
                runtime.workspace_container_spec(self.workspace, [MountSpec(path, True)],
                                                 self.script_dir, self.config)

    def test_join_keeps_creation_choice_and_refuses_a_conflicting_cli_override(self):
        """A later saved setting must not grant browser access absent from a live box."""
        for enabled in (True, False):
            with self.subTest(enabled=enabled):
                created = config.Config()
                created.box.host_tmp = enabled
                container, policy = self._live_box(created)
                if enabled:
                    container['Mounts'].append({'Source': '/tmp', 'Destination': '/tmp', 'RW': True})
                desired = config.Config()
                desired.box.host_tmp = not enabled
                with patch.object(runtime, 'inspect_workspace_container', return_value=container), \
                     patch.object(runtime, 'runtime_config_base', return_value=desired):
                    existing = runtime.existing_workspace_spec(self.workspace, LaunchOptions())
                    self.assertEqual(existing.creation_config.box.host_tmp, enabled)
                    launched = runtime.launch_config(box_policy=policy)
                    self.assertEqual(launched.box.host_tmp, enabled)
                    self.assertEqual(launched._project_base.box.host_tmp, enabled)
                    with self.assertRaisesRegex(AgentBoxError, 'host-tmp.*agent-box stop'):
                        runtime.existing_workspace_spec(
                            self.workspace, LaunchOptions(overrides={'host-tmp': not enabled}))

    def test_legacy_policy_does_not_gain_a_new_host_mount(self):
        """An omitted additive field means disabled even when current host settings enable it."""
        policy = runtime.workspace_box_policy(self.spec)
        policy['box'].pop('host_tmp')
        desired = config.Config()
        desired.box.host_tmp = True
        config.apply_box_policy(desired, policy)
        self.assertFalse(desired.box.host_tmp)

    def test_explicit_mount_remains_when_convenience_is_disabled(self):
        """Withdrawing the shortcut cannot silently remove a user's explicit bind."""
        for read_only in (False, True):
            with self.subTest(read_only=read_only):
                selected = runtime.workspace_container_spec(
                    self.workspace, [MountSpec(Path('/tmp/other'), read_only)], self.script_dir, self.config)
                expected = 'type=bind,src=/tmp/other,dst=/tmp/other' + (',ro=true' if read_only else '')
                self.assertIn(expected, selected.create_argv)
                self.assertNotIn('type=bind,src=/tmp,dst=/tmp', selected.create_argv)


class HostTmpBrowserTests(unittest.TestCase):
    def test_edited_creation_policy_cannot_grant_absent_host_tmp_to_browser(self):
        """A writable policy file cannot grant the sibling more than the actual workspace bind."""
        container = {'Mounts': [], 'Config': {'Labels': {
            runtime.WORKSPACE_CONTAINER_EXTRA_MOUNTS_LABEL: runtime.explicit_mounts_hash([])}}}
        policy = {'box': {'host_tmp': True}}
        for actual in ([], [{'Source': '/tmp', 'Destination': '/tmp', 'RW': False}],
                       [{'Source': '/other', 'Destination': '/tmp', 'RW': True}]):
            with self.subTest(actual=actual):
                container['Mounts'] = actual
                with self.assertRaisesRegex(AgentBoxError, 'browser mounts differ from the workspace'):
                    runtime.verified_browser_mounts(policy, container)
        container['Mounts'] = [{'Source': '/tmp', 'Destination': '/tmp', 'RW': True}]
        self.assertEqual(runtime.verified_browser_mounts(policy, container), [])

    def test_both_launch_modes_share_only_when_selected_and_reject_read_only_overlap(self):
        """Browser uploads must see the same host paths without broadening defaults."""
        for container in ('', 'workspace'):
            with self.subTest(container=container):
                args = browser.container_command('/state/session', '/project', '/project', [], container=container)
                self.assertNotIn('type=bind,src=/tmp,dst=/tmp,ro=false', args)
                args = browser.container_command('/state/session', '/project', '/project', [],
                                                  container=container, host_tmp=True)
                self.assertIn('type=bind,src=/tmp,dst=/tmp,ro=false', args)
                with self.assertRaisesRegex(MountError, 'conflicting mount modes'):
                    browser.container_command('/state/session', '/project', '/project',
                                              [MountSpec(Path('/tmp/private'), True)], host_tmp=True)


class HostTmpK3sTests(k3s_tests.SidecarTestCase):
    def test_toggling_sharing_recreates_sidecar_but_retains_data_and_private_run(self):
        """A changed bind is structural; replacing it must not replace cluster storage."""
        settings = config.K3sConfig(enabled=True)
        k3s.ensure(k3s_tests.WID, settings, self.state, self.root)
        original = k3s.spec_hash(k3s_tests.WID, settings, self.state, self.root)
        for enabled in (True, False):
            with self.subTest(enabled=enabled):
                self.assertTrue(k3s.is_outdated(k3s_tests.WID, settings, self.state, self.root, host_tmp=enabled))
                k3s.ensure(k3s_tests.WID, settings, self.state, self.root, host_tmp=enabled)
                self.assertFalse(k3s.is_outdated(k3s_tests.WID, settings, self.state, self.root, host_tmp=enabled))
                self.assertEqual(self.podman.volumes, {k3s_tests.NAME})
                args = self.podman.did('create')[-1]
                self.assertEqual('--mount=type=bind,src=/tmp,dst=/tmp' in args, enabled)
                self.assertIn('--tmpfs=/run', args)
                self.assertIn('--tmpfs=/var/run', args)
        self.assertEqual(k3s.spec_hash(k3s_tests.WID, settings, self.state, self.root), original)

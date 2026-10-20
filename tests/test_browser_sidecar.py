"""Sibling browser boundaries and migration, independent of installed Podman."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from agent_box import browser, migrations, config, config_menu, project_config, runtime
from agent_box.errors import AgentBoxError
from agent_box.mounts import MountSpec
from agent_box.workspace import Workspace
from test_runtime import IsolatedRuntimeTest


class BrowserPolicyTests(unittest.TestCase):
    def test_reused_native_command_cannot_remove_a_neighbours_container_on_failure(self):
        """Codex resume can overlap two launches of the same prepared MCP command."""
        for personal in (False, True):
            with self.subTest(personal=personal):
                prepared = browser.container_command('/state/session', '/project', '/project', [],
                                                     personal=personal)
                original = list(prepared)
                active = set()
                started = []
                removed = []

                def name(argv):
                    return argv[argv.index('--name') + 1]

                def remove(argv):
                    removed.append(name(argv))
                    active.discard(name(argv))

                def launch(argv):
                    started.append(list(argv))
                    if len(started) == 2:
                        raise OSError('second process failed to start')
                    active.add(name(argv))
                    with self.assertRaisesRegex(OSError, 'second process failed'):
                        browser.run(prepared)
                    self.assertIn(name(argv), active, 'failed neighbour removed the live browser')
                    return 0

                with patch.object(browser.signal, 'signal'), \
                        patch.object(browser.subprocess, 'call', side_effect=launch), \
                        patch.object(browser, 'remove_container', side_effect=remove):
                    self.assertEqual(browser.run(prepared), 0)
                self.assertEqual(prepared, original)
                self.assertNotEqual(name(started[0]), name(started[1]))
                self.assertEqual(removed, [name(started[1]), name(started[0])])
                self.assertFalse(active)
                for argv in started:
                    unchanged = list(argv)
                    unchanged[unchanged.index('--name') + 1] = name(original)
                    self.assertEqual(unchanged, original)

    def test_edited_session_metadata_cannot_add_host_paths_to_browser(self):
        """An inherited mount needs both the user's selection and the actual bind."""
        mounts = [MountSpec(Path('/project/assets'), False)]
        container = {'Mounts': [{'Source': '/project', 'Destination': '/project', 'RW': True}],
                     'Config': {'Labels': {runtime.WORKSPACE_CONTAINER_EXTRA_MOUNTS_LABEL:
                                           runtime.explicit_mounts_hash(mounts)}}}
        policy = {'browser_mounts': [['/private', False]]}
        with self.assertRaisesRegex(AgentBoxError, 'differ from the workspace'):
            runtime.verified_browser_mounts(policy, container)
        policy['browser_mounts'] = [['/project/assets', False]]
        self.assertEqual(runtime.verified_browser_mounts(policy, container),
                         [MountSpec(Path('/project/assets'), False)])

    def test_session_metadata_cannot_reclassify_credentials_as_explicit_mount(self):
        """Credential binds already in the workspace must not leak to the browser."""
        container = {'Mounts': [{'Source': '/user/.codex', 'Destination': '/user/.codex', 'RW': True}],
                     'Config': {'Labels': {runtime.WORKSPACE_CONTAINER_EXTRA_MOUNTS_LABEL:
                                           runtime.explicit_mounts_hash([])}}}
        self.assertEqual(runtime.verified_browser_mounts({}, container), [])
        with self.assertRaisesRegex(AgentBoxError, 'explicit workspace mounts'):
            runtime.verified_browser_mounts({'browser_mounts': [['/user/.codex', False]]}, container)

    def test_browser_menu_keeps_labels_short_and_dependencies_in_help(self):
        """Users must see the documented controls before opening their explanations."""
        self.assertEqual(config_menu.label_for('browser', ('box',)), 'Browser')
        self.assertEqual(config_menu.label_for('enabled', ('box', 'browser')), 'Enabled')
        self.assertIn('rootless Podman', config_menu.HELP['box.browser'])

    def test_workspace_follows_its_network_and_only_selected_files(self):
        """Moving out of the workspace must neither break localhost nor copy credentials."""
        args = browser.container_command(Path('/state/session'), '/project', '/project/sub',
                                         [MountSpec(Path('/uploads'), True)], container='workspace-1')
        self.assertIn('--network=container:workspace-1', args)
        self.assertIn('type=bind,src=/uploads,dst=/uploads,ro=true', args)
        self.assertIn('--isolated', args)
        self.assertIn('--pull=never', args)
        self.assertNotIn('--privileged', args)
        self.assertNotIn('/run/podman', ' '.join(args))
        self.assertNotIn('.codex', ' '.join(args))
        self.assertEqual(args[args.index('--workdir')+1], '/project/sub')

    def test_native_host_retains_localhost_and_session_private_output(self):
        """Host browser URLs and returned filenames must retain their usual meaning."""
        args = browser.container_command(Path('/state/a'), '/project', '/project', [])
        self.assertIn('--network=host', args)
        self.assertEqual(args[-2:], ['--output-dir', '/state/a/output'])
        self.assertIn('--userns=keep-id:uid=1000,gid=1000', args)

    def test_image_storage_error_is_not_treated_as_permission_to_pull(self):
        """An unavailable Podman store must not trigger an unrelated network operation."""
        with patch.object(browser.host_podman, 'require'), patch.object(browser.subprocess, 'run',
                return_value=Mock(returncode=125, stderr='storage inaccessible')) as run:
            with self.assertRaisesRegex(AgentBoxError, 'storage inaccessible'):
                browser.ensure_image()
        self.assertEqual(run.call_count, 1)

    def test_broker_owner_survives_launcher_exit_only_while_its_tmux_session_exists(self):
        """Detaching a terminal must preserve browsing; a dead agent must release it."""
        owner = dict(pid=123, identity='12345', container='workspace', session='codex-1')
        with patch.object(browser, 'process_identity', return_value=''), \
             patch.object(runtime, 'tmux_session_exists', return_value=True) as exists:
            self.assertTrue(browser.owner_alive(owner))
            exists.assert_called_once_with('workspace', 'codex-1')
            exists.return_value = False
            self.assertFalse(browser.owner_alive(owner))

    def test_stop_filters_both_workspace_and_launch_mode(self):
        """Stopping host sessions must not kill an ordinary workspace browser."""
        with patch.object(browser.subprocess, 'run', return_value=Mock(returncode=0, stdout='abc\n')) as run:
            browser.stop('project-1', 'host')
        args = run.call_args_list[0].args[0]
        self.assertIn('label=io.agent-box.browser.workspace=project-1', args)
        self.assertIn('label=io.agent-box.browser.mode=host', args)
        self.assertEqual(run.call_args_list[1].args[0][-1], 'abc')


class BrowserMigrationTests(unittest.TestCase):
    def test_pending_proposals_migrate_leaf_and_section_changes_without_applying_them(self):
        """An old proposal must still request the same opt-in/reset after an upgrade."""
        old = {'set': {'box.tools': {'browser': True, 'analysis': False}}, 'reset': ['box.tools.browser']}
        moved = migrations.CONFIG_PROPOSAL.prepare(old)
        self.assertEqual(moved['set'], {'box.tools': {'analysis': False}, 'box.browser.enabled': True})
        self.assertEqual(moved['reset'], ['box.browser.enabled'])
        self.assertIn('browser', old['set']['box.tools'])
        reset = migrations.CONFIG_PROPOSAL.prepare({'set': {}, 'reset': ['box.tools']})
        self.assertEqual(reset['reset'], ['box.tools', 'box.browser.enabled'])

    def test_global_opt_in_and_project_opt_out_survive_direct_upgrade(self):
        """A renamed setting must preserve explicit project false over global true."""
        fixtures = Path(__file__).parent / 'fixtures/compatibility/1.1.0'
        saved = json.loads((fixtures / 'browser-config.json').read_text())
        project = json.loads((fixtures / 'browser-project.json').read_text())
        upgraded = config.Config.from_dict(migrations.GLOBAL_CONFIG.prepare(saved))
        override = migrations.PROJECT_CONFIG.prepare(project)
        override.pop('version')
        self.assertTrue(upgraded.box.browser.enabled)
        self.assertFalse(upgraded.box.tools.groups['analysis'])
        self.assertFalse(project_config.apply(upgraded, override).box.browser.enabled)
        self.assertTrue(saved['box']['tools']['browser'])
        self.assertNotIn('browser', upgraded.box.tools.groups)

    def test_omitted_old_setting_keeps_project_inheritance(self):
        """Migrating a project must not invent a false override absent from its source."""
        value = migrations.PROJECT_CONFIG.prepare({'box': {'tools': {'analysis': True}}})
        self.assertNotIn('browser', value['box'])

    def test_explicit_upgrade_retains_original_and_retry_preserves_new_setting(self):
        """Package reconciliation can be retried without losing user choices or backups."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'config.json'
            original = b'{"version":1,"box":{"tools":{"browser":true}}}'
            path.write_bytes(original)
            with patch.object(migrations, 'MIGRATION_STATE', Path(tmp)/'backups'):
                self.assertTrue(config.migrate_config(path))
                self.assertFalse(config.migrate_config(path))
            self.assertTrue(config.load_config(path).box.browser.enabled)
            self.assertEqual(next((Path(tmp)/'backups').rglob('schema-*.json')).read_bytes(), original)
            self.assertEqual(json.loads(path.read_text())['version'], 2)


class HostBrowserTests(IsolatedRuntimeTest):
    def setUp(self):
        super().setUp()
        self.root = Path(self._state_tmp.name)

    def test_host_agents_receive_per_invocation_browser_without_rewriting_user_files(self):
        """Native host mode must expose the same named MCP capability as a workspace."""
        for agent in ('claude', 'codex'):
            with self.subTest(agent=agent):
                cfg = config.Config()
                cfg.box.browser.enabled = True
                cfg.box.host_tmp = True
                command = ['python3', '/immutable/agent_box/__main__.py', '_browser', 'run', '[]']
                with patch.object(runtime, 'launch_config', return_value=cfg), \
                     patch.object(runtime, 'resolved_workspace', return_value=Workspace(self.root, self.root)), \
                     patch.object(runtime.shutil, 'which', return_value='/bin/'+agent), \
                     patch.object(runtime, 'HOST_SESSIONS_ROOT', self.root/'sessions'), \
                     patch.object(browser, 'prepare', return_value=command) as prepare, \
                     patch.object(runtime, 'tmux_enabled', return_value=False), \
                     patch.object(runtime, 'browser_claude_args', wraps=runtime.browser_claude_args), \
                     patch.object(runtime.subprocess, 'call', return_value=0) as call:
                    runtime.host_main(agent, ['sample prompt'])
                self.assertTrue(prepare.call_args.kwargs['host_tmp'])
                argv = call.call_args.args[0]
                if agent == 'codex':
                    self.assertIn('mcp_servers.agent_box_browser=', ' '.join(argv))
                    self.assertIn('/immutable/agent_box', ' '.join(argv))
                else:
                    self.assertIn('--mcp-config', argv)
                    self.assertLess(argv.index('sample prompt'), argv.index('--mcp-config'))

    def test_disabled_host_browser_performs_no_container_work(self):
        """Host-only users must not require Podman unless the browser is enabled."""
        with patch.object(runtime, 'launch_config', return_value=config.Config()), \
             patch.object(runtime, 'resolved_workspace', return_value=Workspace(self.root, self.root)), \
             patch.object(runtime.shutil, 'which', return_value='/bin/codex'), \
             patch.object(runtime, 'tmux_enabled', return_value=False), \
             patch.object(browser, 'prepare') as prepare, \
             patch.object(runtime.subprocess, 'call', return_value=0):
            runtime.host_main('codex', [])
        prepare.assert_not_called()

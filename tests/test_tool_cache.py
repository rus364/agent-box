"""Disposable download caches must not discard installed tools or change a live box."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_box import config, migrations, project_config, runtime
from agent_box.workspace import Workspace
from test_runtime import IsolatedRuntimeTest


class ToolCacheMountTests(IsolatedRuntimeTest):
    def test_parent_home_mount_requires_an_explicit_persistence_choice(self):
        """A whole-home bind cannot silently turn the disposable cache into host data."""
        from agent_box.errors import AgentBoxError
        from agent_box.mounts import MountSpec
        settings = config.Config()
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(runtime, "XDG_CACHE_HOME", runtime.HOST_HOME / ".cache"):
            home = Workspace(runtime.HOST_HOME, runtime.HOST_HOME)
            project = Workspace(Path(tmp), Path(tmp))
            for workspace, mounts in ((home, []), (project, [MountSpec(runtime.HOST_HOME, False)])):
                with self.assertRaisesRegex(AgentBoxError, "includes the disposable tool cache"):
                    runtime.build_mounts(workspace, mounts, Path(tmp), settings)
                settings.box.persistent_cache = True
                self.assertIn(f"type=bind,src={runtime.STATE_ROOT / 'cache'},dst={runtime.XDG_CACHE_HOME}",
                              runtime.build_mounts(workspace, mounts, Path(tmp), settings))
                settings.box.persistent_cache = False

    def test_standard_cache_path_is_not_relocated_or_replaced_by_an_extra_volume(self):
        """Disposable caches need only the container's writable layer, not another storage object."""
        settings = config.Config()
        settings.credentials.kubernetes.enabled = True
        settings.credentials.kubernetes.files = ["cluster.yaml"]
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Workspace(Path(tmp), Path(tmp))
            for persistent in (False, True):
                settings.box.persistent_cache = persistent
                cache = runtime.XDG_CACHE_HOME
                with mock.patch.object(runtime.kubeconfigs, "XDG_CACHE_HOME", cache):
                    argv = runtime.container_lifetime_args(workspace, [], Path(tmp), config=settings)
                self.assertIn(f"--env=XDG_CACHE_HOME={cache}", argv)
                self.assertIn(f"--env=UV_CACHE_DIR={cache}/uv", argv)
                self.assertIn(f"--env=BUN_INSTALL_CACHE_DIR={cache}/bun", argv)
                self.assertIn(f"--env=KUBECACHEDIR={cache}/kube", argv)
                self.assertFalse(any(arg.startswith("type=volume,") for arg in argv))

    def test_session_snapshot_retains_cache_policy_and_reads_legacy_retention(self):
        """A nested launch must not confuse an omitted old setting with today's default."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "session/config.json"
            settings = config.Config()
            for persistent in (False, True):
                settings.box.persistent_cache = persistent
                runtime.sync_tracker_runtime(settings, path.parent, policy=True, container=False)
                with mock.patch.dict(os.environ, {"AGENT_BOX_RUNTIME_CONFIG": str(path)}):
                    self.assertEqual(config.effective_config(include_project=False).box.persistent_cache, persistent)
            data = json.loads(path.read_text())
            data["box"].pop("persistent_cache")
            path.write_text(json.dumps(data))
            with mock.patch.dict(os.environ, {"AGENT_BOX_RUNTIME_CONFIG": str(path)}):
                self.assertTrue(config.effective_config(include_project=False).box.persistent_cache)

    def test_only_opted_in_caches_reach_the_host_but_installed_tools_always_survive(self):
        """The default box can be removed without losing Python, mise or Rust installs."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = Workspace(root, root)
            for persistent in (False, True):
                with self.subTest(persistent=persistent):
                    settings = config.Config()
                    settings.box.persistent_cache = persistent
                    argv = runtime.build_mounts(workspace, [], root, settings)
                    for source, destination in (
                        (runtime.STATE_ROOT / "cache", runtime.XDG_CACHE_HOME),
                        (runtime.XDG_CACHE_HOME / "claude-code", runtime.XDG_CACHE_HOME / "claude-code"),
                        (runtime.XDG_CACHE_HOME / "claude-cli-nodejs", runtime.XDG_CACHE_HOME / "claude-cli-nodejs"),
                    ):
                        self.assertEqual(f"type=bind,src={source},dst={destination}" in argv, persistent)
                    self.assertFalse(any(arg.startswith("type=volume,") for arg in argv))
                    for source, destination in (("uv", ".local/share/uv"), ("mise", ".local/share/mise"),
                                                ("cargo", ".cargo"), ("rustup", ".rustup")):
                        self.assertIn(f"type=bind,src={runtime.STATE_ROOT / source},dst={runtime.HOST_HOME / destination}", argv)

    def test_enabling_persistence_prepares_the_cache_without_deleting_old_contents(self):
        """A settings change must never turn mount selection into destructive cleanup."""
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state"
            settings = config.Config()
            with mock.patch.object(runtime, "STATE_ROOT", state):
                runtime.prepare_container_lifetime_state(settings, mount_notifications=False)
                self.assertFalse((state / "cache").exists())
                settings.box.persistent_cache = True
                runtime.prepare_container_lifetime_state(settings, mount_notifications=False)
                kept = state / "cache/kept"
                kept.write_text("download")
                settings.box.persistent_cache = False
                runtime.prepare_container_lifetime_state(settings, mount_notifications=False)
                self.assertEqual(kept.read_text(), "download")


class ToolCacheCompatibilityTests(unittest.TestCase):
    def test_fresh_defaults_and_published_configurations_have_distinct_cache_defaults(self):
        """Every published schema upgrades directly without silently changing retention."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            self.assertFalse(config.load_config(path).box.persistent_cache)
            for version in (1, 2):
                for explicit in (None, False, True):
                    with self.subTest(version=version, explicit=explicit):
                        old = {"version": version}
                        if explicit is not None:
                            old["box"] = {"persistent_cache": explicit}
                        path.write_text(json.dumps(old))
                        loaded = config.load_config(path)
                        self.assertEqual(loaded.box.persistent_cache, explicit is not False)
                        self.assertEqual(json.loads(path.read_text()), old)
                        config.save_config(loaded, path)
                        self.assertEqual(config.load_config(path).box.persistent_cache, loaded.box.persistent_cache)
            config.save_config(config.Config(), path)
            self.assertFalse(config.load_config(path).box.persistent_cache)

    def test_project_cache_choice_inherits_or_overrides_global_settings(self):
        """A project may opt in, or explicitly opt out of a migrated global default."""
        base = config.Config()
        for inherited in (False, True):
            base.box.persistent_cache = inherited
            self.assertEqual(project_config.apply(base, {}).box.persistent_cache, inherited)
            for chosen in (False, True):
                overrides = {"box": {"persistent_cache": chosen}}
                project_config.validate(overrides, Path(".agent-box.json"))
                self.assertEqual(project_config.apply(base, overrides).box.persistent_cache, chosen)

    def test_creation_policy_overrides_later_session_choices(self):
        """Concurrent sessions must use the original box mounts until explicit recreation."""
        for persistent in (False, True):
            settings = config.Config()
            settings.box.persistent_cache = not persistent
            policy = {"format_version": migrations.BOX_POLICY.version,
                      "box": {"persistent_cache": persistent}, "workflow_mount": False}
            config.apply_box_policy(settings, policy)
            self.assertEqual(settings.box.persistent_cache, persistent)
        settings = config.Config()
        config.apply_box_policy(settings, {"format_version": 1, "workflow_mount": False})
        self.assertTrue(settings.box.persistent_cache)

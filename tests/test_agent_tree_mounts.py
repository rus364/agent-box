"""Scope and safety of the ``~/.agents`` mounts.

The decision this pins down: the walk covers the **whole** ``~/.agents`` tree,
not only ``~/.agents/skills``. The tree is bind-mounted read-only at its own
path, with a read-write override for ``skills/`` so agents can create and
maintain skills. Any symlink in the tree dangles inside the container unless
its target is mounted too; narrowing the walk would not expose less of the
host, it would just make the non-skill half of an already-exposed directory
quietly broken.

External targets linked from ``skills/`` are writable for the same reason;
targets linked from elsewhere stay read-only. Internal symlinks are not mounted
twice, and a broken symlink is a warning rather than a failure.
"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_box import runtime


class AgentTreeMountTests(unittest.TestCase):
    def _tree(self, tmp):
        home = Path(tmp) / "home"
        agents = home / ".agents"
        (agents / "skills").mkdir(parents=True)
        (home / "external").mkdir()
        return home, agents

    def _mounts(self, home):
        # The tree's location is its own setting (AGENT_BOX_AGENTS_DIR), so it
        # is resolved once in agent_box.paths rather than re-derived from
        # HOST_HOME on every call; a test home therefore has to redirect both.
        with self._home(home):
            return runtime.agent_tree_mounts()

    def _home(self, home):
        return mock.patch.multiple(
            runtime,
            HOST_HOME=home,
            AGENTS_DIR=home / ".agents",
            AGENT_SKILLS_DIR=home / ".agents" / "skills",
        )

    def test_no_agents_directory_yields_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(self._mounts(Path(tmp) / "home"), [])

    def test_the_tree_itself_is_mounted_read_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            home, agents = self._tree(tmp)
            mounts = self._mounts(home)
            self.assertEqual(mounts[0], (agents, agents, True))

    def test_skills_directory_is_overlaid_read_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            home, agents = self._tree(tmp)
            mounts = self._mounts(home)
            self.assertEqual(mounts[1], (agents / "skills", agents / "skills", False))

    def test_external_skill_target_is_mounted_read_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            home, agents = self._tree(tmp)
            target = home / "external" / "my-skill"
            target.mkdir()
            (agents / "skills" / "my-skill").symlink_to(target)
            mounts = self._mounts(home)
            self.assertIn((target.resolve(), target.resolve(), False), mounts)

    def test_external_non_skill_target_is_mounted_too(self):
        """The documented scope: not just skills/.

        A shared agent definition symlinked into ~/.agents/agents would
        otherwise be a dangling link inside the container, in a directory the
        container can already see.
        """
        with tempfile.TemporaryDirectory() as tmp:
            home, agents = self._tree(tmp)
            target = home / "external" / "shared-agent"
            target.mkdir()
            (agents / "agents").mkdir()
            (agents / "agents" / "shared").symlink_to(target)
            mounts = self._mounts(home)
            self.assertIn((target.resolve(), target.resolve(), True), mounts)

    def test_every_external_skill_target_is_read_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            home, agents = self._tree(tmp)
            for name in ("a", "b"):
                target = home / "external" / name
                target.mkdir()
                (agents / "skills" / name).symlink_to(target)
            external = [mount for mount in self._mounts(home)
                        if mount[0].is_relative_to(home / "external")]
            self.assertTrue(all(not read_only for _src, _dst, read_only in external))

    def test_symlinks_pointing_back_inside_agents_are_not_remounted(self):
        with tempfile.TemporaryDirectory() as tmp:
            home, agents = self._tree(tmp)
            inner = agents / "skills" / "real"
            inner.mkdir()
            (agents / "skills" / "alias").symlink_to(inner)
            self.assertEqual(self._mounts(home), [
                (agents, agents, True),
                (agents / "skills", agents / "skills", False),
            ])

    def test_duplicate_targets_are_mounted_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            home, agents = self._tree(tmp)
            target = home / "external" / "shared"
            target.mkdir()
            (agents / "skills" / "one").symlink_to(target)
            (agents / "skills" / "two").symlink_to(target)
            mounts = self._mounts(home)
            self.assertEqual([m for m in mounts if m[0] == target.resolve()],
                             [(target.resolve(), target.resolve(), False)])

    def test_skill_reference_makes_a_shared_external_target_writable(self):
        """Writable wins when both skill and non-skill links share a target."""
        with tempfile.TemporaryDirectory() as tmp:
            home, agents = self._tree(tmp)
            target = home / "external" / "shared"
            target.mkdir()
            (agents / "agents").mkdir()
            (agents / "agents" / "shared").symlink_to(target)
            (agents / "skills" / "shared").symlink_to(target)
            mounts = self._mounts(home)
            self.assertEqual([m for m in mounts if m[0] == target.resolve()],
                             [(target.resolve(), target.resolve(), False)])

    def test_broken_symlink_warns_and_is_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            home, agents = self._tree(tmp)
            (agents / "skills" / "gone").symlink_to(home / "external" / "missing")
            warnings = []
            with mock.patch.object(runtime, "warn", warnings.append):
                mounts = self._mounts(home)
            self.assertEqual(mounts, [
                (agents, agents, True),
                (agents / "skills", agents / "skills", False),
            ])
            self.assertEqual(len(warnings), 1)
            self.assertIn("broken agent symlink", warnings[0])

    def test_mount_order_is_deterministic(self):
        with tempfile.TemporaryDirectory() as tmp:
            home, agents = self._tree(tmp)
            for name in ("zebra", "alpha", "middle"):
                target = home / "external" / name
                target.mkdir()
                (agents / "skills" / name).symlink_to(target)
            self.assertEqual(self._mounts(home), self._mounts(home))

    def test_external_skill_targets_reach_the_podman_flags_read_write(self):
        from agent_box.workspace import Workspace

        with tempfile.TemporaryDirectory() as tmp:
            home, agents = self._tree(tmp)
            target = home / "external" / "my-skill"
            target.mkdir()
            (agents / "skills" / "my-skill").symlink_to(target)
            project = Path(tmp) / "project"
            project.mkdir()
            state = Path(tmp) / "state"
            with self._home(home), \
                 mock.patch.object(runtime, "STATE_ROOT", state), \
                 mock.patch.object(runtime, "BUILD_STATE", state / "build"), \
                 mock.patch.object(runtime, "NOTIFY_STATE_ROOT", state / "notify"), \
                 mock.patch.object(runtime, "load_config", return_value=runtime.Config()), \
                 mock.patch.object(runtime, "NOTIFY_CREDENTIALS", state / "runtime/notify.json"):
                argv = runtime.container_argv(
                    Workspace(project, project), [], project, ["claude"])
            flag = next(a for a in argv if f"src={target.resolve()}," in a)
            self.assertNotIn("ro=true", flag)


class NormalizedAgentPathTests(unittest.TestCase):
    """`usage`, doctor and the skills sync must all read the same normalized
    directories from agent_box.paths, not re-parse the raw environment."""

    def test_usage_does_not_read_the_raw_config_variables(self):
        from agent_box import usage
        source = Path(usage.__file__).read_text()
        for raw in ('os.environ.get("CODEX_HOME"', 'os.environ.get("CLAUDE_CONFIG_DIR"'):
            self.assertNotIn(raw, source, "re-parsing the raw variable gives a second answer")
        self.assertIn("CLAUDE_HOME", source)
        self.assertIn("CODEX_HOME", source)

    def test_relocated_config_dirs_drive_the_credential_lookups(self):
        import importlib

        with tempfile.TemporaryDirectory() as tmp:
            claude = Path(tmp) / "claude-cfg"
            codex = Path(tmp) / "codex-home"
            env = dict(os.environ)
            env.update({"CLAUDE_CONFIG_DIR": str(claude), "CODEX_HOME": str(codex)})
            env.pop("CLAUDE_CREDENTIALS_PATH", None)
            env.pop("CODEX_AUTH_PATH", None)
            with mock.patch.dict(os.environ, env, clear=True):
                from agent_box import paths
                importlib.reload(paths)
                try:
                    self.assertEqual(
                        paths.env_path("CLAUDE_CREDENTIALS_PATH",
                                       paths.CLAUDE_HOME / ".credentials.json"),
                        claude / ".credentials.json")
                    self.assertEqual(
                        paths.env_path("CODEX_AUTH_PATH", paths.CODEX_HOME / "auth.json"),
                        codex / "auth.json")
                finally:
                    importlib.reload(paths)

    def test_doctor_reports_the_configured_claude_directory(self):
        script = runtime.DOCTOR_SCRIPT
        self.assertIn('claude_home="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"', script)
        self.assertIn('"${claude_home}/skills/', script)
        self.assertNotIn('"$HOME/.claude/skills/', script)


if __name__ == "__main__":
    unittest.main()

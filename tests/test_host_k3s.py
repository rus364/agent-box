"""Native consumers must not lose a shared cluster through another session's exit."""
import copy
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time
from unittest import mock

from agent_box import host_k3s, k3s, runtime
from agent_box.config import Config
from agent_box.errors import AgentBoxError
from agent_box.launch_options import LaunchOptions
from test_k3s_runtime import K3sRuntimeTest, k3s_config


class SharedPolicyTests(K3sRuntimeTest):
    def setUp(self):
        super().setUp()
        self.root = self.state.parent
        self.patchers = [
            mock.patch.object(host_k3s, "local_podman"),
            mock.patch.object(host_k3s, "box_policy", return_value=None),
            mock.patch.object(runtime, "inspect_workspace_container", return_value=None),
            mock.patch.object(runtime.shutil, "which", return_value="/bin/kubectl"),
            mock.patch.object(k3s, "ensure", side_effect=self.ensure),
            mock.patch.object(k3s, "inspect", side_effect=lambda *_: self.sidecar),
            mock.patch.object(k3s, "stop"),
            mock.patch.object(host_k3s.subprocess, "run",
                              return_value=subprocess.CompletedProcess([], 0, "ok", "")),
        ]
        self.sidecar = None
        for patch in self.patchers:
            patch.start()
            self.addCleanup(patch.stop)

    def ensure(self, workspace_id, settings, state, project, *, host_tmp=False):
        self.sidecar = {"State": {"Status": "running"}, "Config": {"Labels": {
            k3s.SPEC_LABEL: k3s.spec_hash(workspace_id, settings, state, project, host_tmp=host_tmp)}}}
        state.mkdir(parents=True, exist_ok=True)
        (state / "kubeconfig").write_text("test managed kubeconfig")
        return 41234

    def reserve(self, config=None, options=None):
        return host_k3s.reserve(config or k3s_config(), options or LaunchOptions(), self.workspace)

    def test_host_join_inherits_enabled_policy_after_saved_defaults_change(self):
        """A later launch must not redirect a running project's agent to unrelated Kubernetes."""
        first = self.reserve()
        changed = Config()
        second = self.reserve(changed)
        self.assertTrue(changed.box.k3s.enabled)
        self.assertNotEqual(first[1], second[1])
        self.assertEqual(len(host_k3s.load(self.root)["owners"]), 2)
        self.assertEqual(list(self.state.iterdir()), [self.state / "kubeconfig"])

    def test_disabled_host_environment_cannot_be_enabled_by_a_join(self):
        """Disabled is also a shared creation choice, not absence of a policy."""
        self.reserve(Config())
        with self.assertRaisesRegex(AgentBoxError, "conflicts"):
            self.reserve(k3s_config(), LaunchOptions(overrides={"k3s": True}))
        joined = k3s_config()
        self.reserve(joined)
        self.assertFalse(joined.box.k3s.enabled)
        k3s.ensure.assert_not_called()
        host_k3s.local_podman.assert_not_called()

    def test_container_creation_inherits_host_policy_and_refuses_conflicting_flags(self):
        """Container creation follows the host-first policy symmetrically."""
        self.reserve()
        config = Config()
        host_k3s.inherit(config, LaunchOptions(), self.root)
        self.assertTrue(config.box.k3s.enabled)
        with self.assertRaisesRegex(AgentBoxError, "conflicts"):
            host_k3s.inherit(Config(), LaunchOptions(overrides={"k3s": False}), self.root)

    def test_host_joins_box_policy_without_enabling_a_disabled_box(self):
        """A live workspace box, rather than new saved defaults, owns its choice."""
        with mock.patch.object(host_k3s, "box_policy", return_value={"k3s": Config().box.k3s.to_dict()}):
            joined = k3s_config()
            self.reserve(joined)
            self.assertFalse(joined.box.k3s.enabled)
            with self.assertRaisesRegex(AgentBoxError, "conflicts"):
                self.reserve(k3s_config(), LaunchOptions(overrides={"k3s": True}))
        k3s.ensure.assert_not_called()

    def test_host_joins_enabled_box_with_the_same_sidecar_recipe(self):
        """Host admission must reuse a container-created cluster despite disabled defaults."""
        settings = k3s_config()
        self.ensure(self.root.name, settings.box.k3s, self.state, self.workspace.host_root)
        with mock.patch.object(host_k3s, "box_policy", return_value={"k3s": settings.box.k3s.to_dict()}):
            joined = Config()
            self.reserve(joined)
            self.assertTrue(joined.box.k3s.enabled)
        self.assertEqual(k3s.ensure.call_count, 1)

    def test_last_consumer_controls_box_lifetime_and_retention(self):
        """One exit cannot stop another consumer or use changed data-retention defaults."""
        first = self.reserve(k3s_config(lifetime="box", persistent_data=False))
        second = self.reserve()
        host_k3s.release(*first)
        runtime.release_k3s(self.workspace, Config())
        k3s.stop.assert_not_called()
        host_k3s.release(*second)
        k3s.stop.assert_called_once()
        self.assertFalse(k3s.stop.call_args.args[1].persistent_data)

    def test_box_keeps_cluster_after_last_host_exits(self):
        """The box's consumer survives host-session cleanup."""
        reservation = self.reserve(k3s_config(lifetime="box"))
        with mock.patch.object(runtime, "inspect_workspace_container",
                               return_value={"State": {"Status": "running"}}):
            host_k3s.release(*reservation)
        k3s.stop.assert_not_called()

    def test_stop_refuses_before_removing_box_or_cluster(self):
        """Explicit container stop cannot partly destroy a project with host consumers."""
        self.reserve()
        with mock.patch.object(runtime, "resolved_workspace", return_value=self.workspace), \
             mock.patch.object(runtime.host_podman, "require"), \
             mock.patch.object(runtime, "remove_workspace_container_then_confirm_absent") as remove, \
             self.assertRaisesRegex(AgentBoxError, "host sessions"):
            runtime.stop_main([])
        remove.assert_not_called()
        k3s.stop.assert_not_called()

    def test_failed_api_probe_never_starts_the_agent(self):
        """A local connectivity failure cannot fall back to the user's kube context."""
        with mock.patch.object(host_k3s.subprocess, "run",
                               return_value=subprocess.CompletedProcess([], 1, "", "connection refused")), \
             self.assertRaisesRegex(AgentBoxError, "host cannot reach"):
            self.reserve()
        self.assertFalse(host_k3s.has_owners(self.root))
        self.reserve()
        self.assertTrue(host_k3s.has_owners(self.root))

    def test_unsupported_topology_cannot_clean_up_or_replace_a_stale_policy(self):
        """A failed host launch must not erase old data through another Podman endpoint."""
        self.reserve(k3s_config(lifetime="box", persistent_data=False))
        data = host_k3s.load(self.root)
        data["owners"] = {}
        host_k3s.save(self.root, data)
        original = (self.root / host_k3s.FILE).read_bytes()
        for enabled in (True, False):
            with self.subTest(enabled=enabled), \
                 mock.patch.object(host_k3s, "local_podman", side_effect=AgentBoxError("unsupported endpoint")), \
                 self.assertRaisesRegex(AgentBoxError, "unsupported endpoint"):
                self.reserve(k3s_config() if enabled else Config())
            self.assertEqual((self.root / host_k3s.FILE).read_bytes(), original)
        k3s.stop.assert_not_called()
        self.assertEqual(k3s.ensure.call_count, 1)

    def test_future_ownership_format_is_not_rewritten(self):
        """An older launcher cannot erase newer consumers or their cleanup policy."""
        self.root.mkdir(parents=True)
        path = self.root / host_k3s.FILE
        original = '{"format_version":999,"owners":{}}'
        path.write_text(original)
        with self.assertRaisesRegex(AgentBoxError, "newer schema"):
            self.reserve()
        self.assertEqual(path.read_text(), original)

    def test_pid_reuse_and_reboot_do_not_keep_stale_consumers(self):
        """PID reuse and reboot must not pin an idle cluster forever."""
        self.reserve()
        data = host_k3s.load(self.root)
        for field, value in (("pid_start", "wrong"), ("boot_id", "previous boot")):
            stale = copy.deepcopy(data)
            next(iter(stale["owners"].values()))["process"][field] = value
            host_k3s.save(self.root, stale)
            self.assertFalse(host_k3s.has_owners(self.root))

    def test_pending_tmux_handoff_survives_early_client_detach(self):
        """The launcher may detach before the pane has consumed its environment."""
        reservation = self.reserve(Config())
        encoded = json.dumps([str(reservation[0]), reservation[1]])
        host_k3s.register_tmux(encoded, "codex-test", self.root / "host")
        with mock.patch("agent_box.sessions.tmux_sessions", return_value=["codex-test"]):
            host_k3s.release(*reservation, pending=True)
            self.assertTrue(host_k3s.has_owners(self.root))

    def test_launch_uses_the_shared_file_without_changing_user_environment(self):
        """Both modes use one managed file, while the parent shell keeps its context."""
        with mock.patch.object(runtime, "resolved_workspace", return_value=self.workspace), \
             mock.patch.object(runtime, "launch_config", return_value=k3s_config()), \
             mock.patch.object(runtime, "tmux_enabled", return_value=False), \
             mock.patch.object(runtime.subprocess, "call", return_value=0) as call, \
             mock.patch.dict(os.environ, {"KUBECONFIG": "/user/original"}):
            runtime.host_main("codex", ["exec", "test"])
            self.assertEqual(call.call_args.kwargs["env"]["KUBECONFIG"], str(self.state / "kubeconfig"))
            self.assertEqual(os.environ["KUBECONFIG"], "/user/original")

    def test_snapshot_reports_the_joined_policy_after_project_settings_change(self):
        """Config diagnostics in a session must describe its actual cluster choice."""
        from agent_box.config import effective_config
        config = Config()
        config._project_base = Config()
        host_k3s.apply(config, LaunchOptions(), {"k3s": k3s_config().box.k3s.to_dict()})
        snapshot = runtime.session_runtime(config, container=False)
        with mock.patch.dict(os.environ, {"AGENT_BOX_RUNTIME_CONFIG": str(snapshot)}), \
             mock.patch("agent_box.project_config.load", return_value={"box": {"k3s": {"enabled": False}}}):
            self.assertTrue(effective_config().box.k3s.enabled)

    def test_incompatible_host_runtime_blocks_container_admission(self):
        """An old consumer must be restarted rather than bypassing new cleanup ownership."""
        self.reserve(Config())
        data = host_k3s.load(self.root)
        data["runtime"] = "older-package"
        host_k3s.save(self.root, data)
        with self.assertRaisesRegex(AgentBoxError, "incompatible runtime"):
            host_k3s.inherit(Config(), LaunchOptions(), self.root)

    def test_disabling_an_idle_cluster_preserves_its_data(self):
        """Withdrawing access cannot delete disposable data before explicit stop."""
        self.reserve(k3s_config(lifetime="persistent", persistent_data=False))
        data = host_k3s.load(self.root)
        data["owners"] = {}
        host_k3s.save(self.root, data)
        self.reserve(Config())
        k3s.stop.assert_called_once()
        self.assertTrue(k3s.stop.call_args.args[1].persistent_data)

    def test_stale_host_cleanup_uses_creation_policy_instead_of_changed_defaults(self):
        """An attach after both processes died cannot discard previously retained data."""
        self.reserve(k3s_config(lifetime="persistent", persistent_data=True))
        data = host_k3s.load(self.root)
        next(iter(data["owners"].values()))["process"]["pid_start"] = "previous process"
        host_k3s.save(self.root, data)
        with mock.patch.object(runtime, "effective_config",
                               return_value=k3s_config(lifetime="box", persistent_data=False)):
            runtime.release_k3s(self.workspace)
        k3s.stop.assert_not_called()

        data["box"]["k3s"]["lifetime"] = "box"
        host_k3s.save(self.root, data)
        runtime.release_k3s(self.workspace, k3s_config(lifetime="box", persistent_data=False))
        k3s.stop.assert_called_once()
        self.assertTrue(k3s.stop.call_args.args[1].persistent_data)

    def test_disabled_container_creation_stops_idle_host_cluster_without_erasing_data(self):
        """A new disabled box withdraws access but never converts that into data removal."""
        self.reserve(k3s_config(lifetime="persistent", persistent_data=False))
        data = host_k3s.load(self.root)
        data["owners"] = {}
        host_k3s.save(self.root, data)
        runtime.prepare_k3s(Config(), self.workspace, runtime.workspace_runtime_state(self.workspace))
        k3s.stop.assert_called_once()
        self.assertTrue(k3s.stop.call_args.args[1].persistent_data)


class RealOwnerTests(K3sRuntimeTest):
    def test_surviving_external_and_remote_agents_keep_ownership_when_their_wrapper_dies(self):
        """Owned wrappers are not the agent; their death must not hide surviving consumers."""
        from test_external_config import external_config
        from agent_box.external_launch import BASELINE_ENV
        root = self.state.parent
        executable = self.workspace.host_root / "agent-probe"
        marker = self.workspace.host_root / "agent.json"
        executable.write_text(
            f"#!{sys.executable}\n"
            "import json, os, pathlib, time\n"
            f"assert {host_k3s.CHILD_ENV!r} not in os.environ\n"
            f"assert {host_k3s.ENV!r} not in os.environ\n"
            f"pathlib.Path({str(marker.with_suffix('.tmp'))!r}).write_text(json.dumps("
            f"{{'pid':os.getpid(), 'baseline':os.environ.get({BASELINE_ENV!r})}}))\n"
            f"pathlib.Path({str(marker.with_suffix('.tmp'))!r}).replace({str(marker)!r})\n"
            "time.sleep(60)\n")
        executable.chmod(0o700)
        for mode in ("external", "remote"):
            with self.subTest(mode=mode):
                marker.unlink(missing_ok=True)
                with mock.patch.object(host_k3s, "box_policy", return_value=None):
                    reservation = host_k3s.reserve(Config(), LaunchOptions(), self.workspace)
                env = dict(os.environ)
                env[host_k3s.ENV] = json.dumps([str(root), reservation[1]])
                env["AGENT_BOX_RUNTIME_CONFIG"] = str(runtime.session_runtime(external_config(), container=False))
                args = (["_external", "deepseek", "codex", str(executable), "--model", "external-small"]
                        if mode == "external" else ["_session", "claude", str(executable), "--remote-control"])
                runner = subprocess.Popen([sys.executable, str(runtime.HOST_SESSION_RUNNER), "--direct",
                                           sys.executable, str(runtime.HOST_LAUNCHER), *args],
                                          env=env, stderr=subprocess.PIPE, text=True)
                payload = None
                owner = None
                try:
                    deadline = time.monotonic() + 10
                    while not marker.exists() and runner.poll() is None and time.monotonic() < deadline:
                        time.sleep(.01)
                    self.assertTrue(marker.exists())
                    payload = json.loads(marker.read_text())
                    with runtime.workspace_transition_lock(self.workspace):
                        owner = host_k3s.load(root)["owners"][reservation[1]]
                    self.assertIn(payload["pid"], [p["pid"] for p in owner["descendants"]])
                    os.kill(owner["child"]["pid"], signal.SIGKILL)
                    runner.wait(timeout=5)
                    self.assertTrue(host_k3s.has_owners(root))
                    with self.assertRaisesRegex(AgentBoxError, "host sessions"):
                        host_k3s.refuse_owners(root)
                    host_k3s.stop(root)
                    self.assertFalse(host_k3s.has_owners(root))
                finally:
                    if runner.poll() is None:
                        runner.kill()
                        runner.wait(timeout=5)
                    if owner:
                        for child in host_k3s.children(owner):
                            if host_k3s.alive(child):
                                os.kill(child["pid"], signal.SIGTERM)
                    runner.communicate(timeout=5)
                    if payload and payload["baseline"]:
                        shutil.rmtree(Path(payload["baseline"]).parent)

    def test_sigkill_before_child_registration_never_executes_the_agent(self):
        """An interrupted ownership handoff must leave no untracked agent using the API."""
        root = self.state.parent
        with mock.patch.object(host_k3s, "box_policy", return_value=None):
            reservation = host_k3s.reserve(Config(), LaunchOptions(), self.workspace)
        marker = self.workspace.host_root / "agent-started"
        child_record = self.workspace.host_root / "child.json"
        env = dict(os.environ)
        env[host_k3s.ENV] = json.dumps([str(root), reservation[1]])
        code = """
import json, os, signal, sys, time
from pathlib import Path
from agent_box import host_k3s
save = host_k3s.save
def interrupted_save(root, data):
    child = next(iter(data['owners'].values())).get('child')
    if child:
        Path(sys.argv[2]).write_text(json.dumps(child))
        deadline = time.monotonic() + .3
        while not Path(sys.argv[1]).exists() and time.monotonic() < deadline:
            time.sleep(.01)
        os.kill(os.getpid(), signal.SIGKILL)
    save(root, data)
host_k3s.save = interrupted_save
child_code = "from pathlib import Path; import time; Path(%r).write_text('started'); time.sleep(60)" % sys.argv[1]
host_k3s.run([sys.executable, '-c', child_code], dict(os.environ))
"""
        runner = subprocess.Popen([sys.executable, "-c", code, str(marker), str(child_record)], env=env)
        child = None
        try:
            self.assertEqual(runner.wait(timeout=10), -signal.SIGKILL)
            child = json.loads(child_record.read_text())
            deadline = time.monotonic() + 5
            while host_k3s.alive(child) and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertFalse(marker.exists(), "the agent executed before durable ownership")
            self.assertFalse(host_k3s.alive(child), "an untracked child survived the failed handoff")
            self.assertFalse(host_k3s.has_owners(root))
        finally:
            if runner.poll() is None:
                runner.kill()
                runner.wait(timeout=5)
            if child and host_k3s.alive(child):
                os.kill(child["pid"], signal.SIGTERM)

    def test_unsupported_topology_fails_before_podman_prerequisite_work(self):
        """Remote and nested requests cannot silently create a cluster elsewhere."""
        for nested, environment in ((True, {}), (False, {"CONTAINER_HOST": "ssh://test"}),
                                    (False, {"CONTAINER_CONNECTION": "test"})):
            with self.subTest(nested=nested, environment=environment), \
                 mock.patch.dict(os.environ, environment, clear=True), \
                 mock.patch.object(runtime, "inside_box", return_value=nested), \
                 mock.patch.object(runtime.host_podman, "require") as require, \
                 self.assertRaises(AgentBoxError):
                host_k3s.local_podman()
            require.assert_not_called()

    def test_remote_config_and_rootful_podman_are_refused(self):
        """Environment variables alone do not describe the selected Podman service."""
        for host in ({"serviceIsRemote": True, "security": {"rootless": True}},
                     {"serviceIsRemote": False, "security": {"rootless": False}}):
            with self.subTest(host=host), mock.patch.dict(os.environ, {}, clear=True), \
                 mock.patch.object(runtime.host_podman, "require"), \
                 mock.patch.object(host_k3s.subprocess, "run",
                                   return_value=subprocess.CompletedProcess([], 0, json.dumps({"host": host}), "")), \
                 self.assertRaisesRegex(AgentBoxError, "local rootless"):
                host_k3s.local_podman()

    def test_explicit_stop_ends_a_direct_host_agent_without_tmux(self):
        """Direct consumers must be stoppable and cannot pin a disabled environment."""
        root = self.state.parent
        with mock.patch.object(host_k3s, "box_policy", return_value=None):
            reservation = host_k3s.reserve(Config(), LaunchOptions(), self.workspace)
        marker = self.workspace.host_root / "started"
        env = dict(os.environ)
        env[host_k3s.ENV] = json.dumps([str(root), reservation[1]])
        code = "import pathlib,time; pathlib.Path(%r).write_text('ready'); time.sleep(60)" % str(marker)
        runner = subprocess.Popen([sys.executable, str(runtime.HOST_SESSION_RUNNER), "--direct",
                                   sys.executable, "-c", code], env=env)
        try:
            deadline = time.monotonic() + 5
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertTrue(marker.exists())
            host_k3s.stop(root)
            runner.wait(timeout=5)
            self.assertFalse(host_k3s.has_owners(root))
        finally:
            if runner.poll() is None:
                runner.kill()
                runner.wait(timeout=5)

    def test_surviving_agent_retains_ownership_after_runner_sigkill(self):
        """A killed supervisor does not make its still-running child safe to clean up."""
        root = self.state.parent
        with mock.patch.object(host_k3s, "box_policy", return_value=None):
            reservation = host_k3s.reserve(Config(), LaunchOptions(), self.workspace)
        marker = self.workspace.host_root / "started"
        env = dict(os.environ)
        env[host_k3s.ENV] = json.dumps([str(root), reservation[1]])
        child_code = "import pathlib,time; pathlib.Path(%r).write_text('ready'); time.sleep(60)" % str(marker)
        runner = subprocess.Popen([sys.executable, str(runtime.HOST_SESSION_RUNNER), "--direct",
                                   sys.executable, "-c", child_code], env=env)
        child = None
        try:
            deadline = time.monotonic() + 5
            while not marker.exists() and runner.poll() is None and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(marker.exists())
            with runtime.workspace_transition_lock(self.workspace):
                child = next(iter(host_k3s.load(root)["owners"].values()))["child"]
            runner.kill()
            runner.wait(timeout=5)
            self.assertTrue(host_k3s.has_owners(root))
            with self.assertRaisesRegex(AgentBoxError, "host sessions"):
                host_k3s.refuse_owners(root)
        finally:
            if runner.poll() is None:
                runner.terminate()
                runner.wait(timeout=5)
            if child and host_k3s.alive(child):
                os.kill(child["pid"], signal.SIGTERM)


"""Doctor must distinguish an installed CLI from a usable engine, without pulls."""
import json
import os
import subprocess
import unittest
from unittest import mock

from agent_box import podman_doctor


def result(code=0, output="", error=""):
    return subprocess.CompletedProcess([], code, output, error)


INFO = json.dumps({"host": {"security": {"rootless": True}, "ociRuntime": {"name": "crun"},
                          "cgroupManager": "cgroupfs", "cgroupVersion": "v2", "networkBackend": "netavark"},
                   "store": {"graphDriverName": "overlay", "graphRoot": "/isolated/storage"}})


class PodmanDoctorTests(unittest.TestCase):
    def check(self, responses, enabled=True):
        with mock.patch.dict(os.environ, {"AGENT_BOX_TOOLS": "podman" if enabled else ""}), \
             mock.patch.object(podman_doctor.shutil, "which", return_value="/bin/podman"), \
             mock.patch.object(podman_doctor.subprocess, "run", side_effect=responses) as run:
            lines = podman_doctor.diagnose()
        return "\n".join(lines), run

    def test_disabled_does_not_touch_engine(self):
        text, run = self.check([], False)
        self.assertIn("disabled", text)
        run.assert_not_called()

    def test_missing_cli_has_rebuild_hint(self):
        with mock.patch.dict(os.environ, {"AGENT_BOX_TOOLS": "podman"}), \
             mock.patch.object(podman_doctor.shutil, "which", return_value=None):
            self.assertIn("MISSING", "\n".join(podman_doctor.diagnose()))

    def test_missing_image_is_a_skip_after_successful_namespace_probe(self):
        text, run = self.check([result(output="podman version 5.7"), result(output=INFO),
                               result(), result(1), result(1), result(1)])
        self.assertIn("podman namespace:    OK", text)
        self.assertIn("SKIPPED", text)
        self.assertIn("overlay", text)
        self.assertFalse(any("pull" in call.args[0] for call in run.call_args_list))

    def test_info_failure_is_visible_and_stops_probes(self):
        text, run = self.check([result(output="podman version 5.7"), result(125, error="cannot clone")])
        self.assertIn("info: FAILED (cannot clone)", text)
        self.assertEqual(run.call_count, 2)

    def test_namespace_failure_is_not_reported_as_usable(self):
        text, _ = self.check([result(), result(output=INFO), result(1, error="unshare denied")])
        self.assertIn("namespace: FAILED (unshare denied)", text)
        self.assertNotIn("SKIPPED", text)

    def test_run_overrides_entrypoint_disables_network_and_cleans_own_name(self):
        text, run = self.check([result(), result(output=INFO), result(), result(), result(), result()])
        self.assertIn("run/storage:  OK", text)
        command = run.call_args_list[-2].args[0]
        self.assertIn("--pull=never", command)
        self.assertIn("--network=none", command)
        self.assertEqual(command[command.index("--entrypoint") + 1], "/bin/sh")
        name = command[command.index("--name") + 1]
        self.assertTrue(name.startswith("agent-box-doctor-"))
        self.assertEqual(run.call_args_list[-1].args[0][-1], name)
        for call in run.call_args_list:
            self.assertEqual(call.kwargs["timeout"], 20)

    def test_timed_out_run_still_cleans_up_and_reports_failure(self):
        text, run = self.check([result(), result(output=INFO), result(), result(),
                               subprocess.TimeoutExpired("podman", 20), result()])
        self.assertIn("run/storage: FAILED", text)
        self.assertEqual(run.call_args_list[-1].args[0][1], "rm")

    def test_image_lookup_failure_is_not_a_missing_image(self):
        text, _ = self.check([result(), result(output=INFO), result(), result(125, error="storage locked")])
        self.assertIn("local image: FAILED", text)
        self.assertNotIn("SKIPPED", text)

    def test_cleanup_timeout_does_not_relabel_a_successful_run(self):
        text, _ = self.check([result(), result(output=INFO), result(), result(), result(),
                              subprocess.TimeoutExpired("podman rm", 20)])
        self.assertIn("run/storage:  OK", text)
        self.assertIn("cleanup:      FAILED (agent-box-doctor-", text)
        self.assertNotIn("run/storage: FAILED", text)

    def test_cleanup_failure_preserves_the_original_run_failure(self):
        text, _ = self.check([result(), result(output=INFO), result(), result(),
                              result(125, error="runtime failed"), result(125, error="storage locked")])
        self.assertIn("run/storage: FAILED (runtime failed)", text)
        self.assertIn("cleanup:      FAILED (agent-box-doctor-", text)
        self.assertIn("storage locked", text)

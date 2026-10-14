"""The host engine check must say what is wrong and how to fix it, never a bare "not found"."""
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_box import host_podman
from agent_box.errors import AgentBoxError


def result(code=0, output="", error=""):
    return subprocess.CompletedProcess([], code, output, error)


UID_MAP, GID_MAP = "/proc/self/uid_map:", "/proc/self/gid_map:"
MAPS = (f"{UID_MAP}0 1000 1\n{UID_MAP}1 100000 65536\n"
        f"{GID_MAP}0 1000 1\n{GID_MAP}1 100000 65536\n")
CACHYOS = {"ID": "cachyos", "ID_LIKE": "arch"}


class HostPodmanTests(unittest.TestCase):
    def problem(self, found=("podman", "newuidmap", "newgidmap"), responses=(), release=CACHYOS, euid=1000):
        with mock.patch.object(host_podman.os, "geteuid", return_value=euid), \
             mock.patch.object(host_podman.shutil, "which",
                               side_effect=lambda name: f"/usr/bin/{name}" if name in found else None), \
             mock.patch.object(host_podman, "os_release", return_value=release), \
             mock.patch.object(host_podman.subprocess, "run", side_effect=list(responses)) as run:
            return host_podman.problem(), run

    def test_a_usable_rootless_podman_passes(self):
        message, run = self.problem(responses=[result(output="podman version 5.4.2"), result(output=MAPS)])
        self.assertIsNone(message)
        self.assertEqual(run.call_count, 2)

    def test_root_is_refused_before_podman_is_consulted(self):
        """Podman under root is rootful: keep-id would no longer map to the user's files."""
        message, run = self.problem(euid=0)
        self.assertIn("not as root", message)
        run.assert_not_called()

    def test_an_empty_host_gets_its_distribution_install_command(self):
        """A derivative resolves through ID_LIKE, so CachyOS gets pacman, not the generic guide."""
        message, _ = self.problem(found=())
        self.assertIn("sudo pacman -S podman", message)
        self.assertNotIn("Docker", message)

    def test_a_docker_host_is_told_both_can_coexist_and_to_avoid_podman_docker(self):
        """podman-docker replaces the docker command, which would break the user's Docker."""
        message, _ = self.problem(found=("docker",), release={"ID": "ubuntu", "ID_LIKE": "debian"})
        self.assertIn("sudo apt install podman", message)
        self.assertIn("not podman-docker", message)

    def test_an_unknown_distribution_gets_the_upstream_guide(self):
        message, _ = self.problem(found=(), release={"ID": "plan9"})
        self.assertIn(host_podman.INSTALL_GUIDE, message)

    def test_an_old_podman_names_both_versions(self):
        """Ubuntu 24.04 ships 4.9: refuse it up front instead of failing mid-launch on a flag."""
        message, run = self.problem(responses=[result(output="podman version 4.9.3")])
        self.assertIn("Podman 5.0 or newer is required", message)
        self.assertIn("4.9.3", message)
        self.assertEqual(run.call_count, 1)

    def test_missing_id_map_helpers_get_their_distribution_package(self):
        """podman system migrate cannot install them; Debian splits them into uidmap."""
        message, run = self.problem(found=("podman",), release={"ID": "debian"},
                                    responses=[result(output="podman version 5.4.2")])
        self.assertIn("newuidmap and newgidmap", message)
        self.assertIn("sudo apt install uidmap", message)
        self.assertEqual(run.call_count, 1)

    def namespace_failure(self, policy=None, helpers=()):
        with tempfile.TemporaryDirectory() as tmp:
            sysctls = []
            for name, value in (policy or {}).items():
                path = Path(tmp, name)
                path.write_text(f"{value}\n")
                sysctls.append((path, name, "1"))
            with mock.patch.object(host_podman, "NAMESPACE_SYSCTLS", tuple(sysctls)), \
                 mock.patch.object(host_podman, "unprivileged_helpers", return_value=list(helpers)):
                message, _ = self.problem(responses=[result(output="podman version 5.4.2"),
                                                     result(1, error="newuidmap: write to uid_map failed")])
        return message

    def test_a_namespace_that_cannot_be_entered_reports_podmans_reason(self):
        message = self.namespace_failure({"user.max_user_namespaces": 28633})
        self.assertIn("write to uid_map failed", message)
        self.assertIn("podman system migrate", message)

    def test_a_kernel_that_forbids_user_namespaces_gets_the_sysctl_not_migrate(self):
        """podman system migrate cannot lift a kernel policy; the sysctl that set it can."""
        message = self.namespace_failure({"user.max_user_namespaces": 28633, "kernel.unprivileged_userns_clone": 0})
        self.assertIn("sudo sysctl -w kernel.unprivileged_userns_clone=1", message)
        self.assertNotIn("podman system migrate", message)

    def test_missing_subordinate_ranges_get_a_range_no_one_else_holds(self):
        """A range overlapping another user's would let their containers share IDs."""
        with tempfile.TemporaryDirectory() as tmp:
            subuid, subgid = Path(tmp, "subuid"), Path(tmp, "subgid")
            subuid.write_text("other:100000:65536\n")
            subgid.write_text("other:100000:70000\n")
            with mock.patch.object(host_podman, "SUBID_FILES", (subuid, subgid)):
                message, _ = self.problem(responses=[result(output="podman version 5.4.2"),
                                                     result(output=f"{UID_MAP}0 1000 1\n{GID_MAP}0 1000 1\n")])
        self.assertIn("--add-subuids 170000-235535 --add-subgids 170000-235535", message)
        self.assertIn("podman system migrate", message)

    def test_helpers_without_privileges_get_reinstalled_not_migrated(self):
        """Without setuid or file capabilities newuidmap cannot write a map; migrate changes nothing."""
        message = self.namespace_failure(helpers=("newuidmap",))
        self.assertIn("newuidmap can map no IDs", message)
        self.assertIn("sudo pacman -S shadow", message)
        self.assertNotIn("podman system migrate", message)

    def test_a_helper_privileged_by_setuid_or_capability_is_not_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            setuid, plain = Path(tmp, "newuidmap"), Path(tmp, "newgidmap")
            for path in (setuid, plain):
                path.write_text("")
            setuid.chmod(0o4755)
            plain.chmod(0o755)
            with mock.patch.object(host_podman.shutil, "which", side_effect=lambda name: str(Path(tmp, name))):
                self.assertEqual(host_podman.unprivileged_helpers(), ["newgidmap"])
            with mock.patch.object(host_podman.shutil, "which", side_effect=lambda name: str(Path(tmp, name))), \
                 mock.patch.object(host_podman.os, "getxattr", return_value=b"caps"):
                self.assertEqual(host_podman.unprivileged_helpers(), [])

    def test_unreadable_allocations_get_no_guessed_range(self):
        """A start guessed past unread ranges could hand out another user's IDs under sudo."""
        with tempfile.TemporaryDirectory() as tmp:
            unreadable = Path(tmp, "subuid")
            unreadable.mkdir()
            with mock.patch.object(host_podman, "SUBID_FILES", (unreadable, Path(tmp, "subgid"))):
                message, _ = self.problem(responses=[result(output="podman version 5.4.2"),
                                                     result(output=f"{UID_MAP}0 1000 1\n{GID_MAP}0 1000 1\n")])
        self.assertIn("--add-subuids FIRST-LAST", message)
        self.assertNotIn(str(host_podman.SUBID_START), message)

    def test_several_uid_extents_do_not_stand_in_for_a_missing_gid_range(self):
        """Counted together, two UID extents would pass a host that cannot map any GID."""
        maps = f"{UID_MAP}0 1000 1\n{UID_MAP}1 100000 999\n{UID_MAP}1000 200000 64537\n{GID_MAP}0 1000 1\n"
        message, _ = self.problem(responses=[result(output="podman version 5.4.2"), result(output=maps)])
        self.assertIn("no subordinate GID range", message)
        self.assertIn("--add-subgids", message)
        self.assertNotIn("--add-subuids", message)

    def test_os_release_reads_quoted_values_and_ignores_comments(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "os-release")
            path.write_text('# comment\nID=fedora\nID_LIKE="rhel centos"\n')
            self.assertEqual(host_podman.os_release((Path(tmp, "missing"), path)),
                             {"ID": "fedora", "ID_LIKE": "rhel centos"})


class EntryPointTests(unittest.TestCase):
    def test_every_box_command_reports_the_diagnosis_instead_of_not_found(self):
        """Launch, attach, stop and build share one check; a scattered which() gave a bare error."""
        from agent_box import cli, project_config, runtime
        calls = {
            "launch": lambda: runtime.runtime_main([], Path("/nonexistent")),
            "attach": lambda: runtime.attach_main([]),
            "stop": lambda: runtime.stop_main([]),
        }
        with mock.patch.object(host_podman, "problem", return_value="install podman, please"):
            for name, call in calls.items():
                with self.subTest(command=name), self.assertRaisesRegex(AgentBoxError, "install podman, please"):
                    call()
            for command in ("build", "update", "rebuild"):
                with self.subTest(command=command), \
                     mock.patch.object(project_config, "load", return_value={}), \
                     mock.patch("sys.argv", ["agent-box", command]), \
                     mock.patch.object(runtime, "build_image") as build, \
                     mock.patch.object(cli, "die", side_effect=SystemExit(1)) as die, \
                     self.assertRaises(SystemExit):
                    cli.main()
                build.assert_not_called()
                self.assertEqual(die.call_args.args[0], "install podman, please")


if __name__ == "__main__":
    unittest.main()

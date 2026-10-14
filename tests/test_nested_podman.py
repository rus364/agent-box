"""Nested containers must remain opt-in and fit the outer rootless namespace."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import tomllib

from agent_box import nested_podman, runtime
from agent_box.config import Config
from agent_box.errors import AgentBoxError
from agent_box.identity import HostIdentity
from agent_box.workspace import Workspace
from test_runtime import IsolatedRuntimeTest


def profile():
    return {
        "defaultAction": "SCMP_ACT_ERRNO", "defaultErrnoRet": 1,
        "archMap": [{"architecture": "SCMP_ARCH_X86_64", "subArchitectures": []}],
        "syscalls": [
            {"names": ["read", "write"], "action": "SCMP_ACT_ALLOW"},
            {"names": ["sethostname", "bpf"], "action": "SCMP_ACT_ERRNO",
             "errnoRet": 1, "excludes": {"caps": ["CAP_SYS_ADMIN"]}},
            {"names": ["chroot"], "action": "SCMP_ACT_ERRNO", "errnoRet": 1},
        ],
    }


class NestedIdTests(unittest.TestCase):
    def test_split_ranges_never_delegate_outer_root_or_the_workspace_user(self):
        """Full image UIDs still fit without granting the helpers outer root."""
        ranges = nested_podman.subid_ranges([{"container_id": 0, "size": 65537}], 1000)
        self.assertEqual(ranges, [(1, 999), (1001, 64536)])
        delegated = {i for start, size in ranges for i in range(start, start + size)}
        self.assertNotIn(0, delegated)
        self.assertNotIn(1000, delegated)
        self.assertEqual(len(delegated) + 1, 65536)

    def test_a_further_level_preserves_parent_extent_boundaries(self):
        """A range spanning parent extents fails uid_map writes despite valid endpoints."""
        mappings = [{"container_id": 0, "size": 1}, {"container_id": 1, "size": 999},
                    {"container_id": 1000, "size": 64536}]
        self.assertEqual(nested_podman.subid_ranges(mappings, 1000),
                         [(1, 998), (999, 1), (1001, 64535)])

    def test_large_workspace_uid_does_not_create_a_subordinate_mapping_gap(self):
        """Keep-id places a high user UID outside the contiguous subordinate block."""
        self.assertEqual(nested_podman.subid_ranges([{"container_id": 0, "size": 65537}], 70000),
                         [(1, 65535)])

    def test_mapping_ranges_ignore_host_numbering_but_reject_container_gaps(self):
        mappings = [{"container_id": 1, "host_id": 100000, "size": 65536},
                    {"container_id": 0, "host_id": 1000, "size": 1}]
        self.assertEqual(nested_podman.subid_ranges(mappings, 1000), [(1, 999), (1001, 64536)])
        mappings[0]["container_id"] = 2
        with self.assertRaisesRegex(ValueError, "contiguous"):
            nested_podman.subid_ranges(mappings, 1000)
        with self.assertRaisesRegex(ValueError, "subordinate"):
            nested_podman.subid_ranges([{"container_id": 0, "size": 1}], 1000)

    def test_image_settings_preserve_distinct_uid_gid_allocations_and_rootless_storage(self):
        """Host UID and GID ranges can differ; rootless storage needs a user config."""
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(nested_podman.os, "chown"):
            root = Path(tmp)
            (root / "etc").mkdir()
            nested_podman.configure_image(root, "tester", 1000, 100, Path("/home/tester"),
                                          [[1, 999], [1001, 64536]], [[1, 99], [101, 65435]])
            self.assertEqual((root / "etc/subuid").read_text(), "tester:1:999\ntester:1001:64536\n")
            self.assertEqual((root / "etc/subgid").read_text(), "tester:1:99\ntester:101:65435\n")
            storage = tomllib.loads((root / "home/tester/.config/containers/storage.conf").read_text())
            self.assertEqual(storage["storage"]["driver"], "overlay")
            self.assertTrue((root / "home/tester/.local/share/containers").is_dir())
            config = tomllib.loads((root / "etc/containers/containers.conf").read_text())
            self.assertEqual(config["containers"]["cgroups"], "disabled")
            self.assertEqual(config["containers"]["netns"], config["network"]["default_rootless_network_cmd"])


class NestedIdDriftTests(unittest.TestCase):
    """An image writes its builder's ranges into /etc/subuid; a box can map only
    what its own Podman was given, so a mismatched image fails on newuidmap
    inside the box unless the launch says to rebuild."""

    HOST = {"uidmap": [{"container_id": 0, "host_id": 1000, "size": 1},
                       {"container_id": 1, "host_id": 100000, "size": 65536}]}
    BOX = {"uidmap": [{"container_id": 0, "host_id": 1000, "size": 1},
                      {"container_id": 1, "host_id": 1, "size": 999},
                      {"container_id": 1000, "host_id": 1001, "size": 64536}]}

    def drift(self, built_on, running_on, labelled=True, podman=True):
        mappings = {kind + "map": rows for kind in ("uid", "gid") for rows in [running_on["uidmap"]]}
        built = nested_podman.subid_ranges(built_on["uidmap"], 1000)
        labels = {label: json.dumps(built) if labelled else None
                  for label in runtime.IMAGE_ID_RANGE_LABELS.values()}
        config = Config()
        config.box.tools.groups["podman"] = podman
        with mock.patch.object(runtime, "image_label", side_effect=lambda name, image=None: labels[name]), \
                mock.patch.object(nested_podman, "host_info", return_value={"idMappings": mappings}), \
                mock.patch.object(runtime, "detect_host_identity",
                                  return_value=HostIdentity("user", 1000, 1000, Path("/home/user"))):
            return runtime.nested_podman_drift(config)

    def test_an_image_built_for_this_podman_is_quiet(self):
        self.assertEqual(self.drift(self.BOX, self.BOX), [])

    def test_an_image_built_a_level_up_says_to_rebuild(self):
        """The host's last subordinate UID does not exist inside the box."""
        warnings = self.drift(self.HOST, self.BOX)
        self.assertEqual(len(warnings), 1)
        self.assertIn("agent-box build", warnings[0])

    def test_an_unlabelled_image_or_a_box_without_podman_is_not_drift(self):
        self.assertEqual(self.drift(self.HOST, self.BOX, labelled=False), [])
        self.assertEqual(self.drift(self.HOST, self.BOX, podman=False), [])


class NestedSeccompTests(unittest.TestCase):
    def test_only_required_syscalls_change_and_conflicting_denies_are_removed(self):
        """Appending allow rules alone leaves higher-precedence errno rules active."""
        original = profile()
        result = nested_podman.seccomp_profile(original)
        self.assertEqual(original, profile())
        self.assertEqual(result["archMap"], original["archMap"])
        self.assertEqual(result["defaultAction"], "SCMP_ACT_ERRNO")
        for syscall in ("chroot", "sethostname"):
            rules = [rule for rule in result["syscalls"] if syscall in rule["names"]]
            self.assertEqual(len(rules), 1)
            self.assertEqual(rules[0]["action"], "SCMP_ACT_ALLOW")
            self.assertNotIn("includes", rules[0])
        bpf_rule = next(rule for rule in result["syscalls"] if "bpf" in rule["names"])
        self.assertEqual(bpf_rule, {**original["syscalls"][1], "names": ["bpf"]})

    def test_unconfined_or_unsupported_profiles_are_not_silently_accepted(self):
        with self.assertRaisesRegex(ValueError, "allowlist"):
            nested_podman.seccomp_profile({"defaultAction": "SCMP_ACT_ALLOW", "syscalls": []})


class NestedRuntimeTests(IsolatedRuntimeTest):
    def setUp(self):
        super().setUp()
        self.profile_path = runtime.STATE_ROOT.parent / "host-seccomp.json"
        self.profile_path.write_text(json.dumps(profile()))
        self.host = {"security": {"rootless": True, "seccompProfilePath": str(self.profile_path)},
                     "idMappings": {name: [{"container_id": 0, "size": count}]
                                    for name, count in (("uidmap", 65537), ("gidmap", 65536))}}
        self.config = Config()
        self.config.box.tools.groups["podman"] = True
        self.workspace = Workspace(Path("/project"), Path("/project"))
        self.identity = HostIdentity("tester", 1000, 1000, Path("/home/tester"))

    def test_disabled_group_does_not_probe_podman_or_widen_access(self):
        """An installed Podman or TUN device must never opt the user in."""
        config = Config()
        self.assertFalse(config.box.tools.groups["podman"])
        with mock.patch.object(nested_podman, "host_info") as info:
            self.assertEqual(runtime.nested_podman_args(config, self.workspace), [])
            self.assertEqual(runtime.nested_podman_build_args(config, self.identity), [])
        info.assert_not_called()
        self.assertFalse(runtime.STATE_ROOT.exists())

    def test_build_uses_live_namespace_sizes_and_round_trips_the_group(self):
        """Nested builds must invalidate cached ID setup when mappings change."""
        config = Config.from_dict(self.config.to_dict())
        with mock.patch.object(nested_podman, "host_info", return_value=self.host):
            args = runtime.nested_podman_build_args(config, self.identity)
        self.assertEqual(args, ["--build-arg", "PODMAN_UID_RANGES=[[1,999],[1001,64536]]",
                                "--build-arg", "PODMAN_GID_RANGES=[[1,999],[1001,64535]]"])
        self.assertIn("TOOLS_PODMAN=1", runtime.tool_build_args(config))

    def test_nested_build_and_launch_use_the_rendered_session_groups(self):
        """A workspace has a rendered config, not the host config.json; defaults lose Podman."""
        from agent_box import project_config
        snapshot = runtime.STATE_ROOT.parent / "session.json"
        snapshot.write_text(json.dumps(self.config.to_dict()))
        with mock.patch.dict(os.environ, {"AGENT_BOX_RUNTIME_CONFIG": str(snapshot)}), \
             mock.patch.object(project_config, "load", return_value={}), \
             mock.patch.object(nested_podman, "host_info", return_value=self.host), \
             mock.patch.object(runtime.subprocess, "call", return_value=0) as call:
            runtime.build_image(Path("/source"), refresh=False, no_cache=False)
            self.assertIn("TOOLS_PODMAN=1", call.call_args.args[0])
            self.assertTrue(runtime.launch_config().box.tools.groups["podman"])

    def test_enabled_lifetime_preserves_nnp_and_only_adds_required_access(self):
        """Nested support must not grant SYS_ADMIN, privileged mode or a host socket."""
        workspace = Workspace(Path("/project"), Path("/project"))
        with mock.patch.object(nested_podman, "host_info", return_value=self.host), \
             mock.patch.object(runtime.os, "access", return_value=True):
            args = runtime.container_lifetime_args(workspace, [], Path("/checkout"), self.config)
        for flag in ("--cap-drop=all", "--security-opt=no-new-privileges", "--userns=keep-id",
                     "--cap-add=SETUID", "--cap-add=SETGID", "--device=/dev/net/tun"):
            self.assertIn(flag, args)
        self.assertFalse(any("SYS_ADMIN" in flag or "privileged" in flag or "unconfined" in flag for flag in args))
        self.assertFalse(any("podman.sock" in flag or "docker.sock" in flag for flag in args))

    def test_changed_profile_changes_lifetime_and_preserves_the_old_artifact(self):
        """Changing host seccomp must recreate a workspace, not alter it in place."""
        with mock.patch.object(nested_podman, "host_info", return_value=self.host), \
             mock.patch.object(runtime.os, "access", return_value=True):
            before = runtime.nested_podman_args(self.config, self.workspace)
            old_path = Path(next(flag.split("seccomp=", 1)[1] for flag in before if "seccomp=" in flag))
            old_content = old_path.read_text()
            changed = profile()
            changed["syscalls"][0]["names"].append("close")
            self.profile_path.write_text(json.dumps(changed))
            after = runtime.nested_podman_args(self.config, self.workspace)
        self.assertNotEqual(before, after)
        self.assertEqual(old_path.read_text(), old_content)

    def test_missing_tun_refuses_instead_of_falling_back_to_shared_network(self):
        """A missing device must not silently change networking or port semantics."""
        with mock.patch.object(runtime.os, "access", return_value=False):
            with self.assertRaisesRegex(AgentBoxError, "/dev/net/tun"):
                runtime.nested_podman_args(self.config, self.workspace)

    def test_rootful_podman_is_rejected_before_runtime_artifacts_are_written(self):
        response = mock.Mock(returncode=0, stdout=json.dumps({"host": {"security": {"rootless": False}}}))
        with mock.patch.object(nested_podman.subprocess, "run", return_value=response), \
             mock.patch.object(runtime.os, "access", return_value=True):
            with self.assertRaisesRegex(AgentBoxError, "rootless outer"):
                runtime.nested_podman_args(self.config, self.workspace)
        self.assertFalse(runtime.STATE_ROOT.exists())

    def test_each_workspace_keeps_a_separate_storage_volume_across_recreation(self):
        """Native overlay needs a non-overlay upper directory without sharing engine storage."""
        other = Workspace(Path("/other"), Path("/other"))
        with mock.patch.object(nested_podman, "host_info", return_value=self.host), \
             mock.patch.object(runtime.os, "access", return_value=True):
            first = runtime.nested_podman_args(self.config, self.workspace)[-1]
            again = runtime.nested_podman_args(self.config, self.workspace)[-1]
            separate = runtime.nested_podman_args(self.config, other)[-1]
        self.assertEqual(first, again)
        self.assertNotEqual(first, separate)
        self.assertTrue(first.startswith("--mount=type=volume,source=agent-box-podman-"))
        self.assertIn(f"target={runtime.HOST_HOME}/.local/share/containers", first)
        self.assertNotIn("/var/lib/containers", first)


class NestedLauncherBundleTests(IsolatedRuntimeTest):
    def test_path_bundle_is_a_complete_source_for_launch_and_further_nesting(self):
        """A working checkout hid missing files in the separately mounted PATH package."""
        import shutil
        import subprocess
        import sys
        source = Path(__file__).resolve().parent.parent
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            home.mkdir()
            library = home / ".local/lib/agent-box"
            workspace = Workspace(Path(tmp), Path(tmp))
            with mock.patch.object(runtime, "HOST_HOME", home), \
                 mock.patch.object(runtime, "CONTAINER_LAUNCHER_ROOT", library), \
                 mock.patch.object(runtime, "agent_tree_mounts", return_value=[]), \
                 mock.patch.object(runtime, "tracker_mounts", return_value=[]):
                flags = runtime.build_mounts(workspace, [], source, Config())
            for flag in flags[1::2]:
                fields = dict(part.split("=", 1) for part in flag.split(","))
                destination = Path(fields["dst"])
                if destination == home / ".local/bin/agent-box" or library in destination.parents:
                    self.assertEqual(fields.get("ro"), "true")
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    origin = Path(fields["src"])
                    if origin.is_dir():
                        shutil.copytree(origin, destination, ignore=shutil.ignore_patterns("__pycache__"))
                    else:
                        shutil.copy2(origin, destination)
            for name in ("agent-box", "Containerfile", ".containerignore", "agent_box/nested_podman.py"):
                self.assertTrue((library / name).is_file(), name)
            # The actual PATH launcher must reach workspace resolution using
            # its own library root, without a checkout in cwd or PYTHONPATH.
            code = '''
import runpy, sys
from unittest.mock import patch
sys.path.insert(0, sys.argv[2])
from agent_box import runtime
class ReachedWorkspace(Exception): pass
sys.argv = [sys.argv[1]]
with patch('agent_box.project_config.load', return_value={}), patch.object(runtime.host_podman, 'require'), patch.object(runtime.shutil, 'which', return_value='/bin/tool'), patch.object(runtime, 'resolve_workspace', side_effect=ReachedWorkspace):
    try:
        runpy.run_path(sys.argv[0], run_name='__main__')
    except ReachedWorkspace:
        print('workspace resolution reached')
'''
            result = subprocess.run([sys.executable, "-c", code, str(home / ".local/bin/agent-box"),
                                     str(library)], cwd=tmp, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("workspace resolution reached", result.stdout)
            with mock.patch.object(runtime, "HOST_HOME", home), \
                 mock.patch.object(runtime, "CONTAINER_LAUNCHER_ROOT", library), \
                 mock.patch.object(runtime, "agent_tree_mounts", return_value=[]), \
                 mock.patch.object(runtime, "tracker_mounts", return_value=[]):
                nested = runtime.build_mounts(workspace, [], library, Config())
            self.assertIn(f"type=bind,src={library / 'agent-box'},dst={home / '.local/bin/agent-box'},ro=true", nested)
            self.assertIn(f"type=bind,src={library / 'Containerfile'},dst={library / 'Containerfile'},ro=true", nested)

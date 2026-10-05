"""Optional tool groups: what the image is built with, and what notices drift.

The image used to be one fixed set including QEMU and a Kubernetes toolchain,
which most projects never touch and everybody paid for in build time and disk.
Now the set is configuration, which creates a second problem worth testing:
the built image and the configuration can disagree, and agent-box has to say
so rather than either ignoring it or rebuilding behind the user's back.
"""
import unittest
from unittest import mock

from agent_box import runtime
from agent_box.config import TOOL_GROUPS, Config, ToolsConfig


def _config(**groups: bool) -> Config:
    config = Config()
    config.box.tools.groups.update(groups)
    return config


class ToolsConfigSchemaTests(unittest.TestCase):
    def test_the_defaults_favour_small_and_widely_useful(self):
        self.assertEqual(Config().box.tools.enabled_groups(), ["analysis", "kubernetes"])

    def test_the_large_groups_are_off_until_asked_for(self):
        self.assertFalse(Config().box.tools.groups["virtualization"])
        self.assertFalse(Config().box.tools.groups["browser"])

    def test_narrow_groups_are_off_until_asked_for(self):
        """Registry work with skopeo is rare enough that nobody else should pay for it."""
        self.assertFalse(Config().box.tools.groups["skopeo"])

    def test_round_trips(self):
        config = _config(virtualization=True, kubernetes=False)
        loaded = Config.from_dict(config.to_dict()).box.tools
        self.assertTrue(loaded.groups["virtualization"])
        self.assertFalse(loaded.groups["kubernetes"])

    def test_an_unknown_group_is_dropped(self):
        """The file must never ask a build for a group that does not exist."""
        loaded = ToolsConfig.from_dict({"quantum": True, "analysis": False})
        self.assertNotIn("quantum", loaded.groups)
        self.assertFalse(loaded.groups["analysis"])

    def test_a_config_file_without_the_section_gets_the_defaults(self):
        config = Config.from_dict({"version": 1, "credentials": {"github": {}}})
        self.assertEqual(config.box.tools.groups, TOOL_GROUPS)

    def test_enabled_groups_are_ordered(self):
        """The label is compared as text, so its order cannot be incidental."""
        config = _config(virtualization=True, browser=True)
        self.assertEqual(config.box.tools.enabled_groups(), sorted(config.box.tools.enabled_groups()))


class BuildArgumentTests(unittest.TestCase):
    def test_every_group_is_passed_explicitly(self):
        """Including the ones that are off: the build must not fall back to
        the Containerfile's own defaults and quietly disagree with the file."""
        args = runtime.tool_build_args(Config())
        flags = [a for a in args if a != "--build-arg"]
        for group in TOOL_GROUPS:
            self.assertTrue(
                any(flag.startswith(f"TOOLS_{group.upper()}=") for flag in flags),
                f"{group} was not passed to the build")
        self.assertIn("TOOLS_ANALYSIS=1", flags)
        self.assertIn("TOOLS_KUBERNETES=1", flags)
        self.assertIn("TOOLS_VIRTUALIZATION=0", flags)
        self.assertIn("TOOLS_BROWSER=0", flags)

    def test_the_label_value_matches_the_enabled_set(self):
        flags = [a for a in runtime.tool_build_args(_config(virtualization=True))
                 if a != "--build-arg"]
        self.assertIn("AGENT_BOX_TOOLS=analysis,kubernetes,virtualization", flags)

    def test_an_empty_selection_still_passes_a_label(self):
        config = _config(analysis=False, kubernetes=False)
        flags = [a for a in runtime.tool_build_args(config) if a != "--build-arg"]
        self.assertIn("AGENT_BOX_TOOLS=", flags)

    def test_each_flag_is_preceded_by_its_option(self):
        args = runtime.tool_build_args(Config())
        self.assertEqual(args[::2], ["--build-arg"] * (len(args) // 2))


class ImageDriftTests(unittest.TestCase):
    def _label(self, value: str | None):
        result = mock.Mock(
            returncode=0 if value is not None else 1, stdout=value or "")
        return mock.patch.object(runtime.subprocess, "run", return_value=result)

    def test_a_matching_image_reports_no_drift(self):
        with self._label("analysis,kubernetes"):
            self.assertEqual(runtime.image_tool_drift(Config()), ([], []))

    def test_a_group_turned_on_since_the_build_is_reported_missing(self):
        with self._label("analysis,kubernetes"):
            missing, extra = runtime.image_tool_drift(_config(virtualization=True))
        self.assertEqual(missing, ["virtualization"])
        self.assertEqual(extra, [])

    def test_a_group_turned_off_since_the_build_is_reported_extra(self):
        with self._label("analysis,kubernetes,browser"):
            missing, extra = runtime.image_tool_drift(Config())
        self.assertEqual(missing, [])
        self.assertEqual(extra, ["browser"])

    def test_an_unlabelled_image_is_unknown_rather_than_empty(self):
        """An image built before the label must not look like it has nothing."""
        for value in ("", "<no value>", None):
            with self.subTest(value=value):
                with self._label(value):
                    self.assertEqual(runtime.image_tool_drift(Config()), ([], []))

    def test_podman_being_unavailable_is_not_drift(self):
        with mock.patch.object(runtime.subprocess, "run", side_effect=OSError):
            self.assertIsNone(runtime.image_tool_groups())
            self.assertEqual(runtime.image_tool_drift(Config()), ([], []))


class KvmPassthroughTests(unittest.TestCase):
    """/dev/kvm follows the group that supplies something to use it."""

    def test_no_virtualization_group_means_no_device(self):
        self.assertEqual(runtime.kvm_args(Config()), [])

    def test_the_device_is_considered_when_the_group_is_on(self):
        config = _config(virtualization=True)
        with mock.patch.object(runtime.Path, "exists", return_value=False):
            self.assertEqual(runtime.kvm_args(config), [])

    def test_no_kvm_withholds_a_present_device(self):
        config = _config(virtualization=True)
        with mock.patch.object(runtime.Path, "exists", return_value=True):
            self.assertEqual(runtime.kvm_args(config, enabled=False), [])


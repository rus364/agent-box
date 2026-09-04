"""Vendoring third-party skills: what gets rewritten, and what must not.

The skills agent-box ships from ``obra/superpowers`` are checked into this
repository rather than fetched at install time, so what a user runs is what
was reviewed. `agent-box skills vendor` is the maintainer command that
refreshes that copy, and its job is narrower than it looks: take the few
skills we ship, point their cross-references at the vendored copies, and
refuse to write anything whose references no longer resolve.

The interesting case is a reference to a skill we do *not* vendor. Rewriting
it would fabricate a link to something the user does not have; leaving it is
prose naming an upstream skill the reader can go and find. The tests below pin
that distinction, because getting it wrong is silent.
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_box import vendor
from agent_box.errors import AgentBoxError

SPEC = vendor.VendorSpec(
    name="example",
    repo="https://example.invalid/upstream.git",
    skills=("alpha", "beta"),
    reference_prefix="upstream:",
)


def _skill(root: Path, name: str, body: str = "", extra: dict | None = None) -> Path:
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(f"---\nname: {name}\n---\n\n{body}")
    for filename, content in (extra or {}).items():
        (directory / filename).write_text(content)
    return directory


class ReferenceRewritingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_a_reference_to_a_vendored_skill_is_pointed_at_the_copy(self):
        skill = _skill(self.root, "alpha", "See upstream:beta for details.\n")
        self.assertEqual(vendor._apply_rewrites(skill, SPEC), [])
        self.assertIn("$beta", (skill / "SKILL.md").read_text())

    def test_a_reference_to_a_skill_we_do_not_ship_is_left_alone(self):
        """Rewriting it would invent a link to a skill the user lacks."""
        skill = _skill(self.root, "alpha", "", {"notes.md": "Compare upstream:writing.\n"})
        untouched = vendor._apply_rewrites(skill, SPEC)
        self.assertEqual(untouched, ["upstream:writing"])
        self.assertIn("upstream:writing", (skill / "notes.md").read_text())

    def test_both_kinds_can_appear_in_one_file(self):
        skill = _skill(self.root, "alpha", "", {"n.md": "upstream:beta and upstream:writing\n"})
        untouched = vendor._apply_rewrites(skill, SPEC)
        self.assertEqual(untouched, ["upstream:writing"])
        text = (skill / "n.md").read_text()
        self.assertIn("$beta", text)
        self.assertIn("upstream:writing", text)

    def test_sentence_punctuation_is_not_part_of_the_name(self):
        """'upstream:beta.' names beta; swallowing the dot would skip it."""
        skill = _skill(self.root, "alpha", "Use upstream:beta. Then stop.\n")
        self.assertEqual(vendor._apply_rewrites(skill, SPEC), [])
        self.assertIn("$beta. Then", (skill / "SKILL.md").read_text())

    def test_a_hyphenated_name_survives_intact(self):
        spec = vendor.VendorSpec(
            name="e", repo="r", skills=("test-driven-development",),
            reference_prefix="upstream:")
        skill = _skill(self.root, "alpha", "See upstream:test-driven-development.\n")
        self.assertEqual(vendor._apply_rewrites(skill, spec), [])
        self.assertIn("$test-driven-development.", (skill / "SKILL.md").read_text())

    def test_a_spec_without_a_prefix_rewrites_nothing(self):
        plain = vendor.VendorSpec(name="p", repo="r", skills=("alpha",))
        skill = _skill(self.root, "alpha", "upstream:beta\n")
        self.assertEqual(vendor._apply_rewrites(skill, plain), [])
        self.assertIn("upstream:beta", (skill / "SKILL.md").read_text())


class SkillCheckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_a_well_formed_skill_passes(self):
        skill = _skill(self.root, "alpha", "Nothing unusual.\n")
        vendor._check_skill(skill, "alpha", SPEC)

    def test_a_missing_skill_file_is_rejected(self):
        (self.root / "alpha").mkdir()
        with self.assertRaises(AgentBoxError):
            vendor._check_skill(self.root / "alpha", "alpha", SPEC)

    def test_a_renamed_skill_is_rejected(self):
        """The frontmatter name is what an agent resolves; it must match."""
        skill = _skill(self.root, "alpha")
        (skill / "SKILL.md").write_text("---\nname: something-else\n---\n")
        with self.assertRaises(AgentBoxError) as caught:
            vendor._check_skill(skill, "alpha", SPEC)
        self.assertIn("something-else", str(caught.exception))

    def test_a_nested_skill_file_is_rejected(self):
        """It means upstream restructured into a shape this does not handle."""
        skill = _skill(self.root, "alpha")
        (skill / "inner").mkdir()
        (skill / "inner/SKILL.md").write_text("---\nname: inner\n---\n")
        with self.assertRaises(AgentBoxError):
            vendor._check_skill(skill, "alpha", SPEC)

    def test_an_unresolved_reference_in_skill_md_is_rejected(self):
        """There it is a runtime dependency, not a remark."""
        skill = _skill(self.root, "alpha", "Requires upstream:writing.\n")
        with self.assertRaises(AgentBoxError):
            vendor._check_skill(skill, "alpha", SPEC)

    def test_an_unresolved_reference_in_prose_is_accepted(self):
        skill = _skill(self.root, "alpha", "", {"notes.md": "See upstream:writing.\n"})
        vendor._check_skill(skill, "alpha", SPEC)

    def test_a_broken_local_link_is_rejected(self):
        skill = _skill(self.root, "alpha", "Read [more](missing.md).\n")
        with self.assertRaises(AgentBoxError) as caught:
            vendor._check_skill(skill, "alpha", SPEC)
        self.assertIn("missing.md", str(caught.exception))

    def test_external_and_absolute_links_are_not_checked(self):
        skill = _skill(
            self.root, "alpha",
            "[a](https://example.com/x.md) [b](/abs/x.md) [c](#anchor)\n")
        vendor._check_skill(skill, "alpha", SPEC)


class ReleaseSelectionTests(unittest.TestCase):
    def _remote(self, tags: list[str]):
        output = "".join(f"abc123\\trefs/tags/{tag}\n" for tag in tags)
        return mock.patch.object(vendor, "_run", return_value=output)

    def test_the_highest_release_wins_numerically(self):
        """String order would put v6.9.0 above v6.10.0."""
        with self._remote(["v6.9.0", "v6.10.0", "v6.2.0"]):
            self.assertEqual(vendor.latest_release("r"), "v6.10.0")

    def test_pre_releases_and_branches_are_ignored(self):
        with self._remote(["v7.0.0-rc1", "nightly", "v6.3.0"]):
            self.assertEqual(vendor.latest_release("r"), "v6.3.0")

    def test_no_release_is_an_error_rather_than_a_guess(self):
        with self._remote(["main", "v7.0.0-rc1"]):
            with self.assertRaises(AgentBoxError):
                vendor.latest_release("r")


class VendoredCopyTests(unittest.TestCase):
    """What is actually checked in, independent of how it got here."""

    def test_the_manifest_records_the_upstream_and_the_exact_commit(self):
        manifest = vendor.read_manifest()
        record = manifest.get("superpowers")
        self.assertIsNotNone(record, "superpowers must be vendored")
        self.assertEqual(record["source"], vendor.SPECS["superpowers"].repo)
        self.assertRegex(record["tag"], r"^v\d+\.\d+\.\d+$")
        self.assertRegex(record["commit"], r"^[0-9a-f]{40}$")

    def test_every_vendored_skill_is_present_and_carries_its_licence(self):
        from agent_box.config import SUPERPOWERS_SKILLS
        from agent_box.paths import MANAGED_SKILLS_SOURCE
        for name in SUPERPOWERS_SKILLS:
            with self.subTest(skill=name):
                directory = MANAGED_SKILLS_SOURCE / name
                self.assertTrue((directory / "SKILL.md").is_file())
                self.assertIn("MIT License", (directory / "LICENSE").read_text())

    def test_no_vendored_skill_file_still_depends_on_the_plugin(self):
        from agent_box.config import SUPERPOWERS_SKILLS
        from agent_box.paths import MANAGED_SKILLS_SOURCE
        for name in SUPERPOWERS_SKILLS:
            skill_md = MANAGED_SKILLS_SOURCE / name / "SKILL.md"
            self.assertNotIn("superpowers:", skill_md.read_text(), name)

    def test_the_manifest_is_valid_json_naming_the_skills(self):
        from agent_box.paths import MANAGED_SKILLS_SOURCE
        data = json.loads((MANAGED_SKILLS_SOURCE / "VENDORED.json").read_text())
        self.assertEqual(
            sorted(data["superpowers"]["skills"]),
            sorted(vendor.SPECS["superpowers"].skills))


class CommandTests(unittest.TestCase):
    def test_an_unknown_upstream_is_rejected(self):
        with self.assertRaises(AgentBoxError):
            vendor.vendor_main(["vendor", "nonesuch"])

    def test_a_malformed_invocation_is_rejected(self):
        for args in ([], ["vendor"], ["fetch", "superpowers"], ["vendor", "a", "b"]):
            with self.subTest(args=args):
                with self.assertRaises(AgentBoxError):
                    vendor.vendor_main(args)


if __name__ == "__main__":
    unittest.main()

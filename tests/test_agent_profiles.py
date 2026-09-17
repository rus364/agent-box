"""Native profile pins and local edits survive the apply/remove lifecycle."""
import tempfile
import unittest
from pathlib import Path
import tomllib
from agent_box.agent_profiles import body, sync
from agent_box.config import Config


def entry(provider, model, effort, **fields):
    return {"provider": provider, "model": model, "effort": effort, "fast": False, "api_provider": "", **fields}


class AgentProfileTests(unittest.TestCase):
    def test_both_providers_keep_report_language_and_role_evidence_semantics(self):
        """An installed scout must not inherit the reviewer's findings contract, nor a worker its read-only rule."""
        for provider in ("codex", "claude"):
            for role in ("worker", "scout", "reviewer"):
                with self.subTest(provider=provider, role=role):
                    content = body(provider, role, 1, {"model": "chosen-model", "effort": "high"})
                    instructions = (tomllib.loads(content)["developer_instructions"]
                                    if provider == "codex" else content.split("---\n", 2)[2])
                    self.assertIn("English", instructions)
                    self.assertIn("never write to any of them", instructions)
                    if role == "reviewer":
                        self.assertIn("Review findings require location, evidence and violated requirement", instructions)
                        self.assertIn("do not load parent conversation", instructions.lower())
                    else:
                        self.assertIn("## Done, ## Evidence, ## Limitations", instructions)
                        self.assertNotIn("Review findings require", instructions)
                        self.assertEqual("Read-only: change nothing" in instructions, role == "scout")
                        self.assertEqual("Change project files only within the assigned scope" in instructions,
                                         role == "worker")

    def test_empty_reviewer_models_never_install_worker_models_as_reviewers(self):
        with tempfile.TemporaryDirectory() as directory:
            codex, claude = Path(directory) / "codex", Path(directory) / "claude"
            config = Config()
            config.workflow.enabled = True
            config.workflow.worker.tiers["2"] = [entry(p, "worker-model", "medium") for p in ("codex", "claude")]
            config.workflow.review.tiers["2"] = [entry(p, "reviewer-model", "default") for p in ("codex", "claude")]
            sync(config, codex, claude)
            self.assertNotIn("model_reasoning_effort", tomllib.loads((codex / "agents/ab-reviewer-2.toml").read_text()))
            self.assertNotIn("effort:", (claude / "agents/ab-reviewer-2.md").read_text())
            config.workflow.review.tiers["2"] = []
            sync(config, codex, claude)
            for home, suffix in ((codex, "toml"), (claude, "md")):
                self.assertFalse((home / f"agents/ab-reviewer-2.{suffix}").exists())
                self.assertTrue((home / f"agents/ab-worker-2.{suffix}").exists())

    def test_each_provider_profile_takes_that_providers_first_native_candidate(self):
        """A mixed list installs a profile per CLI: the other CLI's and API candidates are not native to it."""
        with tempfile.TemporaryDirectory() as directory:
            codex, claude = Path(directory) / "codex", Path(directory) / "claude"
            config = Config()
            config.workflow.enabled = True
            config.workflow.scout.models = [entry("codex", "gpt-5.6-luna", "high"),
                                            entry("claude", "deepseek-v4-flash", "high", api_provider="deepseek"),
                                            entry("claude", "haiku", "low")]
            sync(config, codex, claude)
            for tier in range(4):
                self.assertEqual(tomllib.loads((codex / f"agents/ab-scout-{tier}.toml").read_text())["model"], "gpt-5.6-luna")
                self.assertIn('model: "haiku"', (claude / f"agents/ab-scout-{tier}.md").read_text())

    def test_apply_with_a_malformed_list_removes_profiles_instead_of_crashing(self):
        """config apply reports validation warnings and still syncs; a wrong container means no candidate."""
        with tempfile.TemporaryDirectory() as directory:
            codex, claude = Path(directory) / "codex", Path(directory) / "claude"
            config = Config.from_dict({"workflow": {"enabled": True, "scout": {"models": "luna", "tiers": ["x"]}}})
            self.assertTrue(config.workflow.validate())
            self.assertEqual(sync(config, codex, claude), [])
            self.assertFalse((codex / "agents/ab-scout-0.toml").exists())

    def test_profile_toml_carries_what_toml_can_spell(self):
        """A profile is TOML Codex parses, so its fields need the TOML escaper.

        A tier model is not restricted to TOML's safe characters; json.dumps
        wrote a non-BMP one as a surrogate pair, which TOML rejects, and left
        DEL literal, which a basic string forbids.
        """
        spec = {"model": "🚀 del\x7fmodel", "effort": "low", "fast": False}
        self.assertEqual(tomllib.loads(body("codex", "worker", 1, spec))["model"],
                         "🚀 del\x7fmodel")

    def test_a_configured_field_toml_cannot_carry_leaves_the_profile_uninstalled(self):
        """A lone surrogate cannot reach a UTF-8 file, whichever field holds it.

        Tier models and efforts arrive from JSON without a charset check and the
        profile write is strict UTF-8: each has to be reported as uninstalled
        instead of raising out of `config apply`.
        """
        for held in ("model", "effort"):
            with self.subTest(held=held), tempfile.TemporaryDirectory() as directory:
                codex, claude = Path(directory) / "codex", Path(directory) / "claude"
                config = Config()
                config.workflow.enabled = True
                config.workflow.worker.tiers["1"] = [{**entry("codex", "pinned-model", "high"), held: "\ud83d"}]
                messages = sync(config, codex, claude)
                self.assertTrue(any("ab-worker-1.toml" in message and "UTF-8" in message
                                    for message in messages), messages)
                self.assertFalse((codex / "agents/ab-worker-1.toml").exists())

    def test_exact_models_are_installed_and_locally_edited_profiles_survive_disable(self):
        with tempfile.TemporaryDirectory() as directory:
            codex, claude = Path(directory) / "codex", Path(directory) / "claude"
            config = Config()
            config.workflow.enabled = True
            config.workflow.worker.tiers["1"] = [entry("codex", "pinned-model", "high"), entry("claude", "other-model", "medium")]
            config.workflow.review.tiers["1"] = [entry("codex", "review-model", "low"), entry("claude", "review-claude", "high")]
            self.assertEqual(sync(config, codex, claude), [])
            profile = codex / "agents/ab-reviewer-1.toml"
            data = tomllib.loads(profile.read_text())
            self.assertEqual((data["model"], data["model_reasoning_effort"], data["sandbox_mode"]), ("review-model", "low", "read-only"))
            worker = tomllib.loads((codex / "agents/ab-worker-1.toml").read_text())
            self.assertEqual(worker["model"], "pinned-model")
            self.assertIn('model: "review-claude"', (claude / "agents/ab-reviewer-1.md").read_text())
            self.assertTrue((claude / "agents/ab-worker-1.md").read_text().startswith("---\n"))
            profile.write_text(profile.read_text() + "# user customization\n")
            config.workflow.enabled = False
            self.assertTrue(sync(config, codex, claude))
            self.assertTrue(profile.read_text().endswith("# user customization\n"))
            self.assertFalse((claude / "agents/ab-worker-1.md").exists())

    def test_default_effort_is_omitted_from_profiles(self):
        with tempfile.TemporaryDirectory() as directory:
            codex, claude = Path(directory) / "codex", Path(directory) / "claude"
            config = Config()
            config.workflow.enabled = True
            config.workflow.worker.tiers["1"] = [entry("codex", "chosen-model", "default"),
                                                 entry("claude", "chosen-model", "default")]
            self.assertEqual(sync(config, codex, claude), [])
            codex_profile = codex / "agents/ab-worker-1.toml"
            claude_profile = claude / "agents/ab-worker-1.md"
            self.assertNotIn("model_reasoning_effort", tomllib.loads(codex_profile.read_text()))
            self.assertNotIn("effort:", claude_profile.read_text())

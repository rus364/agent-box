"""A configured account is not an implicit choice of tracker provider."""
import tempfile
import unittest
from pathlib import Path

from agent_box import project_config, runtime
from agent_box.config import Config, TrackerConnection, load_config, save_config
from agent_box.tracker.core import TrackerError
from agent_box.tracker.registry import make_adapter


class TrackerSelectionTests(unittest.TestCase):
    def test_missing_and_cleared_provider_stay_unselected_after_save(self):
        """Saving an unset default must never silently select a product on reload."""
        for data in ({}, {"tracker": {}}, {"tracker": {"provider": ""}}):
            with self.subTest(data=data), tempfile.TemporaryDirectory() as directory:
                config = Config.from_dict(data)
                self.assertEqual(config.tracker.provider, "")
                self.assertFalse(config.tracker.enabled)
                path = Path(directory) / "config.json"
                save_config(config, path)
                self.assertEqual(load_config(path).tracker.provider, "")

    def test_enabled_unselected_tracker_requests_a_choice_without_guessing_an_account(self):
        """Even a usable account must not make provider selection implicit."""
        config = Config()
        config.tracker.enabled = True
        config.tracker.connections["vikunja"] = TrackerConnection("https://tasks.example", "fixture")
        with self.assertRaisesRegex(TrackerError, "No tracker provider selected.*agent-box config") as caught:
            make_adapter(config=config)
        self.assertEqual(caught.exception.category, "configuration")
        warnings = runtime.tracker_warnings(config)
        self.assertEqual(len(warnings), 1)
        self.assertIn("No tracker provider selected", warnings[0])

    def test_disabled_unselected_tracker_reports_disabled(self):
        """Fresh installs need the enable instruction before provider diagnostics."""
        with self.assertRaisesRegex(TrackerError, "Tracker is disabled"):
            make_adapter(config=Config())
        self.assertEqual(runtime.tracker_warnings(Config()), [])

    def test_explicit_global_project_and_command_choices_work_with_an_unset_default(self):
        """Every existing explicit selection route must keep using its own account."""
        for provider, url in (("vikunja", "https://tasks.example"), ("github", "https://api.github.com")):
            with self.subTest(provider=provider):
                config = Config()
                config.tracker.enabled = True
                config.tracker.connections[provider] = TrackerConnection(url, "fixture")
                self.assertEqual(make_adapter(provider=provider, config=config).client.token, "fixture")
                project = {"tracker": {"provider": provider}}
                project_config.validate(project, Path(".agent-box.json"))
                selected = project_config.apply(config, project)
                self.assertEqual(make_adapter(config=selected).client.token, "fixture")
                self.assertEqual(runtime.tracker_warnings(selected), [])
                self.assertEqual(config.tracker.provider, "")
                config.tracker.provider = provider
                restored = Config.from_dict(config.to_dict())
                self.assertEqual(make_adapter(config=restored).client.token, "fixture")

    def test_project_can_clear_an_inherited_selection(self):
        """An explicit empty project value is a choice, distinct from inheritance."""
        config = Config()
        config.tracker.provider = "github"
        project = {"tracker": {"provider": ""}}
        project_config.validate(project, Path(".agent-box.json"))
        self.assertEqual(project_config.apply(config, project).tracker.provider, "")

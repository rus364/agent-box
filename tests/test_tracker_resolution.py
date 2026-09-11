"""Visible numbers must never select a global ID or a text-search accident."""
import unittest
from unittest.mock import patch

from agent_box.repo_binding import Binding
from agent_box.tracker import cli
from agent_box.tracker.core import TrackerError
from agent_box.tracker.vikunja import VikunjaAdapter
from test_tracker_staging import NativeClient
from test_tracker_lifecycle import task


class ResolutionTests(unittest.TestCase):
    def setUp(self):
        self.client = NativeClient()
        self.client.tasks = {
            319: task(319, 1, 10, 100, index=21, title="Reviewer problem"),
            21: task(21, 1, 10, 100, index=8, title="Other task", description="21:01 Reviewer problem"),
            322: task(322, 2, 20, 200, index=24, title="Resolution"),
            400: task(400, 9, 90, 900, index=21, title="Reviewer problem"),
        }
        self.binding = Binding('vikunja', self.client.url, '1', True)
        self.adapter = VikunjaAdapter(self.client.url, 'token', self.binding, self.client)

    def resolve(self, *arguments):
        with patch.object(cli, 'context', return_value=(self.adapter, self.binding, None)):
            return cli.dispatch(cli.parser().parse_args(['resolve', *arguments]))

    def test_visible_number_wins_over_global_id_and_description(self):
        """A copied UI number identifies #21 even when global ID 21 exists."""
        for reference in ('21', '#21'):
            with self.subTest(reference=reference):
                found = self.resolve(reference)
                self.assertEqual(found.id, '319')
                for full in (True, False):
                    self.assertEqual(found.to_dict(full=full)['number'], 21)
                self.assertEqual(self.adapter.get(found.id).id, '319')
        self.assertEqual(self.client.writes, [])

    def test_exact_title_does_not_match_description_or_partial_title(self):
        """Only a complete title can establish an automatic selection."""
        self.assertEqual(self.resolve('Reviewer problem').id, '319')
        for reference in ('Reviewer', '999', '21:01'):
            with self.subTest(reference=reference), self.assertRaisesRegex(TrackerError, 'No exact'):
                self.resolve(reference)

    def test_missing_number_does_not_fall_back_to_global_id(self):
        """An existing canonical ID cannot rescue an absent visible number."""
        with self.assertRaisesRegex(TrackerError, 'No exact'):
            self.resolve('319')

    def test_recursive_scope_finds_child_without_prioritizing_root(self):
        """Duplicate numbers and titles across projects require a choice."""
        self.assertEqual(self.resolve('24').id, '322')
        self.client.tasks[322].update(index=21, title='Reviewer problem')
        for reference in ('21', 'Reviewer problem'):
            with self.subTest(reference=reference), self.assertRaises(TrackerError) as caught:
                self.resolve(reference)
            message = str(caught.exception)
            self.assertIn('Root (project 1)', message)
            self.assertIn('Child (project 2)', message)
            self.assertIn('ID 319', message)
            self.assertIn('ID 322', message)
        self.assertEqual(self.resolve('21', '--project', '2').id, '322')
        self.assertEqual(self.client.writes, [])

    def test_nonrecursive_binding_and_project_override_keep_scope(self):
        """An explicit project can narrow a binding, never widen it."""
        self.binding = Binding('vikunja', self.client.url, '1', False)
        with self.assertRaisesRegex(TrackerError, 'No exact'):
            self.resolve('24')
        for project in ('2', '9'):
            with self.assertRaisesRegex(TrackerError, 'outside'):
                self.resolve('21', '--project', project)

    def test_url_selects_canonical_task_without_adoption_redirect(self):
        """A URL points at its own object; old/outside objects are not copies."""
        self.assertEqual(self.resolve(self.client.url + '/tasks/21').id, '21')
        self.assertEqual(self.resolve(self.client.url + '/tasks/319?view=1#details').id, '319')
        for reference in (self.client.url + '/tasks/400',
                          'https://elsewhere.example/tasks/319',
                          self.client.url + '/tasks/319/other'):
            with self.subTest(reference=reference), self.assertRaises(TrackerError):
                self.resolve(reference)
        self.assertEqual(self.client.writes, [])

    def test_explicit_title_disambiguates_numeric_titles(self):
        """A title resembling a number remains reachable without guessing."""
        self.client.tasks[322]['title'] = '21'
        self.assertEqual(self.resolve('21', '--title').id, '322')
        self.assertEqual(self.resolve('21').id, '319')

    def test_canonical_result_reaches_update_without_number_resolution(self):
        """Passing the resolved ID to a write cannot switch to another #319."""
        self.client.tasks[322]['index'] = 319
        found = self.resolve('21')
        with patch.object(cli, 'context', return_value=(self.adapter, self.binding, None)):
            changed = cli.dispatch(cli.parser().parse_args(['update', found.id, '--title', 'Fixed']))
        self.assertEqual(changed.id, '319')
        self.assertEqual(self.client.tasks[319]['title'], 'Fixed')
        self.assertEqual(self.client.tasks[322]['title'], 'Resolution')

    def test_unbound_resolution_refuses_to_guess_project(self):
        """User numbers need a known project namespace."""
        self.binding = None
        with self.assertRaisesRegex(TrackerError, 'binding'):
            self.resolve('21')

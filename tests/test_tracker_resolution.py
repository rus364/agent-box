"""Visible numbers must never select a global ID or a text-search accident."""
import unittest
from unittest.mock import patch

from agent_box import workflow
from agent_box.config import Config
from agent_box.repo_binding import Binding
from agent_box.tracker import cli
from agent_box.tracker import vikunja_api as api
from agent_box.tracker.core import TrackerError
from agent_box.tracker.vikunja import VikunjaAdapter
from tracker_fixtures import NativeClient, task


class IndexClient(NativeClient):
    def get(self, path):
        parts = path.split('?')[0].strip('/').split('/')
        if len(parts) == 5 and parts[:1] == ['projects'] and parts[2:4] == ['tasks', 'by-index']:
            self.reads.append(path)
            for row in self.tasks.values():
                if row['project_id'] == int(parts[1]) and row['index'] == int(parts[4]):
                    return dict(row)
            raise api.APIError(404, 'This task does not exist', 4002)
        if len(parts) == 2 and parts[0] == 'tasks' and int(parts[1]) not in self.tasks:
            raise api.APIError(404, 'This task does not exist', 4002)
        return super().get(path)


class ResolutionTests(unittest.TestCase):
    def setUp(self):
        self.client = IndexClient()
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

    def get(self, reference):
        with patch.object(cli, 'context', return_value=(self.adapter, self.binding, None)), \
             patch.object(self.adapter, 'comments', return_value=[]):
            return cli.dispatch(cli.parser().parse_args(['get', reference]))

    def test_get_reads_a_user_reference_without_a_separate_resolve(self):
        """A copied number, title or URL reads its task; canonical IDs keep working."""
        for reference, expected in (('#21', '319'), ('24', '322'), ('Resolution', '322'),
                                    (self.client.url + '/tasks/319', '319'), ('319', '319'), ('400', '400')):
            with self.subTest(reference=reference):
                self.assertEqual(self.get(reference)['id'], expected)
        self.assertEqual(self.client.writes, [])

    def test_get_without_comments_preserves_reference_resolution_and_revision_without_fetching_history(self):
        """Reading a sync candidate must not load its whole journal just to obtain the description."""
        with patch.object(cli, 'context', return_value=(self.adapter, self.binding, None)), \
                patch.object(self.adapter, 'comments', return_value=[{'comment': 'History'}]) as comments:
            result = cli.dispatch(cli.parser().parse_args(['get', '#21', '--no-comments']))
            self.assertEqual(result['id'], '319')
            self.assertIn('description', result)
            self.assertIn('revision', result)
            self.assertNotIn('comments', result)
            comments.assert_not_called()
            result = cli.dispatch(cli.parser().parse_args(['get', '#21']))
            self.assertEqual(result['comments'], [{'comment': 'History'}])
            comments.assert_called_once_with('319')

    def inspect(self, reference):
        config = Config()
        config.workflow.enabled = True
        with patch.object(workflow, 'effective_config', return_value=config), \
             patch.object(workflow, 'repository_root'), \
             patch.object(Binding, 'session', return_value=self.binding), \
             patch.object(workflow, 'make_adapter', return_value=self.adapter), \
             patch.object(workflow, 'inspect', side_effect=lambda root, binding, adapter, task, config, authors: task.id):
            return workflow.dispatch(workflow.parser().parse_args(['inspect', reference]))

    def test_workflow_inspect_accepts_the_same_references_as_get(self):
        """Read-only workflow diagnostics must understand a copied task link or UI number."""
        for reference, expected in (('#21', '319'), ('24', '322'), ('Resolution', '322'),
                                    (self.client.url + '/tasks/319', '319'), ('319', '319'), ('400', '400')):
            with self.subTest(reference=reference):
                self.assertEqual(self.inspect(reference), expected)
        self.assertEqual(self.client.writes, [])

    def test_workflow_inspect_preserves_reference_ambiguity_and_scope_refusals(self):
        """Convenient reads cannot guess conflicting IDs or redirect an outside URL."""
        for reference in ('21', 'Reviewer', self.client.url + '/tasks/400',
                          'https://elsewhere.example/tasks/319'):
            with self.subTest(reference=reference), self.assertRaises(TrackerError):
                self.inspect(reference)
        self.client.tasks[322]['index'] = 21
        with self.assertRaisesRegex(TrackerError, 'Ambiguous'):
            self.inspect('#21')
        self.assertEqual(self.client.writes, [])

    def test_recording_workflow_results_still_uses_canonical_ids(self):
        """A read convenience must not retarget a mutating command to a visible number."""
        config = Config()
        config.workflow.enabled = True
        with patch.object(workflow, 'effective_config', return_value=config), \
             patch.object(workflow, 'repository_root'), \
             patch.object(Binding, 'session', return_value=self.binding), \
             patch.object(workflow, 'make_adapter', return_value=self.adapter), \
             patch.object(workflow, 'native_result', side_effect=lambda args, root, binding, adapter, config, task: task.id):
            args = workflow.parser().parse_args(['review', '21', '--result', '/unused/report'])
            self.assertEqual(workflow.dispatch(args), '21')

    def test_get_refuses_a_number_that_is_one_tasks_id_and_anothers_visible_number(self):
        """Silently choosing either reading of 21 would show the wrong requirements."""
        with self.assertRaises(TrackerError) as caught:
            self.get('21')
        message = str(caught.exception)
        for fragment in ('canonical ID 21', 'ID 319', '"#21"', 'URL'):
            self.assertIn(fragment, message)
        self.assertEqual(caught.exception.category, 'conflict')
        with self.assertRaisesRegex(TrackerError, 'No exact'):
            self.get('999')

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

    def test_number_uses_native_index_lookup_without_listing_tasks(self):
        """The provider resolves (project, index) without downloading all tasks."""
        with patch.object(self.adapter, 'search', side_effect=AssertionError('Unexpected task listing')):
            self.assertEqual(self.resolve('21').id, '319')
        for project in (1, 2):
            self.assertTrue(any(path.startswith(f'/projects/{project}/tasks/by-index/21?')
                                for path in self.client.reads))

    def test_partial_index_lookup_failure_cannot_claim_unique_match(self):
        """An unreadable sibling could contain another #21, so errors propagate."""
        original = self.client.get
        for status, code in ((403, 0), (404, 1), (500, 0)):
            def get(path):
                if path.startswith('/projects/2/tasks/by-index/'):
                    raise api.APIError(status, 'lookup failed', code)
                return original(path)
            with self.subTest(status=status), patch.object(self.client, 'get', side_effect=get):
                with self.assertRaises(api.APIError):
                    self.resolve('21')
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

    def test_browser_url_survives_container_transport_translation(self):
        """Users paste public links, while the API may use a container host alias."""
        self.binding = Binding('vikunja', 'http://localhost:3456/vikunja', '1', True)
        self.client.url = 'http://host.containers.internal:3456/vikunja'
        self.adapter = VikunjaAdapter(self.client.url, 'token', self.binding, self.client)
        public = self.binding.server + '/tasks/319'
        self.assertEqual(self.resolve(public).id, '319')
        for found in (self.resolve('21'), self.adapter.get('319')):
            for full in (True, False):
                self.assertEqual(found.to_dict(full=full)['url'], public)
        self.assertEqual(self.client.url, 'http://host.containers.internal:3456/vikunja')

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

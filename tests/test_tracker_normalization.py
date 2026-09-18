"""Incremental normalization skips marked tasks without hiding relation candidates."""
import copy
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from agent_box import runtime
from agent_box.config import Config
from agent_box.repo_binding import Binding
from agent_box.tracker import cli, vikunja_api as api
from agent_box.tracker.core import NORMALIZED_LABEL, TrackerError
from agent_box.tracker.vikunja import VikunjaAdapter
from test_tracker_lifecycle import task
from test_tracker_staging import NativeClient


class NormalizationClient(NativeClient):
    per_page = 1

    def __init__(self):
        super().__init__()
        self.labels = [{'id': 1, 'title': 'keep'}]
        self.tasks = {
            5: task(5, 1, 10, 100, labels=[self.labels[0]], description='Unresolved question'),
            6: task(6, 2, 20, 200, labels=[{'id': 2, 'title': NORMALIZED_LABEL}], description='Do not output'),
            7: task(7, 9, 90, 900, description='Outside'),
            8: task(8, 2, 20, 200, description='Last page candidate'),
        }

    def paginate(self, path, params=None):
        if path == '/tasks':
            return api.VikunjaClient.paginate(self, path, params)
        if path == '/labels':
            return copy.deepcopy(self.labels)
        return super().paginate(path, params)

    def get(self, path):
        if path.startswith('/tasks?'):
            self.reads.append(path)
            params = parse_qs(urlsplit(path).query)
            ids = {int(n) for n in re.findall(r'\d+', params.get('filter', [''])[0])}
            rows = [r for r in self.tasks.values() if r['project_id'] in ids]
            query = params.get('q', [''])[0].casefold()
            rows = [r for r in rows if query in (r['title'] + r['description']).casefold()]
            page = int(params['page'][0])
            return {'items': copy.deepcopy(rows[page-1:page]), 'total': len(rows), 'total_pages': len(rows)}
        return super().get(path)

    def post(self, path, body):
        if path == '/labels':
            self.writes.append((path, body))
            label = {'id': max([r['id'] for r in self.labels] + [2]) + 1, **body}
            self.labels.append(label)
            return dict(label)
        if path.endswith('/labels'):
            self.writes.append((path, body))
            label = next(r for r in self.labels if r['id'] == body['label_id'])
            self.tasks[int(path.split('/')[2])].setdefault('labels', []).append(dict(label))
            return dict(body)
        return super().post(path, body)

    def delete(self, path):
        self.writes.append((path, None))
        parts = path.split('/')
        self.tasks[int(parts[2])]['labels'] = [r for r in self.tasks[int(parts[2])]['labels']
                                             if r['id'] != int(parts[4])]


class NormalizationTests(unittest.TestCase):
    def setUp(self):
        self.client = NormalizationClient()
        self.binding = Binding('vikunja', self.client.url, '1', True)
        self.adapter = VikunjaAdapter(self.client.url, 'token', self.binding, self.client)
        self.context = patch.object(cli, 'context', return_value=(self.adapter, self.binding, None))
        self.context.start()
        self.addCleanup(self.context.stop)

    def run_cli(self, *args):
        return cli.dispatch(cli.parser().parse_args(args))

    def test_filtered_pages_preserve_scope_and_unfiltered_relation_discovery(self):
        """A marked middle page must not hide later candidates or leak full text."""
        rows = self.run_cli('search', '--exclude-label', NORMALIZED_LABEL)
        self.assertEqual([r['id'] for r in rows], ['5', '8'])
        self.assertNotIn('description', json.dumps(rows))
        self.assertNotIn('Do not output', json.dumps(rows))
        self.assertTrue(any('page=3' in r for r in self.client.reads))
        self.assertEqual([r['id'] for r in self.run_cli('search', 'Task 6')], ['6'])
        self.assertEqual([r['id'] for r in self.run_cli('search', '--project', '2', '--exclude-label', NORMALIZED_LABEL)], ['8'])
        self.assertEqual(self.client.writes, [])

    def test_nonrecursive_scope_does_not_include_child_candidates(self):
        """A nonrecursive binding must remain narrower than the whole project tree."""
        binding = Binding('vikunja', self.client.url, '1', False)
        with patch.object(cli, 'context', return_value=(self.adapter, binding, None)):
            rows = self.run_cli('search', '--exclude-label', NORMALIZED_LABEL)
        self.assertEqual([r['id'] for r in rows], ['5'])

    def test_new_candidate_can_depend_on_marked_task_without_renormalizing_it(self):
        """Candidate filtering must not become a prohibition on legitimate old-task links."""
        before = copy.deepcopy(self.client.tasks[6])
        changed = self.run_cli('relation', '5', 'blocked-by', '6')
        self.assertEqual(changed.blockers, ['6'])
        after = self.client.tasks[6]
        self.assertEqual(after['description'], before['description'])
        self.assertEqual(after['labels'], before['labels'])

    def test_mark_repeat_and_undo_preserve_backlog_and_other_labels(self):
        """The marker is processing state, not readiness or permission to replace labels."""
        before = copy.deepcopy(self.client.tasks[5])
        marked, = self.run_cli('normalized', '5')
        self.assertEqual((marked['state'], marked['description']), ('Backlog', before['description']))
        self.assertEqual(set(marked['labels']), {'keep', NORMALIZED_LABEL})
        writes = copy.deepcopy(self.client.writes)
        self.run_cli('normalized', '5')
        self.assertEqual(self.client.writes, writes)
        self.assertEqual([r['id'] for r in self.run_cli('search', '--exclude-label', NORMALIZED_LABEL)], ['8'])
        undone, = self.run_cli('normalized', '5', '--undo')
        self.assertEqual(undone['labels'], ['keep'])
        self.assertEqual(self.client.tasks[5], before)
        self.assertEqual([r['id'] for r in self.run_cli('search', '--exclude-label', NORMALIZED_LABEL)], ['5', '8'])
        self.run_cli('normalized', '5', '--undo')
        self.assertFalse(any(path.startswith('/labels/') for path, _ in self.client.writes))

    def test_failed_marker_attachment_leaves_candidate_and_retry_reuses_label(self):
        """A global label can exist after an interrupted write without marking the task."""
        post = self.client.post
        def fail(path, body):
            if path == '/tasks/5/labels':
                raise TrackerError('Interrupted', 'transport')
            return post(path, body)
        with patch.object(self.client, 'post', side_effect=fail):
            with self.assertRaises(TrackerError):
                self.run_cli('normalized', '5')
        self.assertIn('5', [r['id'] for r in self.run_cli('search', '--exclude-label', NORMALIZED_LABEL)])
        self.run_cli('normalized', '5')
        self.assertEqual(len([r for r in self.client.labels if r['title'] == NORMALIZED_LABEL]), 1)

    def test_ambiguous_global_marker_refuses_without_writes(self):
        """Choosing an arbitrary duplicate label would split future marker operations."""
        self.client.labels += [{'id': n, 'title': NORMALIZED_LABEL} for n in (10, 11)]
        with self.assertRaisesRegex(TrackerError, 'Ambiguous label'):
            self.run_cli('normalized', '5')
        self.assertEqual(self.client.writes, [])

    def test_staging_marker_refuses_source_and_external_tasks(self):
        """The new write command must obey the adoption boundary like text edits."""
        self.binding = Binding('vikunja', self.client.url, '2', True)
        with patch.object(cli, 'context', return_value=(self.adapter, self.binding, None)):
            for ident in ('5', '7'):
                with self.assertRaisesRegex(TrackerError, 'only change staging'):
                    self.run_cli('--adoption', '/unused/receipt', 'normalized', ident)
            self.run_cli('--adoption', '/unused/receipt', 'normalized', '8')
        self.assertNotIn(NORMALIZED_LABEL, self.adapter.get('5').labels)
        self.assertIn(NORMALIZED_LABEL, self.adapter.get('8').labels)

    def test_workflow_installs_new_skill_into_isolated_managed_tree(self):
        """Shipping the file alone must not leave enabled workflow users without the skill."""
        config = Config(); config.workflow.enabled = True
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'skills'
            shipped = Path(__file__).resolve().parents[1] / 'agent_box/skills'
            with patch.object(runtime, 'AGENT_SKILLS_DIR', target), \
                    patch.object(runtime, 'MANAGED_SKILLS_SOURCE', shipped):
                runtime.sync_managed_skills(config)
                self.assertEqual((target/'ab-normalize/SKILL.md').read_text(),
                                 (shipped/'ab-normalize/SKILL.md').read_text())
                config.workflow.enabled = False
                runtime.sync_managed_skills(config)
                self.assertFalse((target/'ab-normalize').exists())

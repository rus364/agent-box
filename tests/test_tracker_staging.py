"""A failed adoption leaves source data usable and never needs destructive undo."""
import copy
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_box.repo_binding import Binding, BINDING_FILE
from agent_box.tracker import adoption, cli
from agent_box.tracker.core import TrackerError
from agent_box.tracker.vikunja import VikunjaAdapter
from test_tracker_lifecycle import FakeClient, board, task


class NativeClient(FakeClient):
    def __init__(self):
        projects = [{"id": 1, "title": "Root", "parent_project_id": 0, "max_permission": 2},
                    {"id": 2, "title": "Child", "parent_project_id": 1, "max_permission": 2},
                    {"id": 9, "title": "Outside", "parent_project_id": 0, "max_permission": 2}]
        tasks = {5: task(5, 1, 10, 100, index=1, title="Same", assignees=[{"id": 7}],
                         related_tasks={"blocking": [{"id": 6}]}),
                 6: task(6, 2, 20, 200, index=1, title="Same", related_tasks={"blocked": [{"id": 5}]}),
                 7: task(7, 9, 90, 900, index=1)}
        super().__init__(projects, {1: board(10, 100, done_bucket=104),
                                   2: board(20, 200, done_bucket=204), 9: board(90, 900)}, tasks)
        self.lose_response = False
        self.duplicates = 0

    def paginate(self, path, params=None):
        if path == "/tasks":
            ids = {int(n) for n in re.findall(r'\d+', (params or {}).get('filter', ''))}
            return copy.deepcopy([t for t in self.tasks.values() if not ids or t['project_id'] in ids])
        if path.endswith(("/comments", "/attachments")):
            return []
        return super().paginate(path, params)

    def get(self, path):
        match = re.fullmatch(r'/projects/(\d+)', path)
        if match:
            return copy.deepcopy(next(p for p in self.projects if p['id'] == int(match[1])))
        return copy.deepcopy(super().get(path))

    def post(self, path, body):
        parts = path.strip('/').split('/')
        if parts[-1] == 'duplicate':
            self.writes.append((path, body))
            source = int(parts[1]); target = max(p['id'] for p in self.projects) + 1
            original = self.get(f'/projects/{source}')
            self.projects.append({**original, 'id': target, 'parent_project_id': body['parent_project_id'],
                                  'title': original['title'] + ' - duplicate'})
            self.boards[target] = board(target * 10, target * 100, done_bucket=target * 100 + 4)
            for row in list(self.tasks.values()):
                if row['project_id'] == source:
                    ident = max(self.tasks) + 1
                    self.tasks[ident] = {**copy.deepcopy(row), 'id': ident, 'project_id': target,
                                        'assignees': [], 'related_tasks': {},
                                        'buckets': [{'id': target*100, 'project_view_id': target*10}]}
            self.duplicates += 1
            if self.lose_response:
                self.lose_response = False
                raise TrackerError('lost response', 'transport')
            return {'duplicated_project': self.get(f'/projects/{target}')}
        if parts[-1] == 'assignees':
            self.writes.append((path, body))
            self.tasks[int(parts[1])]['assignees'].append({'id': body['user_id']})
            return body
        if parts[-1] == 'relations':
            self.writes.append((path, body))
            self.tasks[int(parts[1])]['related_tasks'].setdefault(body['relation_kind'], []).append({'id': body['other_task_id']})
            return body
        return super().post(path, body)

    def patch(self, path, body, markdown=False):
        match = re.fullmatch(r'/projects/(\d+)', path)
        if match:
            self.writes.append((path, body))
            project = next(p for p in self.projects if p['id'] == int(match[1]))
            project.update(body)
            return dict(project)
        if re.fullmatch(r'/tasks/\d+', path):
            self.writes.append((path, body)); self.tasks[int(path.split('/')[2])].update(body)
            return copy.deepcopy(self.tasks[int(path.split('/')[2])])
        return super().patch(path, body, markdown)


class StagingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.receipt = self.root / 'operation.json'
        self.client = NativeClient()
        self.source = copy.deepcopy(self.client.tasks)
        def make(binding=None, provider=None):
            return VikunjaAdapter('https://tracker.example', 'token', binding, self.client)
        self.make = make
        self.addCleanup(patch.stopall)
        patch.object(adoption, 'make_adapter', side_effect=make).start()
        from agent_box.config import Config, TrackerConnection
        config = Config(); config.tracker.provider = 'vikunja'
        config.tracker.connections['vikunja'] = TrackerConnection('https://tracker.example', 'token')
        patch('agent_box.config.effective_config', return_value=config).start()
        patch.object(cli, 'effective_config', return_value=config).start()

    def clone(self):
        return adoption.start(self.root, '1', self.receipt, apply=True)

    def test_native_tree_clone_preserves_source_and_repairs_only_staging_edges_and_assignments(self):
        report = self.clone()
        self.assertEqual(self.client.duplicates, 2)
        self.assertFalse((self.root / BINDING_FILE).exists())
        for ident, original in self.source.items():
            self.assertEqual(self.client.tasks[ident], original)
        copies = report['projects']
        root_copy = int(copies['1']); child_copy = int(copies['2'])
        self.assertEqual(self.client.get(f'/projects/{child_copy}')['parent_project_id'], root_copy)
        first = next(t for t in self.client.tasks.values() if t['project_id'] == root_copy)
        second = next(t for t in self.client.tasks.values() if t['project_id'] == child_copy)
        self.assertEqual(first['assignees'], [{'id': 7}])
        self.assertEqual(first['related_tasks']['blocking'], [{'id': second['id']}])
        self.assertTrue(all(body['duplicate_shares'] for path, body in self.client.writes if path.endswith('/duplicate')))
        self.assertEqual(adoption.start(self.root, '1', self.receipt, apply=True)['project'], str(root_copy))
        self.assertEqual(self.client.duplicates, 2)

    def test_a_lost_duplicate_response_is_never_retried_automatically(self):
        self.client.lose_response = True
        with self.assertRaisesRegex(TrackerError, 'lost response'):
            self.clone()
        with self.assertRaisesRegex(TrackerError, 'uncertain result'):
            self.clone()
        self.assertEqual(self.client.duplicates, 1)
        self.assertEqual(json.loads(self.receipt.read_text())['clone']['pending'], '1')
        self.assertEqual(self.client.get('/projects/1')['title'], 'Root')

    def test_saved_project_ids_do_not_hide_an_interrupted_relation_copy(self):
        """All projects may exist while a post-duplicate repair is still unfinished."""
        post = self.client.post
        def fail_relation(path, body):
            if path.endswith('/relations'):
                raise TrackerError('relation write interrupted', 'transport')
            return post(path, body)
        with patch.object(self.client, 'post', side_effect=fail_relation):
            with self.assertRaisesRegex(TrackerError, 'relation write interrupted'):
                self.clone()
        report = adoption.cutover(self.root, self.receipt)
        self.assertFalse(report['ok'])
        self.assertTrue(any('missing source relation' in error for error in report['errors']))
        self.clone()
        self.assertEqual(self.client.duplicates, 2)
        self.assertTrue(adoption.cutover(self.root, self.receipt)['ok'])

    def test_cutover_resumes_after_each_remote_write_and_binding_failure(self):
        for failure in (1, 2, 3):
            with self.subTest(failure=failure):
                # Each subcase starts with a fresh provider and no repository binding.
                self.client = NativeClient()
                self.receipt.unlink(missing_ok=True)
                (self.root / BINDING_FILE).unlink(missing_ok=True)
                report = self.clone(); target = report['project']
                original_rename = VikunjaAdapter.rename_project
                count = 0
                def rename(instance, ident, title):
                    nonlocal count
                    result = original_rename(instance, ident, title); count += 1
                    if count == failure: raise OSError('interrupted after rename')
                    return result
                with patch.object(VikunjaAdapter, 'rename_project', rename), patch.object(
                        adoption, 'write_binding', side_effect=OSError('binding write interrupted')):
                    with self.assertRaises(OSError):
                        adoption.cutover(self.root, self.receipt, True)
                self.assertFalse((self.root / BINDING_FILE).exists())
                self.assertTrue(adoption.cutover(self.root, self.receipt, True)['complete'])
                self.assertEqual(Binding.load(self.root).project, target)
                self.assertEqual(self.client.get('/projects/1')['title'], 'Root — backup 1')
                self.assertEqual(self.client.get(f'/projects/{target}')['title'], 'Root')
                before = len(self.client.writes)
                self.assertTrue(adoption.cutover(self.root, self.receipt, True)['complete'])
                self.assertEqual(len(self.client.writes), before)

    def test_external_edges_block_cutover_without_touching_the_outside_task(self):
        self.client.tasks[5]['related_tasks']['related'] = [{'id': 7}]
        self.clone(); before = copy.deepcopy(self.client.tasks[7])
        result = adoption.cutover(self.root, self.receipt)
        self.assertFalse(result['ok'])
        self.assertTrue(result['clone']['external_relations'])
        with self.assertRaisesRegex(TrackerError, 'External source relations'):
            adoption.cutover(self.root, self.receipt, True)
        self.assertEqual(self.client.tasks[7], before)
        self.assertEqual(self.client.get('/projects/1')['title'], 'Root')

    def test_staging_cli_refuses_source_mutations_and_external_relations(self):
        result = self.clone()
        patch.object(cli, 'repository_root', return_value=self.root).start()
        for argv in (['update', '5', '--title', 'wrong'], ['relation', '8', 'related', '7']):
            with self.subTest(argv=argv), self.assertRaisesRegex(TrackerError, 'staging'):
                cli.dispatch(cli.parser().parse_args(['--adoption', str(self.receipt), *argv]))
        self.assertEqual(self.client.tasks[5], self.source[5])
        instance, binding, _ = adoption.staging_context(self.receipt, self.root)
        self.assertEqual(binding.project, result['project'])
        self.assertEqual(instance.get('8').state, 'Backlog')
        self.assertFalse((self.root / BINDING_FILE).exists())

    def test_a_changed_binding_or_edited_instruction_block_stops_before_renaming_source(self):
        self.clone()
        (self.root / 'AGENTS.md').write_text('<!-- BEGIN AGENT-BOX workflow broken -->\n')
        with self.assertRaises(Exception):
            adoption.cutover(self.root, self.receipt, True)
        self.assertEqual(self.client.get('/projects/1')['title'], 'Root')
        (self.root / 'AGENTS.md').unlink()
        from agent_box.repo_binding import write_binding
        write_binding(self.root, Binding('vikunja', 'https://tracker.example', '9'))
        with self.assertRaisesRegex(TrackerError, 'binding differs'):
            adoption.cutover(self.root, self.receipt, True)
        self.assertEqual(self.client.get('/projects/1')['title'], 'Root')

    def test_explicit_project_audit_reads_tasks_without_persisting_a_binding(self):
        patch.object(cli, 'repository_root', return_value=self.root).start()
        patch.object(cli, 'make_adapter', side_effect=self.make).start()
        result = cli.dispatch(cli.parser().parse_args(['audit', '--project', '1', '--recursive']))
        self.assertEqual({t['id'] for t in result['tasks']}, {'5', '6'})
        self.assertTrue(result['clean'])
        self.assertFalse((self.root / BINDING_FILE).exists())

"""A failed adoption leaves source data usable and never needs destructive undo."""
import copy
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_box.errors import AgentBoxError
from agent_box.repo_binding import Binding, BINDING_FILE
from agent_box.tracker import adoption, cli
from agent_box.tracker import vikunja_api as api
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
        self.journal = {}

    def paginate(self, path, params=None):
        if path == "/tasks":
            ids = {int(n) for n in re.findall(r'\d+', (params or {}).get('filter', ''))}
            return copy.deepcopy([t for t in self.tasks.values() if not ids or t['project_id'] in ids])
        if path.endswith('/comments'):
            return copy.deepcopy(self.journal.get(int(path.split('/')[2]), []))
        if path.endswith('/attachments'):
            return []
        return super().paginate(path, params)

    def get(self, path):
        match = re.fullmatch(r'/projects/(\d+)', path)
        if match:
            return copy.deepcopy(next(p for p in self.projects if p['id'] == int(match[1])))
        return copy.deepcopy(super().get(path))

    def post(self, path, body):
        parts = path.split('?')[0].strip('/').split('/')
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
            base, other, kind = int(parts[1]), body['other_task_id'], body['relation_kind']
            existing = self.tasks[base]['related_tasks'].setdefault(kind, [])
            if {'id': other} in existing:
                raise api.APIError(409, 'Relation already exists')
            existing.append({'id': other})
            self.tasks[other]['related_tasks'].setdefault(api.RELATION_INVERSE[kind], []).append({'id': base})
            return body
        if parts[-1] == 'comments':
            self.writes.append((path, body))
            self.journal.setdefault(int(parts[1]), []).append(dict(body))
            return body
        return super().post(path, body)

    def delete(self, path):
        parts = path.strip('/').split('/')
        if len(parts) != 5 or parts[0] != 'tasks' or parts[2] != 'relations':
            raise AssertionError(path)
        base, kind, other = int(parts[1]), parts[3], int(parts[4])
        self.writes.append((path, None))
        for left, edge, right in ((base, kind, other), (other, api.RELATION_INVERSE[kind], base)):
            relations = self.tasks[left]['related_tasks']
            relations[edge] = [t for t in relations.get(edge, []) if t['id'] != right]

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

    def test_launch_only_project_config_survives_public_adoption_and_cutover(self):
        """Adoption can bind a launch-configured repository without discarding its policy."""
        path = self.root / BINDING_FILE
        overrides = {'browser': {'enabled': True}, 'workflow': {'auto_push': False}}
        path.write_text(json.dumps(overrides))
        report = self.clone()
        self.assertEqual(json.loads(path.read_text()), overrides)
        self.assertIsNone(json.loads(self.receipt.read_text())['previous_binding'])
        self.assertEqual(adoption.staging_context(self.receipt, self.root)[1].project, report['project'])
        self.assertTrue(adoption.cutover(self.root, self.receipt, True)['complete'])
        result = json.loads(path.read_text())
        self.assertEqual(result['browser'], overrides['browser'])
        self.assertEqual(result['workflow'], overrides['workflow'])
        self.assertEqual(Binding.load(self.root).project, report['project'])

    def test_adoption_rejects_invalid_project_config_before_clone_writes(self):
        """Launch-only support must not hide malformed files or an attempted incomplete binding."""
        path = self.root / BINDING_FILE
        for content in ('{', '{"browser":{"enabled":1}}', '{"tracker":{"project":"1"}}'):
            with self.subTest(content=content):
                path.write_text(content)
                with self.assertRaises(AgentBoxError):
                    self.clone()
                self.assertFalse(self.receipt.exists())
                self.assertEqual(self.client.writes, [])
        path.unlink()
        path.symlink_to(self.root / 'missing.json')
        with self.assertRaisesRegex(AgentBoxError, 'symlink'):
            self.clone()
        self.assertFalse(self.receipt.exists())
        self.assertEqual(self.client.writes, [])

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

    def staging_relation(self, base, kind, other, remove=False):
        context = adoption.staging_context(self.receipt, self.root)
        args = ['--adoption', str(self.receipt), 'relation', str(base), kind, str(other)]
        if remove:
            args.append('--remove')
        with patch.object(cli, 'context', return_value=context):
            return cli.dispatch(cli.parser().parse_args(args))

    def cloned_endpoints(self):
        report = self.clone()
        return [next(t['id'] for t in self.client.tasks.values() if t['project_id'] == int(report['projects'][p]))
                for p in ('1', '2')]

    def test_intentional_inverse_relation_removal_can_cut_over_without_touching_source(self):
        """An evidenced correction must not be mistaken for an incomplete native copy."""
        first, second = self.cloned_endpoints()
        self.staging_relation(second, 'blocked-by', first, True)
        self.assertEqual(json.loads(self.receipt.read_text())['clone']['relation_removals'],
                         [[str(first), 'blocks', str(second)]])
        preview = adoption.cutover(self.root, self.receipt)
        self.assertTrue(preview['ok'])
        self.assertEqual(preview['clone']['relation_removals'],
                         [[str(first), 'blocks', str(second)]])
        self.assertTrue(adoption.cutover(self.root, self.receipt, True)['complete'])
        for ident, original in self.source.items():
            self.assertEqual(self.client.tasks[ident], original)

    def test_lost_relation_delete_response_can_resume_from_recorded_intent(self):
        """The server may commit before transport fails; retry must not lose intent."""
        first, second = self.cloned_endpoints()
        delete = self.client.delete
        def lose_response(path):
            delete(path)
            raise TrackerError('Lost delete response', 'transport')
        with patch.object(self.client, 'delete', side_effect=lose_response):
            with self.assertRaises(TrackerError):
                self.staging_relation(first, 'blocks', second, True)
        writes = list(self.client.writes)
        self.staging_relation(second, 'blocked-by', first, True)
        self.assertEqual(self.client.writes, writes)
        self.assertTrue(adoption.cutover(self.root, self.receipt)['ok'])
        with self.assertRaisesRegex(TrackerError, 'do not rerun clone'):
            self.clone()

    def test_readding_relation_clears_exception_and_unexplained_loss_blocks_again(self):
        """A past correction cannot waive preservation of a subsequently restored edge."""
        first, second = self.cloned_endpoints()
        self.staging_relation(first, 'blocks', second, True)
        self.staging_relation(second, 'blocked-by', first)
        self.assertEqual(json.loads(self.receipt.read_text())['clone']['relation_removals'], [])
        self.client.delete(f'/tasks/{first}/relations/blocking/{second}')
        report = adoption.cutover(self.root, self.receipt)
        self.assertFalse(report['ok'])
        self.assertTrue(any('missing source relation' in e for e in report['errors']))

    def test_removal_exception_does_not_cover_another_relation_kind(self):
        """An intended blocker correction cannot hide loss of a separate related edge."""
        self.client.tasks[5]['related_tasks']['related'] = [{'id': 6}]
        self.client.tasks[6]['related_tasks']['related'] = [{'id': 5}]
        first, second = self.cloned_endpoints()
        self.staging_relation(first, 'blocks', second, True)
        self.client.delete(f'/tasks/{first}/relations/related/{second}')
        report = adoption.cutover(self.root, self.receipt)
        self.assertFalse(report['ok'])
        self.assertTrue(any('missing source relation 5 related 6' in e for e in report['errors']))

    def test_relation_write_must_be_visible_before_success_is_reported(self):
        """An API success response alone cannot prove the requested correction happened."""
        first, second = self.cloned_endpoints()
        with patch.object(self.client, 'delete', return_value={}):
            with self.assertRaisesRegex(TrackerError, 'not confirmed'):
                self.staging_relation(first, 'blocks', second, True)
        self.assertIn({'id': second}, self.client.tasks[first]['related_tasks']['blocking'])

    def test_failed_receipt_save_prevents_relation_mutation(self):
        """An unrecorded deletion would be indistinguishable from copy data loss."""
        first, second = self.cloned_endpoints()
        before = list(self.client.writes)
        with patch.object(adoption, 'save', side_effect=OSError('Read-only receipt')):
            with self.assertRaises(OSError):
                self.staging_relation(first, 'blocks', second, True)
        self.assertEqual(self.client.writes, before)
        self.assertTrue(adoption.cutover(self.root, self.receipt)['ok'])

    def test_external_removal_cannot_record_an_internal_preservation_exception(self):
        """Adoption's explicit edge repair does not widen the staging mutation scope."""
        first, _ = self.cloned_endpoints()
        before = self.receipt.read_bytes()
        for base, other in ((5, first), (first, 7)):
            with self.assertRaisesRegex(TrackerError, 'staging'):
                self.staging_relation(base, 'related', other, True)
        self.assertEqual(self.receipt.read_bytes(), before)

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

    def test_external_related_is_copied_with_its_inverse_and_does_not_block_cutover(self):
        self.client.tasks[5]['related_tasks']['related'] = [{'id': 7}]
        original = copy.deepcopy(self.client.tasks[5])
        self.clone()
        result = adoption.cutover(self.root, self.receipt)
        self.assertTrue(result['ok'])
        self.assertEqual(result['clone']['external_relations'][0]['status'], 'preserved')
        self.assertIn({'id': 8}, self.client.tasks[7]['related_tasks']['related'])
        self.assertEqual(self.client.tasks[5], original)
        self.assertTrue(adoption.cutover(self.root, self.receipt, True)['complete'])
        before = len(self.client.writes)
        repeated = adoption.cutover(self.root, self.receipt, True)
        self.assertEqual(repeated['clone']['external_relations'][0]['status'], 'preserved')
        self.assertEqual(len(self.client.writes), before)

    def test_all_external_kinds_have_an_explicit_policy_without_changing_source(self):
        """Metadata and incoming blockers copy; external ownership and provenance need people."""
        kinds = api.RELATION_KINDS | {'future-kind'}
        for kind in kinds:
            self.client.tasks[5]['related_tasks'][kind] = [{'id': 7}]
        original = copy.deepcopy(self.client.tasks[5])
        preview = adoption.start(self.root, '1', self.receipt)
        self.assertEqual(self.client.writes, [])
        self.assertFalse(self.receipt.exists())
        self.assertEqual(len(preview['external_relations']), len(kinds))
        report = self.clone()
        rows = {row['kind']: row for row in report['external_relations']}
        safe = {'related', 'duplicateof', 'duplicates', 'precedes', 'follows', 'blocked'}
        for kind in kinds:
            self.assertEqual(rows[kind]['status'], 'preserved' if kind in safe else 'skipped')
        self.assertIn('warning', rows['blocked'])
        self.assertEqual(self.client.tasks[5], original)
        self.assertEqual(len(self.client.journal[8]), len(kinds - safe))
        self.assertTrue(adoption.cutover(self.root, self.receipt, True)['complete'])
        self.assertEqual(len(self.client.journal[8]), len(kinds - safe))

    def test_external_lower_id_is_written_from_staging_with_the_correct_inverse(self):
        """Canonical edge order must never select the external task as the write endpoint."""
        self.client.tasks[3] = self.client.tasks.pop(7)
        self.client.tasks[3]['id'] = 3
        self.client.tasks[5]['related_tasks']['blocked'] = [{'id': 3}]
        report = self.clone()
        row, = report['external_relations']
        self.assertEqual((row['task'], row['other'], row['kind'], row['status']),
                         ('5', '3', 'blocked', 'preserved'))
        self.assertFalse(any(path.startswith('/tasks/3/') for path, _ in self.client.writes))
        self.assertIn({'id': int(row['staging'])}, self.client.tasks[3]['related_tasks']['blocking'])

    def test_existing_unsafe_relations_keep_their_policy_and_type_specific_explanation(self):
        """Presence proves an edge exists, never that its external impact was approved."""
        kinds = {'parenttask', 'subtask', 'blocking', 'copiedfrom', 'copiedto', 'future-kind'}
        for kind in kinds:
            self.client.tasks[5]['related_tasks'][kind] = [{'id': 7}]
        rows = self.clone()['external_relations']
        for kind in kinds:
            self.client.tasks[8]['related_tasks'][kind] = [{'id': 7}]
        before = copy.deepcopy(self.client.tasks)
        adapter = self.make()
        for apply in (False, True):
            for row in adapter.preserve_external_relations(rows, apply):
                with self.subTest(kind=row['kind'], apply=apply):
                    self.assertEqual(row['status'], 'manual')
                    self.assertTrue(row['warning'])
        self.assertEqual(self.client.tasks, before)
        comments = [item['comment'] for item in self.client.journal[8]]
        self.assertTrue(any('non-hierarchical relation' in text for text in comments))
        self.assertTrue(any('impact on the external workflow' in text for text in comments))
        self.assertTrue(any('historical provenance' in text for text in comments))
        self.assertFalse(any('add the relation manually' in text for text in comments))
        self.assertTrue(any('already present on staging' in text for text in comments))

    def test_manual_outgoing_blocker_remains_visible_after_cutover(self):
        self.client.tasks[5]['related_tasks']['blocking'].append({'id': 7})
        self.clone()
        self.client.post('/tasks/8/relations', {'other_task_id': 7, 'relation_kind': 'blocking'})
        before = copy.deepcopy(self.client.tasks)
        result = adoption.cutover(self.root, self.receipt, True)
        row, = result['clone']['external_relations']
        self.assertTrue(result['complete'])
        self.assertEqual(row['status'], 'manual')
        self.assertIn("external task's workflow", row['warning'])
        self.assertEqual(self.client.tasks, before)
        self.assertEqual(adoption.cutover(self.root, self.receipt)['clone']['external_relations'], [row])

    def test_manual_external_subtask_blocks_cutover_even_when_only_parent_is_in_scope(self):
        self.client.tasks[5]['related_tasks']['subtask'] = [{'id': 7}]
        self.clone()
        self.client.post('/tasks/8/relations', {'other_task_id': 7, 'relation_kind': 'subtask'})
        before = copy.deepcopy(self.client.tasks)
        result = adoption.cutover(self.root, self.receipt)
        self.assertFalse(result['ok'])
        self.assertEqual(result['clone']['external_relations'][0]['status'], 'manual')
        self.assertIn('split-child', {error['code'] for error in result['audit']['errors']})
        with self.assertRaisesRegex(TrackerError, 'structural errors'):
            adoption.cutover(self.root, self.receipt, True)
        self.assertEqual(self.client.tasks, before)
        self.assertEqual(self.client.get('/projects/1')['title'], 'Root')
        self.assertFalse((self.root / BINDING_FILE).exists())

    def test_denied_external_access_is_a_journal_warning_and_cutover_can_retry(self):
        self.client.tasks[5]['related_tasks']['related'] = [{'id': 7}]
        get = self.client.get
        def deny(path):
            if path == '/tasks/7':
                raise api.APIError(403, 'No access to external project')
            return get(path)
        with patch.object(self.client, 'get', side_effect=deny):
            row, = self.clone()['external_relations']
            self.assertIn('403', row['warning'])
            self.assertIn('Check access', self.client.journal[8][0]['comment'])
            self.assertTrue(adoption.cutover(self.root, self.receipt)['ok'])
        result = adoption.cutover(self.root, self.receipt, True)
        self.assertEqual(result['clone']['external_relations'][0]['status'], 'preserved')
        self.assertEqual(len(self.client.journal[8]), 1)

    def test_denied_relation_and_comment_are_reported_without_requiring_external_write_access(self):
        self.client.tasks[5]['related_tasks']['related'] = [{'id': 7}]
        post = self.client.post
        def deny(path, body):
            if path.startswith('/tasks/8/comments') or path.endswith('/relations') and body['other_task_id'] == 7:
                raise api.APIError(403, 'Operation denied')
            return post(path, body)
        with patch.object(self.client, 'post', side_effect=deny):
            row, = self.clone()['external_relations']
            self.assertIn('403', row['warning'])
            self.assertIn('403', row['comment_warning'])
            result = adoption.cutover(self.root, self.receipt, True)
            self.assertTrue(result['complete'])
            self.assertIn('comment_warning', result['clone']['external_relations'][0])
        before = len(self.client.writes)
        repeated = adoption.cutover(self.root, self.receipt)
        row, = repeated['clone']['external_relations']
        self.assertTrue(row['warning'])
        # A current graph report cannot reconstruct the previous POST failure.
        self.assertNotIn('comment_warning', row)
        self.assertEqual(len(self.client.writes), before)

    def test_a_lost_external_relation_response_is_confirmed_without_another_post(self):
        self.client.tasks[5]['related_tasks']['related'] = [{'id': 7}]
        post = self.client.post
        def lose(path, body):
            result = post(path, body)
            if path.endswith('/relations') and body['other_task_id'] == 7:
                raise api.APIError(0, 'Response lost')
            return result
        with patch.object(self.client, 'post', side_effect=lose):
            self.assertEqual(self.clone()['external_relations'][0]['status'], 'preserved')
        self.assertEqual(self.client.journal, {})
        adoption.cutover(self.root, self.receipt, True)
        writes = [path for path, body in self.client.writes
                  if path.endswith('/relations') and body['other_task_id'] == 7]
        self.assertEqual(len(writes), 1)

    def test_an_external_dependency_cycle_is_skipped_and_comment_text_cannot_resolve_it(self):
        self.clone()
        self.client.tasks[5]['related_tasks']['blocked'] = [{'id': 7}]
        self.client.tasks[7]['related_tasks']['blocked'] = [{'id': 8}]
        result = adoption.cutover(self.root, self.receipt, True)
        row, = result['clone']['external_relations']
        self.assertIn('cycle', row['warning'])
        self.assertTrue(result['complete'])
        self.client.journal[8][0]['comment'] = 'Resolved, everything is fine.'
        row, = adoption.cutover(self.root, self.receipt)['clone']['external_relations']
        self.assertIn('cycle', row['warning'])

    def test_global_api_failure_still_prevents_cutover(self):
        self.clone()
        with patch.object(self.client, 'get', side_effect=api.APIError(503, 'Unavailable')):
            with self.assertRaises(api.APIError):
                adoption.cutover(self.root, self.receipt, True)
        self.assertFalse((self.root / BINDING_FILE).exists())

    def test_an_outage_after_relation_write_is_not_reported_as_successful_cutover(self):
        """Uncertain writes require a readable staging task before any root rename."""
        self.clone()
        self.client.tasks[5]['related_tasks']['related'] = [{'id': 7}]
        get, post, unavailable = self.client.get, self.client.post, False
        def fail_get(path):
            if unavailable:
                raise api.APIError(503, 'Provider unavailable')
            return get(path)
        def fail_post(path, body):
            nonlocal unavailable
            result = post(path, body)
            if path.endswith('/relations'):
                unavailable = True
                raise api.APIError(0, 'Response lost')
            return result
        with patch.object(self.client, 'get', side_effect=fail_get), patch.object(self.client, 'post', side_effect=fail_post):
            with self.assertRaises(api.APIError):
                adoption.cutover(self.root, self.receipt, True)
        self.assertEqual(self.client.get('/projects/1')['title'], 'Root')
        self.assertFalse((self.root / BINDING_FILE).exists())
        self.assertTrue(adoption.cutover(self.root, self.receipt, True)['complete'])

    def test_a_lost_journal_response_does_not_duplicate_the_explanation_on_resume(self):
        self.client.tasks[5]['related_tasks']['parenttask'] = [{'id': 7}]
        post = self.client.post
        def lose(path, body):
            result = post(path, body)
            if '/comments' in path:
                raise api.APIError(0, 'Response lost')
            return result
        with patch.object(self.client, 'post', side_effect=lose):
            self.assertIn('comment_warning', self.clone()['external_relations'][0])
        # Markdown rendering may append a newline; this is still the same journal entry.
        self.client.journal[8][0]['comment'] += '\n'
        result = adoption.cutover(self.root, self.receipt, True)
        self.assertTrue(result['complete'])
        self.assertEqual(len(self.client.journal[8]), 1)
        self.assertEqual(result['clone']['external_relations'][0]['status'], 'skipped')

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

"""A failed adoption leaves source data usable and never needs destructive undo."""
import copy
import json
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
from tracker_fixtures import NativeClient


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
        # The suite may itself run inside a box; receipts in the temporary repository are host behaviour.
        patch.object(adoption, 'in_container', return_value=False).start()
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

    def workflow_state(self):
        state = self.root / 'state' / 'workflow'
        patch.object(adoption, 'WORKFLOW_STATE', state).start()
        patch.object(cli, 'repository_root', return_value=self.root).start()
        return state

    def test_receipt_defaults_to_a_fixed_path_in_workflow_state(self):
        """Without --receipt the mapping lands in mounted workflow state, where a recreated box still finds it.

        A path chosen per run, or one on the box's own filesystem, would lose the
        only record of which staging project copies which source project.
        """
        state = self.workflow_state()
        from agent_box.workflow import repository_key
        expected = state / 'adoptions' / repository_key(self.root) / '1.json'
        report = cli.dispatch(cli.parser().parse_args(['adopt', 'clone', '--project', '1', '--apply']))
        self.assertEqual(report['receipt'], str(expected))
        self.assertTrue(expected.is_file())
        self.assertEqual(adoption.start(self.root, '1')['receipt'], str(expected))
        self.assertTrue(cli.dispatch(cli.parser().parse_args(['adopt', 'cutover']))['ok'])

    def test_several_receipts_need_an_explicit_choice(self):
        """Guessing between two operations could cut over the wrong staging tree."""
        self.workflow_state()
        adoption.receipts(self.root).mkdir(parents=True)
        for name in ('1.json', '2.json'):
            (adoption.receipts(self.root) / name).write_text('{}')
        with self.assertRaisesRegex(TrackerError, 'Found 2 adoption receipts.*--receipt'):
            cli.dispatch(cli.parser().parse_args(['adopt', 'cutover']))

    def test_box_refuses_an_explicit_receipt_outside_workflow_state(self):
        """Inside a box only mounted workflow state survives recreation; elsewhere the receipt vanishes silently."""
        state = self.workflow_state()
        patch.object(adoption, 'in_container', return_value=True).start()
        for args in (['adopt', 'clone', '--project', '1', '--receipt', str(self.receipt)],
                     ['--adoption', str(self.receipt), 'audit']):
            with self.assertRaisesRegex(TrackerError, 'under .*workflow'):
                cli.dispatch(cli.parser().parse_args(args))
        self.assertFalse(self.receipt.exists())
        kept = state / 'mine.json'
        args = ['adopt', 'clone', '--project', '1', '--receipt', str(kept), '--apply']
        self.assertEqual(cli.dispatch(cli.parser().parse_args(args))['receipt'], str(kept))

    def staging_move(self, *argv):
        state = self.root / 'state' / 'workflow'
        from agent_box import workflow_state
        for name in (cli, workflow_state):
            patch.object(name, 'WORKFLOW_STATE', state).start()
        patch.object(cli, 'repository_root', return_value=self.root).start()
        return cli.dispatch(cli.parser().parse_args(['--adoption', str(self.receipt), 'move', *argv]))

    def test_a_move_inside_staging_still_cuts_over_with_every_copy_accounted_for(self):
        """A moved task is renumbered, so pairing numbers per project would call the copy lost.

        The mapping must be in the receipt before the first write: a move that
        renumbers a task first could never pair it up again afterwards.
        """
        report = self.clone()
        root_copy, child_copy = report['projects']['1'], report['projects']['2']
        moving = next(t['id'] for t in self.client.tasks.values() if t['project_id'] == int(child_copy))
        original = self.client.patch
        saved = []

        def patched(path, body, markdown=False):
            if 'project_id' in body:
                saved.append(json.loads(self.receipt.read_text())['clone'].get('tasks'))
            return original(path, body, markdown)

        with patch.object(self.client, 'patch', patched):
            moved = self.staging_move(str(moving), '--project', root_copy)
        self.assertEqual(saved, [{'5': str(moving - 1), '6': str(moving)}])
        self.assertEqual([(row['id'], row['project'], row['number']) for row in moved],
                         [(str(moving), root_copy, 2)])
        preview = adoption.cutover(self.root, self.receipt)
        self.assertEqual((preview['errors'], preview['clone']['incomplete']), ([], []))
        self.assertTrue(adoption.cutover(self.root, self.receipt, True)['complete'])
        for ident, source in self.source.items():
            self.assertEqual(self.client.tasks[ident], source)

    def test_a_staging_move_cannot_reach_the_source_tree(self):
        """Adoption leaves the source intact until the final rename."""
        self.clone()
        with self.assertRaisesRegex(TrackerError, 'only move tasks inside staging'):
            self.staging_move(str(max(self.client.tasks)), '--project', '1')
        self.assertNotIn('tasks', json.loads(self.receipt.read_text())['clone'])

    def test_clone_is_not_rerun_over_moved_tasks(self):
        """A rerun would duplicate again and pair numbers the move has already changed."""
        report = self.clone()
        moving = next(t['id'] for t in self.client.tasks.values() if t['project_id'] == int(report['projects']['2']))
        self.staging_move(str(moving), '--project', report['projects']['1'])
        with self.assertRaisesRegex(TrackerError, 'do not rerun clone'):
            self.clone()

    def test_an_unfinished_copy_is_not_frozen_by_a_move(self):
        """Freezing ends clone resumption, which is the only repair for a half-copied assignee."""
        report = self.clone()
        child_copy = int(report['projects']['2'])
        copy_of_five = next(t for t in self.client.tasks.values()
                            if t['project_id'] == int(report['projects']['1']))
        copy_of_five['assignees'] = []
        moving = next(t['id'] for t in self.client.tasks.values() if t['project_id'] == child_copy)
        with self.assertRaisesRegex(TrackerError, 'finish or inspect the clone first.*assignees'):
            self.staging_move(str(moving), '--project', report['projects']['1'])
        self.assertNotIn('tasks', json.loads(self.receipt.read_text())['clone'])
        self.assertEqual(self.client.tasks[moving]['project_id'], child_copy)

    def test_a_malformed_task_mapping_is_reported_as_an_invalid_receipt(self):
        """Two source tasks sharing one copy would leave one of them never verified."""
        self.clone()
        receipt = json.loads(self.receipt.read_text())
        for mapping in ([], {'5': '8', '6': '8'}, {'x': '8'}):
            with self.subTest(mapping=mapping):
                receipt['clone']['tasks'] = mapping
                self.receipt.write_text(json.dumps(receipt))
                with self.assertRaisesRegex(TrackerError, 'Invalid adoption receipt.*clone.tasks'):
                    adoption.cutover(self.root, self.receipt)

    def test_cutover_waits_for_an_interrupted_staging_move(self):
        """After the rename no staging move can run, and the journal holds the only record of the column."""
        report = self.clone()
        moving = next(t['id'] for t in self.client.tasks.values() if t['project_id'] == int(report['projects']['2']))
        # A card in Review, which the target's default column would silently turn into Backlog.
        self.client.tasks[moving].update(description='Acceptance.', buckets=[
            {'id': int(report['projects']['2']) * 100 + 3, 'project_view_id': int(report['projects']['2']) * 10}])
        original = VikunjaAdapter.set_state

        def interrupted(instance, found, state):
            if found.id == str(moving):
                raise TrackerError('lost connection', 'transport', retryable=True)
            return original(instance, found, state)

        with patch.object(VikunjaAdapter, 'set_state', interrupted), self.assertRaises(TrackerError):
            self.staging_move(str(moving), '--project', report['projects']['1'])
        preview = adoption.cutover(self.root, self.receipt)
        self.assertTrue(any(error.startswith('An interrupted move is unfinished') for error in preview['errors']))
        with self.assertRaisesRegex(TrackerError, 'interrupted move'):
            adoption.cutover(self.root, self.receipt, True)
        self.assertEqual(self.client.get('/projects/1')['title'], 'Root')
        moved = self.staging_move(str(moving), '--project', report['projects']['1'])
        self.assertEqual(moved[0]['state'], 'Review')
        self.assertTrue(adoption.cutover(self.root, self.receipt, True)['complete'])

    def test_the_frozen_mapping_still_names_every_lost_copy(self):
        """Without index pairing the mapping is cutover's only proof; a lost pair must stop it."""
        report = self.clone()
        root_copy, child_copy = report['projects']['1'], report['projects']['2']
        moving = next(t['id'] for t in self.client.tasks.values() if t['project_id'] == int(child_copy))
        self.staging_move(str(moving), '--project', root_copy)
        for ident in (moving - 1, 6):
            # The provider removes a deleted task's relations on both ends.
            del self.client.tasks[ident]
            for row in self.client.tasks.values():
                for kind, others in row['related_tasks'].items():
                    row['related_tasks'][kind] = [other for other in others if other['id'] != ident]
        errors = adoption.cutover(self.root, self.receipt)['errors']
        self.assertIn('Source task 6 from the recorded task mapping no longer exists in the source', errors)
        self.assertIn(f'Staging task {moving - 1} from the recorded task mapping is not in staging', errors)
        with self.assertRaisesRegex(TrackerError, 'recorded task mapping'):
            adoption.cutover(self.root, self.receipt, True)
        self.assertEqual(self.client.get('/projects/1')['title'], 'Root')

"""Deletion is conditional on live integration proof, never on tracker Done alone."""
import copy
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from agent_box import branch_cleanup as cleanup
from agent_box import git_workflow as gw
from agent_box import github_workflow as github
from agent_box import workflow, workflow_state
from agent_box.config import Config
from agent_box.errors import AgentBoxError
from agent_box.repo_binding import Binding
from agent_box.tracker.core import Task
from test_workflow_handoff import BINDING, origin_path, repository


class CleanupTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.root = repository(temporary.name)
        self.origin = origin_path(temporary.name)
        state = patch.object(workflow_state, 'WORKFLOW_STATE', self.directory / 'state')
        state.start()
        self.addCleanup(state.stop)
        evidence = patch.object(workflow, 'WORKFLOW_STATE', self.directory / 'state')
        evidence.start()
        self.addCleanup(evidence.stop)
        self.branch = 'ab/7-original-title'
        self.ref = 'refs/heads/' + self.branch
        gw.git(self.root, 'switch', '-c', self.branch)
        gw.git(self.root, 'commit', '--allow-empty', '-m', 'task work')
        self.head = gw.head_sha(self.root)
        gw.git(self.root, 'push', 'origin', self.ref)
        gw.git(self.root, 'switch', 'main')
        self.task = Task('7', '1', 'Changed title', state='Done', assignees=['1'],
                         description='- [x] accepted')
        self.tasks = {self.task.id: self.task}
        self.adapter = Mock()
        self.adapter.get.side_effect = self.tasks.__getitem__
        self.config = Config()
        self.config.workflow.enabled = True
        self.config.workflow.merge_mode = 'local'
        self.config.workflow.cleanup_local_branches = True
        self.config.workflow.cleanup_remote_branches = True

    def integrated(self, *, published=True):
        gw.git(self.root, 'merge', '--ff-only', self.head)
        if published:
            gw.git(self.root, 'push', 'origin', 'refs/heads/main')

    def run_cleanup(self, **kwargs):
        return cleanup.cleanup(self.root, self.config, self.task, self.adapter, **kwargs)

    def accept_parent(self):
        """Supply the same disposable snapshot as a successful local landing."""
        tasks = workflow.tree(self.adapter, self.task)
        requirements = workflow.requirements(tasks)
        base_head = gw.git(self.root, 'rev-parse', self.head + '^')
        fingerprint = workflow.scope_fingerprint(self.head, base_head, requirements)
        saved = {'scope': fingerprint, 'local_acceptance': {
            'head': self.head, 'base': 'main', 'base_head': base_head,
            'requirements': requirements, 'scope': fingerprint,
            'validation': {'exit': 0}, 'review': {'verdict': 'PASS'}}}
        workflow.save_evidence(self.root, self.task, saved)
        return saved

    def exists(self, *, remote=False):
        root = self.origin if remote else self.root
        return gw._run(['git', 'show-ref', '--verify', '--quiet', self.ref], cwd=root).returncode == 0

    def descendant(self, task_id, *, parent=None, state='Done', head=None):
        parent = parent or self.task
        task = Task(task_id, '1', 'Renamed descendant', state=state, parent=parent.id)
        parent.children.append(task_id)
        self.tasks[task_id] = task
        branch = f'ab/{task_id}-original-title'
        gw.git(self.root, 'branch', branch, head or self.head)
        gw.git(self.root, 'push', 'origin', 'refs/heads/' + branch)
        return task, branch

    def descendant_outcomes(self, result):
        return {item['task']: item for item in result['descendants']}

    def github_proof(self):
        self.config.workflow.merge_mode = 'github'
        self.config.github.gh_token = True
        pr = {'number': 9, 'headRefName': self.branch, 'headRefOid': self.head,
              'baseRefName': 'main', 'mergedAt': '2026-09-08T12:00:00Z',
              'mergeCommit': {'oid': gw.head_sha(self.root)}, 'state': 'MERGED'}
        for target in (patch.object(gw, 'github_repository', return_value='o/r'),
                       patch.object(github, 'gh', return_value=[{'number': 9, 'state': 'MERGED',
                                                               'headRefName': self.branch}]),
                       patch.object(github, 'view', return_value=pr)):
            target.start()
            self.addCleanup(target.stop)
        return pr

    def test_disabled_cleanup_does_not_even_query_git_or_github(self):
        self.config.workflow.cleanup_local_branches = False
        self.config.workflow.cleanup_remote_branches = False
        with patch.object(gw, '_run') as git, patch.object(github, 'gh') as gh:
            result = self.run_cleanup()
        self.assertEqual(result['local']['status'], 'disabled')
        self.assertEqual(result['remote']['status'], 'disabled')
        git.assert_not_called()
        gh.assert_not_called()

    def dispatch_land(self, action='reconcile'):
        adapter = self.adapter
        with patch.object(workflow, 'effective_config', return_value=self.config), \
             patch.object(workflow, 'repository_root', return_value=self.root), \
             patch.object(Binding, 'load', return_value=BINDING), \
             patch.object(workflow, 'make_adapter', return_value=adapter):
            try:
                return workflow.dispatch(workflow.parser().parse_args(['land', '7', action]))
            finally:
                adapter.set_state.assert_not_called()

    def test_dispatch_done_reconcile_rejects_later_unmerged_work_with_any_cleanup_policy(self):
        """Done cannot conceal new work even when automatic deletion is disabled."""
        self.integrated()
        self.github_proof()
        gw.git(self.root, 'switch', self.branch)
        gw.git(self.root, 'commit', '--allow-empty', '-m', 'later unmerged task work')
        moved = gw.head_sha(self.root)
        for mode in ('local', 'github'):
            for enabled in (False, True):
                with self.subTest(mode=mode, cleanup=enabled):
                    self.config.workflow.merge_mode = mode
                    self.config.workflow.cleanup_local_branches = enabled
                    self.config.workflow.cleanup_remote_branches = enabled
                    with self.assertRaises(AgentBoxError):
                        self.dispatch_land()
                    self.assertEqual(gw.local_branch_head(self.root, self.branch), moved)
                    self.assertTrue(self.exists(remote=True))

    def test_dispatch_done_surviving_refs_prove_integration_even_while_occupied(self):
        """Current/worktree protection prevents deletion, not read-only reconciliation."""
        self.integrated()
        self.github_proof()
        gw.git(self.root, 'switch', self.branch)
        for mode in ('local', 'github'):
            for enabled in (False, True):
                with self.subTest(mode=mode, cleanup=enabled):
                    self.config.workflow.merge_mode = mode
                    self.config.workflow.cleanup_local_branches = enabled
                    self.config.workflow.cleanup_remote_branches = enabled
                    with patch.object(cleanup, '_merged_head', wraps=cleanup._merged_head) as proof, \
                         patch.object(gw, '_is_ancestor', wraps=gw._is_ancestor) as ancestry:
                        self.assertEqual(self.dispatch_land()['state'], 'Done')
                    (proof if mode == 'github' else ancestry).assert_called()
                    self.assertTrue(self.exists())
                    self.assertTrue(self.exists(remote=True))

    def test_dispatch_done_remote_only_moved_head_refuses_without_deleting(self):
        """Live origin must expose new work even after the local task ref is gone."""
        self.integrated()
        self.github_proof()
        gw.git(self.root, 'switch', self.branch)
        gw.git(self.root, 'commit', '--allow-empty', '-m', 'new remote work')
        moved = gw.head_sha(self.root)
        gw.git(self.root, 'push', 'origin', self.ref)
        gw.git(self.root, 'switch', 'main')
        gw.git(self.root, 'update-ref', '-d', self.ref)
        gw.git(self.root, 'update-ref', '-d', 'refs/remotes/origin/' + self.branch)
        for mode in ('local', 'github'):
            for enabled in (False, True):
                with self.subTest(mode=mode, cleanup=enabled):
                    self.config.workflow.merge_mode = mode
                    self.config.workflow.cleanup_local_branches = enabled
                    self.config.workflow.cleanup_remote_branches = enabled
                    with self.assertRaises(AgentBoxError):
                        self.dispatch_land()
                    self.assertEqual(gw.local_branch_head(self.origin, self.branch), moved)

    def test_dispatch_done_checks_remote_before_deleting_a_proven_local_branch(self):
        """A valid local head must not allow deletion before remote proof succeeds."""
        self.integrated()
        self.github_proof()
        gw.git(self.root, 'switch', self.branch)
        gw.git(self.root, 'commit', '--allow-empty', '-m', 'remote work')
        gw.git(self.root, 'push', 'origin', self.ref)
        gw.git(self.root, 'switch', 'main')
        gw.git(self.root, 'update-ref', self.ref, self.head)
        for mode in ('local', 'github'):
            with self.subTest(mode=mode):
                self.config.workflow.merge_mode = mode
                with self.assertRaises(AgentBoxError):
                    self.dispatch_land()
                self.assertEqual(gw.local_branch_head(self.root, self.branch), self.head)
                self.assertTrue(self.exists(remote=True))

    def test_dispatch_done_github_squash_proof_allows_cleanup_and_absent_retry(self):
        """Exact PR proof permits rewritten merges and later retries from base."""
        self.github_proof()
        self.assertFalse(gw._is_ancestor(self.root, self.head, gw.head_sha(self.root)))
        result = self.dispatch_land()
        self.assertEqual([result['cleanup'][k]['status'] for k in ('local', 'remote')],
                         ['deleted', 'deleted'])
        result = self.dispatch_land()
        self.assertEqual([result['cleanup'][k]['status'] for k in ('local', 'remote')],
                         ['absent', 'absent'])
        self.assertNotIn('merge', result)

    def test_dispatch_done_ambiguous_live_refs_refuse_with_any_cleanup_policy(self):
        """A second live candidate cannot be ignored because deletion is disabled."""
        self.integrated()
        self.github_proof()
        gw.git(self.origin, 'update-ref', 'refs/heads/ab/7-second', self.head)
        for mode in ('local', 'github'):
            for enabled in (False, True):
                with self.subTest(mode=mode, cleanup=enabled):
                    self.config.workflow.merge_mode = mode
                    self.config.workflow.cleanup_local_branches = enabled
                    self.config.workflow.cleanup_remote_branches = enabled
                    with self.assertRaisesRegex(AgentBoxError, 'Ambiguous'):
                        self.dispatch_land()
                    self.assertTrue(self.exists())
                    self.assertTrue(self.exists(remote=True))

    def test_dispatch_done_absent_refs_retry_from_base_never_invents_merge_identity(self):
        """Absence allows an idempotent no-op in either mode without querying a PR."""
        gw.git(self.root, 'update-ref', '-d', self.ref)
        gw.git(self.origin, 'update-ref', '-d', self.ref)
        # A stale tracking ref supplies no surviving head or merge identity.
        for mode in ('local', 'github'):
            for enabled in (False, True):
                with self.subTest(mode=mode, cleanup=enabled):
                    self.config.workflow.merge_mode = mode
                    self.config.github.gh_token = True
                    self.config.workflow.cleanup_local_branches = enabled
                    self.config.workflow.cleanup_remote_branches = enabled
                    with patch.object(github, 'gh') as gh:
                        result = self.dispatch_land()
                    self.assertEqual(result['state'], 'Done')
                    self.assertNotIn('merge', result)
                    gh.assert_not_called()

    def test_dispatch_explicit_disabled_cleanup_does_not_inspect_merge_proof(self):
        """Only reconcile requires proof when the deletion policy is disabled."""
        self.config.workflow.cleanup_local_branches = False
        self.config.workflow.cleanup_remote_branches = False
        with patch.object(cleanup, '_local_refs') as refs, patch.object(github, 'gh') as gh:
            result = self.dispatch_land('cleanup')
        self.assertEqual(result['cleanup']['local']['status'], 'disabled')
        refs.assert_not_called()
        gh.assert_not_called()

    def test_local_merge_ignores_branches_outside_live_hierarchy_and_retry_handles_absence(self):
        self.integrated()
        gw.git(self.root, 'branch', 'ab/8-child', self.head)
        with patch.object(github, 'gh') as gh:
            result = self.run_cleanup()
            retry = self.run_cleanup()
        self.assertEqual([result[k]['status'] for k in ('local', 'remote')], ['deleted', 'deleted'])
        self.assertEqual([retry[k]['status'] for k in ('local', 'remote')], ['absent', 'absent'])
        self.assertEqual(gw.local_branch_head(self.root, 'ab/8-child'), self.head)
        gh.assert_not_called()

    def test_multiple_and_nested_descendants_use_live_hierarchy_and_exact_git_names(self):
        """Only completed tasks in the current hierarchy authorize branch deletion."""
        self.integrated()
        child, _ = self.descendant('8')
        self.descendant('9')
        self.descendant('10', parent=child)
        self.accept_parent()
        gw.git(self.root, 'branch', 'ab/80-unrelated', self.head)
        with patch.object(github, 'gh') as gh:
            result = self.dispatch_land('cleanup')['cleanup']
        self.assertEqual(set(self.descendant_outcomes(result)), {'8', '9', '10'})
        for item in [result, *result['descendants']]:
            self.assertEqual([item[k]['status'] for k in ('local', 'remote')], ['deleted', 'deleted'])
            self.assertEqual(item['local']['head'], self.head)
            for root in (self.root, self.origin):
                self.assertNotEqual(gw._run(['git', 'show-ref', '--verify', '--quiet',
                                            'refs/heads/' + item['branch']], cwd=root).returncode, 0)
        self.assertEqual(gw.local_branch_head(self.root, 'ab/80-unrelated'), self.head)
        self.adapter.comments.assert_not_called()
        gh.assert_not_called()

    def test_disabled_cleanup_does_not_read_descendants(self):
        """Both existing opt-outs suppress traversal and all ref operations."""
        self.descendant('8')
        self.config.workflow.cleanup_local_branches = False
        self.config.workflow.cleanup_remote_branches = False
        self.adapter.reset_mock()
        with patch.object(gw, '_run') as git:
            result = self.run_cleanup()
        self.assertEqual(result['descendants'], [])
        self.adapter.get.assert_not_called()
        git.assert_not_called()

    def test_descendant_local_and_remote_settings_remain_independent(self):
        """Local-only deletion leaves child origin refs for a later remote-only retry."""
        self.integrated()
        _, branch = self.descendant('8')
        self.config.workflow.cleanup_remote_branches = False
        self.accept_parent()
        with patch.object(cleanup, '_remote_refs') as remote:
            first = self.descendant_outcomes(self.run_cleanup())['8']
        self.assertEqual([first[k]['status'] for k in ('local', 'remote')], ['deleted', 'disabled'])
        self.assertEqual(gw.local_branch_head(self.origin, branch), self.head)
        remote.assert_not_called()
        self.config.workflow.cleanup_local_branches = False
        self.config.workflow.cleanup_remote_branches = True
        second = self.descendant_outcomes(self.run_cleanup())['8']
        self.assertEqual([second[k]['status'] for k in ('local', 'remote')], ['disabled', 'deleted'])

    def test_absent_parent_and_repeated_retry_still_clean_nested_descendants(self):
        """Deleted parent refs cannot short-circuit remaining descendant cleanup."""
        self.integrated()
        self.run_cleanup()
        gw.git(self.root, 'update-ref', '-d', 'refs/remotes/origin/' + self.branch)
        child, _ = self.descendant('8')
        self.descendant('9', parent=child)
        self.accept_parent()
        for expected in ('deleted', 'absent'):
            result = self.dispatch_land('reconcile')['cleanup']
            self.assertEqual(result['local']['status'], 'absent')
            for item in result['descendants']:
                self.assertEqual([item[k]['status'] for k in ('local', 'remote')], [expected, expected])

    def test_unfinished_descendant_is_preserved_but_completed_nested_task_is_considered(self):
        """Done on the parent never substitutes for a descendant's live lifecycle."""
        self.integrated()
        child, branch = self.descendant('8', state='Doing')
        self.descendant('9', parent=child)
        self.accept_parent()
        result = self.descendant_outcomes(self.run_cleanup())
        for kind in ('local', 'remote'):
            self.assertEqual(result['8'][kind]['status'], 'preserved')
            self.assertIn('Doing', result['8'][kind]['reason'])
            self.assertEqual(result['9'][kind]['status'], 'deleted')
        self.assertEqual(gw.local_branch_head(self.root, branch), self.head)
        self.assertEqual(gw.local_branch_head(self.origin, branch), self.head)

    def test_unfinished_discovery_reports_refs_without_proof_or_writes(self):
        """Unfinished namespaces expose live refs and failures without authorizing cleanup."""
        _, branch = self.descendant('8', state='Doing')
        ref = 'refs/heads/' + branch
        second = 'ab/8-second'
        second_ref = 'refs/heads/' + second
        gw.git(self.root, 'commit', '--allow-empty', '-m', 'unfinished local work')
        moved = gw.head_sha(self.root)
        gw.git(self.origin, 'fetch', str(self.root), 'refs/heads/main')
        for root in (self.root, self.origin):
            gw.git(root, 'update-ref', '-d', self.ref)
        gw.git(self.root, 'update-ref', '-d', 'refs/remotes/origin/' + self.branch)
        self.config.workflow.merge_mode = 'github'
        self.config.github.gh_token = False
        original_remote = cleanup._remote_refs
        reason = 'Task 8 is Doing, not Done'
        for scenario in ('unique', 'ambiguous-local', 'ambiguous-remote', 'absent', 'remote-error'):
            for local_enabled, remote_enabled in ((True, True), (True, False), (False, True)):
                with self.subTest(scenario=scenario, local=local_enabled, remote=remote_enabled):
                    self.config.workflow.cleanup_local_branches = local_enabled
                    self.config.workflow.cleanup_remote_branches = remote_enabled
                    for root, head in ((self.root, moved), (self.origin, self.head)):
                        gw.git(root, 'update-ref', ref, head)
                        gw.git(root, 'update-ref', '-d', second_ref)
                        if scenario == 'absent':
                            gw.git(root, 'update-ref', '-d', ref)
                    if scenario.startswith('ambiguous-'):
                        root = self.root if scenario == 'ambiguous-local' else self.origin
                        gw.git(root, 'update-ref', second_ref, moved)
                    before = [gw.git(root, 'show-ref') for root in (self.root, self.origin)]
                    remote_effect = (AgentBoxError('origin unavailable') if scenario == 'remote-error'
                                     else original_remote)
                    with patch.object(cleanup, '_remote_refs', side_effect=remote_effect) as remote, \
                         patch.object(cleanup, '_parent_proof') as parent_proof, \
                         patch.object(cleanup, '_merged_head') as merged, \
                         patch.object(cleanup, '_integrated_head') as integrated, \
                         patch.object(gw, 'git', wraps=gw.git) as git:
                        result = self.descendant_outcomes(self.dispatch_land('cleanup')['cleanup'])['8']
                    parent_proof.assert_not_called()
                    merged.assert_not_called()
                    integrated.assert_not_called()
                    self.assertFalse(any('update-ref' in call.args or 'push' in call.args
                                         for call in git.call_args_list))
                    if remote_enabled:
                        self.assertTrue(any(call.args[-1] == 'ab/8-' for call in remote.call_args_list))
                    else:
                        remote.assert_not_called()
                    self.assertEqual(before, [gw.git(root, 'show-ref') for root in (self.root, self.origin)])
                    for kind, enabled, head in (('local', local_enabled, moved),
                                                 ('remote', remote_enabled, self.head)):
                        if not enabled:
                            expected = {'status': 'disabled'}
                        elif scenario == 'remote-error' and kind == 'remote':
                            expected = {'status': 'error', 'reason': 'origin unavailable'}
                        elif scenario == 'absent':
                            expected = {'status': 'absent'}
                        elif scenario == 'ambiguous-' + kind:
                            expected = {'status': 'error', 'reason': 'Ambiguous task namespace; ' + reason,
                                        'candidates': [{'branch': branch, 'head': head},
                                                       {'branch': second, 'head': moved}]}
                        else:
                            expected = {'status': 'preserved', 'reason': reason, 'branch': branch, 'head': head}
                        self.assertEqual(result[kind], expected)

    def land_with_occupied_child(self):
        child, branch = self.descendant('8')
        occupied = self.directory / 'occupied'
        gw.git(self.root, 'worktree', 'add', str(occupied), branch)
        self.task.state = 'Review'
        gw.git(self.root, 'switch', self.branch)
        self.adapter.identity.return_value = {'id': 1}
        self.config.workflow.review.policy = 'off'
        saved = {'scope': workflow.scope(self.root, 'main', [self.task, child]), 'validation': {'exit': 0}}
        workflow.save_evidence(self.root, self.task, saved)
        args = SimpleNamespace(command='land', task=self.task.id, action='merge', method=None)
        result = workflow.landing(args, self.root, self.config, self.adapter)
        self.assertEqual(result['state'], 'Done')
        for kind in ('local', 'remote'):
            self.assertEqual(result['cleanup'][kind]['status'], 'deleted')
            self.assertIn('worktree', self.descendant_outcomes(result['cleanup'])['8'][kind]['reason'])
        self.task.state = 'Done'
        self.adapter.reset_mock()
        gw.git(self.root, 'worktree', 'remove', str(occupied))
        return branch

    def test_absent_parent_retry_preserves_child_moved_to_later_published_base(self):
        """Later base ancestry cannot enlarge the result accepted before parent deletion."""
        branch = self.land_with_occupied_child()
        gw.git(self.root, 'commit', '--allow-empty', '-m', 'later base work')
        moved = gw.head_sha(self.root)
        gw.git(self.root, 'push', 'origin', 'refs/heads/main')
        gw.git(self.root, 'update-ref', 'refs/heads/' + branch, moved)
        gw.git(self.root, 'push', 'origin', 'refs/heads/' + branch)
        for action in ('cleanup', 'reconcile', 'cleanup'):
            result = self.dispatch_land(action)
            self.assertEqual(result['state'], 'Done')
            for kind in ('local', 'remote'):
                self.assertEqual(result['cleanup'][kind]['status'], 'absent')
                self.assertEqual(self.descendant_outcomes(result['cleanup'])['8'][kind]['status'], 'error')
                self.assertIn('accepted parent result',
                              self.descendant_outcomes(result['cleanup'])['8'][kind]['reason'])
            for root in (self.root, self.origin):
                self.assertEqual(gw.local_branch_head(root, branch), moved)

    def test_invalid_acceptance_preserves_children_without_blocking_parent_or_done(self):
        """Missing, stale or inconsistent evidence cannot authorize retry deletion."""
        branch = self.land_with_occupied_child()
        original = workflow.read_evidence(self.root, self.task)
        changes = (
            ('missing', None, None), ('base', 'base', 'other'),
            ('requirements', 'requirements', []), ('head', 'head', 'f' * 40),
            ('base_head', 'base_head', 'f' * 40), ('scope', 'scope', 'stale'),
            ('nonexact head', 'head', 'main'), ('validation', 'validation', {'exit': 1}),
            ('malformed', None, []), ('retired scope', None, 'retired'),
        )
        for label, key, value in changes:
            with self.subTest(evidence=label):
                saved = copy.deepcopy(original)
                if key:
                    saved['local_acceptance'][key] = value
                elif label == 'retired scope':
                    saved['scope'] = value
                else:
                    saved['local_acceptance'] = value
                workflow.save_evidence(self.root, self.task, saved)
                for root in (self.root, self.origin):
                    gw.git(root, 'update-ref', self.ref, self.head)
                for action, parent_status in (('cleanup', 'deleted'), ('reconcile', 'absent')):
                    result = self.dispatch_land(action)
                    self.assertEqual(result['state'], 'Done')
                    for kind in ('local', 'remote'):
                        self.assertEqual(result['cleanup'][kind]['status'], parent_status)
                        self.assertIn('acceptance evidence',
                                      self.descendant_outcomes(result['cleanup'])['8'][kind]['reason'])
                    for root in (self.root, self.origin):
                        self.assertEqual(gw.local_branch_head(root, branch), self.head)
        workflow.save_evidence(self.root, self.task, original)
        result = self.dispatch_land('cleanup')['cleanup']
        for kind in ('local', 'remote'):
            self.assertEqual(self.descendant_outcomes(result)['8'][kind]['status'], 'deleted')

    def test_absent_parent_acceptance_allows_local_cleanup_when_origin_is_unavailable(self):
        """Recoverable local proof stays usable even after both parent refs were deleted."""
        branch = self.land_with_occupied_child()
        with patch.object(cleanup, '_remote_refs', side_effect=AgentBoxError('origin unavailable')):
            result = self.descendant_outcomes(self.dispatch_land('cleanup')['cleanup'])['8']
        self.assertEqual(result['local']['status'], 'deleted')
        self.assertIn('origin unavailable', result['remote']['reason'])
        self.assertEqual(gw.local_branch_head(self.origin, branch), self.head)

    def test_changed_task_requirements_retire_absent_parent_cleanup_proof(self):
        """A snapshot for earlier requirements cannot authorize the live task tree."""
        branch = self.land_with_occupied_child()
        self.task.description += '\nNew acceptance requirement'
        result = self.dispatch_land('cleanup')
        self.assertEqual(result['state'], 'Done')
        for kind in ('local', 'remote'):
            self.assertIn('task/base scope', self.descendant_outcomes(result['cleanup'])['8'][kind]['reason'])
        for root in (self.root, self.origin):
            self.assertEqual(gw.local_branch_head(root, branch), self.head)

    def test_informational_metadata_keeps_absent_parent_cleanup_proof(self):
        """Ordinary links and labels cannot invalidate an accepted integration snapshot."""
        self.land_with_occupied_child()
        self.task.related.append('99')
        self.task.labels.append('ordinary')
        result = self.descendant_outcomes(self.dispatch_land('cleanup')['cleanup'])['8']
        for kind in ('local', 'remote'):
            self.assertEqual(result[kind]['status'], 'deleted')

    def test_descendant_new_work_outside_accepted_parent_is_preserved(self):
        """Even later base work is outside a supplied accepted parent result."""
        self.integrated()
        gw.git(self.root, 'commit', '--allow-empty', '-m', 'later base work')
        moved = gw.head_sha(self.root)
        _, branch = self.descendant('8', head=moved)
        result = self.descendant_outcomes(self.run_cleanup(expected_head=self.head))['8']
        for kind in ('local', 'remote'):
            self.assertIn('accepted parent result', result[kind]['reason'])
        self.assertEqual(gw.local_branch_head(self.root, branch), moved)
        self.assertEqual(gw.local_branch_head(self.origin, branch), moved)

    def test_descendant_current_worktree_ambiguous_and_symbolic_refs_do_not_block_siblings(self):
        """Each protected namespace reports why it survives while siblings progress."""
        self.integrated()
        _, current = self.descendant('8')
        _, occupied = self.descendant('9')
        self.descendant('10')
        _, symbolic = self.descendant('11')
        self.descendant('12')
        self.accept_parent()
        gw.git(self.root, 'branch', 'ab/10-second', self.head)
        gw.git(self.root, 'symbolic-ref', 'refs/heads/' + symbolic, 'refs/heads/main')
        gw.git(self.root, 'worktree', 'add', str(self.directory / 'occupied'), occupied)
        gw.git(self.root, 'switch', current)
        result = self.run_cleanup()
        children = self.descendant_outcomes(result)
        for task_id, reason in (('8', 'current branch'), ('9', 'worktree'),
                                ('10', 'Ambiguous'), ('11', 'symbolic')):
            for kind in ('local', 'remote'):
                self.assertIn(reason, children[task_id][kind]['reason'])
        self.assertEqual(children['12']['local']['status'], 'deleted')
        self.assertEqual(result['local']['status'], 'deleted')

    def test_descendant_live_remote_ambiguity_preserves_both_candidates(self):
        """A remote-only second name prevents guessing the child's intended branch."""
        self.integrated()
        _, branch = self.descendant('8')
        gw.git(self.origin, 'update-ref', 'refs/heads/ab/8-second', self.head)
        child = self.descendant_outcomes(self.run_cleanup())['8']
        for kind in ('local', 'remote'):
            self.assertIn('Ambiguous', child[kind]['reason'])
        self.assertEqual(gw.local_branch_head(self.root, branch), self.head)
        self.assertEqual(gw.local_branch_head(self.origin, 'ab/8-second'), self.head)

    def test_descendant_deletion_races_preserve_new_heads_and_do_not_block_siblings(self):
        """Local compare-and-delete and the remote lease cover descendant heads too."""
        self.integrated()
        _, branch = self.descendant('8')
        self.descendant('9')
        ref = 'refs/heads/' + branch
        gw.git(self.root, 'commit', '--allow-empty', '-m', 'concurrent work')
        moved = gw.head_sha(self.root)
        gw.git(self.origin, 'fetch', str(self.root), 'refs/heads/main')
        real_git = gw.git

        def race(root, *args, **kwargs):
            if args[:4] == ('update-ref', '--no-deref', '-d', ref):
                real_git(self.root, 'update-ref', ref, moved)
            if 'push' in args and args[-1] == ':' + ref:
                real_git(self.origin, 'update-ref', ref, moved)
            return real_git(root, *args, **kwargs)

        with patch.object(gw, 'git', side_effect=race):
            result = self.run_cleanup(expected_head=self.head)
        children = self.descendant_outcomes(result)
        for kind in ('local', 'remote'):
            self.assertEqual(children['8'][kind]['status'], 'error')
            self.assertEqual(children['9'][kind]['status'], 'deleted')
            self.assertEqual(result[kind]['status'], 'deleted')
        self.assertEqual(gw.local_branch_head(self.root, branch), moved)
        self.assertEqual(gw.local_branch_head(self.origin, branch), moved)

    def test_descendant_remote_cleanup_waits_for_published_local_base(self):
        """Local success cannot authorize origin deletion until that head is published."""
        self.integrated(published=False)
        self.descendant('8')
        self.accept_parent()
        child = self.descendant_outcomes(self.run_cleanup())['8']
        self.assertEqual(child['local']['status'], 'deleted')
        self.assertIn('published origin base', child['remote']['reason'])
        gw.git(self.root, 'push', 'origin', 'refs/heads/main')
        retry = self.descendant_outcomes(self.run_cleanup())['8']
        self.assertEqual([retry[k]['status'] for k in ('local', 'remote')], ['absent', 'deleted'])

    def test_descendant_origin_failure_keeps_local_cleanup_independent(self):
        """An unavailable remote must not suppress proven local descendant deletion."""
        self.integrated()
        _, branch = self.descendant('8')
        self.accept_parent()
        with patch.object(cleanup, '_remote_refs', side_effect=AgentBoxError('origin unavailable')):
            child = self.descendant_outcomes(self.run_cleanup())['8']
        self.assertEqual(child['local']['status'], 'deleted')
        self.assertIn('origin unavailable', child['remote']['reason'])
        self.assertEqual(gw.local_branch_head(self.origin, branch), self.head)

    def test_github_descendant_moved_head_is_not_authorized_by_parent_pr(self):
        """A merged parent PR does not cover later descendant work, even on base."""
        self.github_proof()
        gw.git(self.root, 'commit', '--allow-empty', '-m', 'unrelated accepted base work')
        moved = gw.head_sha(self.root)
        _, branch = self.descendant('8', head=moved)
        child = self.descendant_outcomes(self.run_cleanup())['8']
        for kind in ('local', 'remote'):
            self.assertIn('accepted parent result', child[kind]['reason'])
        self.assertEqual(gw.local_branch_head(self.root, branch), moved)
        self.assertEqual(gw.local_branch_head(self.origin, branch), moved)

    def rewritten_parent_cleanup(self, method):
        gw.git(self.root, 'switch', self.branch)
        (self.root / 'accepted.txt').write_text('accepted descendant work\n')
        gw.git(self.root, 'add', 'accepted.txt')
        gw.git(self.root, 'commit', '-m', 'accepted result')
        self.head = gw.head_sha(self.root)
        gw.git(self.root, 'push', 'origin', self.ref)
        gw.git(self.root, 'switch', 'main')
        gw.git(self.root, 'commit', '--allow-empty', '-m', 'independent base advance')
        if method == 'squash':
            gw.git(self.root, 'merge', '--squash', self.branch)
            gw.git(self.root, 'commit', '-m', 'squashed parent result')
        else:
            gw.git(self.root, 'cherry-pick', self.head)
        gw.git(self.root, 'push', 'origin', 'refs/heads/main')
        child, _ = self.descendant('8')
        self.descendant('9', parent=child)
        self.github_proof()
        self.assertFalse(gw._is_ancestor(self.root, self.head, gw.head_sha(self.root)))
        result = self.run_cleanup()
        for item in [result, *result['descendants']]:
            self.assertEqual([item[k]['status'] for k in ('local', 'remote')], ['deleted', 'deleted'])

    def test_github_squashed_parent_proves_descendants_without_base_ancestry(self):
        """Squashing loses ancestry but the exact merged PR still proves the result."""
        self.rewritten_parent_cleanup('squash')

    def test_github_rebased_parent_proves_descendants_without_base_ancestry(self):
        """Replaying the parent changes its OID without invalidating exact PR proof."""
        self.rewritten_parent_cleanup('rebase')

    def test_github_absent_parent_recovers_pr_proof_for_descendant_retry(self):
        """A merged PR supplies integration evidence even after every parent ref is gone."""
        self.github_proof()
        self.run_cleanup()
        gw.git(self.root, 'update-ref', '-d', 'refs/remotes/origin/' + self.branch)
        self.descendant('8')
        first = self.dispatch_land('reconcile')['cleanup']
        self.assertEqual(first['local']['status'], 'absent')
        self.assertEqual(self.descendant_outcomes(first)['8']['remote']['status'], 'deleted')
        with patch.object(github, 'gh') as gh:
            second = self.run_cleanup()
        self.assertEqual(self.descendant_outcomes(second)['8']['local']['status'], 'absent')
        gh.assert_not_called()

    def test_github_absent_parent_origin_failure_allows_exact_pr_local_cleanup(self):
        """Origin transport failure cannot suppress local proof from the unique parent PR."""
        self.github_proof()
        self.run_cleanup()
        gw.git(self.root, 'update-ref', '-d', 'refs/remotes/origin/' + self.branch)
        _, branch = self.descendant('8')
        with patch.object(cleanup, '_remote_refs', side_effect=AgentBoxError('origin unavailable')):
            result = self.dispatch_land('cleanup')['cleanup']
        child = self.descendant_outcomes(result)['8']
        self.assertEqual(child['local'], {'status': 'deleted', 'head': self.head})
        self.assertEqual(result['local']['status'], 'absent')
        for item in (result, child):
            self.assertEqual(item['remote'], {'status': 'error', 'reason': 'origin unavailable'})
        self.assertNotIn('--head', github.gh.call_args.args)
        github.view.assert_called_with(self.root, 'o/r', 9)
        self.assertEqual(gw.local_branch_head(self.origin, branch), self.head)
        self.assertNotEqual(gw._run(['git', 'show-ref', '--verify', '--quiet',
                                    'refs/heads/' + branch], cwd=self.root).returncode, 0)

    def test_github_absent_parent_origin_failure_still_requires_unique_pr_proof(self):
        """Independent remote failure must not weaken missing, ambiguous or unavailable PR proof."""
        self.github_proof()
        self.run_cleanup()
        gw.git(self.root, 'update-ref', '-d', 'refs/remotes/origin/' + self.branch)
        _, branch = self.descendant('8')
        rows = [{'number': number, 'state': 'MERGED', 'headRefName': self.branch} for number in (9, 10)]
        for response, reason in (([], 'missing or ambiguous'), (rows, 'missing or ambiguous'),
                                 (AgentBoxError('GitHub unavailable'), 'GitHub unavailable')):
            with self.subTest(reason=reason, response=response):
                options = {'side_effect': response} if isinstance(response, Exception) else {'return_value': response}
                with patch.object(cleanup, '_remote_refs', side_effect=AgentBoxError('origin unavailable')), \
                     patch.object(github, 'gh', **options):
                    child = self.descendant_outcomes(self.dispatch_land('cleanup')['cleanup'])['8']
                self.assertEqual(child['local']['status'], 'error')
                self.assertIn(reason, child['local']['reason'])
                self.assertEqual(child['remote'], {'status': 'error', 'reason': 'origin unavailable'})
                for root in (self.root, self.origin):
                    self.assertEqual(gw.local_branch_head(root, branch), self.head)

    def test_github_ambiguous_absent_parent_pr_preserves_descendants(self):
        """Retry must not choose among merged or reopened parent results."""
        self.github_proof()
        self.run_cleanup()
        gw.git(self.root, 'update-ref', '-d', 'refs/remotes/origin/' + self.branch)
        _, branch = self.descendant('8')
        rows = [{'number': number, 'state': 'MERGED', 'headRefName': self.branch} for number in (9, 10)]
        with patch.object(github, 'gh', return_value=rows):
            result = self.descendant_outcomes(self.run_cleanup())['8']
        self.assertIn('ambiguous', result['local']['reason'])
        self.assertEqual(gw.local_branch_head(self.root, branch), self.head)
        self.assertEqual(gw.local_branch_head(self.origin, branch), self.head)

    def test_hierarchy_read_failure_does_not_undo_done_or_parent_cleanup(self):
        """Unavailable hierarchy is reported separately and never inferred from Git."""
        self.integrated()
        _, branch = self.descendant('8')
        del self.tasks['8']
        result = self.dispatch_land('cleanup')
        self.assertEqual(result['state'], 'Done')
        self.assertIn('8', result['cleanup']['descendants_error'])
        self.assertEqual(result['cleanup']['local']['status'], 'deleted')
        self.assertEqual(gw.local_branch_head(self.root, branch), self.head)

    def test_invalid_hierarchy_preserves_descendants_without_following_unrelated_tasks(self):
        """Cycles and inconsistent parent links cannot authorize branch deletion."""
        self.integrated()
        child, branch = self.descendant('8')
        for parent, children in (('other', []), (self.task.id, [self.task.id])):
            child.parent, child.children = parent, children
            result = self.run_cleanup()
            self.assertIn('hierarchy', result['descendants_error'])
            self.assertEqual(gw.local_branch_head(self.root, branch), self.head)

    def test_each_opt_in_is_independent(self):
        self.integrated()
        self.config.workflow.cleanup_remote_branches = False
        with patch.object(cleanup, '_remote_refs') as remote:
            result = self.run_cleanup()
        self.assertEqual(result['local']['status'], 'deleted')
        self.assertTrue(self.exists(remote=True))
        remote.assert_not_called()
        self.config.workflow.cleanup_local_branches = False
        self.config.workflow.cleanup_remote_branches = True
        self.assertEqual(self.run_cleanup()['remote']['status'], 'deleted')

    def test_unpublished_local_merge_preserves_remote_until_base_is_published(self):
        self.integrated(published=False)
        result = self.run_cleanup()
        self.assertEqual(result['local']['status'], 'deleted')
        self.assertIn('publish', result['remote']['reason'])
        self.assertTrue(self.exists(remote=True))
        gw.git(self.root, 'push', 'origin', 'refs/heads/main')
        self.assertEqual(self.run_cleanup()['remote']['status'], 'deleted')

    def test_done_does_not_prove_integration(self):
        result = self.run_cleanup()
        for kind in ('local', 'remote'):
            self.assertIn('not integrated', result[kind]['reason'])
        self.assertTrue(self.exists())
        self.assertTrue(self.exists(remote=True))

    def test_current_branch_preserves_both_copies_without_switching(self):
        self.integrated()
        gw.git(self.root, 'switch', self.branch)
        result = self.run_cleanup()
        for kind in ('local', 'remote'):
            self.assertIn('current branch', result[kind]['reason'])
        self.assertEqual(gw.current_branch(self.root), self.branch)
        self.assertTrue(self.exists(remote=True))

    def test_other_worktree_preserves_both_copies(self):
        self.integrated()
        gw.git(self.root, 'worktree', 'add', str(self.directory / 'other'), self.branch)
        result = self.run_cleanup()
        for kind in ('local', 'remote'):
            self.assertIn('worktree', result[kind]['reason'])
        self.assertTrue(self.exists())
        self.assertTrue(self.exists(remote=True))

    def test_worktree_is_checked_again_immediately_before_deletion(self):
        self.integrated()
        original = cleanup._unoccupied
        calls = []
        def occupy(root, branch, base):
            calls.append(branch)
            if len(calls) == 2:
                gw.git(root, 'worktree', 'add', str(self.directory / 'other'), branch)
            original(root, branch, base)
        with patch.object(cleanup, '_unoccupied', side_effect=occupy):
            result = self.run_cleanup()
        self.assertIn('worktree', result['local']['reason'])
        self.assertTrue(self.exists())
        self.assertTrue(self.exists(remote=True))

    def test_default_or_supplied_base_is_never_deleted(self):
        self.integrated()
        result = self.run_cleanup(base=self.branch)
        self.assertIn('base/default', result['local']['reason'])
        gw.git(self.origin, 'symbolic-ref', 'HEAD', self.ref)
        result = self.run_cleanup(base='main')
        self.assertIn('base/default', result['remote']['reason'])
        self.assertTrue(self.exists())
        self.assertTrue(self.exists(remote=True))

    def test_ambiguous_local_or_live_remote_namespace_preserves_everything(self):
        self.integrated()
        gw.git(self.root, 'branch', 'ab/7-second', self.head)
        self.assertIn('Ambiguous', self.run_cleanup()['local']['reason'])
        gw.git(self.root, 'push', 'origin', 'refs/heads/ab/7-second')
        gw.git(self.root, 'branch', '-D', 'ab/7-second')
        gw.git(self.root, 'update-ref', '-d', 'refs/remotes/origin/ab/7-second')
        self.assertIn('Ambiguous', self.run_cleanup()['remote']['reason'])
        self.assertTrue(self.exists())
        self.assertTrue(self.exists(remote=True))

    def test_symbolic_local_and_remote_refs_are_preserved(self):
        self.integrated()
        gw.git(self.root, 'symbolic-ref', self.ref, 'refs/heads/main')
        result = self.run_cleanup()
        self.assertIn('symbolic', result['local']['reason'])
        gw.git(self.root, 'symbolic-ref', '--delete', self.ref)
        gw.git(self.origin, 'symbolic-ref', self.ref, 'refs/heads/main')
        result = self.run_cleanup()
        self.assertIn('symbolic', result['remote']['reason'])
        self.assertTrue(self.exists(remote=True))

    def test_expected_merge_head_preserves_refs_that_moved_after_merge(self):
        self.integrated()
        gw.git(self.root, 'commit', '--allow-empty', '-m', 'new work')
        moved = gw.head_sha(self.root)
        gw.git(self.root, 'update-ref', self.ref, moved)
        gw.git(self.root, 'push', 'origin', self.ref)
        result = self.run_cleanup(expected_head=self.head)
        for kind in ('local', 'remote'):
            self.assertIn('moved', result[kind]['reason'])
        self.assertEqual(gw.local_branch_head(self.root, self.branch), moved)

    def test_local_expected_oid_refuses_race_and_remote_failure_can_be_retried(self):
        self.integrated()
        gw.git(self.root, 'commit', '--allow-empty', '-m', 'new work')
        moved = gw.head_sha(self.root)
        real_git = gw.git
        def race(root, *args, **kwargs):
            if args[:3] == ('update-ref', '--no-deref', '-d'):
                real_git(root, 'update-ref', self.ref, moved)
            return real_git(root, *args, **kwargs)
        with patch.object(gw, 'git', side_effect=race):
            result = self.run_cleanup()
        self.assertEqual(result['local']['status'], 'error')
        self.assertEqual(result['remote']['status'], 'deleted')
        self.assertEqual(gw.local_branch_head(self.root, self.branch), moved)
        # The moved tip is now integrated locally, so a new proof can remove it.
        self.assertEqual(self.run_cleanup()['local']['status'], 'deleted')

    def test_exact_remote_lease_refuses_race_without_repair_or_side_pushes(self):
        self.integrated()
        gw.git(self.root, 'commit', '--allow-empty', '-m', 'another checkout work')
        moved = gw.head_sha(self.root)
        gw.git(self.origin, 'fetch', str(self.root), 'refs/heads/main')
        real_git = gw.git
        pushes = []
        def race(root, *args, **kwargs):
            if 'push' in args:
                pushes.append(args)
                real_git(self.origin, 'update-ref', self.ref, moved)
            return real_git(root, *args, **kwargs)
        with patch.object(gw, 'git', side_effect=race):
            result = self.run_cleanup()
        self.assertEqual(result['local']['status'], 'deleted')
        self.assertEqual(result['remote']['status'], 'error')
        self.assertEqual(gw.local_branch_head(self.origin, self.branch), moved)
        self.assertEqual(len(pushes), 1)
        self.assertIn('--force-with-lease=' + self.ref + ':' + self.head, pushes[0])
        self.assertEqual(pushes[0][-1], ':' + self.ref)
        self.assertNotIn('--force', pushes[0])
        self.assertIn('--no-follow-tags', pushes[0])
        self.assertIn('--recurse-submodules=no', pushes[0])

    def test_push_options_override_tag_submodule_and_mirror_configuration(self):
        self.integrated()
        gw.git(self.root, 'tag', '-a', 'private-tag', '-m', 'must stay local')
        for key, value in (('push.followTags', 'true'), ('push.recurseSubmodules', 'on-demand'),
                           ('remote.origin.mirror', 'true')):
            gw.git(self.root, 'config', key, value)
        result = self.run_cleanup()
        self.assertEqual(result['remote']['status'], 'deleted')
        self.assertEqual(gw.git(self.origin, 'tag', '--list'), '')
        self.assertEqual(gw.local_branch_head(self.origin, 'main'), self.head)
        self.assertEqual(gw.git(self.root, 'config', 'remote.origin.mirror'), 'true')

    def test_multiple_destinations_preserve_remote_but_allow_local_cleanup(self):
        self.integrated()
        gw.git(self.root, 'config', '--add', 'remote.origin.pushurl', str(self.origin))
        gw.git(self.root, 'config', '--add', 'remote.origin.pushurl', str(self.directory / 'elsewhere'))
        result = self.run_cleanup()
        self.assertEqual(result['local']['status'], 'deleted')
        self.assertIn('exactly one', result['remote']['reason'])
        self.assertTrue(self.exists(remote=True))

    def test_rewritten_destination_is_used_for_read_proof_and_delete(self):
        self.integrated()
        gw.git(self.root, 'config', 'remote.origin.pushurl', 'cleanup-test:repo')
        gw.git(self.root, 'config', 'url.' + str(self.origin) + '.insteadOf', 'cleanup-test:repo')
        self.assertEqual(self.run_cleanup()['remote']['status'], 'deleted')

    def test_push_instead_of_resolves_once_and_destination_stays_frozen(self):
        self.integrated()
        gw.git(self.root, 'config', 'remote.origin.url', 'cleanup-test:repo')
        gw.git(self.root, 'config', 'url.' + str(self.origin) + '.pushInsteadOf', 'cleanup-test:repo')
        original = cleanup._remote_refs
        def change_origin(root, url, prefix):
            proof = original(root, url, prefix)
            gw.git(root, 'config', 'remote.origin.pushurl', str(self.directory / 'new-destination'))
            return proof
        with patch.object(cleanup, '_remote_refs', side_effect=change_origin):
            result = self.run_cleanup()
        self.assertEqual(result['remote']['status'], 'deleted')
        self.assertFalse(self.exists(remote=True))

    def test_transport_failure_preserves_remote_and_retry_uses_fresh_proof(self):
        self.integrated()
        with patch.object(cleanup, '_remote_refs', side_effect=AgentBoxError('origin unavailable')):
            result = self.run_cleanup()
        self.assertEqual(result['local']['status'], 'deleted')
        self.assertIn('origin unavailable', result['remote']['reason'])
        self.assertTrue(self.exists(remote=True))
        self.assertEqual(self.run_cleanup()['remote']['status'], 'deleted')

    def test_different_push_destination_requires_its_own_published_base(self):
        self.integrated(published=False)
        elsewhere = self.directory / 'elsewhere.git'
        gw.git(self.root, 'clone', '--bare', str(self.origin), str(elsewhere))
        gw.git(self.root, 'config', 'remote.origin.pushurl', str(elsewhere))
        gw.git(self.root, 'push', str(self.origin), 'refs/heads/main')
        result = self.run_cleanup()
        self.assertIn('published origin base', result['remote']['reason'])
        self.assertTrue(self.exists(remote=True))
        self.assertEqual(gw.local_branch_head(elsewhere, self.branch), self.head)

    def test_chained_rewrites_preserve_remote_instead_of_deleting_elsewhere(self):
        self.integrated()
        gw.git(self.root, 'config', 'remote.origin.pushurl', 'first:repo')
        gw.git(self.root, 'config', 'url.second:repo.pushInsteadOf', 'first:repo')
        gw.git(self.root, 'config', 'url.' + str(self.origin) + '.insteadOf', 'second:repo')
        result = self.run_cleanup()
        self.assertIn('chained', result['remote']['reason'])
        self.assertTrue(self.exists(remote=True))

    def test_github_squash_proof_does_not_require_original_head_ancestry(self):
        self.github_proof()
        self.assertFalse(gw._is_ancestor(self.root, self.head, gw.head_sha(self.root)))
        result = self.run_cleanup()
        self.assertEqual([result[k]['status'] for k in ('local', 'remote')], ['deleted', 'deleted'])

    def test_missing_published_commit_object_preserves_remote_with_fetch_guidance(self):
        self.integrated(published=False)
        other = self.directory / 'other'
        gw.git(self.root, 'clone', str(self.origin), str(other))
        gw.git(other, 'config', 'user.name', 'Other')
        gw.git(other, 'config', 'user.email', 'other@example.invalid')
        gw.git(other, 'merge', '--ff-only', 'origin/' + self.branch)
        gw.git(other, 'commit', '--allow-empty', '-m', 'published elsewhere')
        gw.git(other, 'push', 'origin', 'refs/heads/main')
        result = self.run_cleanup()
        self.assertEqual(result['local']['status'], 'deleted')
        self.assertIn('fetch missing objects', result['remote']['reason'])
        self.assertTrue(self.exists(remote=True))
        gw.git(self.root, 'fetch', 'origin', 'refs/heads/main')
        self.assertEqual(self.run_cleanup()['remote']['status'], 'deleted')

    def test_github_land_rereads_merge_identity_and_current_branch_cleanup_is_retryable(self):
        child, _ = self.descendant('8')
        merged = self.github_proof()
        merged['url'] = 'https://github.com/o/r/pull/9'
        opened = {**merged, 'state': 'OPEN', 'isDraft': False, 'mergeable': 'MERGEABLE',
                  'mergeStateStatus': 'CLEAN', 'latestReviews': [], 'statusCheckRollup': []}
        gw.git(self.root, 'switch', self.branch)
        self.task.state = 'Review'
        adapter = self.adapter
        adapter.identity.return_value = {'id': 1}
        args = SimpleNamespace(command='land', task=self.task.id, action='merge', method=None)
        saved = {'validation': {'exit': 0}, 'review': {'verdict': 'PASS'}}
        saved['scope'] = workflow.scope(self.root, 'main', [self.task, child])
        workflow.save_evidence(self.root, self.task, saved)
        with patch.object(gw, 'github_origin', return_value='o/r'), \
             patch.object(github, 'find_pr', return_value=9), \
             patch.object(github, 'threads', return_value=[]):
            for change in ({'headRefOid': 'f' * 40}, {'baseRefName': 'other'},
                           {'mergedAt': None}, {'mergeCommit': None}):
                with patch.object(github, 'view', side_effect=[opened, {**merged, **change}]):
                    with self.assertRaisesRegex(AgentBoxError, 'identity'):
                        workflow.landing(args, self.root, self.config, adapter)
            adapter.set_state.assert_not_called()
            with patch.object(github, 'view', side_effect=[opened, merged, merged]):
                result = workflow.landing(args, self.root, self.config, adapter)
        self.assertEqual(result['state'], 'Done')
        for kind in ('local', 'remote'):
            self.assertIn('current branch', result['cleanup'][kind]['reason'])
            self.assertEqual(self.descendant_outcomes(result['cleanup'])['8'][kind]['status'], 'deleted')
        adapter.set_state.assert_called_once_with(self.task, 'Done')
        self.task.state = 'Done'
        adapter.set_state.reset_mock()
        gw.git(self.root, 'switch', 'main')
        self.assertEqual(self.dispatch_land('cleanup')['cleanup']['local']['status'], 'deleted')

    def test_github_mismatched_or_missing_merge_identity_preserves_branches(self):
        pr = self.github_proof()
        for key, value in (('headRefOid', 'f' * 40), ('baseRefName', 'other'),
                           ('headRefName', 'other'), ('mergedAt', None), ('mergeCommit', {})):
            original = pr[key]
            pr[key] = value
            result = self.run_cleanup()
            self.assertEqual(result['local']['status'], 'error', key)
            self.assertEqual(result['remote']['status'], 'error', key)
            pr[key] = original
        self.assertTrue(self.exists())
        self.assertTrue(self.exists(remote=True))

    def test_github_ambiguous_prs_or_disabled_integration_preserve_branches(self):
        self.github_proof()
        with patch.object(github, 'gh', return_value=[{'number': 9, 'state': 'MERGED'},
                                                    {'number': 10, 'state': 'OPEN'}]):
            self.assertIn('ambiguous', self.run_cleanup()['local']['reason'])
        self.config.github.gh_token = False
        with patch.object(github, 'gh') as gh:
            self.assertIn('Enable GitHub', self.run_cleanup()['remote']['reason'])
        gh.assert_not_called()
        self.assertTrue(self.exists(remote=True))

    def test_done_retry_and_reconcile_bypass_resolution_after_all_refs_are_absent(self):
        self.integrated()
        self.run_cleanup()
        gw.git(self.root, 'update-ref', '-d', 'refs/remotes/origin/' + self.branch)
        adapter = Mock()
        adapter.get.return_value = self.task
        with patch.object(workflow, 'effective_config', return_value=self.config), \
             patch.object(workflow, 'repository_root', return_value=self.root), \
             patch.object(Binding, 'load', return_value=BINDING), \
             patch.object(workflow, 'make_adapter', return_value=adapter), \
             patch.object(workflow, 'resolve', side_effect=AssertionError('must not resolve absent branch')), \
             patch.object(github, 'gh') as gh:
            for action in ('cleanup', 'reconcile'):
                result = workflow.dispatch(workflow.parser().parse_args(['land', '7', action]))
                self.assertEqual(result['cleanup']['local']['status'], 'absent')
                self.assertNotIn('merge', result)
        adapter.set_state.assert_not_called()
        gh.assert_not_called()

    def test_retry_refuses_non_done_or_child_tasks_without_deletion(self):
        for state, parent in (('Review', None), ('Done', '6')):
            self.task.state, self.task.parent = state, parent
            with self.assertRaisesRegex(AgentBoxError, 'Done parent'):
                self.dispatch_land('cleanup')
        self.assertTrue(self.exists())

    def test_cleanup_runs_only_after_tracker_success_and_failure_preserves_done_result(self):
        child, _ = self.descendant('8')
        self.task.state = 'Review'
        gw.git(self.root, 'switch', self.branch)
        adapter = self.adapter
        adapter.identity.return_value = {'id': 1}
        self.config.workflow.review.policy = 'off'
        saved = {'scope': workflow.scope(self.root, 'main', [self.task, child]), 'validation': {'exit': 0}}
        workflow.save_evidence(self.root, self.task, saved)
        args = SimpleNamespace(command='land', task=self.task.id, action='merge', method=None)
        adapter.set_state.side_effect = AgentBoxError('tracker unavailable')
        with patch.object(cleanup, 'cleanup') as clean:
            with self.assertRaisesRegex(AgentBoxError, 'tracker unavailable'):
                workflow.landing(args, self.root, self.config, adapter)
        clean.assert_not_called()
        self.assertTrue(self.exists())
        adapter.set_state.side_effect = None
        args.action = 'reconcile'
        with patch.object(cleanup, '_local_refs', side_effect=RuntimeError('unexpected failure')):
            result = workflow.landing(args, self.root, self.config, adapter)
        self.assertEqual(result['state'], 'Done')
        self.assertEqual(result['merge']['head'], self.head)
        self.assertEqual(result['cleanup']['local']['status'], 'error')
        self.assertIn('unexpected failure', result['cleanup']['local']['reason'])
        result = workflow.landing(args, self.root, self.config, adapter)
        self.assertEqual(result['cleanup']['local']['status'], 'deleted')
        self.assertEqual(self.descendant_outcomes(result['cleanup'])['8']['local']['status'], 'deleted')

    def test_local_landing_discovers_child_attached_at_done_despite_stale_snapshot(self):
        """Adapters return new snapshots; a late Done child must not escape cleanup."""
        self.task.state = 'Review'
        fresh = replace(self.task, state='Done', children=[])
        child, child_branch = self.descendant('8', parent=fresh)
        self.adapter.get.side_effect = lambda task_id: copy.deepcopy(self.tasks[task_id])

        def set_done(task, state):
            self.assertEqual((task.id, state), (self.task.id, 'Done'))
            self.tasks[self.task.id] = fresh
            return copy.deepcopy(fresh)

        self.adapter.set_state.side_effect = set_done
        gw.git(self.root, 'switch', self.branch)
        self.adapter.identity.return_value = {'id': 1}
        self.config.workflow.review.policy = 'off'
        saved = {'scope': workflow.scope(self.root, 'main', [self.task]), 'validation': {'exit': 0}}
        workflow.save_evidence(self.root, self.task, saved)
        args = SimpleNamespace(command='land', task=self.task.id, action='merge', method=None)
        original = cleanup.cleanup

        def after_state(*args, **kwargs):
            self.adapter.set_state.assert_called_once_with(self.task, 'Done')
            return original(*args, **kwargs)

        with patch.object(cleanup, 'cleanup', side_effect=after_state):
            result = workflow.landing(args, self.root, self.config, self.adapter)
        self.assertEqual(result['state'], 'Done')
        self.assertEqual(self.task.state, 'Review')
        self.assertEqual(self.task.children, [])
        self.assertEqual(set(self.descendant_outcomes(result['cleanup'])), {child.id})
        for item in [result['cleanup'], *result['cleanup']['descendants']]:
            self.assertEqual([item[k]['status'] for k in ('local', 'remote')], ['deleted', 'deleted'])
        for root in (self.root, self.origin):
            self.assertNotEqual(gw._run(['git', 'show-ref', '--verify', '--quiet',
                                        'refs/heads/' + child_branch], cwd=root).returncode, 0)


if __name__ == '__main__':
    unittest.main()

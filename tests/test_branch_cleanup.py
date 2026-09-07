"""Deletion is conditional on live integration proof, never on tracker Done alone."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from agent_box import branch_cleanup as cleanup
from agent_box import git_workflow as gw
from agent_box import github_workflow as github
from agent_box import local_workflow, workflow, workflow_state
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
        return cleanup.cleanup(self.root, self.config, self.task, **kwargs)

    def exists(self, *, remote=False):
        root = self.origin if remote else self.root
        return gw._run(['git', 'show-ref', '--verify', '--quiet', self.ref], cwd=root).returncode == 0

    def github_proof(self):
        self.config.workflow.merge_mode = 'github'
        self.config.github.gh_token = True
        pr = {'number': 9, 'headRefName': self.branch, 'headRefOid': self.head,
              'baseRefName': 'main', 'mergedAt': '2026-09-08T12:00:00Z',
              'mergeCommit': {'oid': gw.head_sha(self.root)}, 'state': 'MERGED'}
        for target in (patch.object(gw, 'github_repository', return_value='o/r'),
                       patch.object(github, 'gh', return_value=[{'number': 9, 'state': 'MERGED'}]),
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

    def test_local_merge_deletes_only_parent_and_retry_handles_absence(self):
        self.integrated()
        gw.git(self.root, 'branch', 'ab/8-child', self.head)
        with patch.object(github, 'gh') as gh:
            result = self.run_cleanup()
            retry = self.run_cleanup()
        self.assertEqual([result[k]['status'] for k in ('local', 'remote')], ['deleted', 'deleted'])
        self.assertEqual([retry[k]['status'] for k in ('local', 'remote')], ['absent', 'absent'])
        self.assertEqual(gw.local_branch_head(self.root, 'ab/8-child'), self.head)
        gh.assert_not_called()

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
        merged = self.github_proof()
        merged['url'] = 'https://github.com/o/r/pull/9'
        opened = {**merged, 'state': 'OPEN', 'isDraft': False, 'mergeable': 'MERGEABLE',
                  'mergeStateStatus': 'CLEAN', 'latestReviews': [], 'statusCheckRollup': []}
        gw.git(self.root, 'switch', self.branch)
        self.task.state = 'Review'
        adapter = Mock()
        adapter.identity.return_value = {'id': 1}
        adapter.get.return_value = self.task
        args = SimpleNamespace(command='land', action='merge', method=None)
        info = {'branch': self.branch, 'base': 'main'}
        saved = {'validation': {'exit': 0}, 'review': {'verdict': 'PASS'}}
        with patch.object(gw, 'github_origin', return_value='o/r'), \
             patch.object(github, 'find_pr', return_value=9), \
             patch.object(github, 'threads', return_value=[]):
            for change in ({'headRefOid': 'f' * 40}, {'baseRefName': 'other'},
                           {'mergedAt': None}, {'mergeCommit': None}):
                with patch.object(github, 'view', side_effect=[opened, {**merged, **change}]):
                    with self.assertRaisesRegex(AgentBoxError, 'identity'):
                        github.operate(args, self.root, self.config, adapter, self.task, self.task, info, saved)
            adapter.set_state.assert_not_called()
            with patch.object(github, 'view', side_effect=[opened, merged]):
                result = github.operate(args, self.root, self.config, adapter, self.task, self.task, info, saved)
        self.assertEqual(result['state'], 'Done')
        for kind in ('local', 'remote'):
            self.assertIn('current branch', result['cleanup'][kind]['reason'])
        adapter.set_state.assert_called_once_with(self.task, 'Done')
        self.task.state = 'Done'
        gw.git(self.root, 'switch', 'main')
        self.assertEqual(cleanup.retry(self.root, self.config, self.task)['cleanup']['local']['status'], 'deleted')

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
                cleanup.retry(self.root, self.config, self.task)
        self.assertTrue(self.exists())

    def test_cleanup_runs_only_after_tracker_success_and_failure_preserves_done_result(self):
        self.task.state = 'Review'
        gw.git(self.root, 'switch', self.branch)
        adapter = Mock()
        adapter.get.return_value = self.task
        adapter.identity.return_value = {'id': 1}
        self.config.workflow.review.policy = 'off'
        saved = {'scope': workflow.scope(self.root, 'main', [self.task]), 'validation': {'exit': 0}}
        args = SimpleNamespace(command='land', action='merge', method=None)
        info = {'branch': self.branch, 'base': 'main'}
        adapter.set_state.side_effect = AgentBoxError('tracker unavailable')
        with patch.object(cleanup, 'cleanup') as clean:
            with self.assertRaisesRegex(AgentBoxError, 'tracker unavailable'):
                local_workflow.operate(args, self.root, self.config, adapter, self.task, self.task, info, saved)
        clean.assert_not_called()
        self.assertTrue(self.exists())
        adapter.set_state.side_effect = None
        args.action = 'reconcile'
        accepted = workflow.read_evidence(self.root, self.task)
        with patch.object(cleanup, '_local_refs', side_effect=RuntimeError('unexpected failure')):
            result = local_workflow.operate(args, self.root, self.config, adapter, self.task, self.task, info, accepted)
        self.assertEqual(result['state'], 'Done')
        self.assertEqual(result['merge']['head'], self.head)
        self.assertEqual(result['cleanup']['local']['status'], 'error')
        self.assertIn('unexpected failure', result['cleanup']['local']['reason'])
        result = local_workflow.operate(args, self.root, self.config, adapter, self.task, self.task, info, accepted)
        self.assertEqual(result['cleanup']['local']['status'], 'deleted')


if __name__ == '__main__':
    unittest.main()

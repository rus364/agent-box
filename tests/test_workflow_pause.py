"""Stopping work preserves Git refs and never decides a task's lifecycle for its owner."""
from pathlib import Path
from unittest.mock import patch

from agent_box import branch_cleanup, git_workflow as gw, workflow, workflow_pause as pause
from agent_box.config import Config
from agent_box.errors import AgentBoxError
from agent_box.tracker.core import Task
from workflow_fixtures import WorkflowCase, clone, origin_path, accepted_evidence


class PauseTests(WorkflowCase):
    def setUp(self):
        super().setUp()
        self.config.workflow.sync.policy = 'off'
        self.branch = gw.current_branch(self.root)
        gw.git(self.root, 'commit', '--allow-empty', '-m', 'unfinished work')
        self.head = gw.head_sha(self.root)
        self.origin = origin_path(self.directory.name)
        self.adapter.set_state.side_effect = self.set_state

    def set_state(self, task, state):
        task.state = state
        return task

    def stop(self, *flags, task='1'):
        return self.run_workflow('pause', task, '--reason', 'Independent network unavailable',
                                 '--resume-when', 'A working route can be tested', *flags)

    def test_local_pause_preserves_code_evidence_and_lifecycle(self):
        """Pausing is preservation, even when a dependency is open; it must not defer or block the task."""
        saved = accepted_evidence(self.root, self.task, review={'verdict': 'PASS'})
        self.task.blockers = ['unknown-external-task']
        for state in ('In progress', 'In review', 'Backlog'):
            self.task.state = state
            result = self.stop('--no-push')
            self.assertEqual(result['preservation'], 'local-only')
            self.assertEqual(result['state'], state)
            self.assertEqual(gw.current_branch(self.root), 'main')
            self.assertEqual(gw.local_branch_head(self.root, self.branch), self.head)
        self.assertEqual(workflow.read_evidence(self.root, self.task), saved)
        self.assertEqual(self.task.blockers, ['unknown-external-task'])
        self.assertEqual(self.task.assignees, ['1'])
        self.adapter.set_state.assert_not_called()
        self.adapter.update.assert_not_called()
        self.adapter.relate.assert_not_called()

    def test_explicit_checkpoint_publishes_even_in_local_delivery_mode(self):
        """Checkpoint opt-in is independent of landed-base publication and ignores ambient mirror/tag pushes."""
        self.config.workflow.merge_mode = 'local'
        self.config.workflow.auto_push = False
        gw.git(self.root, 'config', 'remote.origin.mirror', 'true')
        gw.git(self.root, 'config', 'push.followTags', 'true')
        gw.git(self.root, 'tag', '-a', 'private', '-m', 'not part of checkpoint')
        result = self.stop('--push')
        self.assertEqual(result['preservation'], 'remote-verified')
        self.assertEqual(gw.remote_branches(self.root, self.branch)[self.branch], self.head)
        self.assertEqual(gw.git(self.origin, 'tag', '--list'), '')
        self.assertNotEqual(gw.local_branch_head(self.origin, 'main'), self.head)
        self.adapter.set_state.assert_not_called()

    def test_one_pause_can_disable_configured_checkpoint_publication(self):
        """The opt-out must win over persisted policy and not push unrelated unfinished code."""
        self.config.workflow.pause_push = True
        self.stop('--no-push')
        self.assertEqual(gw.remote_branches(self.root, self.branch), {})

    def test_configured_checkpoint_is_honoured_without_a_flag(self):
        """Explicit project/user opt-in must survive the dispatcher and reach actual publication."""
        self.config.workflow.pause_push = True
        self.assertEqual(self.stop()['preservation'], 'remote-verified')

    def test_dirty_tree_stops_before_any_publication_or_journal(self):
        """Untracked and edited files cannot be silently left behind or committed by pause."""
        (self.root / 'unsaved').write_text('keep me')
        with self.assertRaisesRegex(AgentBoxError, 'clean working tree'):
            self.stop('--push')
        self.assertEqual(gw.current_branch(self.root), self.branch)
        self.assertEqual((self.root / 'unsaved').read_text(), 'keep me')
        self.assertEqual(gw.remote_branches(self.root, self.branch), {})
        self.adapter.comment.assert_not_called()

    def test_foreign_owner_and_ambiguous_branches_are_never_adopted(self):
        """Preserving a task does not authorize taking another person's work or guessing its branch."""
        self.task.assignees = ['someone-else']
        with self.assertRaisesRegex(AgentBoxError, 'assigned only'):
            self.stop('--push')
        self.task.assignees = ['1']
        gw.git(self.root, 'branch', 'ab/1-other')
        with self.assertRaisesRegex(AgentBoxError, 'Several local'):
            self.stop('--push')
        self.adapter.comment.assert_not_called()

    def test_unavailable_origin_refuses_push_but_allows_explicit_local_preservation(self):
        """Offline pause can retain existing local work, but must never report a remote backup."""
        gw.git(self.root, 'remote', 'set-url', 'origin', str(Path(self.directory.name) / 'missing.git'))
        with self.assertRaises(AgentBoxError):
            self.stop('--push')
        self.assertEqual(gw.current_branch(self.root), self.branch)
        result = self.stop('--no-push')
        self.assertEqual(result['preservation'], 'local-only')
        self.assertTrue(result['warnings'])
        self.assertEqual(gw.local_branch_head(self.root, self.branch), self.head)

    def test_remote_ahead_or_diverged_is_preserved_without_force(self):
        """An unseen commit from another checkout must stop publication, even if the task is ours."""
        other = clone(self.directory.name)
        gw.git(other, 'switch', '-c', self.branch)
        gw.git(other, 'commit', '--allow-empty', '-m', 'someone continued work')
        remote = gw.head_sha(other)
        gw.git(other, 'push', 'origin', self.branch)
        with self.assertRaisesRegex(AgentBoxError, 'ahead, diverged or not fetched'):
            self.stop('--push')
        self.assertEqual(gw.local_branch_head(self.origin, self.branch), remote)
        self.assertEqual(gw.local_branch_head(self.root, self.branch), self.head)
        self.assertEqual(gw.current_branch(self.root), self.branch)

    def test_push_with_unknown_outcome_can_be_retried_without_overwriting(self):
        """A lost push reply can leave a valid checkpoint; retry reads its real head instead of forcing."""
        real_git = gw.git
        def lost_reply(root, *args, **kwargs):
            value = real_git(root, *args, **kwargs)
            if 'push' in args:
                raise AgentBoxError('lost push reply')
            return value
        with patch.object(gw, 'git', side_effect=lost_reply):
            with self.assertRaisesRegex(AgentBoxError, 'push may have succeeded'):
                self.stop('--push')
        self.assertEqual(gw.current_branch(self.root), self.branch)
        self.assertEqual(gw.local_branch_head(self.origin, self.branch), self.head)
        self.assertEqual(self.stop('--push')['preservation'], 'remote-verified')
        self.adapter.set_state.assert_not_called()

    def test_journal_failure_after_switch_keeps_checkpoint_and_allows_retry(self):
        """Tracker failure must not erase work or undo a successful remote checkpoint."""
        self.adapter.comment.side_effect = AgentBoxError('tracker unavailable')
        with self.assertRaisesRegex(AgentBoxError, 'Remote checkpoint was verified'):
            self.stop('--push')
        self.assertEqual(gw.current_branch(self.root), 'main')
        self.assertEqual(gw.local_branch_head(self.origin, self.branch), self.head)
        self.adapter.comment.side_effect = None
        self.assertEqual(self.stop('--push')['preservation'], 'remote-verified')
        self.adapter.set_state.assert_not_called()

    def test_local_head_moving_during_push_stops_the_switch(self):
        """The remote snapshot is not permission to abandon a newer local commit."""
        real_verify = pause.verify_remote
        def moved(*args):
            real_verify(*args)
            gw.git(self.root, 'commit', '--allow-empty', '-m', 'concurrent new work')
        with patch.object(pause, 'verify_remote', side_effect=moved):
            with self.assertRaisesRegex(AgentBoxError, 'moved or disappeared'):
                self.stop('--push')
        self.assertEqual(gw.current_branch(self.root), self.branch)
        self.assertNotEqual(gw.local_branch_head(self.root, self.branch), self.head)
        self.adapter.comment.assert_not_called()

    def test_remote_checkpoint_must_match_after_a_successful_push(self):
        """A push exit code cannot certify a remote branch that changed before verification."""
        real_git = gw.git
        def moved_remote(root, *args, **kwargs):
            value = real_git(root, *args, **kwargs)
            if 'push' in args:
                real_git(self.origin, 'update-ref', 'refs/heads/' + self.branch,
                         gw.local_branch_head(self.origin, 'main'))
            return value
        with patch.object(gw, 'git', side_effect=moved_remote):
            with self.assertRaisesRegex(AgentBoxError, 'checkpoint could not be verified'):
                self.stop('--push')
        self.assertEqual(gw.current_branch(self.root), self.branch)
        self.assertEqual(gw.local_branch_head(self.root, self.branch), self.head)

    def test_occupied_base_refuses_before_publishing(self):
        """Pause must not force another worktree off the base or push before discovering the conflict."""
        gw.git(self.root, 'worktree', 'add', str(Path(self.directory.name) / 'other'), 'main')
        with self.assertRaisesRegex(AgentBoxError, 'occupied'):
            self.stop('--push')
        self.assertEqual(gw.remote_branches(self.root, self.branch), {})
        self.assertEqual(gw.current_branch(self.root), self.branch)

    def test_existing_only_switch_does_not_fall_back_to_a_stale_tracking_ref(self):
        """A disappearing local head must not be replaced with an older cached remote head."""
        gw.git(self.root, 'update-ref', 'refs/remotes/origin/' + self.branch,
               gw.local_branch_head(self.root, 'main'))
        gw.git(self.root, 'switch', 'main')
        gw.git(self.root, 'branch', '-D', self.branch)
        with self.assertRaisesRegex(AgentBoxError, 'disappeared'):
            gw.switch_branch(self.root, self.branch, 'main', resume=True, existing_only=True)
        self.assertEqual(gw.task_branches(self.root, '1'), [])

    def test_paused_unmerged_work_survives_even_an_incorrect_done_cleanup(self):
        """The lifecycle field cannot authorize removing commits that main lacks."""
        self.stop('--no-push')
        self.task.state = 'Done'
        self.config.workflow.merge_mode = 'local'
        self.config.workflow.cleanup_local_branches = True
        result = branch_cleanup._cleanup_branch(self.root, self.config, self.task, base='main')
        self.assertEqual(result['local']['status'], 'error')
        self.assertEqual(gw.local_branch_head(self.root, self.branch), self.head)

    def test_resume_keeps_the_same_branch_and_existing_acceptance(self):
        """An unchanged checkpoint retains its evidence; resume itself must not bless a changed scope."""
        saved = accepted_evidence(self.root, self.task, review={'verdict': 'PASS'})
        self.stop('--push')
        result = self.run_workflow('resume', '1')
        self.assertEqual(result['branch'], self.branch)
        self.assertEqual(gw.head_sha(self.root), self.head)
        self.assertEqual(workflow.read_evidence(self.root, self.task), saved)

    def test_resume_adopts_a_live_remote_branch_in_a_fresh_clone(self):
        """A checkpoint is useful on another machine without a preexisting tracking ref."""
        self.stop('--push')
        other = clone(self.directory.name)
        gw.git(other, 'update-ref', '-d', 'refs/remotes/origin/' + self.branch)
        result = pause.resume(other, self.adapter, self.task)
        self.assertEqual(result['branch'], self.branch)
        self.assertEqual(gw.head_sha(other), self.head)

    def test_resume_never_recreates_a_lost_branch_from_main(self):
        """Missing work must be recovered explicitly, not replaced by an empty branch with a familiar name."""
        gw.git(self.root, 'switch', 'main')
        gw.git(self.root, 'branch', '-D', self.branch)
        with self.assertRaisesRegex(AgentBoxError, 'missing or ambiguous'):
            self.run_workflow('resume', '1')
        self.assertEqual(gw.task_branches(self.root, '1'), [])
        self.adapter.set_state.assert_not_called()

    def test_stale_tracking_ref_cannot_resurrect_a_deleted_checkpoint(self):
        """A cached origin ref is not proof that the expected remote work still exists."""
        self.stop('--push')
        gw.git(self.root, 'update-ref', 'refs/remotes/origin/' + self.branch, self.head)
        gw.git(self.root, 'branch', '-D', self.branch)
        gw.git(self.origin, 'update-ref', '-d', 'refs/heads/' + self.branch)
        with self.assertRaisesRegex(AgentBoxError, 'stale tracking'):
            self.run_workflow('resume', '1')
        self.assertEqual(gw.current_branch(self.root), 'main')

    def test_backlog_resume_requires_the_users_ready_decision(self):
        """Resume may not undo a deliberate deferral or silently unblock a task."""
        self.task.state = 'Backlog'
        self.stop('--no-push')
        with self.assertRaisesRegex(AgentBoxError, 'Ask the user'):
            self.run_workflow('resume', '1')
        self.assertEqual(self.task.state, 'Backlog')
        self.adapter.set_state.assert_not_called()

    def test_child_checkpoint_preserves_both_child_and_parent_refs(self):
        """A child checkpoint needs its parent's name as well as the objects in child history."""
        child = Task('2', '1', 'Child', parent='1', state='In progress', assignees=['1'])
        tasks = {'1': self.task, '2': child}
        self.adapter.get.side_effect = tasks.__getitem__
        gw.git(self.root, 'switch', '-c', 'ab/2-child')
        gw.git(self.root, 'commit', '--allow-empty', '-m', 'unfinished child')
        head = gw.head_sha(self.root)
        result = self.stop('--push', task='2')
        self.assertEqual(len(result['branches']), 2)
        self.assertEqual(gw.local_branch_head(self.origin, self.branch), self.head)
        self.assertEqual(gw.local_branch_head(self.origin, 'ab/2-child'), head)
        resumed = self.run_workflow('resume', '2')
        self.assertEqual(resumed['branch'], 'ab/2-child')
        self.assertEqual(gw.head_sha(self.root), head)


class PauseConfigTests(WorkflowCase):
    def test_omitted_checkpoint_option_remains_off_across_config_roundtrips(self):
        """Existing auto_push opt-in must not silently start publishing unfinished branches."""
        for flag in (False, True):
            config = Config.from_dict({'workflow': {'auto_push': flag}})
            self.assertFalse(config.workflow.pause_push)
            config.workflow.pause_push = True
            self.assertTrue(Config.from_dict(config.to_dict()).workflow.pause_push)

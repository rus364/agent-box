"""Maintaining many tasks at once: closing unbuilt work, checking items, marking Done.

Closing tasks the workflow never implemented used to need the provider's own
bulk-update extension, and every checklist item cost its own description write.
These tests protect the replacements together with the refusal that keeps the
shortcut honest: a task branch holding commits nothing has integrated is
unfinished work, not a card to close by hand.
"""
import tempfile
import unittest
from unittest.mock import patch

from agent_box import git_workflow as gw
from agent_box.repo_binding import Binding
from agent_box.tracker import cli
from agent_box.tracker.core import NORMALIZED_LABEL, TrackerError, checkboxes
from agent_box.tracker.vikunja import VikunjaAdapter
from test_tracker_normalization import NormalizationClient
from workflow_fixtures import clone, origin_path, repository


class MaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.client = NormalizationClient()
        self.binding = Binding('vikunja', self.client.url, '1', True)
        self.adapter = VikunjaAdapter(self.client.url, 'token', self.binding, self.client)
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = repository(self.directory.name)
        context = patch.object(cli, 'context', return_value=(self.adapter, self.binding, self.root))
        context.start()
        self.addCleanup(context.stop)

    def run_cli(self, *args):
        return cli.dispatch(cli.parser().parse_args(args))

    def commit_on(self, branch):
        gw.git(self.root, 'switch', '-c', branch)
        (self.root / branch.replace('/', '-')).write_text('work')
        gw.git(self.root, 'add', '-A')
        gw.git(self.root, 'commit', '-m', 'work')
        gw.git(self.root, 'switch', 'main')

    def test_tasks_that_were_never_implemented_close_with_one_journalled_reason(self):
        """Closing decided-elsewhere cards must not need the provider's bulk extension."""
        closed = self.run_cli('state', '5', '8', 'Done', '--reason', 'Решено в задаче 12')
        self.assertEqual([(row['id'], row['state']) for row in closed], [('5', 'Done'), ('8', 'Done')])
        self.assertEqual([entry['comment'] for entry in self.client.journal[5]], ['Решено в задаче 12'])
        self.assertEqual([entry['comment'] for entry in self.client.journal[8]], ['Решено в задаче 12'])

    def test_closing_without_a_reason_still_sends_the_user_to_the_workflow(self):
        """The shortcut is for unbuilt work; implemented work still proves integration."""
        with self.assertRaisesRegex(TrackerError, 'workflow finish'):
            self.run_cli('state', '5', 'Done')
        self.assertEqual(self.client.writes, [])
        with self.assertRaisesRegex(TrackerError, 'belongs to Done'):
            self.run_cli('state', '5', 'Ready', '--reason', 'Не нужна')
        self.assertEqual(self.client.writes, [])

    def test_a_branch_whose_commits_nothing_contains_stops_the_whole_batch(self):
        """Closing that task by hand would abandon commits finish or land would move."""
        self.commit_on('ab/5-work')
        with self.assertRaisesRegex(TrackerError, 'ab/5-work.*workflow finish 5'):
            self.run_cli('state', '8', '5', 'Done', '--reason', 'Не нужна')
        self.assertEqual(self.client.writes, [])
        gw.git(self.root, 'merge', '--ff-only', 'ab/5-work')
        closed = self.run_cli('state', '8', '5', 'Done', '--reason', 'Не нужна')
        self.assertEqual([row['state'] for row in closed], ['Done', 'Done'])

    def test_a_branch_another_checkout_pushed_further_is_still_unintegrated(self):
        """The local tip does not speak for the branch; origin may hold newer commits."""
        self.commit_on("ab/5-work")
        gw.git(self.root, "push", "-q", "origin", "ab/5-work")
        gw.git(self.root, "merge", "--ff-only", "ab/5-work")
        gw.git(self.root, "switch", "ab/5-work")
        (self.root / "later.txt").write_text("later")
        gw.git(self.root, "add", "-A")
        gw.git(self.root, "commit", "-m", "later work")
        gw.git(self.root, "push", "-q", "origin", "ab/5-work")
        gw.git(self.root, "reset", "--hard", "HEAD~1")
        gw.git(self.root, "switch", "main")
        with self.assertRaisesRegex(TrackerError, "ab/5-work"):
            self.run_cli("state", "5", "Done", "--reason", "Не нужна")
        self.assertEqual(self.client.writes, [])
        gw.git(self.root, "merge", "--ff-only", "origin/ab/5-work")
        self.assertEqual(self.run_cli("state", "5", "Done", "--reason", "Не нужна")[0]["state"], "Done")

    def test_a_branch_only_origin_knows_about_stops_the_close_until_it_is_fetched(self):
        """Another checkout can create the branch; this one would never see it locally."""
        other = clone(self.directory.name, "other")
        gw.git(other, "switch", "-c", "ab/5-work")
        (other / "elsewhere.txt").write_text("work")
        gw.git(other, "add", "-A")
        gw.git(other, "commit", "-m", "work elsewhere")
        gw.git(other, "push", "-q", "origin", "ab/5-work")
        with self.assertRaisesRegex(TrackerError, "has not fetched"):
            self.run_cli("state", "5", "Done", "--reason", "Не нужна")
        gw.git(self.root, "fetch", "-q", "origin")
        with self.assertRaisesRegex(TrackerError, "workflow finish 5"):
            self.run_cli("state", "5", "Done", "--reason", "Не нужна")
        self.assertEqual(self.client.writes, [])
        gw.git(self.root, "merge", "--ff-only", "origin/ab/5-work")
        self.assertEqual(self.run_cli("state", "5", "Done", "--reason", "Не нужна")[0]["state"], "Done")

    def test_an_origin_that_cannot_be_read_stops_the_close(self):
        """Reading nothing from origin must not be read as origin holding nothing."""
        origin = str(origin_path(self.directory.name))
        for url in (origin, origin + ".mirror"):
            gw.git(self.root, "remote", "set-url", "--add", "--push", "origin", url)
        with self.assertRaisesRegex(TrackerError, "no single push destination"):
            self.run_cli("state", "5", "Done", "--reason", "Не нужна")
        self.assertEqual(self.client.writes, [])

    def test_a_base_another_checkout_advanced_asks_for_a_fetch_not_for_landing(self):
        """The work is landed; only this clone has not heard, and the remedy is a fetch."""
        self.commit_on("ab/5-work")
        gw.git(self.root, "push", "-q", "origin", "ab/5-work")
        other = clone(self.directory.name, "other")
        gw.git(other, "merge", "--no-ff", "-m", "land the work", "origin/ab/5-work")
        gw.git(other, "push", "-q", "origin", "main")
        with self.assertRaisesRegex(TrackerError, "has not fetched"):
            self.run_cli("state", "5", "Done", "--reason", "Не нужна")
        self.assertEqual(self.client.writes, [])
        gw.git(self.root, "fetch", "-q", "origin")
        self.assertEqual(self.run_cli("state", "5", "Done", "--reason", "Не нужна")[0]["state"], "Done")

    def test_work_the_parent_branch_already_carries_may_be_closed_by_hand(self):
        """A subtask branch outlives its integration in a clone; its commits are not lost."""
        self.client.tasks[8]['related_tasks'] = {'parenttask': [{'id': 5}]}
        gw.git(self.root, 'switch', '-c', 'ab/5-parent')
        gw.git(self.root, 'switch', '-c', 'ab/8-child')
        (self.root / 'child.txt').write_text('work')
        gw.git(self.root, 'add', '-A')
        gw.git(self.root, 'commit', '-m', 'child work')
        gw.git(self.root, 'switch', 'ab/5-parent')
        gw.git(self.root, 'merge', '--ff-only', 'ab/8-child')
        gw.git(self.root, 'switch', 'main')
        self.assertEqual(self.run_cli('state', '8', 'Done', '--reason', 'Вошла в родителя')[0]['state'], 'Done')
        with self.assertRaisesRegex(TrackerError, 'ab/5-parent'):
            self.run_cli('state', '5', 'Done', '--reason', 'Не нужна')

    def test_a_batch_moves_nothing_until_every_named_task_is_reachable(self):
        """Half a closed batch is worse than none: the user has to reconstruct what moved."""
        with self.assertRaisesRegex(TrackerError, "outside the repository binding: 7"):
            self.run_cli("state", "5", "7", "Done", "--reason", "Не нужна")
        self.assertEqual(self.client.writes, [])
        self.assertEqual(self.adapter.get("5").state, "Backlog")

    def test_the_reason_reaches_the_journal_before_the_task_closes(self):
        """A task closed without the reason it was closed for defeats the whole command."""
        with self.assertRaisesRegex(TrackerError, "must say something"):
            self.run_cli("state", "5", "Done", "--reason", "   ")
        self.assertEqual(self.client.writes, [])
        with patch.object(self.adapter, "set_state", side_effect=TrackerError("transport", "transport")):
            with self.assertRaises(TrackerError):
                self.run_cli("state", "5", "Done", "--reason", "Решено в задаче 12")
        self.assertEqual([entry["comment"] for entry in self.client.journal[5]], ["Решено в задаче 12"])
        self.assertEqual(self.adapter.get("5").state, "Backlog")

    def test_one_write_records_several_checklist_decisions_for_a_known_revision(self):
        """A per-item write raced with itself and quoted back what the revision proves."""
        self.client.tasks[5]['description'] = '## Приёмка\n- [ ] первый\n- [ ] второй\n- [ ] третий\n'
        self.client.revisions['/tasks/5'] = 'rev-1'
        listing = self.run_cli('checklist', '5')
        self.assertEqual(listing['revision'], 'rev-1')
        self.assertEqual([item['index'] for item in listing['items']], [1, 2, 3])
        updated = self.run_cli('check', '5', '1', '3', '--revision', 'rev-1')
        self.assertEqual([item['checked'] for item in checkboxes(updated.description)], [True, False, True])
        self.assertEqual([path for path, _ in self.client.writes], ['/tasks/5'])
        undone = self.run_cli('check', '5', '1', '--revision', 'rev-1', '--undo')
        self.assertEqual([item['checked'] for item in checkboxes(undone.description)], [False, False, True])

    def test_a_checklist_read_before_someone_else_edited_it_changes_nothing(self):
        """Without the item text quoted back, the revision is what proves the read is current."""
        self.client.tasks[5]['description'] = '- [ ] первый\n'
        self.client.revisions['/tasks/5'] = 'rev-2'
        with self.assertRaisesRegex(TrackerError, 'revision'):
            self.run_cli('check', '5', '1', '--revision', 'rev-1')
        with self.assertRaisesRegex(TrackerError, 'no item 2'):
            self.run_cli('check', '5', '1', '2', '--revision', 'rev-2')
        self.assertEqual(self.client.writes, [])

    def test_an_edit_needs_a_revision_the_provider_actually_returned(self):
        """Without an ETag the comparison would pass on two empty strings and prove nothing."""
        self.client.tasks[5]["description"] = "- [ ] первый\n"
        with self.assertRaisesRegex(TrackerError, "no revision"):
            self.run_cli("check", "5", "1", "--revision", "")
        self.assertEqual(self.client.writes, [])

    def test_naming_a_task_by_id_is_no_way_past_the_binding(self):
        """Every task write shares one boundary; a project outside it stays readable only."""
        self.client.tasks[7]["description"] = "- [ ] первый\n"
        self.client.revisions["/tasks/7"] = "rev-1"
        for command in (("check", "7", "1", "--revision", "rev-1"), ("normalized", "7"),
                        ("tier", "7", "2"), ("claim", "7")):
            with self.subTest(command=command[0]):
                with self.assertRaisesRegex(TrackerError, "outside the repository binding"):
                    self.run_cli(*command)
        self.assertEqual(self.client.writes, [])
        with self.assertRaisesRegex(TrackerError, "inside the repository binding"):
            self.run_cli("create", "--project", "9", "--title", "Outside", "--description-file", "/dev/null")
        self.assertEqual(self.client.writes, [])

    def test_completed_tasks_are_marked_processed_in_one_call(self):
        """A Done task has nothing left to normalize, so a pass should not read each one."""
        for ident in (5, 6, 8):
            self.client.tasks[ident]['done'] = True
        marked = self.run_cli('normalized', '--done')
        self.assertEqual([row['id'] for row in marked], ['5', '8'])
        self.assertTrue(all(NORMALIZED_LABEL in row['labels'] for row in marked))
        self.assertEqual(self.run_cli('normalized', '--done'), [])
        with self.assertRaisesRegex(TrackerError, "pass --done"):
            self.run_cli("normalized", "5", "--done")

    def test_recursion_searches_inside_the_binding_and_never_widens_it(self):
        """A binding that stops at its root must not label tasks in child projects."""
        self.binding.recursive = False
        self.adapter._scope = None
        for ident in (5, 8):
            self.client.tasks[ident]["done"] = True
        marked = self.run_cli("normalized", "--done", "--recursive")
        self.assertEqual([row["id"] for row in marked], ["5"])
        self.assertNotIn(NORMALIZED_LABEL, self.adapter.get("8").labels)

    def test_a_project_named_outside_the_binding_is_not_marked(self):
        """This command writes to every task it selects, so its scope is the binding's."""
        self.client.tasks[7]["done"] = True
        with self.assertRaisesRegex(TrackerError, "outside the repository binding"):
            self.run_cli("normalized", "--done", "--project", "9")
        self.assertEqual(self.client.writes, [])


if __name__ == '__main__':
    unittest.main()

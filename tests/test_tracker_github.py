"""GitHub Projects read as the portable contract: lifecycle, relations, references and errors."""
import io
import json
import unittest
import urllib.error
from unittest.mock import patch

from agent_box.config import Config
from agent_box.repo_binding import Binding
from agent_box.tracker import github_api as api
from agent_box.tracker.core import TrackerError
from agent_box.tracker.github import GitHubAdapter, repository_from_url
from github_fixtures import REPO, adapter, issue, project


class LifecycleReadTests(unittest.TestCase):
    def state(self, raw, **options):
        tracker, _ = adapter([raw], **options)
        task = tracker.get(str(raw["number"]))
        return task.state, task.state_reason

    def test_closed_issue_is_done_whatever_its_column_says(self):
        """GitHub keeps issue state and Status apart; closing is what finishes work."""
        self.assertEqual(self.state(issue(1, status="Ready", state="CLOSED", reason="COMPLETED")), ("Done", None))
        self.assertEqual(self.state(issue(1, state="CLOSED", reason="NOT_PLANNED")),
                         ("Done", "closed as not planned"))
        self.assertEqual(self.state(issue(1, state="CLOSED", reason="DUPLICATE", duplicate_of=9)),
                         ("Done", "closed as a duplicate of 9"))

    def test_reopened_issue_left_in_done_is_unknown_not_done(self):
        """Reopening leaves the card in Done; reading it as Done would unblock dependants of open work."""
        state, reason = self.state(issue(1, status="Done"))
        self.assertEqual(state, "Unknown")
        self.assertIn("open but its Status is Done", reason)

    def test_open_issue_reads_its_status_option_ignoring_case(self):
        board = project(status=("backlog", "Ready", "IN PROGRESS", "In review", "Done"))
        self.assertEqual(self.state(issue(1, status="IN PROGRESS"), projects={2: board}), ("In progress", None))

    def test_unmapped_missing_or_absent_status_names_the_reason(self):
        board = project(status=("Todo", "Backlog", "Ready", "In progress", "In review", "Done"))
        self.assertEqual(self.state(issue(1, status="Todo"), projects={2: board}),
                         ("Unknown", "Status 'Todo' maps to no lifecycle state"))
        self.assertEqual(self.state(issue(1, on=[2])), ("Unknown", "issue has no Status on the project"))
        self.assertEqual(self.state(issue(1)), ("Unknown", "issue is not on the project"))

    def test_board_without_every_state_cannot_be_written_but_still_reads(self):
        board = project(status=("Todo", "In Progress", "Done"))
        tracker, _ = adapter([issue(1, status="Todo")], projects={2: board})
        report = tracker.lifecycle_report()
        self.assertEqual(report[0]["problems"], ["no Status option for Backlog, Ready, In review"])
        self.assertEqual(tracker.get("1").state, "Unknown")
        with self.assertRaises(TrackerError) as raised:
            tracker.board("octo/2")
        self.assertEqual(raised.exception.category, "configuration")


class TaskShapeTests(unittest.TestCase):
    def test_relations_use_numbers_here_and_full_references_elsewhere(self):
        """A number is the canonical ID only inside this repository; elsewhere it names another issue."""
        tracker, _ = adapter([issue(5, status="Ready", parent=1, children=[6, ("other/lib", 6)],
                                    blocked_by=[("other/lib", 2)], related=[7])])
        task = tracker.get("5")
        self.assertEqual((task.id, task.number, task.project), ("5", 5, "octo/2"))
        self.assertEqual((task.parent, task.children, task.blockers, task.related),
                         ("1", ["6", "other/lib#6"], ["other/lib#2"], ["7"]))

    def test_other_repository_issue_is_readable_but_outside_the_binding(self):
        tracker, _ = adapter([issue(2, repo="other/lib", status="Ready")])
        task = tracker.get("other/lib#2")
        self.assertEqual((task.id, task.project, task.state), ("other/lib#2", "", "Ready"))

    def test_priority_comes_from_named_options_and_p_scale_reads_unset(self):
        """P0-P2 mean different things on different boards; only the agreed names carry a value."""
        tracker, _ = adapter([issue(1, status="Ready", priority="Urgent"), issue(2, status="Ready", priority="Low"),
                              issue(3, status="Ready")])
        self.assertEqual([tracker.get(n).priority for n in ("1", "2", "3")], [4, 1, 0])
        legacy, _ = adapter([issue(1, status="Ready", priority="P0")], projects={2: project(priority=("P0", "P1"))})
        self.assertEqual(legacy.get("1").priority, 0)

    def test_revision_moves_with_the_issue_and_with_its_card(self):
        """A Status change does not touch the issue's own timestamp, so the card's must count too."""
        first, _ = adapter([issue(1, status="Ready")])
        moved, _ = adapter([issue(1, status="Ready", item_updated="2026-02-02T00:00:00Z")])
        self.assertNotEqual(first.get("1").revision, moved.get("1").revision)

    def test_long_lists_are_read_to_their_end(self):
        """A list read in part would make a blocker, a child or the bound card silently vanish."""
        tracker, client = adapter([issue(1, status="Ready", priority="High", blocked_by=[2, 3, 4], children=[5, 6, 7],
                                         labels=["a", "b", "c"], on=[9, 2])],
                                  projects={2: project(), 9: project(number=9)}, list_page=2)
        task = tracker.get("1")
        self.assertEqual((task.blockers, task.children, task.labels), (["2", "3", "4"], ["5", "6", "7"], ["a", "b", "c"]))
        self.assertEqual((task.state, task.priority), ("Ready", 3))
        self.assertIn("IssueBlockedBy", [operation for operation, _ in client.operations])


class ReferenceTests(unittest.TestCase):
    def setUp(self):
        self.tracker, _ = adapter([issue(7, status="Ready", title="Seven"), issue(8, title="Off board"),
                                   issue(9, status="Ready", title="Twin"), issue(10, status="Backlog", title="Twin"),
                                   issue(3, repo="other/lib", status="Ready")])

    def resolve(self, reference, **options):
        return self.tracker.resolve(reference, "octo/2", **options)

    def test_number_hash_url_and_title_name_the_same_issue(self):
        for reference in ("7", "#7", "https://github.com/octo/app/issues/7", "Seven"):
            with self.subTest(reference=reference):
                self.assertEqual(self.resolve(reference).id, "7")

    def test_bare_number_is_never_ambiguous_with_a_canonical_id(self):
        """IDs are the visible numbers, so tracker get needs no #N to disambiguate."""
        self.assertEqual(self.resolve("7", canonical=True).id, "7")

    def test_canonical_id_of_an_issue_off_the_board_still_reads(self):
        """A blocker off the board is returned as ID 8; tracker get 8 must then read it."""
        self.assertEqual(self.resolve("8", canonical=True).project, "")
        with self.assertRaises(TrackerError):
            self.resolve("#8", canonical=True)

    def test_other_repository_reads_by_canonical_id_but_not_by_url(self):
        self.assertEqual(self.resolve("other/lib#3").id, "other/lib#3")
        with self.assertRaisesRegex(TrackerError, "outside the selected project scope"):
            self.resolve("https://github.com/other/lib/issues/3")

    def test_issue_off_the_board_or_unknown_is_not_found(self):
        for reference in ("8", "#404"):
            with self.subTest(reference=reference), self.assertRaises(TrackerError) as raised:
                self.resolve(reference)
            self.assertEqual(raised.exception.category, "not-found")

    def test_duplicate_title_and_foreign_host_are_refused(self):
        with self.assertRaisesRegex(TrackerError, "Ambiguous"):
            self.resolve("Twin")
        with self.assertRaisesRegex(TrackerError, "issue URL from github.com"):
            self.resolve("https://example.com/octo/app/issues/7")

    def test_projects_are_named_by_owner_and_number_or_url(self):
        for reference in ("octo/2", "https://github.com/users/octo/projects/2/views/1", "https://github.com/orgs/octo/projects/2"):
            with self.subTest(reference=reference):
                self.assertEqual(self.tracker.project(reference)["id"], "octo/2")
        with self.assertRaisesRegex(TrackerError, "owner/number"):
            self.tracker.project("2")


class SearchTests(unittest.TestCase):
    def test_search_returns_this_repositorys_issues_only(self):
        """Drafts and pull requests are not tasks; other repositories' issues are not ours to pick up."""
        tracker, _ = adapter([issue(1, status="Ready", body="mentions widget"), issue(2, status="Ready"),
                              issue(3, repo="other/lib", status="Ready", title="widget")], drafts=2, pulls=1)
        self.assertEqual([t.id for t in tracker.search("octo/2")], ["1", "2"])
        self.assertEqual([t.id for t in tracker.search("octo/2", "WIDGET")], ["1"])

    def test_open_only_drops_closed_issues_on_the_server_and_every_page_is_read(self):
        issues = [issue(n, status="Ready") for n in range(1, 8)] + [issue(8, status="Done", state="CLOSED")]
        tracker, client = adapter(issues, page=3)
        self.assertEqual(len(tracker.search("octo/2", open_only=True)), 7)
        self.assertEqual(len(tracker.search("octo/2")), 8)
        self.assertEqual([v["query"] for op, v in client.operations if op == "Items"][0], "is:open")

    def test_selected_project_supplies_the_columns_bound_or_not(self):
        """Searching another board must read that board's Status and Priority, not the bound one's."""
        issues = [issue(1, status="Ready", priority="High", on=[9])]
        boards = {2: project(), 9: project(number=9)}
        for bound in (None, "octo/2"):
            tracker, _ = adapter(issues, bound=bound, projects=boards)
            with self.subTest(bound=bound):
                found = tracker.search("octo/9")
                self.assertEqual([(t.project, t.state, t.priority) for t in found], [("octo/9", "Ready", 3)])

    def test_recursive_scope_is_refused(self):
        """GitHub projects have no children; quietly ignoring the flag would misreport the scope."""
        tracker, _ = adapter([])
        with self.assertRaisesRegex(TrackerError, "no subprojects"):
            tracker.search("octo/2", recursive=True)
        with self.assertRaisesRegex(TrackerError, "no subprojects"):
            GitHubAdapter("https://api.github.com", "t", Binding("github", "https://api.github.com", "octo/2", True))


class TransportTests(unittest.TestCase):
    def client(self, outcome):
        def opener(request, timeout):
            if isinstance(outcome, Exception):
                raise outcome
            body = io.BytesIO(json.dumps(outcome).encode())
            body.status = 200
            return body
        return api.GitHubClient(api.DEFAULT_API, "secret-token", opener=opener)

    def http_error(self, code, message, headers=None):
        return urllib.error.HTTPError("https://api.github.com/graphql", code, message, headers or {},
                                      io.BytesIO(json.dumps({"message": message}).encode()))

    def test_errors_are_categorized_for_the_command_that_made_them(self):
        cases = [(self.http_error(401, "Bad credentials"), "query Viewer {}", "authentication", False),
                 (self.http_error(403, "API rate limit exceeded", {"x-ratelimit-remaining": "0"}), "query Viewer {}",
                  "transport", True),
                 ({"errors": [{"type": "NOT_FOUND", "message": "Could not resolve"}]}, "query Issue {}", "not-found", False),
                 ({"errors": [{"type": "INSUFFICIENT_SCOPES", "message": "needs project"}]}, "query Project {}",
                  "authentication", False),
                 (OSError("timed out"), "mutation AddComment {}", "unknown-outcome", False),
                 (OSError("timed out"), "query Viewer {}", "transport", True)]
        for outcome, query, category, retryable in cases:
            with self.subTest(query=query, category=category), self.assertRaises(api.APIError) as raised:
                self.client(outcome).graphql(query)
            self.assertEqual((raised.exception.category, raised.exception.retryable), (category, retryable))

    def test_token_never_reaches_a_message(self):
        with self.assertRaises(api.APIError) as raised:
            self.client(OSError("connection to secret-token failed")).graphql("query Viewer {}")
        self.assertNotIn("secret-token", str(raised.exception))

    def test_enterprise_server_endpoints_derive_from_the_api_url(self):
        self.assertEqual(api.endpoints("https://ghe.example/api/v3"),
                         ("https://ghe.example/api/v3", "https://ghe.example/api/graphql", "ghe.example"))
        with self.assertRaises(TrackerError):
            api.endpoints("https://ghe.example")


class CommandTests(unittest.TestCase):
    def run_command(self, *argv):
        from agent_box.tracker import cli
        tracker, _ = adapter([issue(1, status="Ready", title="One"), issue(2, status="Backlog")])
        binding = Binding("github", api.DEFAULT_API, "octo/2")
        with patch.object(cli, "make_adapter", return_value=tracker), \
                patch.object(cli, "repository_root", return_value="/repo"), \
                patch.object(cli.Binding, "session", return_value=binding):
            return cli.dispatch(cli.parser().parse_args(list(argv)))

    def test_status_reports_account_binding_and_lifecycle(self):
        status = self.run_command("--provider", "github", "status")
        self.assertEqual((status["provider"], status["user"]["id"], status["binding"]["project"]),
                         ("github", "octocat", "octo/2"))
        self.assertNotIn("move", status["capabilities"])
        self.assertEqual(status["lifecycle"][0]["problems"], [])

    def test_get_ready_and_next_work_through_the_portable_commands(self):
        self.assertEqual(self.run_command("get", "#1", "--no-comments")["title"], "One")
        self.assertEqual([row["id"] for row in self.run_command("ready")], ["1"])
        self.assertEqual(self.run_command("next")["id"], "1")


class ConfigurationTests(unittest.TestCase):
    def test_github_connection_needs_only_a_token(self):
        config = Config.from_dict({"tracker": {"enabled": True, "provider": "github",
                                               "connections": {"github": {"token": "t"}}}})
        connection = config.tracker.connection()
        self.assertEqual((connection.url, connection.usable), (api.DEFAULT_API, True))

    def test_registry_builds_the_github_adapter(self):
        from agent_box.tracker.registry import make_adapter
        config = Config.from_dict({"tracker": {"enabled": True, "provider": "github",
                                               "connections": {"github": {"token": "t"}}}})
        with patch.dict("os.environ", {}, clear=False) as env:
            env.pop("AGENT_BOX_RUNTIME_CONFIG", None)
            tracker = make_adapter(Binding("github", api.DEFAULT_API, "octo/2"), config=config)
        self.assertIsInstance(tracker, GitHubAdapter)

    def test_launch_and_apply_do_not_call_github_unimplemented(self):
        from agent_box.runtime import tracker_warnings
        for provider, warned in (("github", False), ("jira", True)):
            config = Config.from_dict({"tracker": {"enabled": True, "provider": provider,
                                                   "connections": {provider: {"url": "https://jira.example", "token": "t"}}}})
            with self.subTest(provider=provider):
                self.assertEqual(any("not implemented" in line for line in tracker_warnings(config)), warned)

    def test_repository_comes_from_an_origin_on_the_same_instance(self):
        for url in ("git@github.com:octo/app.git", "https://github.com/octo/app", "ssh://git@github.com/octo/app.git"):
            with self.subTest(url=url):
                self.assertEqual(repository_from_url(url, "github.com"), REPO)
        self.assertEqual(repository_from_url("ssh://git@ghe.example:2222/octo/app.git", "ghe.example"), REPO)
        self.assertIsNone(repository_from_url("https://gitlab.com/octo/app.git", "github.com"))
        self.assertIsNone(repository_from_url("https://user:pw@github.com/octo/app.git", "github.com"))


if __name__ == "__main__":
    unittest.main()

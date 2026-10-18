"""Jira uses native workflow and issue fields without weakening portable gates."""
import copy
import io
import json
import unittest
import urllib.error
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from agent_box.config import Config, TrackerConnection
from agent_box.repo_binding import Binding
from agent_box.tracker.core import STATES, TrackerError, claim, ready_tasks, set_checkboxes
from agent_box.tracker.jira import JiraAdapter
from agent_box.tracker.jira_api import JiraClient


def issue(key="TEST-1", state="Ready", **fields):
    return {"key": key, "fields": {"project": {"key": key.split("-")[0]}, "summary": "A task",
            "description": "- [ ] Evidence", "status": {"id": state, "name": state}, "updated": "one",
            "labels": [], "subtasks": [], "issuelinks": [], "priority": {"name": "Medium"}, **fields}}


class Fixture:
    url = "https://jira.example"

    def __init__(self, cloud=False):
        self.cloud = cloud
        self.issues = {"TEST-1": issue(), "TEST-2": issue("TEST-2"), "OTHER-1": issue("OTHER-1")}
        self.journal = []
        self.writes = []
        self.statuses = [{"id": state, "name": state} for state in STATES]
        self.transitions = [{"id": state, "to": {"id": state}} for state in STATES]

    def request(self, method, path, body=None, params=None):
        if method != "GET":
            self.writes.append((method, path, body))
        if path == "/myself":
            return {"name": "alice", "accountId": "cloud-alice"}
        if path.startswith("/project/") and path.endswith("/statuses"):
            return [{"id": "task", "name": "Task", "subtask": False, "statuses": copy.deepcopy(self.statuses)},
                    {"id": "subtask", "name": "Sub-task", "subtask": True, "statuses": copy.deepcopy(self.statuses)}]
        if path.startswith("/project/"):
            key = path.split("/")[2]
            return {"key": key, "id": "100", "name": key}
        if path == "/priority":
            return [{"id": "medium", "name": "Medium"}, {"id": "high", "name": "High"}]
        if path == "/issue" and method == "POST":
            fields = body["fields"]
            key = "TEST-3"
            self.issues[key] = issue(key, "Backlog", summary=fields["summary"], description=fields["description"],
                                     parent=fields.get("parent"))
            return {"key": key}
        if path.startswith("/issue/"):
            key = path.split("/")[2]
            if path.endswith("/comment") and method == "POST":
                row = {"id": str(len(self.journal) + 1), "body": body["body"]}
                self.journal.append(row)
                return row
            elif path.endswith("/transitions"):
                if method == "GET":
                    return {"transitions": copy.deepcopy(self.transitions)}
                self.issues[key]["fields"]["status"] = {"id": body["transition"]["id"], "name": body["transition"]["id"]}
            elif method == "PUT":
                self.issues[key]["fields"].update(copy.deepcopy(body["fields"]))
            return copy.deepcopy(self.issues[key])
        raise AssertionError((method, path, body))

    def pages(self, path, key, params=None):
        return copy.deepcopy(self.journal)

    def search(self, jql):
        return [copy.deepcopy(row) for row in self.issues.values() if row["fields"]["project"]["key"] == "TEST"]


def adapter(cloud=False):
    fixture = Fixture(cloud)
    return JiraAdapter(fixture.url, "token", Binding("jira", fixture.url, "TEST"), client=fixture), fixture


class JiraContractTests(unittest.TestCase):
    def test_claim_and_checklists_keep_the_portable_workflow_contract(self):
        """A successful claim confirms the actor and state; comments invalidate stale checklist reads."""
        for cloud, user in ((False, "alice"), (True, "cloud-alice")):
            tracker, fixture = adapter(cloud)
            task, started = claim(tracker, "TEST-1")
            self.assertTrue(started)
            self.assertEqual((task.state, task.assignees), ("In progress", [user]))
            text = set_checkboxes(task.description, [1], True)
            changed = tracker.update(task, {"description": text})
            self.assertIn("[x]", changed.description)
            fixture.journal.append({"id": "1", "body": "New decision", "updated": "later"})
            with self.assertRaisesRegex(TrackerError, "changed"):
                tracker.update(changed, {"description": "stale overwrite"})

    def test_bad_workflow_is_visible_as_unknown_and_blocks_writes(self):
        """A default Jira workflow lacks Ready and must never be silently treated as the full lifecycle."""
        tracker, fixture = adapter()
        fixture.statuses = [row for row in fixture.statuses if row["name"] != "Ready"]
        self.assertEqual(tracker.get("TEST-1").state, "Unknown")
        self.assertIn("Ready", tracker.lifecycle_report()[0]["problems"][0])
        with self.assertRaises(TrackerError):
            tracker.set_state(tracker.get("TEST-1"), "In progress")
        self.assertEqual(fixture.writes, [])

    def test_status_names_match_case_but_duplicate_transitions_are_refused(self):
        """Picking an arbitrary transition could bypass a screen or validator."""
        tracker, fixture = adapter()
        fixture.statuses[2]["name"] = "IN PROGRESS"
        fixture.transitions.append({"id": "alternate", "to": {"id": "In progress"}})
        with self.assertRaisesRegex(TrackerError, "found 2"):
            tracker.set_state(tracker.get("TEST-1"), "In progress")
        self.assertFalse(fixture.writes)

    def test_links_keep_blocker_direction_and_gate_ready(self):
        """Incoming Blocks links are dependencies; outgoing ones do not block their source."""
        tracker, fixture = adapter()
        fixture.issues["TEST-1"]["fields"]["issuelinks"] = [
            {"type": {"name": "Blocks", "outward": "blocks"}, "inwardIssue": {"key": "TEST-2"}},
            {"type": {"name": "Blocks", "outward": "blocks"}, "outwardIssue": {"key": "OTHER-1"}}]
        task = tracker.get("TEST-1")
        self.assertEqual(task.blockers, ["TEST-2"])
        self.assertEqual(ready_tasks([task], tracker.get, "alice"), [])

    def test_scope_protects_every_write_even_when_canonical_reads_are_allowed(self):
        """A canonical external key is useful for dependencies, never authorization to mutate it."""
        tracker, fixture = adapter()
        task = tracker.resolve("OTHER-1", "TEST", canonical=True)
        for action in (lambda: tracker.update(task, {"title": "bad"}), lambda: tracker.comment(task.id, "bad"),
                       lambda: tracker.set_tier(task.id, 1), lambda: tracker.set_state(task, "Done"),
                       lambda: tracker.relate("TEST-1", "blocks", task.id)):
            with self.assertRaises(TrackerError):
                action()
        self.assertFalse(fixture.writes)

    def test_references_never_confuse_numeric_ids_keys_and_other_servers(self):
        """Only project-local visible numbers expand; canonical keys retain their identity."""
        tracker, fixture = adapter()
        self.assertEqual(tracker.resolve("#1", "TEST").id, "TEST-1")
        self.assertEqual(tracker.resolve("https://jira.example/browse/TEST-1", "TEST").id, "TEST-1")
        with self.assertRaises(TrackerError):
            tracker.get("1")
        with self.assertRaises(TrackerError):
            tracker.resolve("https://other.example/browse/TEST-1", "TEST")
        with self.assertRaisesRegex(TrackerError, "Ambiguous"):
            tracker.resolve("A task", "TEST")

    def test_subtasks_are_created_natively_and_reparenting_is_explicitly_unsupported(self):
        """A label or issue link pretending to be a parent would break workflow integration."""
        tracker, fixture = adapter()
        child = tracker.create("TEST", "Child", parent="TEST-1")
        self.assertEqual(child.parent, "TEST-1")
        payload = next(body for method, path, body in fixture.writes if path == "/issue")
        self.assertEqual(payload["fields"]["issuetype"], {"id": "subtask"})
        with self.assertRaisesRegex(TrackerError, "unsupported"):
            tracker.relate(child.id, "parent", "TEST-2")

    def test_lossy_descriptions_and_multiple_assignees_are_refused_before_writes(self):
        """An unsupported field shape must not destroy content or silently drop an owner."""
        tracker, fixture = adapter()
        with self.assertRaisesRegex(TrackerError, "one assignee"):
            tracker.update(tracker.get("TEST-1"), {"assignees": ["alice", "bob"]})
        fixture.issues["TEST-1"]["fields"]["description"] = {"type": "doc", "content": []}
        with self.assertRaisesRegex(TrackerError, "lossy"):
            tracker.get("TEST-1")
        self.assertFalse(fixture.writes)

    def test_cloud_username_survives_config_and_registry_without_changing_old_connections(self):
        """Existing URL/token accounts retain their shape; Cloud identity reaches the private client."""
        from agent_box.tracker.registry import make_adapter
        config = Config()
        config.tracker.enabled = True
        config.tracker.provider = "jira"
        config.tracker.connections["jira"] = TrackerConnection("https://jira.example", "token", "me@example.com")
        restored = Config.from_dict(config.to_dict())
        with patch.dict("os.environ", {}, clear=True):
            tracker = make_adapter(Binding("jira", "https://jira.example", "TEST"), config=restored)
        self.assertEqual(tracker.client.username, "me@example.com")
        self.assertEqual(TrackerConnection("old", "secret").to_dict(), {"url": "old", "token": "secret"})

    def test_close_with_reason_rereads_after_its_own_journal_write(self):
        """A command's own reason changes the revision, but must not make its following transition stale."""
        from agent_box.tracker import cli
        tracker, fixture = adapter()
        args = cli.parser().parse_args(["state", "TEST-1", "Done", "--reason", "Cancelled before implementation"])
        with patch.object(cli, "context", return_value=(tracker, tracker.binding, Path("/tmp"))), \
                patch.object(cli, "unintegrated", return_value=None):
            rows = cli.dispatch(args)
        self.assertEqual(rows[0]["state"], "Done")
        self.assertEqual(fixture.journal[0]["body"], "Cancelled before implementation")

    def test_cloud_search_hydrates_text_instead_of_flattening_enhanced_search_adf(self):
        """Enhanced search may return rich text even in v2; the issue endpoint preserves wiki text."""
        tracker, fixture = adapter(cloud=True)
        fixture.search = Mock(return_value=[{"key": "TEST-1", "fields": {"description": {"type": "doc"}}}])
        self.assertEqual(tracker.search("TEST", "Evidence")[0].description, "- [ ] Evidence")

    def test_username_uses_the_private_snapshot_and_disabled_trackers_render_no_accounts(self):
        """Cloud identity follows the token into private snapshots without exposing disabled integrations."""
        from agent_box import runtime
        config = Config()
        config.tracker.enabled = True
        config.tracker.connections["jira"] = TrackerConnection("https://jira.example", "secret", "me@example.com")
        with TemporaryDirectory() as directory:
            target = Path(directory) / "snapshot"
            runtime.sync_tracker_runtime(config, directory=target, container=False)
            path = target / "config.json"
            data = json.loads(path.read_text())
            self.assertEqual(data["tracker"]["connections"]["jira"]["username"], "me@example.com")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            config.tracker.enabled = False
            config.workflow.enabled = True
            runtime.sync_tracker_runtime(config, directory=target, container=False)
            self.assertEqual(json.loads(path.read_text())["tracker"]["connections"], {})


class JiraTransportTests(unittest.TestCase):
    def response(self, data, status=200):
        response = io.BytesIO(json.dumps(data).encode())
        response.status = status
        return response

    def test_authentication_is_in_headers_and_redirects_are_never_followed(self):
        """Site redirects must not forward credentials; both native auth forms stay out of URLs."""
        for username, prefix in (("", "Bearer "), ("a@example.com", "Basic ")):
            opener = Mock(return_value=self.response({"name": "me"}))
            client = JiraClient("https://jira.example/context", "secret", username, opener=opener)
            client.request("GET", "/myself")
            req = opener.call_args.args[0]
            self.assertTrue(req.get_header("Authorization").startswith(prefix))
            self.assertEqual(req.full_url, "https://jira.example/context/rest/api/2/myself")
            error = urllib.error.HTTPError(req.full_url, 302, "redirect", {}, io.BytesIO())
            with patch.object(client, "opener", side_effect=error):
                with self.assertRaises(TrackerError):
                    client.request("GET", "/myself")

    def test_lost_and_malformed_write_replies_are_unknown_without_retries_or_secret_echo(self):
        """Repeating a lost create/comment could duplicate user data."""
        for failure in (OSError("secret"), urllib.error.HTTPError("url", 503, "secret", {}, io.BytesIO(b"secret"))):
            opener = Mock(side_effect=failure)
            client = JiraClient("https://jira.example", "secret", opener=opener)
            with self.assertRaises(TrackerError) as raised:
                client.request("POST", "/issue", {})
            self.assertEqual((raised.exception.category, raised.exception.retryable), ("unknown-outcome", False))
            self.assertNotIn("secret", str(raised.exception))
            self.assertEqual(opener.call_count, 1)
        response = io.BytesIO(b"not json")
        response.status = 201
        client.opener = Mock(return_value=response)
        with self.assertRaises(TrackerError) as raised:
            client.request("POST", "/issue", {})
        self.assertEqual(raised.exception.category, "unknown-outcome")

    def test_cloud_cursor_and_dc_offset_search_read_every_page(self):
        """Cloud removed the legacy search endpoint; a first-page-only read could lose blockers."""
        for cloud in (True, False):
            client = JiraClient("https://jira.example", "secret", "me" if cloud else "")
            client._cloud = cloud
            pages = ([{"issues": [1], "nextPageToken": "next", "isLast": False}, {"issues": [2], "isLast": True}]
                     if cloud else [{"issues": [1], "total": 2}, {"issues": [2], "total": 2}])
            client.request = Mock(side_effect=pages)
            self.assertEqual(client.search('project = "TEST"'), [1, 2])
            last = client.request.call_args.kwargs["params"]
            self.assertEqual(last["nextPageToken"] if cloud else last["startAt"], "next" if cloud else 1)
            self.assertEqual(client.request.call_args.args[1], "/search/jql" if cloud else "/search")

    def test_incomplete_pagination_fails_instead_of_proving_absence(self):
        """A broken cursor or an empty partial page cannot prove absence."""
        client = JiraClient("https://jira.example", "secret", "me")
        client._cloud = True
        client.request = Mock(return_value={"issues": [], "isLast": False})
        with self.assertRaisesRegex(TrackerError, "incomplete"):
            client.search("query")
        client.request = Mock(return_value={"comments": [], "total": 2})
        with self.assertRaisesRegex(TrackerError, "before all"):
            client.pages("/issue/TEST-1/comment", "comments")

    def test_empty_link_created_reply_is_success_but_empty_issue_create_remains_unknown(self):
        """Jira link creation returns 201 without JSON; issue creation must still return its key."""
        response = io.BytesIO(b"")
        response.status = 201
        client = JiraClient("https://jira.example", "secret", opener=Mock(return_value=response))
        self.assertIsNone(client.request("POST", "/issueLink", {}))
        tracker, fixture = adapter()
        original = fixture.request
        fixture.request = lambda method, path, body=None, params=None: (
            None if method == "POST" and path == "/issue" else original(method, path, body, params))
        with self.assertRaises(TrackerError) as raised:
            tracker.create("TEST", "Created but no key")
        self.assertEqual(raised.exception.category, "unknown-outcome")

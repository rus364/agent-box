"""Exercise the real adapter over an HTTP fixture, including conditional edits."""
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from agent_box.repo_binding import Binding


BOARD = {
    "/api/v2/projects/12/views": [
        {"id": 7, "title": "Board", "view_kind": "kanban", "done_bucket_id": 5}],
    "/api/v2/projects/12/views/7/buckets": [
        {"id": 1, "title": "Backlog"}, {"id": 2, "title": "Ready"}, {"id": 3, "title": "Doing"},
        {"id": 4, "title": "Review"}, {"id": 5, "title": "Done"}],
}


class Handler(BaseHTTPRequestHandler):
    requests = []
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.requests.append((self.command, self.path, None))
        self.send_response(200)
        self.send_header("ETag", '"v1"')
        self.end_headers()
        body = BOARD.get(self.path.split("?")[0], {
            "id": 3, "project_id": 12, "title": "Task", "description": "- [ ] API",
            "done": False, "buckets": [{"id": 2, "project_view_id": 7}],
            "assignees": [{"id": 1}], "related_tasks": {"parenttask": [{"id": 2}]}})
        self.wfile.write(json.dumps(body).encode())

    def do_PATCH(self):
        self.requests.append((self.command, self.path, self.headers.get("If-Match")))
        self.send_response(412)
        self.end_headers()
        self.wfile.write(b'{"detail":"Resource changed"}')


class TrackerAdapterTests(unittest.TestCase):
    def test_direct_get_avoids_inventory_and_conflicting_edit_is_not_retried(self):
        from agent_box.tracker.vikunja import VikunjaAdapter
        from agent_box.tracker.core import TrackerError
        Handler.requests = []
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        binding = Binding("vikunja", "http://example", "12")
        adapter = VikunjaAdapter(f"http://127.0.0.1:{server.server_port}", "secret", binding)
        task = adapter.get("3")
        self.assertEqual((task.state, task.parent, task.assignees), ("Ready", "2", ["1"]))
        reads = [path for _, path, _ in Handler.requests]
        self.assertEqual([path for path in reads if path.startswith("/api/v2/tasks")],
                         ["/api/v2/tasks/3?format=markdown&expand=buckets&expand=subtasks"])
        before = len(Handler.requests)
        with self.assertRaises(TrackerError) as caught:
            adapter.update(task, {"description": "- [x] API"})
        self.assertEqual(caught.exception.category, "conflict")
        self.assertEqual(Handler.requests[-1], ("PATCH", "/api/v2/tasks/3", '"v1"'))
        # The board is cached for the process, so the retry only rereads the task.
        self.assertEqual(len(Handler.requests) - before, 2)

    def test_redirect_does_not_forward_tracker_credentials_to_another_origin(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        import threading
        from agent_box.tracker.vikunja_api import VikunjaClient, APIError
        received = []
        class Target(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_GET(self):
                received.append(self.headers.get("Authorization"))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"{}")
        target = ThreadingHTTPServer(("127.0.0.1", 0), Target)
        class Redirect(Target):
            def do_GET(self):
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{target.server_port}/stolen")
                self.end_headers()
        origin = ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
        for server in (origin, target):
            threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            client = VikunjaClient(f"http://127.0.0.1:{origin.server_port}", "secret")
            with self.assertRaises(APIError):
                client.get("/user")
            self.assertEqual(received, [])
        finally:
            for server in (origin, target):
                server.shutdown()
                server.server_close()

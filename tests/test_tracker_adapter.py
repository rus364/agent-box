"""Exercise the real adapter over an HTTP fixture, including conditional edits."""
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from agent_box.repo_binding import Binding


class Handler(BaseHTTPRequestHandler):
    requests = []
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.requests.append((self.command, self.path, None))
        self.send_response(200)
        self.send_header("ETag", '"v1"')
        self.end_headers()
        self.wfile.write(json.dumps({"id": 3, "project_id": 12, "title": "Task", "description": "- [ ] API",
            "done": False, "buckets": [{"id": 2, "project_view_id": 7}],
            "assignees": [{"id": 1}], "related_tasks": {"parenttask": [{"id": 2}]}}).encode())

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
        binding = Binding("vikunja", "http://example", "12", "7", {"Ready": "2"})
        adapter = VikunjaAdapter(f"http://127.0.0.1:{server.server_port}", "secret", binding)
        task = adapter.get("3")
        self.assertEqual((task.state, task.parent, task.assignees), ("Ready", "2", ["1"]))
        self.assertEqual(len(Handler.requests), 1)
        with self.assertRaises(TrackerError) as caught:
            adapter.update(task, {"description": "- [x] API"})
        self.assertEqual(caught.exception.category, "conflict")
        self.assertEqual(Handler.requests[-1], ("PATCH", "/api/v2/tasks/3", '"v1"'))
        self.assertEqual(len(Handler.requests), 3)

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

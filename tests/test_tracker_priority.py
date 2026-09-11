"""Priority chosen during task creation must survive into task selection."""
import unittest
from unittest.mock import patch

from agent_box.repo_binding import Binding
from agent_box.tracker import cli
from agent_box.tracker.vikunja import VikunjaAdapter
from test_tracker_staging import NativeClient
from test_tracker_lifecycle import task


class PriorityClient(NativeClient):
    def post(self, path, body):
        if path.startswith('/projects/') and path.split('?')[0].endswith('/tasks'):
            self.writes.append((path, body))
            ident = max(self.tasks) + 1
            project = int(path.split('/')[2])
            self.tasks[ident] = task(ident, project, project * 10, project * 100, **body)
            return dict(self.tasks[ident])
        return super().post(path, body)


class PriorityTests(unittest.TestCase):
    def test_created_priority_is_visible_and_drives_ready_order_after_update(self):
        """CLI creation must not discard urgency already supported by the adapter."""
        client = PriorityClient()
        binding = Binding('vikunja', client.url, '1', True)
        adapter = VikunjaAdapter(client.url, 'token', binding, client)
        with patch.object(cli, 'context', return_value=(adapter, binding, None)), \
                patch.object(adapter, 'identity', return_value={'id': 7}):
            def run(*args):
                return cli.dispatch(cli.parser().parse_args(args))
            low = run('create', '--title', 'Ordinary', '--description-file', '/dev/null')
            high = run('create', '--title', 'Deadline', '--priority', '4', '--description-file', '/dev/null')
            self.assertEqual((low.priority, high.priority), (0, 4))
            for item in (low, high):
                run('state', item.id, 'Ready')
            self.assertEqual(run('next')['id'], high.id)
            changed = run('update', low.id, '--priority', '5')
            self.assertEqual(changed.priority, 5)
            self.assertEqual(run('next')['id'], low.id)
            rows = {row['id']: row for row in run('search')}
            self.assertEqual((rows[low.id]['priority'], rows[high.id]['priority']), (5, 4))

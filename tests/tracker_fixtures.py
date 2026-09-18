"""Shared tracker test fixtures: a fake Vikunja client, its boards and tasks.

Test modules import these from here rather than from each other, so deleting
one module's tests never breaks another's setup. The three clients are one
chain because each adds only the endpoints its own tests reach: a new call is
visible as an AssertionError instead of silently returning something plausible.
"""
import copy
import re
from urllib.parse import parse_qs, urlsplit

from agent_box.repo_binding import Binding
from agent_box.tracker import vikunja_api as api
from agent_box.tracker.core import NORMALIZED_LABEL, TrackerError
from agent_box.tracker.vikunja import VikunjaAdapter


def board(view_id, first, *, done_bucket=None, titles=("Backlog", "Ready", "Doing", "Review", "Done"),
          extra_views=()):
    view = {"id": view_id, "title": "Board", "view_kind": "kanban", "bucket_configuration_mode": "manual"}
    if done_bucket is not None:
        view["done_bucket_id"] = done_bucket
    buckets = [{"id": first + index, "title": title, "position": (index + 1) * 100}
               for index, title in enumerate(titles)]
    return {"views": [view, *extra_views], "buckets": {view_id: buckets}}


def task(ident, project, view_id, bucket_id, **fields):
    return {"id": ident, "project_id": project, "title": f"Task {ident}", "description": "",
            "done": False, "buckets": [{"id": bucket_id, "project_view_id": view_id}],
            "assignees": [], "related_tasks": {}, **fields}


class FakeClient:
    """Only the endpoints the adapter actually reaches, so a new call is visible."""

    def __init__(self, projects, boards, tasks):
        self.url = "https://tracker.example"
        self.revisions = {}
        self.projects = projects
        self.boards = boards
        self.tasks = tasks
        self.writes = []
        self.reads = []

    def paginate(self, path, params=None):
        self.reads.append(path)
        if path == "/projects":
            return [dict(project) for project in self.projects]
        raise AssertionError(path)

    def get(self, path):
        self.reads.append(path)
        parts = path.split("?")[0].strip("/").split("/")
        if parts[0] == "projects" and parts[-1] == "views":
            return {"items": self.boards[int(parts[1])]["views"]}
        if parts[0] == "projects" and parts[-1] == "buckets":
            return {"items": self.boards[int(parts[1])]["buckets"][int(parts[3])]}
        if parts[0] == "tasks":
            return dict(self.tasks[int(parts[1])])
        raise AssertionError(path)

    def post(self, path, body):
        self.writes.append((path, body))
        parts = path.split("?")[0].strip("/").split("/")
        if parts[0] == "projects" and parts[-1] == "buckets":
            buckets = self.boards[int(parts[1])]["buckets"][int(parts[3])]
            created = {"id": max(bucket["id"] for bucket in buckets) + 1,
                       "title": body["title"], "position": body.get("position")}
            buckets.append(created)
            buckets.sort(key=lambda bucket: bucket["position"])
            return dict(created)
        raise AssertionError(path)

    def patch(self, path, body, markdown=False):
        self.writes.append((path, body))
        parts = path.split("?")[0].strip("/").split("/")
        if parts[0] == "projects" and parts[2] == "views":
            view = next(v for v in self.boards[int(parts[1])]["views"] if v["id"] == int(parts[3]))
            view.update(body)
            return dict(view)
        if parts[0] == "tasks":
            self.tasks[int(parts[1])].update(body)
            return dict(self.tasks[int(parts[1])])
        raise AssertionError(path)

    def put(self, path, body):
        self.writes.append((path, body))
        parts = path.strip("/").split("/")
        self.tasks[int(body["task_id"])]["buckets"] = [
            {"id": int(parts[5]), "project_view_id": int(parts[3])}]
        return {}


PROJECTS = [{"id": 1, "title": "Root", "parent_project_id": 0},
            {"id": 2, "title": "Child", "parent_project_id": 1},
            {"id": 9, "title": "Elsewhere", "parent_project_id": 0}]


def adapter(recursive=True, boards=None, tasks=None):
    defaults = {1: board(10, 100), 2: board(20, 200), 9: board(90, 900)}
    boards = {**defaults, **(boards or {})}
    tasks = tasks or {}
    client = FakeClient(PROJECTS, boards, tasks)
    binding = Binding("vikunja", "https://tracker.example", "1", recursive)
    return VikunjaAdapter(binding.server, "token", binding, client=client), client


class NativeClient(FakeClient):
    def __init__(self):
        projects = [{"id": 1, "title": "Root", "parent_project_id": 0, "max_permission": 2},
                    {"id": 2, "title": "Child", "parent_project_id": 1, "max_permission": 2},
                    {"id": 9, "title": "Outside", "parent_project_id": 0, "max_permission": 2}]
        tasks = {5: task(5, 1, 10, 100, index=1, title="Same", assignees=[{"id": 7}],
                         related_tasks={"blocking": [{"id": 6}]}),
                 6: task(6, 2, 20, 200, index=1, title="Same", related_tasks={"blocked": [{"id": 5}]}),
                 7: task(7, 9, 90, 900, index=1)}
        super().__init__(projects, {1: board(10, 100, done_bucket=104),
                                   2: board(20, 200, done_bucket=204), 9: board(90, 900)}, tasks)
        self.lose_response = False
        self.duplicates = 0
        self.journal = {}

    def paginate(self, path, params=None):
        if path == "/tasks":
            ids = {int(n) for n in re.findall(r'\d+', (params or {}).get('filter', ''))}
            return copy.deepcopy([t for t in self.tasks.values() if not ids or t['project_id'] in ids])
        if path.endswith('/comments'):
            return copy.deepcopy(self.journal.get(int(path.split('/')[2]), []))
        if path.endswith('/attachments'):
            return []
        return super().paginate(path, params)

    def get(self, path):
        match = re.fullmatch(r'/projects/(\d+)', path)
        if match:
            return copy.deepcopy(next(p for p in self.projects if p['id'] == int(match[1])))
        return copy.deepcopy(super().get(path))

    def post(self, path, body):
        parts = path.split('?')[0].strip('/').split('/')
        if parts[-1] == 'duplicate':
            self.writes.append((path, body))
            source = int(parts[1]); target = max(p['id'] for p in self.projects) + 1
            original = self.get(f'/projects/{source}')
            self.projects.append({**original, 'id': target, 'parent_project_id': body['parent_project_id'],
                                  'title': original['title'] + ' - duplicate'})
            self.boards[target] = board(target * 10, target * 100, done_bucket=target * 100 + 4)
            for row in list(self.tasks.values()):
                if row['project_id'] == source:
                    ident = max(self.tasks) + 1
                    self.tasks[ident] = {**copy.deepcopy(row), 'id': ident, 'project_id': target,
                                        'assignees': [], 'related_tasks': {},
                                        'buckets': [{'id': target*100, 'project_view_id': target*10}]}
            self.duplicates += 1
            if self.lose_response:
                self.lose_response = False
                raise TrackerError('lost response', 'transport')
            return {'duplicated_project': self.get(f'/projects/{target}')}
        if parts[-1] == 'assignees':
            self.writes.append((path, body))
            self.tasks[int(parts[1])]['assignees'].append({'id': body['user_id']})
            return body
        if parts[-1] == 'relations':
            self.writes.append((path, body))
            base, other, kind = int(parts[1]), body['other_task_id'], body['relation_kind']
            existing = self.tasks[base]['related_tasks'].setdefault(kind, [])
            if {'id': other} in existing:
                raise api.APIError(409, 'Relation already exists')
            existing.append({'id': other})
            self.tasks[other]['related_tasks'].setdefault(api.RELATION_INVERSE[kind], []).append({'id': base})
            return body
        if parts[-1] == 'comments':
            self.writes.append((path, body))
            self.journal.setdefault(int(parts[1]), []).append(dict(body))
            return body
        return super().post(path, body)

    def delete(self, path):
        parts = path.strip('/').split('/')
        if len(parts) == 2 and parts[0] == 'projects':
            # Vikunja deletes the tasks and child projects too; the adapter must never rely on that.
            self.writes.append((path, None))
            self.projects = [p for p in self.projects if p['id'] != int(parts[1])]
            return None
        if len(parts) != 5 or parts[0] != 'tasks' or parts[2] != 'relations':
            raise AssertionError(path)
        base, kind, other = int(parts[1]), parts[3], int(parts[4])
        self.writes.append((path, None))
        for left, edge, right in ((base, kind, other), (other, api.RELATION_INVERSE[kind], base)):
            relations = self.tasks[left]['related_tasks']
            relations[edge] = [t for t in relations.get(edge, []) if t['id'] != right]

    def patch(self, path, body, markdown=False):
        match = re.fullmatch(r'/projects/(\d+)', path)
        if match:
            self.writes.append((path, body))
            project = next(p for p in self.projects if p['id'] == int(match[1]))
            project.update(body)
            return dict(project)
        if re.fullmatch(r'/tasks/\d+', path):
            self.writes.append((path, body))
            row = self.tasks[int(path.split('/')[2])]
            target = body.get('project_id', row['project_id'])
            if target != row['project_id']:
                # Vikunja keeps the ID, journal, labels and relations, renumbers
                # the task inside the target, and drops the card into the target's
                # default column, not its old one. Nothing proves a done card goes
                # to the done column, so the fake assumes the worse case.
                view = self.boards[target]['views'][0]
                columns = sorted(self.boards[target]['buckets'][view['id']], key=lambda b: b['position'])
                bucket = columns[0]['id']
                row['index'] = 1 + max([t.get('index') or 0 for t in self.tasks.values()
                                        if t['project_id'] == target], default=0)
                row['buckets'] = [{'id': bucket, 'project_view_id': view['id']}]
            row.update(body)
            return copy.deepcopy(row)
        return super().patch(path, body, markdown)



class NormalizationClient(NativeClient):
    per_page = 1

    def __init__(self):
        super().__init__()
        self.labels = [{'id': 1, 'title': 'keep'}]
        self.tasks = {
            5: task(5, 1, 10, 100, labels=[self.labels[0]], description='Unresolved question'),
            6: task(6, 2, 20, 200, labels=[{'id': 2, 'title': NORMALIZED_LABEL}], description='Do not output'),
            7: task(7, 9, 90, 900, description='Outside'),
            8: task(8, 2, 20, 200, description='Last page candidate'),
        }

    def paginate(self, path, params=None):
        if path == '/tasks':
            return api.VikunjaClient.paginate(self, path, params)
        if path == '/labels':
            return copy.deepcopy(self.labels)
        return super().paginate(path, params)

    def get(self, path):
        if path.startswith('/tasks?'):
            self.reads.append(path)
            params = parse_qs(urlsplit(path).query)
            ids = {int(n) for n in re.findall(r'\d+', params.get('filter', [''])[0])}
            rows = [r for r in self.tasks.values() if r['project_id'] in ids]
            query = params.get('q', [''])[0].casefold()
            rows = [r for r in rows if query in (r['title'] + r['description']).casefold()]
            page = int(params['page'][0])
            return {'items': copy.deepcopy(rows[page-1:page]), 'total': len(rows), 'total_pages': len(rows)}
        return super().get(path)

    def post(self, path, body):
        if path == '/labels':
            self.writes.append((path, body))
            label = {'id': max([r['id'] for r in self.labels] + [2]) + 1, **body}
            self.labels.append(label)
            return dict(label)
        if path.endswith('/labels'):
            self.writes.append((path, body))
            label = next(r for r in self.labels if r['id'] == body['label_id'])
            self.tasks[int(path.split('/')[2])].setdefault('labels', []).append(dict(label))
            return dict(body)
        return super().post(path, body)

    def delete(self, path):
        self.writes.append((path, None))
        parts = path.split('/')
        self.tasks[int(parts[2])]['labels'] = [r for r in self.tasks[int(parts[2])]['labels']
                                             if r['id'] != int(parts[4])]

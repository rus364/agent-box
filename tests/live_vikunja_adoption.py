"""Opt-in API contract check; creates and removes only its own test fixtures.

Run from the checkout: PYTHONPATH=. python3 tests/live_vikunja_adoption.py --run
The ordinary unittest discovery never executes this module's main function.
"""
import argparse
import json
import os
import subprocess
import tempfile
import urllib.request
import uuid
from pathlib import Path

from agent_box.config import effective_config
from agent_box.paths import WORKFLOW_STATE
from agent_box.repo_binding import Binding
from agent_box.tracker import adoption, cli, vikunja_api as api
from agent_box.tracker.audit import audit_project
from agent_box.tracker.core import TrackerError, checkboxes


def run():
    connection = effective_config().tracker.connection('vikunja')
    client = api.VikunjaClient(connection.url, connection.token)
    name = 'agent-box adoption test ' + uuid.uuid4().hex[:10]
    roots, label = [], None
    user = client.get('/user')
    WORKFLOW_STATE.mkdir(parents=True, exist_ok=True)
    # Inside a box, staging commands accept only a receipt under workflow state.
    with tempfile.TemporaryDirectory(prefix='agent-box-adoption-live-') as tmp, \
            tempfile.TemporaryDirectory(prefix='adoption-live-', dir=WORKFLOW_STATE) as state:
        repository = Path(tmp)
        subprocess.run(['git', 'init', '-q', str(repository)], check=True)
        receipt = Path(state) / 'receipt.json'
        try:
            source = client.post('/projects', {'title': name})['id']; roots.append(source)
            child = client.post('/projects', {'title': 'Child', 'parent_project_id': source})['id']
            outside = client.post('/projects', {'title': name + ' external'})['id']; roots.append(outside)
            label = client.post('/labels', {'title': name})['id']
            rows = []
            for project, done in [(source, False), (source, True), (child, False), (outside, False)]:
                rows.append(client.post(f'/projects/{project}/tasks?format=markdown',
                                        {'title': 'Same title', 'description': 'Existing acceptance.', 'done': done}))
            first, second, third, external = [t['id'] for t in rows]
            client.patch(f'/tasks/{first}', {'description': 'Original meaning.'}, markdown=True)
            client.post(f'/tasks/{first}/assignees', {'user_id': user['id']})
            api.attach_label(client, first, label)
            client.post(f'/tasks/{first}/comments', {'comment': '<p>Original journal</p>'})
            client.post(f'/tasks/{first}/relations', {'other_task_id': second, 'relation_kind': 'related'})
            client.post(f'/tasks/{first}/relations', {'other_task_id': third, 'relation_kind': 'blocking'})
            client.post(f'/projects/{source}/shares', {'name': 'read-only test', 'permission': 0})
            boundary = uuid.uuid4().hex
            payload = (f'--{boundary}\r\nContent-Disposition: form-data; name="files"; filename="evidence.txt"\r\n'
                       f'Content-Type: text/plain\r\n\r\noriginal fixture\r\n--{boundary}--\r\n').encode()
            request = urllib.request.Request(connection.url + f'/api/v2/tasks/{first}/attachments', data=payload,
                headers={'Authorization': 'Bearer ' + connection.token,
                         'Content-Type': 'multipart/form-data; boundary=' + boundary}, method='POST')
            with client.opener(request, timeout=30) as response:
                uploaded = json.load(response)
            assert not uploaded.get('errors'), uploaded
            originals = {str(t['id']): client.get(f"/tasks/{t['id']}") for t in rows}
            preview = adoption.start(repository, str(source), receipt)
            assert preview['project'] is None and not receipt.exists()
            result = adoption.start(repository, str(source), receipt, apply=True)
            target = result['project']; roots.append(int(target))
            assert not (repository / '.agent-box.json').exists()
            for ident, task in originals.items():
                assert client.get(f'/tasks/{ident}') == task, f'source task {ident} changed'
            source_tasks = api.collect_tasks(client, project=str(source))
            target_tasks = api.collect_tasks(client, project=target)
            mapping = {s['id']: next(t['id'] for t in target_tasks if t['index'] == s['index']) for s in source_tasks}
            copied_first = mapping[first]
            copy = client.get(f'/tasks/{copied_first}')
            assert {u['id'] for u in copy['assignees']} == {user['id']}
            assert {l['id'] for l in copy['labels']} == {label}
            assert len(client.paginate(f'/tasks/{copied_first}/attachments')) == 1
            assert client.paginate(f'/tasks/{copied_first}/comments')[0]['comment'] == '<p>Original journal</p>'
            original_shares = client.paginate(f'/projects/{source}/shares')
            copied_shares = client.paginate(f'/projects/{target}/shares')
            assert len(original_shares) == len(copied_shares) == 1
            assert original_shares[0]['hash'] != copied_shares[0]['hash']
            copied_child = result['projects'][str(child)]
            copied_third = api.collect_tasks(client, project=copied_child)[0]['id']
            assert copied_third in {t['id'] for t in copy['related_tasks']['blocking']}
            assert mapping[second] in {t['id'] for t in copy['related_tasks']['related']}
            instance, binding, _ = adoption.staging_context(receipt, repository)
            instance.initialize_boards(instance.subtree(target, True), apply=True)
            instance.update(instance.get(str(copied_first)), {
                'title': 'Existing task, clarified',
                'description': 'Goal\n\nOriginal meaning.\n\nAcceptance\n\nRequires ab-interview.'})
            assert audit_project(instance, target)['clean']
            # Optional collapse: move the child's only task into the root, then delete the emptied child.
            os.chdir(repository)

            def staging(*argv):
                return cli.dispatch(cli.parser().parse_args(['--adoption', str(receipt), *argv]))

            checklist = 'Goal\n\nMoved work.\n\nAcceptance\n\n- [x] Kept evidence\n- [ ] Open item'
            instance.update(instance.get(str(copied_third)), {'description': checklist})
            # A done task is listed like any other, so it holds its project back too.
            client.patch(f'/tasks/{copied_third}', {'done': True})
            try:
                staging('delete-project', copied_child)
                raise AssertionError('a project holding a done task was deleted')
            except TrackerError as exc:
                assert 'still has 1 tasks' in str(exc), exc
            client.patch(f'/tasks/{copied_third}', {'done': False})
            state = instance.get(str(copied_third)).state
            moved = staging('move', str(copied_third), '--project', target)
            assert [(row['project'], row['state']) for row in moved] == [(target, state)], moved
            items = [(item['checked'], item['text']) for item in checkboxes(instance.get(str(copied_third)).description)]
            assert items == [(True, 'Kept evidence'), (False, 'Open item')], items
            after = client.get(f'/tasks/{copied_third}')
            assert copied_first in {t['id'] for t in after['related_tasks']['blocked']}
            assert staging('delete-project', copied_child)['deleted'] == copied_child
            assert int(copied_child) not in {p['id'] for p in api.projects_from_client(client)}
            assert json.loads(receipt.read_text())['clone']['removed'] == [str(child)]
            instance, _, _ = adoption.staging_context(receipt, repository)
            assert audit_project(instance, target)['clean']
            # Cutover also repairs eligible edges added after the original clone.
            client.post(f'/tasks/{first}/relations', {'other_task_id': external, 'relation_kind': 'related'})
            outside_before = client.get(f'/tasks/{external}')
            assert adoption.cutover(repository, receipt)['ok']
            assert client.get(f'/tasks/{external}') == outside_before
            result = adoption.cutover(repository, receipt, apply=True)
            assert result['complete'] and Binding.load(repository).project == target
            assert result['clone']['external_relations'][0]['status'] == 'preserved'
            related = {t['id'] for t in client.get(f'/tasks/{external}')['related_tasks']['related']}
            assert {first, copied_first} <= related
            assert client.get(f'/projects/{source}')['title'] == name + f' — backup {source}'
            assert client.get(f'/projects/{target}')['title'] == name
            assert adoption.cutover(repository, receipt, apply=True)['complete']
            plan_task = api.PlanTask.from_row({'Title': 'Imported update', 'Description': 'Original meaning.',
                                              'Project': 'Plan root', 'Plan ID': 'TEST-1', 'Labels': name})
            plan = api.Plan([plan_task], repository / 'plan.csv', 'test-plan')
            importer = api.Importer(client, plan, repository / 'imports', project=target)
            report = importer.apply()
            assert report['verification']['ok'], report
            assert client.get(f'/tasks/{first}')['description'] == originals[str(first)]['description']
            assert client.get(f'/tasks/{copied_first}')['title'] == 'Imported update'
            print('PASS: native tree, indexes, comments, attachment, shares, assignees, relations, staging audit, '
                  'staging move and project removal, cutover, scoped import')
        finally:
            # A recorded clone may exist even when a later verification raised.
            if receipt.exists():
                recorded = json.loads(receipt.read_text())
                copied = recorded['clone']['projects'].get(str(source))
                if copied and int(copied) not in roots:
                    roots.append(int(copied))
            for ident in reversed(roots):
                project = client.get(f'/projects/{ident}')
                if not project['title'].startswith(name):
                    raise RuntimeError(f'Refusing cleanup of renamed fixture {ident}')
                client.delete(f'/projects/{ident}')
            if label is not None:
                client.delete(f'/labels/{label}')
            print('Test fixtures removed:', roots)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', action='store_true', help='Create test projects on the configured Vikunja server')
    if not parser.parse_args().run:
        parser.error('--run is required; this check writes to the configured tracker')
    run()

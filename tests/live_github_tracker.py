"""Opt-in end-to-end check of the workflow on a GitHub Projects tracker.

Run from the checkout against a purpose-created sandbox (its default branch receives
one setup commit and one test commit per run):

    PYTHONPATH=. python3 tests/live_github_tracker.py --run --project OWNER/NUMBER --repository OWNER/NAME [--gh-token]

The token is the configured GitHub tracker token, or `gh auth token` with --gh-token.
The ordinary unittest discovery never executes this module's main function.
"""
import argparse
import os
import subprocess
import tempfile
import uuid
from pathlib import Path
from unittest.mock import patch

from agent_box import workflow
from agent_box.atomicio import atomic_write_text
from agent_box.config import effective_config
from agent_box.repo_binding import Binding, write_setup
from agent_box.tracker import github_api as api
from agent_box.tracker.github import GitHubAdapter


def git(root, *args):
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True).stdout.strip()


def run(project, repository, token):
    binding = Binding("github", api.DEFAULT_API, project)
    tracker = GitHubAdapter(binding.server, token, binding, repository=repository)
    config = effective_config()
    # Plan sync would launch an agent per finish; this check is about the tracker.
    config.workflow.sync.policy = "off"
    config.workflow.merge_mode = "local"
    tag = uuid.uuid4().hex[:8]
    with tempfile.TemporaryDirectory(prefix="agent-box-github-live-") as tmp:
        root = Path(tmp) / "clone"
        subprocess.run(["gh", "repo", "clone", repository, str(root), "--", "-q"], check=True)
        write_setup(root, binding)
        git(root, "add", "-A")
        # An earlier run already landed the same binding on the sandbox's default branch.
        if subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=root).returncode:
            git(root, "commit", "-q", "-m", f"Bind the sandbox for live check {tag}")
        parent = tracker.create(project, f"Live cycle {tag}", "- [x] parent acceptance\n")
        child = tracker.create(project, f"Live cycle {tag} child", "- [x] child acceptance\n", parent=parent.id)
        for task in (parent, child):
            tracker.set_state(tracker.get(task.id), "Ready")

        def step(*argv):
            with patch.object(workflow, "make_adapter", return_value=tracker), \
                    patch.object(workflow, "effective_config", return_value=config):
                result = workflow.dispatch(workflow.parser().parse_args(list(argv)))
            print(" ".join(argv), "->", {key: result.get(key) for key in ("task", "state", "branch") if key in result})
            return result

        cwd = os.getcwd()
        os.chdir(root)
        try:
            step("begin", parent.id)
            step("begin", child.id)
            atomic_write_text(root / f"live-{tag}.txt", "checked\n")
            git(root, "add", "-A")
            git(root, "commit", "-q", "-m", f"Live check {tag}")
            step("validate", child.id, "--", "true")
            step("finish", child.id)
            step("validate", parent.id, "--", "true")
            step("finish", parent.id)
            step("land", parent.id, "merge")
        finally:
            os.chdir(cwd)
        for task in (child, parent):
            raw = api.issue(tracker.client, repository, int(task.id))
            column = tracker.column(raw, tracker.bound_project())
            assert (raw["state"], raw["state_reason"], column) == ("CLOSED", "COMPLETED", "Done"), (task.id, raw, column)
        print(f"Live cycle passed: parent #{parent.id}, child #{child.id} closed as completed in Done")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", action="store_true", required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--gh-token", action="store_true", help="Use `gh auth token` instead of the configured token")
    args = parser.parse_args()
    token = (subprocess.run(["gh", "auth", "token"], check=True, capture_output=True, text=True).stdout.strip()
             if args.gh_token else effective_config().tracker.connection("github").token)
    run(args.project, args.repository, token)


if __name__ == "__main__":
    main()

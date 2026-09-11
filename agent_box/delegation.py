"""The shared delegation contract; provider settings describe a separate choice."""
import json

from .provider_runner import report_schema


def context_policy():
    return {
        "context": "new instance for each new delegation",
        "parent_transcript": False,
        "tracker_comments": False,
        "previous_agent_history": False,
        "handoff_language": "English",
        "report_language": "English",
        "allowed_input": ["current English assignment", "repository instructions",
                          "explicitly labeled source evidence"],
        "continuation": "Clarifications and ordinary edits stay with the active worker; file/hash changes alone do not start a delegation.",
        "new_assignment": "New task or scope, work after completion (including review fixes), and every reviewer verdict round require a new instance.",
    }


LAUNCH_CONTEXT = (
    "Prefer native when the live interface satisfies the prepared route and context_policy. "
    "Append --fast on to report_command only if the candidate that ran requests fast and native fast is verified; otherwise omit --fast. "
    "Codex native requires fork_turns=none or a verified equivalent; omitted inheritance, all, "
    "and completed-agent reuse fail the contract. Claude native needs a verified fresh conversation "
    "without parent history; otherwise use managed CLI. Profiles, prepare, scope and coordinator claims "
    "do not attest hidden context; record observed facts and limitations. A missing full-context receipt "
    "alone does not prohibit native. Clarifications stay with the active worker; a new scope/task, work "
    "after completion and every review verdict require a new instance. Pass only delegate_handoff as "
    "the initial bootstrap message; never inline, summarize or reconstruct its canonical input file. "
)


def role_instructions(role):
    text = (
        f"Act only as the assigned {role}. Use English for handoffs, reports, implementation and code. "
        "The coordinator owns branches, commits, task lifecycle, requirements, dependencies, "
        "the tracker and GitHub; never write to any of them or edit .agent-box.json. "
        "Use only the current assignment, repository instructions and explicitly labeled source evidence; do not load parent conversation, "
        "tracker comments or previous-agent history. The supplied current requirements satisfy the "
        "repository instruction to read the tracked task; do not query its history. "
        "Read AGENTS.md or CLAUDE.md in directories you touch and files they import with @path. "
        "Read every chunk; report missing, unreadable or truncated instructions explicitly. "
        "Return the role JSON schema with concrete evidence and limitations. "
    )
    if role in ("worker", "scout"):
        text += "Do not delegate or launch nested scouts; only the coordinator dispatches scouts. Selected facts are source evidence, not normative requirements. "
    if role == "worker":
        return text + "Verify selected facts only when correctness depends on them; do not repeat sufficient scout research in full. Change project files only within the assigned scope; you are not alone in the working tree, so preserve others' changes. Report changes, validation results and uncertainty."
    if role == "scout":
        return text + "Read-only: change nothing. Report facts with source locations and uncertainty; evidence is not a reviewer verdict or violated-requirement finding."
    return text + "Read-only: change nothing. Selected scout facts are not normative requirements. Review findings require location, evidence and violated requirement; distinguish verified consequences from uncertainty."


def profile_instructions(role):
    """Keep stable native role identity short; each handoff supplies the full contract."""
    common = (f"Act only as the assigned {role}. Follow the complete current handoff and repository instructions. "
              "Use English for handoffs, reports and implementation. The coordinator owns commits, branches, "
              "requirements, tracker and GitHub; never write to any of them; do not load parent conversation, "
              "tracker comments or previous-agent history. Read all instructions and report missing, unreadable "
              "or truncated instructions. ")
    if role == "worker":
        return common + "Never launch nested scouts; only the coordinator dispatches scouts. Change only assigned project files and return evidence; preserve other work."
    if role == "scout":
        return common + "Never launch nested scouts; only the coordinator dispatches scouts. Read only; report facts with source locations and uncertainty, never findings."
    return common + "Read only; Review findings require location, evidence and violated requirement."


def render_handoff(role, payload, instructions=""):
    """One logical input for native and managed, with role-specific evidence."""
    handoff = {"role": role, "context_policy": context_policy(), **payload,
               "report_schema": report_schema(role)}
    return (role_instructions(role) + "\n" + instructions + "\nDelegation handoff:\n"
            + json.dumps(handoff, ensure_ascii=False, indent=2) + "\n")


def bootstrap(role, artifact):
    """Native messages locate the canonical input without copying its contents."""
    return (
        f"Assigned role: {role}. Read the complete UTF-8 delegation input at "
        f"{json.dumps(artifact['path'], ensure_ascii=False)} in chunks without truncation. "
        f"Verify SHA-256 {artifact['sha256']} before acting. Follow that input's role, "
        "scope, repository instructions, evidence policy and report schema. Do not load "
        "parent conversation, tracker comments or previous-agent history. If the file is "
        "missing, unreadable, truncated or its digest differs, stop and report blocked "
        "with the concrete limitation; never reconstruct the assignment."
    )

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
    "Prefer native when the live interface supports the configured route and context_policy. "
    "For Codex native explicitly pass fork_turns=\"none\" or a verified equivalent; all, omitted "
    "default inheritance, and reuse of a completed agent are noncompliant. For Claude native verify "
    "an equivalent new conversation without parent history using the available tool; if unknown or "
    "unsupported, use managed CLI. Profile names, prepare, scope, and coordinator assertions do not "
    "independently attest hidden context. Record observed tool capabilities and actual calls separately "
    "from unconfirmed behavior; missing full-context receipts alone do not prohibit native. "
    "Keep ordinary clarifications with the active worker; new work after completion, changed assignment "
    "scope, and every reviewer verdict require a new instance. "
    "Pass delegate_handoff as the complete initial message, never this prepare output or conversation. "
)


def role_instructions(role):
    text = (
        f"Act only as the assigned {role}. Use English for handoffs, reports, implementation and code. "
        "The coordinator owns branches, commits, task lifecycle, requirements, dependencies, "
        "the tracker and GitHub; never write to any of them or edit .agent-box.json. "
        "Use only the current assignment and repository instructions; do not load parent conversation, "
        "tracker comments or previous-agent history. The supplied current requirements satisfy the "
        "repository instruction to read the tracked task; do not query its history. "
        "Read AGENTS.md or CLAUDE.md in directories you touch and files they import with @path. "
        "Read every chunk; report missing, unreadable or truncated instructions explicitly. "
        "Return the role JSON schema with concrete evidence and limitations. "
    )
    if role == "worker":
        return text + "Change project files only within the assigned scope; you are not alone in the working tree, so preserve others' changes. Report changes, validation results and uncertainty."
    if role == "scout":
        return text + "Read-only: change nothing. Report facts with source locations and uncertainty; evidence is not a reviewer verdict or violated-requirement finding."
    return text + "Read-only: change nothing. Review findings require location, evidence and violated requirement; distinguish verified consequences from uncertainty."


def render_handoff(role, payload, instructions=""):
    """One logical input for native and managed, with role-specific evidence."""
    handoff = {"role": role, "context_policy": context_policy(), **payload,
               "report_schema": report_schema(role)}
    return (role_instructions(role) + "\n" + instructions + "\nDelegation handoff:\n"
            + json.dumps(handoff, ensure_ascii=False, indent=2) + "\n")

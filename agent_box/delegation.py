"""Instructions for delegated agents; provider settings describe a separate choice."""
import json

from .provider_runner import report_schema

# Workers and scouts answer in these sections; nothing parses them, so a
# different shape costs the coordinator a reread, never a paid run.
RESPONSE_SECTIONS = ("Done", "Evidence", "Limitations")

LAUNCH_CONTEXT = (
    "managed_only routes require managed CLI; native agents run on the session's API and cannot switch API identity. "
    "In an external session Claude Agent model aliases resolve to the session model and session effort overrides "
    "profile effort, so native Claude needs the route's model and effort to equal the session's; otherwise use managed CLI. "
    "Follow prepared provider order; prefer native when the live interface satisfies the route. "
    "Append --fast on to report_command only if the candidate that ran requests fast and native fast is verified; otherwise omit --fast. "
    "Codex native requires fork_turns=none or a verified equivalent; omitted inheritance, all, "
    "and completed-agent reuse fail the contract. Claude native needs a verified fresh conversation "
    "without parent history; otherwise use managed CLI. Profiles, prepare, scope and coordinator claims "
    "do not attest hidden context; record observed facts and limitations. Every review verdict requires "
    "a new instance. Pass only delegate_handoff as the initial bootstrap message; never inline, summarize "
    "or reconstruct its canonical input file. "
)

COORDINATOR_OWNS = ("The coordinator owns branches, commits, task lifecycle, requirements, the tracker and GitHub; "
                    "never write to any of them or edit .agent-box.json. ")


def reviewer_instructions():
    return (
        "Act only as the assigned reviewer. Use English for the report. " + COORDINATOR_OWNS +
        "Use only the current assignment, repository instructions and explicitly labeled source evidence; do not load parent conversation, "
        "tracker comments or previous-agent history. The supplied current requirements satisfy the "
        "repository instruction to read the tracked task; do not query its history. "
        "Read AGENTS.md or CLAUDE.md in directories you touch and files they import with @path. "
        "Read every chunk; report missing, unreadable or truncated instructions explicitly. "
        "Your final message is the report: one JSON object matching report_schema, with concrete evidence "
        "and limitations, and no prose or Markdown around it. "
        "Read-only: change nothing. Review findings require location, evidence and violated requirement; "
        "distinguish verified consequences from uncertainty."
    )


def delegate_rules(role):
    """What a worker or scout may do, shared by managed prompts and native profiles."""
    text = (f"Act only as the assigned {role}. Use English. " + COORDINATOR_OWNS +
            "Do not launch other agents. Read AGENTS.md or CLAUDE.md in directories you touch and files they "
            "import with @path. ")
    if role == "worker":
        text += ("Change project files only within the assigned scope; you are not alone in the working tree, "
                 "so preserve others' changes. ")
    else:
        text += "Read-only: change nothing. Answer with facts, source locations and uncertainty, not review verdicts. "
    return text + ("End with a Markdown answer in sections " + ", ".join(f"## {name}" for name in RESPONSE_SECTIONS)
                   + ": what you did or found, the evidence (files, commands and their results), and what remains "
                   "unverified or blocked.")


def delegate_prompt(role, task, brief, repository_instructions):
    """A managed worker or scout starts from the brief alone, never the coordinator's history."""
    return (delegate_rules(role) + f"\n\nTask {task.id}: {task.title}\n\n----- Assignment -----\n{brief}\n"
            + (f"\n----- Repository instructions -----\n{repository_instructions}\n" if repository_instructions else ""))


def profile_instructions(role):
    """Keep stable native role identity short; the coordinator's message supplies the assignment."""
    if role == "reviewer":
        return ("Act only as the assigned reviewer. Follow the complete current handoff and repository instructions. "
                "Use English for reports. " + COORDINATOR_OWNS + "Do not load parent conversation, tracker comments "
                "or previous-agent history. Read all instructions and report missing, unreadable or truncated "
                "instructions. Read only; Review findings require location, evidence and violated requirement.")
    return delegate_rules(role)


def render_handoff(role, payload, instructions=""):
    """One logical reviewer input for native and managed runs."""
    handoff = {"role": role, **payload, "report_schema": report_schema()}
    return (reviewer_instructions() + "\n" + instructions + "\nDelegation handoff:\n"
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

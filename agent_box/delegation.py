"""Instructions for delegated agents; provider settings describe a separate choice."""
import json
from .workflow_behavior import instructions as behavior_instructions

# Workers and scouts answer in these sections; nothing parses them, so a
# different shape costs the coordinator a reread, never a paid run.
RESPONSE_SECTIONS = ("Done", "Evidence", "Limitations")

COORDINATOR_OWNS = ("The coordinator owns branches, commits, task lifecycle, requirements, the tracker and GitHub; "
                    "never write to any of them or edit .agent-box.json. ")


def reviewer_instructions():
    return (
        "Act only as the assigned reviewer. Use English for the report. " + COORDINATOR_OWNS +
        "Use only the current assignment, repository instructions and explicitly labeled source evidence; do not load parent conversation, "
        "tracker comments or previous-agent history. The supplied task descriptions satisfy the "
        "repository instruction to read the tracked task; do not query its history. "
        "Read AGENTS.md or CLAUDE.md in directories you touch and files they import with @path. "
        "Supplied instruction snapshots satisfy those reads; load additional scoped instructions when needed. "
        "Read every chunk; report missing, unreadable or truncated instructions explicitly. "
        "Read-only: change nothing. Read referenced repository sections; distinguish verified consequences from uncertainty. " + REPORT_FORMAT
    )


# Acceptance belongs to code; the coordinator closes the rest in the checklist that
# landing requires. A reviewer asked to judge tracker state could only guess from
# an untrusted brief, so the same change passed or stayed INCONCLUSIVE by wording.
REVIEW_SCOPE = (
    "Judge only what the repository shows: the diff, the code and the repository's files. Requirements about "
    "tracker state (other tasks' descriptions, relations or states), operator actions or anything else outside the "
    "repository are closed by the coordinator: list them under limitations as closed by the coordinator, and "
    "neither report findings on them nor return INCONCLUSIVE because of them; claims about them are not evidence. "
)

# The workflow reads only the verdict line and finding severities; the rest is for the coordinator.
REPORT_FORMAT = REVIEW_SCOPE + (
    "Your final message is the report in Markdown. It has exactly one line `Verdict: PASS`, `Verdict: NEEDS_WORK` "
    "or `Verdict: INCONCLUSIVE`, and no other line starting with `Verdict:`. Give each finding as one bullet "
    "`- [SEVERITY] path:line — problem, concrete scenario, evidence and violated requirement`. SEVERITY is one of: "
    "CRITICAL — data loss, a security breach or the task's core goal broken; HIGH — a stated requirement or a safety "
    "check fails in a normal scenario; MEDIUM — a real defect in an edge or error path, or a test gap hiding one; "
    "LOW — a minor defect, misleading text, or needless complexity with a simpler alternative. PASS has no findings; "
    "INCONCLUSIVE states what could not be established about the code. Add a short summary and limitations after "
    "the verdict line."
)


def review_language(language):
    """What a reviewer of any route must know to judge repository text; empty names nothing."""
    if not language:
        return ""
    return (f"Working language: {language}. New or changed code comments, docstrings, documentation and commit "
            f"messages belong in {language}; task descriptions written earlier may be in another language.\n")


def delegate_rules(role):
    """What a worker or scout may do, shared by managed prompts and native profiles."""
    text = (f"Act only as the assigned {role}. Use English. " + COORDINATOR_OWNS +
            "Do not launch other agents. Read AGENTS.md or CLAUDE.md in directories you touch and files they "
            "import with @path. Supplied instruction snapshots satisfy those reads. "
            "Inspect relevant source/diff ranges when needed; no diff belongs in an initial assignment. ")
    if role == "worker":
        text += ("Read the task’s reference documents before implementation; report missing sections or conflicting requirements. ")
        text += ("Write new or changed code comments, docstrings and documentation in the assignment's Working language "
                 "when it names one; the answer stays English. ")
        text += ("Change project files only within the assigned scope; you are not alone in the working tree, "
                 "so preserve others' changes. ")
    else:
        text += "Read-only: change nothing. Answer with facts, source locations and uncertainty, not review verdicts. "
    return text + ("End with a Markdown answer in sections " + ", ".join(f"## {name}" for name in RESPONSE_SECTIONS)
                   + ": what you did or found, the evidence (files, commands and their results), and what remains "
                   "unverified or blocked.")


def delegate_prompt(role, task, brief, repository_instructions, language="", *, behavior_version):
    """Both routes receive authoritative requirements and bounded scope, never parent history.

    The rule for the working language lives in delegate_rules, which native
    profiles install once for every repository; only the value is this
    repository's, so it travels with the run. Only a worker writes project text.
    """
    named = f"Working language: {language}\n" if language and role == "worker" else ""
    return (delegate_rules(role) + "\n" + behavior_instructions(behavior_version, role)
            + f"\n\nTask {task.id}: {task.title}\n{named}"
            + f"\n----- Task requirements -----\n{task.description}\n"
            + f"\n----- Assignment scope and supplemental notes -----\n{brief}\n"
            + "The assignment bounds your work; it does not replace the task requirements. "
              "Report conflicts to the coordinator before acting.\n"
            + (f"\n----- Repository instructions -----\n{repository_instructions}\n" if repository_instructions else ""))


def profile_instructions(role):
    """Keep stable native role identity short; the coordinator's message supplies the assignment."""
    if role == "sync":
        from .plan_sync import RULES
        return RULES
    if role == "reviewer":
        return ("Act only as the assigned reviewer. Follow the complete current handoff and repository instructions. "
                "Use English for reports. " + COORDINATOR_OWNS + "Do not load parent conversation, tracker comments "
                "or previous-agent history. Read all instructions and report missing, unreadable or truncated "
                "instructions. Read only. " + REPORT_FORMAT)
    return (delegate_rules(role) + f" Follow the assignment's Workflow behavior. If absent, read "
            f"`agent-box workflow behavior --role {role}` before work; do not infer a version from another project.")


def render_handoff(role, payload, instructions=""):
    """One logical reviewer input for native and managed runs."""
    handoff = {"role": role, **payload}
    return (reviewer_instructions() + "\n" + instructions + "\nDelegation handoff:\n"
            + json.dumps(handoff, ensure_ascii=False, indent=2) + "\n")


def bootstrap(role, artifact):
    """The helper verified the input; restricted readers need no shell/hash command."""
    from .text_delivery import message
    return (f"Assigned role: {role}. " + message(artifact)
            + " Follow the supplied role, behavior version, scope and report format. "
              "Do not load parent conversation, tracker comments or previous-agent history. "
              "Never reconstruct a missing assignment or substitute another role's behavior.")

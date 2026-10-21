"""Versioned engineering guidance; role permissions and execution policy stay separate."""
from .errors import AgentBoxError

VERSIONS = ("v1", "v2")
ROLES = ("coordinator", "worker", "reviewer", "scout", "sync")

COMMON = """Before proposing or changing an implementation, trace the relevant callers, dependencies and contracts: public interfaces, configuration readers/writers/defaults, persisted data, generated/installed artifacts, tests and documentation. Cover additions, modifications and deletions, including indirect consumers and obsolete cleanup. Follow evidenced dependencies outside the diff; bound research by impact, risk and uncertainty, not every imaginable connection.
Search existing implementations before adding similar logic. Reuse when responsibility and behavior fit; weigh coupling and future change cost. Avoid duplicated logic/state/checks, forced abstractions for superficial similarity and speculative generalization. A broad refactor needs a concrete benefit and an explanation why a smaller change is insufficient. Preserve required error, safety and compatibility checks.
Assess the requested approach against the user's goal. Investigate material contradictions, harmful consequences, simpler alternatives and disproportionate effort before raising them. Explain evidence, impact and a recommendation; do not manufacture disagreement or reopen settled choices without new facts. Do not silently weaken requirements or override an informed user decision.
Separate observed facts, hypotheses and unverified assumptions. Use the cheapest discriminating check: inspect a consumer or test, reproduce in isolation, or consult authoritative documentation when needed. If proof is unavailable or disproportionately costly, state the uncertainty and concrete alternatives with effort/risk tradeoffs. A guess is not a blocker; passing static tests is not live acceptance.
Scale investigation and validation with risk, not line count. Flag a small benefit requiring a large rewrite before expanding scope; a tiny public-interface or data-handling change may still need broad checks. Report material affected contracts, decisions and verification limits concisely, without mandatory essays or repetitive checklists.
"""

ROLE_RULES = {
    "coordinator": "Discuss material scope/requirement alternatives with the user; routine implementation choices need no extra approval. Verify delegated claims and close required executable checks.\n",
    "worker": "Stay within the assignment. Send evidenced scope conflicts and costly alternatives to the coordinator before expanding work; the coordinator owns requirements and user decisions.\n",
    "reviewer": "Independently trace consequences rather than accepting the author's explanation. Each finding needs a location, concrete scenario, evidence and impact with proportionate severity. A maintainability defect needs a current cost or divergence risk and a simpler behavior-preserving alternative; no immediate runtime bug is required. Reject taste-only or hypothetical blockers, finding quotas and mandatory refactors. Respect read-only limits: identify the missing executable check for the coordinator, never claim to have run it.\n",
    "scout": "Answer only the assigned research question with source locations and uncertainty. Identify the smallest check that resolves an assumption; do not implement or change requirements.\n",
    "sync": "Apply this reasoning only to evidenced stale task assertions. Do not invent requirements, backfill policy text into tasks or broaden synchronization scope. Preserve the sync role's existing write limits.\n",
}


def instructions(version, role):
    """Global profiles cannot hold a project's choice; each assignment carries it."""
    if version not in VERSIONS:
        raise AgentBoxError("workflow.behavior_version must be v1 or v2")
    if role not in ROLES:
        raise AgentBoxError("Unknown workflow behavior role")
    text = f"Workflow behavior: {version} ({role}).\n"
    if version == "v2":
        text += COMMON + ROLE_RULES[role]
    return text


def describe(config, role):
    result = {"version": config.workflow.behavior_version, "role": role,
              "instructions": instructions(config.workflow.behavior_version, role)}
    # Startup reports live in the launch snapshot, never in saved user settings.
    source = getattr(config, "_project_base", config)
    notice = getattr(source, "_workflow_guidance", None)
    if notice:
        result["startup_guidance"] = notice
    return result

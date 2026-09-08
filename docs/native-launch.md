# Native launch contract

Investigated for task 327 on 2026-09-08 with Codex CLI 0.153.4,
Claude Code 2.1.261 and the coordinator's exposed native tool schema.
No user configuration or installed profiles were changed.

| Interface | Evidence and decision |
| --- | --- |
| Codex explicit spawn | The current tool accepts `model` and `reasoning_effort` with `fork_turns="none"`. Its full-history fork inherits settings and disallows overrides. Check each session's actual schema; this is not a capability claim for every CLI or hosted environment. |
| Codex profiles/defaults | Custom agent model/effort settings override explicit values. Otherwise explicit settings precede agent defaults and parent settings. A model selection without effort can use the model default, but configured agent effort can still apply. Even built-in agent names can be shadowed. Pass both settings and use a role without conflicting configuration. |
| Claude native | Documentation describes per-call model selection, profile effort and session effort inheritance. This Codex session exposes no Claude native tool, so explicit per-call effort and fresh-context controls were not verified here. CLI `--agents` creates launch-time definitions; it is not proof that an existing session can pass both native parameters. |
| Claude overrides | Documentation describes model forcing, allowlist substitution, and version-dependent precedence. Local profile readiness conservatively rejects safe mode, force mode and conflicting model/effort environment settings. The coordinator must still check live settings and substitutions; exact file contents alone are insufficient. |
| Default effort | Preserve task 298: omit the managed override and clear inherited Claude effort. Native omission is not proof of no inherited override, so `default` stays managed. |
| Fresh context | Every verdict round launches a new independent read-only reviewer. Codex uses `fork_turns="none"`; followup/resume of an earlier reviewer is forbidden. All native reviewer reports require `--fresh-context`; untiered review additionally preserves the prepared session settings. A new prepare/descriptor or profile name does not prove a new instance. Managed Codex is ephemeral and managed Claude disables session persistence. |
| Model availability | The live tool must support the selected provider/model/effort. A configured name or successful prepare is not account availability. Unsupported/unknown native capabilities use the managed route; errors never authorize an unconfigured model or tier. |

Sources: [Codex subagents](https://learn.chatgpt.com/docs/agent-configuration/subagents),
[Claude subagents](https://code.claude.com/docs/en/sub-agents), locally inspected
`codex exec --help`, `claude --help`, and the active `collaboration.spawn_agent`
schema. Native launch is coordinated through prepare/spawn/report. Managed
run/review do not choose a native backend, and provider selection remains separate.

## Validation boundaries

Regression tests exercise prepared route diagnostics for missing, exact and
modified profiles, explicit report acceptance without profile readiness, rejection
of mismatched actual settings, reviewer matrices, fallback reasons, handoff/scope,
fresh review and default effort. Instruction checks require explicit verification
of unknown/unsupported capabilities and custom-agent precedence. They do not
execute or simulate provider internals and do not prove external precedence.

A bounded native smoke used the active tool with `agent_type="default"`,
`model="gpt-5.6-luna"`, `reasoning_effort="low"`, `fork_turns="none"`. It completed
a read-only inspection of test isolation and returned structured JSON. No named
`ab-*` profile was selected; existing profiles were left installed. This proves
that this session can execute explicit native parameters without selecting a
profile, not that every model, provider or default-effort path works. Model/effort
are enforced by the exposed tool contract, not attested by a model's self-report.

Ingesting that scout report was correctly rejected as stale because the
coordinator edited the working scope during the scout's independent inspection.
The smoke is execution evidence, not accepted workflow evidence. Unit tests
separately exercise ingestion on an unchanged prepared scope. Real Claude native
execution, profile-precedence collisions and effort-default smoke were not run.

Validation: 995 isolated unittests, 45 selftests and Ruff E4/E9/F passed.
The two targeted regressions failed before the fix and passed afterwards.

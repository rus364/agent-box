# External API providers

Open `agent-box config` → **External API providers** → **Add provider**.
Choose a name such as `deepseek`, enter the API key, and configure the URL and
models under Codex, Claude, or both. Under **Models**, use **Add model**, edit
its fields, then **Use as default**. Each CLI has its own endpoint and model
settings; there is one key per named connection and no default CLI. Enable the
connection and save. New connections start disabled.

Keys live in the global protected `config.json` (mode `0600`), are masked in the
menu and `config show`, and reach processes through their environment or private
runtime files. Only enabled connections enter session snapshots. Projects may
select an `api_provider` in workflow candidates but cannot define connections,
URLs or keys. Cancel leaves the saved configuration unchanged.

## Launch

```bash
agent-box ext deepseek codex
agent-box ext deepseek claude --model your-model-id
agent-box --host ext deepseek codex exec 'Inspect this repository'
agent-box --host ext deepseek claude -p 'Inspect this repository'
```

The selected CLI's `default_model` starts the session unless `--model` selects
another ID. An empty default uses the first entry; choose a default explicitly
to make selection independent of JSON object ordering. Empty models require an
explicit ID. Configured models supply workflow suggestions; manual IDs remain available. Codex
gets a separate model catalog and Claude gets a replacement model picker whose
Default points at the selected external model. These catalogs are configured
definitions, not live model discovery. A manual, unconfigured model has unknown
context and conservative text-only Codex capabilities.

## Model settings

`models` is an object keyed by the model ID. Each entry accepts `display_name`,
`effort` (default: `default`), `supported_efforts` (default: empty/unknown),
`context_window`, `auto_compact_token_limit` (both default: `0`, CLI default),
and `env` (a JSON object of strings). A declared effort list constrains launch
requests. Explicit CLI/workflow effort wins over the model default. Set
workflow effort to `default` to use the model's settings.

Codex entries also accept `input_modalities` (`text`, `image`),
`supports_parallel_tool_calls`, `supports_reasoning_summaries`, and `metadata`.
The latter supplies additional native catalog fields, such as `base_instructions`,
`support_verbosity`, `default_verbosity`, or `truncation_policy`. It cannot
override fields owned by the typed editor. Use fields supported by your installed
Codex; unknown native metadata is passed through, not interpreted by agent-box.
Additional session config can use ordinary permitted Codex `-c` arguments;
API/profile/catalog and Remote Control ownership remain with agent-box.

Claude accepts model IDs with `[1m]`. Setting `context_window` to `1048576`
adds that suffix automatically; `200000` uses the ordinary context and `0`
leaves it unspecified. Arbitrary context sizes cannot be expressed through
this Claude CLI syntax. The compact threshold becomes
`CLAUDE_CODE_AUTO_COMPACT_WINDOW`, and default effort becomes
`CLAUDE_CODE_EFFORT_LEVEL`. Claude's picker receives IDs and display labels;
its native effort UI is controlled by Claude Code. Agent-box uses
`supported_efforts` for its workflow picker and launch validation.

Endpoint `env` applies to that CLI only; model `env` overrides matching names.
Typed effort/compact values and explicit effort then take priority. These are
per-process values, stored in protected config/snapshots, masked in the menu,
and absent from argv and `config show`. They cannot override API credentials,
endpoint/main model selection, integration homes, agent-box identity or Remote
Control. Nested external launches and managed built-in fallbacks restore the
original environment before applying their own settings. Global env remains
independent. Native `/model` switches do not re-run agent-box: restart with
`--model` to apply another model's env or Claude effort/compact defaults.

Claude subagent/small-model env variables, such as `CLAUDE_CODE_SUBAGENT_MODEL`
and `ANTHROPIC_SMALL_FAST_MODEL`, remain user-controlled. They select auxiliary
models on the same API; the main model's capabilities/defaults are not applied
to those models. They are restored at the same process boundary as other env
values. Managed workflow routes describe the main CLI model.

For example, this global definition illustrates DeepSeek V4 Pro settings.
Confirm IDs and capabilities against the provider's current documentation;
agent-box does not ship a fixed DeepSeek registry. The empty key and disabled
connection must be configured before use.

```json
{
  "external_providers": {
    "deepseek": {
      "enabled": false,
      "api_key": "",
      "codex": {
        "url": "https://api.deepseek.com",
        "default_model": "deepseek-v4-pro",
        "models": {
          "deepseek-v4-pro": {
            "display_name": "DeepSeek V4 Pro",
            "effort": "high",
            "supported_efforts": ["low", "high", "max"],
            "context_window": 1048576,
            "auto_compact_token_limit": 786432,
            "input_modalities": ["text"],
            "supports_parallel_tool_calls": true,
            "supports_reasoning_summaries": true,
            "metadata": {"support_verbosity": true, "default_verbosity": "low"}
          }
        }
      },
      "claude": {
        "url": "https://api.deepseek.com/anthropic",
        "default_model": "deepseek-v4-pro",
        "env": {"API_TIMEOUT_MS": "600000"},
        "models": {
          "deepseek-v4-pro": {
            "display_name": "DeepSeek V4 Pro",
            "effort": "max",
            "supported_efforts": ["low", "high", "max"],
            "context_window": 1048576,
            "auto_compact_token_limit": 786432
          }
        }
      }
    }
  }
}
```

See the provider's [Codex catalog example](https://api-docs.deepseek.com/quick_start/agent_integrations/codex/),
[Claude Code integration](https://api-docs.deepseek.com/quick_start/agent_integrations/claude_code/),
and the [Codex configuration reference](https://developers.openai.com/codex/config-reference).

`--host` runs the installed CLI directly. Inside an existing workspace box it
runs directly in that box. The ordinary launch uses the existing workspace
container, mounts and integrations. Container snapshots map host-local API
addresses through `host.containers.internal`; host-local DNS names retain their
name for TLS and use the existing host-gateway mapping. Host settings remain
unchanged. New snapshots do not modify already running sessions.

Fresh Codex `exec`/`review` and Claude `-p` calls are supported. Service, auth,
resume, cloud and background-management commands are rejected, along with
conflicting Codex profiles/API overrides and Claude `--settings`. Use the
ordinary CLI for those operations. Put literal prompt text after `--` when it
would otherwise look like an option or command.

## Workflow

In each Codex/Claude candidate under worker/scout tiers, reviewer tiers, or
worker/reviewer model overrides, select **API provider**, then the model and
effort. **Built-in API** retains the ordinary provider. An empty model disables
that candidate. For example, a project may select this global connection:

```json
{
  "workflow": {
    "tiers": {
      "1": {
        "codex": {
          "api_provider": "deepseek",
          "model": "your-model-id",
          "effort": "default",
          "fast": false
        }
      }
    }
  }
}
```

External candidates require a separate managed `codex exec` or `claude -p`
process, even when the parent uses the same CLI. A built-in candidate called
from an external session also uses a managed process with the original API
environment restored. Prepared routes expose CLI, API name, model and an
endpoint/catalog fingerprint; keys and key fingerprints are absent. Changed
endpoints, deleted/disabled definitions and missing credentials fail before
execution. Changing a prepared endpoint or catalog requires fresh preparation.
Direct work records the API identity captured when the active session launched,
even if the saved connection is later edited.

`managed_only` routes return a managed command and refuse native reports. Owned
native profiles are removed for external candidates when configuration is
applied. Existing role contracts, structured report validation, review budgets
and fallback rules still apply. CLI routing policies and weights keep their
meaning; subscription usage and exhaustion are ignored for external candidates.
When usage budgets cannot be compared, selection uses configured static weights.
Subscription fast mode is unsupported for external workflow candidates.

Direct worker policy still means work in the active coordinator session; it
does not consult model candidates. Untiered delegated work from an external
session needs verified `--session-model` and `--session-effort`, or an applicable
model override. See [workflow routing](workflow.md).

## API and Remote Control boundaries

Codex endpoints must implement the **Responses API**, including streaming and
tool use; Chat Completions compatibility alone is insufficient. Claude endpoints
must implement the **Anthropic Messages API** and the tool/streaming behavior
Claude Code requires. Structured reports and requested reasoning effort also
need support from the selected API/model. The URL for one CLI need not be the
URL for the other: use each provider's documented endpoint. For example,
DeepSeek documents [Codex integration](https://api-docs.deepseek.com/quick_start/agent_integrations/codex/)
and a separate [Anthropic-compatible API](https://api-docs.deepseek.com/guides/anthropic_api/).
No external proxy or custom CLI build is required by agent-box.

`/model` changes models within the current API. It does not switch between
subscription and external APIs. Start another `ext` process or use a managed
workflow candidate to change API identity.

External launches bypass agent-box Remote Control, do not start an RC
app-server or acquire its slot, and reject explicit remote/session-reuse flags.
Claude's external session settings also disable native RC startup. A normal
session and an external process can coexist without changing the parent's
endpoint, auth, settings or RC lifecycle. See [Remote Control](remote-control.md).

Local probes used Codex 0.154.0 and Claude Code 2.1.269 with temporary provider
homes, fake keys and synthetic HTTP APIs. Codex TUI/exec worked while the RC
lock remained held; no RC descendant process was observed. These checks do not
prove compatibility with a live commercial API or phone-paired Remote Control.

Model-settings smoke on the same CLI versions verified Codex `model/list`
returns the configured display name, image modality and low/high/max efforts;
`config/read` confirms explicit `-c model_reasoning_effort="max"` overrides the
model's `high` default in the effective session config.
Claude `-p` completed against a loopback Messages stub, reported its `[1m]`
context as 1,000,000 tokens, stripped the suffix in the API request and sent
explicit `max` effort. The unknown synthetic model warning was expected.
Unit tests verify catalog context/compact fields and env restoration; this smoke
does not prove automatic compaction at the threshold or live DeepSeek behavior.

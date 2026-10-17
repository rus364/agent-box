# External API providers

Open `agent-box config` → **Agents** → **External APIs** → **Add provider**.
Choose a name such as `deepseek`, enter the API key, and set the URL under
Codex, Claude, or both. Under **Models**, use **Add from provider list** or
**Add model**, edit its fields, then **Use as default**. One model list serves
both CLIs; each CLI has its own endpoint, and there is one key per named
connection. Enable the connection and save. New connections start disabled.

## Provider model lists

Opening a provider, its **Models** or one of its models asks the provider for
the models it publishes: `GET {Codex URL}/models` (OpenAI shape, Bearer key)
first, then `GET {Claude URL}/v1/models` (Anthropic shape, `x-api-key`).
Either list serves both CLIs: DeepSeek's Anthropic URL has none, while its
OpenAI-compatible one does. Pages are followed only on the configured host.
The list is read once per key and URL combination in a menu session, with a
five-second timeout per URL, and is never stored. The key travels only in the
request header; a failure shows a short diagnostic and leaves manual entry.

**Add from provider list** offers the models not configured yet; typing
filters them by ID or name. A chosen model gets the settings the list states:
`display_name`, `supported_efforts` and the provider's default `effort`,
`context_window` and `input_modalities`. DeepSeek, OpenRouter and Anthropic
state most of these; a plain OpenAI-compatible list gives only IDs. Compaction
thresholds, Codex tool flags, `metadata` and `env` stay manual. A configured
model the list lacks is marked `not listed` and kept; **Update from API**
replaces only the fields the list states for that model. A listed model is a
suggestion, not proof that the account may use it.

Keys live in the global protected `config.json` (mode `0600`), are masked in the
menu and `config show`, and reach processes through their environment or private
runtime files. Only enabled connections enter session snapshots. Projects may
select an `api_provider` in workflow candidates but cannot define connections,
URLs or keys. Cancel leaves the saved configuration unchanged.

## Launch

```bash
agent-box ext deepseek                 # the default agent (agents.default)
agent-box ext deepseek codex
agent-box ext deepseek claude --model your-model-id
agent-box --host ext deepseek codex exec 'Inspect this repository'
agent-box --host ext deepseek claude -p 'Inspect this repository'
```

Without `codex` or `claude` the default agent starts, chosen as for a plain
launch; the connection must configure an endpoint for that CLI.

The selected CLI's `default_model` starts the session unless `--model` selects
another ID. An empty default uses the first entry; choose a default explicitly
to make selection independent of JSON object ordering. Empty models require an
explicit ID. Configured models supply workflow suggestions; manual IDs remain available. Codex
gets a separate model catalog and Claude gets a replacement model picker whose
Default points at the selected external model. Launches use these configured
definitions; the provider's list is read only by the configuration menu. A
manual, unconfigured model has unknown context and conservative text-only Codex
capabilities.

## Native Codex options

The Codex endpoint has two JSON objects with native Codex option names:

- `provider_options` extends `model_providers.<id>`: query parameters, headers,
  retries, timeouts and transport options.
- `config` supplies session settings such as `web_search` and reasoning behavior.

Both are editable in the configuration menu. For example, under a named
connection's `codex` endpoint:

```json
{
  "provider_options": {
    "query_params": {"api-version": "YOUR_API_VERSION"},
    "http_headers": {"X-Tenant": "YOUR_TENANT"},
    "stream_idle_timeout_ms": 300000
  },
  "config": {
    "web_search": "live"
  }
}
```

`query_params` adds URL parameters; `http_headers` sends HTTP headers. Session
settings are interpreted by Codex, not appended to the URL. Use only options
supported by the installed CLI and endpoint. Agent-box passes their native
values without maintaining a second catalog of supported provider settings.
Nested JSON objects represent native TOML tables; do not use dotted JSON keys.

The URL, API key, selected model/catalog and credential store remain owned by
agent-box. Native options cannot replace API/auth fields or enable Remote
Control. Header values are moved into private per-process environment variables;
`env_http_headers` may reference names supplied through endpoint `env` instead.
Authentication headers cannot override the connection's API key. Use non-secret
query parameters and session settings: these appear in CLI arguments. Put
secrets in the API-key field, endpoint `env`, or header settings.

Defaults are applied first, then endpoint `config`, then an explicit model's
configured effort, then explicit CLI `-c`/workflow effort. Search defaults to
`disabled`; use `config: {"web_search": "live"}` or the CLI's `--search` when the
endpoint supports the native Responses search tool. A provider's website search
feature alone does not establish API support. This setting cannot translate a
provider-specific search protocol. Native `/model` changes do not restart the
session or reapply endpoint settings.

Native session settings such as `model_context_window` and
`model_auto_compact_token_limit` override the model catalog values. Leave them
unset to use each model's configured context and compaction threshold. They
remain available for deliberate session-wide overrides; agent-box does not
duplicate Codex's configuration schema.

See the [Codex configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference)
and the provider's integration guide, for example
[Kimi with Codex](https://platform.kimi.ai/docs/guide/codex-kimi) or
[Kimi with Claude Code](https://platform.kimi.ai/docs/guide/claude-code-kimi).
Claude retains its native per-process `env` settings; the Codex option objects
are specific to Codex.

## Model settings

A provider's `models` is an object keyed by the model ID and shared by both
CLIs. Each entry accepts `display_name`, `effort` (default: `default`),
`supported_efforts` (default: empty/unknown), `context_window` (default: `0`,
CLI default) and `input_modalities` (`text`, `image`; default: `text`). A
declared effort list constrains launch requests. Explicit CLI/workflow effort
wins over the model default. Set workflow effort to `default` to use the
model's settings.

Settings one CLI alone uses live in the entry's `codex` and `claude` objects.
Both accept `auto_compact_token_limit` (default: `0`, CLI default) and `env` (a
JSON object of strings). `codex` also accepts `supports_parallel_tool_calls`,
`supports_reasoning_summaries`, and `metadata`.
The latter supplies additional native catalog fields, such as `base_instructions`,
`support_verbosity`, `default_verbosity`, or `truncation_policy`. It cannot
override fields owned by the typed editor. Use fields supported by your installed
Codex; unknown native metadata is passed through, not interpreted by agent-box.
External catalogs default `apply_patch_tool_type` to `null`: a provider that
accepts function tools need not support Codex's custom freeform patch tool.
For an endpoint that supports it, set `"apply_patch_tool_type": "freeform"`
in the model's `codex.metadata` object. Explicit
`null` keeps the conservative default. Other tool support still depends on
the installed Codex and the API.
Additional session config can use ordinary permitted Codex `-c` arguments;
API/profile/catalog and Remote Control ownership remain with agent-box.

Claude Code knows a 200K and a 1M context window only, so Claude derives its
window from `context_window`: from 1,000,000 tokens it launches the ID with
`[1m]`, from 200,000 with the ordinary window, and below that leaves it
unspecified. Configured IDs never carry `[1m]`; an explicit `--model ID[1m]`
still selects that model. Claude uses only the efforts among its own levels: a
shared default it lacks, such as `minimal`, launches with Claude's default, and
a model declaring no level Claude has accepts only that default. The
compact threshold must stay below the window that CLI uses. It becomes
`CLAUDE_CODE_AUTO_COMPACT_WINDOW`, and default effort becomes
`CLAUDE_CODE_EFFORT_LEVEL`. Claude's picker receives IDs and display labels;
its native effort UI is controlled by Claude Code. Agent-box uses
`supported_efforts` for its workflow picker and launch validation.

Endpoint `env` applies to that CLI only; the model's `env` for that CLI
overrides matching names.
`ANTHROPIC_CUSTOM_HEADERS` is reserved because custom headers can override authentication.
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

For example, this global definition shows DeepSeek V4 Pro as its model list
described it on 2026-10-12; **Add from provider list** fills the shared fields.
Agent-box does not ship a fixed DeepSeek registry. The empty key and disabled
connection must be configured before use.

```json
{
  "agents": {
    "external_apis": {
      "deepseek": {
        "enabled": false,
        "api_key": "",
        "models": {
          "deepseek-v4-pro": {
            "display_name": "DeepSeek-V4-Pro",
            "effort": "high",
            "supported_efforts": ["low", "high", "max"],
            "context_window": 1048576,
            "input_modalities": ["text"],
            "codex": {
              "supports_parallel_tool_calls": true,
              "supports_reasoning_summaries": true,
              "metadata": {"support_verbosity": true, "default_verbosity": "low"}
            },
            "claude": {"auto_compact_token_limit": 786432}
          }
        },
        "codex": {"url": "https://api.deepseek.com", "default_model": "deepseek-v4-pro"},
        "claude": {
          "url": "https://api.deepseek.com/anthropic",
          "default_model": "deepseek-v4-pro",
          "env": {"API_TIMEOUT_MS": "600000"}
        }
      }
    }
  }
}
```

OpenRouter serves both CLIs from one key: `https://openrouter.ai/api/v1` for
Codex and `https://openrouter.ai/api` for Claude, with model IDs such as
`deepseek/deepseek-v4.1-flash` or `anthropic/claude-opus-5`.

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

In each Codex/Claude candidate of a role's `models` or a worker/reviewer
`tiers.N` list, open **Model**. The single list includes
built-in models and configured models from enabled API connections, labeled
with their API. Selecting a row saves API and model together; project overrides
pin both, and resetting the model restores inheritance for both. Custom IDs can
be entered for any enabled API. Every candidate needs a model; to stop using one,
remove the candidate. For example, a project may select this global connection:

```json
{
  "workflow": {
    "worker": {
      "models": [
        {
          "provider": "codex",
          "api_provider": "deepseek",
          "model": "your-model-id",
          "effort": "default",
          "speed": "inherit"
        }
      ]
    }
  }
}
```

A native subagent always runs on its parent's API: both CLIs send the child's
requests to the session's endpoint with the session's credentials. A candidate on
another API therefore runs as a separate managed `codex exec` or `claude -p`
process, and a built-in candidate called from an external session uses one with
the original API environment restored. `workflow delegate` and `workflow review`
always run managed, so a worker, scout or reviewer reaches its candidate's API.
A changed endpoint, a deleted or disabled connection or a missing key fails
before anything runs. A native review runs on the coordinator's own API,
whatever the reviewer list names.

Owned native profiles are removed for external candidates when configuration is
applied. Existing role contracts, the reviewer's verdict format, review budgets
and fallback rules still apply. Candidate order keeps its meaning; subscription
usage and exhaustion are ignored for external candidates. Subscription fast mode
is unsupported for external workflow candidates. Managed review from an external
session needs configured reviewer models. See
[workflow models](workflow.md#models-and-routing).

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

External Codex launches use its ephemeral credential store, so they neither read
nor delete the saved subscription login. External launches bypass agent-box Remote Control, do not start an RC
app-server or acquire its slot, and reject explicit remote/session-reuse flags.
Claude's external session settings also disable native RC startup. A normal
session and an external process can coexist without changing the parent's
endpoint, auth, settings or RC lifecycle. See [Remote Control](remote-control.md).

Compatibility evidence for these boundaries is recorded in the
[integration design notes](../design/integrations.md#external-api-providers).

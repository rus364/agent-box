# agent-box

`agent-box` runs Codex CLI and Claude Code inside the same rootless Podman development container.

The container mounts the host `~/code` tree at the same logical path, preserves agent configuration and authentication, provides the development tools used by the projects in that tree, and can pass through KVM when available.

## Requirements

- Linux
- Podman
- `~/code` must exist (it may be a symlink)

## Install the launcher

Clone the repository somewhere under `~/code`, then expose the launcher on `PATH`:

```bash
ln -s ~/code/agent-box/agent-box ~/.local/bin/agent-box
ln -s ~/code/agent-box/codex-usage ~/.local/bin/codex-usage
ln -s ~/code/agent-box/claude-usage ~/.local/bin/claude-usage
```

If the repository lives at another path under `~/code`, adjust the source path accordingly.

## Build

```bash
agent-box build
```

The image is also built automatically on first use.

Image management commands:

```bash
agent-box build    # regular cached build
agent-box update   # refresh frequently updated tools while preserving base layers
agent-box rebuild  # refresh tools and rebuild without cache
```

The default image is `localhost/agent-box:latest`. Override it with `AGENT_BOX_IMAGE`.

## Run Codex

```bash
agent-box codex
agent-box codex exec "inspect this repository"
```

Codex remains the default agent for compatibility with the previous launcher, so these are equivalent:

```bash
agent-box
agent-box codex
```

To change the default:

```bash
export AGENT_BOX_AGENT=claude
```

## Run Claude Code

```bash
agent-box claude
agent-box claude -p "summarize this repository"
```

Claude Code is installed from the official `@anthropic-ai/claude-code` npm package. Its `~/.claude` state is mounted from the host so authentication, settings, and history survive disposable container runs.

If `~/.claude.json` already exists on the host, it is mounted as well.

The launcher also forwards common Anthropic/provider variables when they are already set on the host, including `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_BASE_URL`, Bedrock/Vertex selectors, and common HTTP/TLS proxy variables.

## Usage limits

The optional helpers query the subscription usage endpoints without sending a
model prompt:

```bash
codex-usage
claude-usage
```

They print remaining short/weekly quota and time until reset:

```text
83/61 · 3h24m/4d11h
```

An unavailable window is shown as `—`. Responses are cached for 60 seconds to
avoid unnecessary polling. `claude-usage --statusline` can also consume the JSON
provided by Claude Code's `statusLine` integration.

## Shell and diagnostics

Open a shell in the same container environment:

```bash
agent-box shell
```

Check both agents, development tools, QEMU, KVM, and agent skill links:

```bash
agent-box doctor
```

## KVM

When `/dev/kvm` is accessible, it is passed through automatically. Disable passthrough explicitly with:

```bash
AGENT_BOX_KVM=0 agent-box codex
```

ARM64 QEMU remains available through TCG.

## Persistent host state

The launcher uses:

```text
~/.local/share/agent-box/
~/.codex/
~/.claude/
```

Container runs themselves are disposable (`podman run --rm`).

## Migration from codex-box

The launcher, image, hostname, environment variables, and persistent state directory were renamed from `codex-box` to `agent-box`:

```text
codex-box                         -> agent-box
CODEX_BOX_IMAGE                   -> AGENT_BOX_IMAGE
CODEX_BOX_KVM                     -> AGENT_BOX_KVM
localhost/codex-box:latest        -> localhost/agent-box:latest
~/.local/share/codex-box/         -> ~/.local/share/agent-box/
```

The old `codex-box` launcher is intentionally not kept in the repository.

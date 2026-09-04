# Agent browser

Off by default. When enabled, containerized Claude and Codex sessions get an MCP
server driving a real headless browser, so an agent can open a page, read it,
follow links and fill forms on its own — useful for reading documentation,
checking a locally served app, or reproducing something that only shows up in a
browser.

```text
Claude / Codex
      |
      | stdio MCP
      v
Playwright MCP
      |
      v
headless Chromium
```

All three run inside the workspace container that is already there. No browser
sidecar, no Podman pod, no HTTP MCP service, no host browser integration, no
X11/Wayland forwarding.

## Enabling it

This is two decisions, because the browser is two things: a Chromium in the
image, and a session wired to drive it.

```bash
agent-box config
```

1. Turn on the **`browser` image tool group** — Chromium and Playwright MCP are
   roughly 400 MB, so they are built only when asked for.
2. Answer yes to *"Let containerized agents drive a headless browser"*.

Then:

```bash
agent-box build     # once, to add the browser layers
agent-box claude
```

`agent-box config show` and `agent-box doctor` both say plainly when the switch
is on but the image has no browser to drive.

Once the image has it, turning the *switch* on and off costs nothing: the
browser is not part of the container's identity, so it never rebuilds or
destroys a running workspace box. It applies to the next `agent-box claude` or
`agent-box codex` — a session started earlier keeps running without it.

Both agents see the server under the same name:

```text
agent_box_browser
```

It is added for that invocation only. Your `~/.codex/config.toml` and your
Claude MCP configuration are never rewritten, and the MCP servers you
configured yourself keep working alongside it.

`agent-box --host claude` and `agent-box --host codex` never get it: the browser
is a capability of the workspace image, and agent-box does not rewrite your host
agent configuration to fake one.

## What it does and does not touch

| | |
| --- | --- |
| Browser | Chromium, headless, from the image — installed at build time, never downloaded during a session |
| Profile | Isolated and in-memory, new for every session |
| Cookies / logins / localStorage | Discarded when the session ends |
| Your own browser's profile and cookies | Never exposed — nothing from the host is mounted |
| Screenshots, snapshots, traces | Written under `~/.local/share/agent-box/runtime/workspaces/<id>/browser/<session>/output`, never into your repository |
| Concurrent sessions | Separate profile and output directory each |

Because every session is isolated, an agent cannot reuse a login from an earlier
session, and cannot reach a site that depends on your own browser's
authentication. That is deliberate: a page the agent visits must not be able to
act as you.

## Trust boundary

> [!IMPORTANT]
> Browser automation is **not** a separate security boundary. The browser, the
> Playwright MCP server, Claude/Codex, shell processes, the mounted project, and
> every other enabled integration all live inside the same workspace box, as the
> same Unix user. Content the browser loads is on the *inside* of that boundary,
> and anything a page can persuade the agent to do, the agent can already do to
> your project.

Treat enabling the browser the way you would treat handing the agent any other
network-reading capability. The container still runs with `--cap-drop=all`,
`--security-opt=no-new-privileges` and rootless `--userns=keep-id`, and none of
that is relaxed for the browser. Chromium's own internal sandbox is unavailable
under those flags — Playwright launches its bundled Chromium with `--no-sandbox`,
as it does in any container — and agent-box does not weaken the Podman hardening
to change that. The container, not Chromium, is the boundary.

## Checking it

```bash
agent-box doctor
```

Enabled and healthy. The check actually starts a headless Chromium, renders an
offline `data:` URL and exits; it needs no network and is bounded by a short
timeout:

```text
playwright-mcp:      Version 0.0.80
browser chromium:    OK (153.0.8010.12)
browser integration: enabled
```

Disabled is a normal state, not an error:

```text
browser integration: disabled (enable it with 'agent-box config')
```

## Troubleshooting

**The agent says it has no browser tools.** Check `agent-box config show`:
*Agent browser* must be `on`, and it must not say the image has none. The server
is added per invocation, so a session started before you enabled it keeps
running without one — start a new session.

**Chromium fails to start.** `agent-box doctor` prints the launch error.
Chromium needs a writable `$HOME/.config` for its crash-reports database, which
the image provides; an image built by hand without agent-box's build args can
lack it. A `chrome_crashpad_handler: --database is required` message is that
failure. Rebuild with `agent-box build`.

**It cannot open a site that needs me to be logged in.** Expected. Each session
gets an isolated, in-memory profile and your own browser's cookies are never
exposed, so there is no session for the agent to reuse. Have the agent log in
within its own session, or point it at something that does not require your
credentials.

**Screenshots are cluttering my repository.** They should not be: Playwright MCP
is pointed at a per-session output directory under
`~/.local/share/agent-box/runtime/workspaces/<id>/browser/`. An agent writing
into the project is doing so with its ordinary file tools, not through the
browser. `agent-box stop` clears that workspace's browser output.

## Limitation

amd64 and arm64 only, because those are the architectures Playwright publishes
Chromium builds for. On anything else the build fails with an explanation and
tells you to turn the group off.

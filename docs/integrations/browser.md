# Agent browser

The optional `agent_box_browser` MCP server gives Claude and Codex an isolated
headless Chromium. Playwright MCP and Chromium come together in one official
image, pinned by digest. Each agent session gets its own browser container and
in-memory profile, in both ordinary workspace mode and native `--host` mode.

The host launcher owns Podman. A workspace agent connects over a private Unix
socket to a fixed MCP server; it receives no Podman socket or general host
command endpoint. Native host agents use stdio directly. Browser control has no
published TCP listener.

## Enabling it

Open `agent-box config`, then **Workspace box → Browser → Enabled**
(`box.browser.enabled`). Projects may override the isolated-browser setting.
It is off by default. First enabled launch downloads the pinned image; subsequent
launches reuse it. Enabling it does not rebuild the workspace image.

```bash
agent-box codex
agent-box --host claude
agent-box --no-browser codex
agent-box --host --no-browser claude
```

The server is added only to this invocation. User-owned Claude/Codex configuration
and other MCP servers remain intact. `--no-browser` does no browser image/start
work. If setup fails, the launcher explains the error and starts without browsing.

## What it does and does not touch

| Resource | Behavior |
| --- | --- |
| Browser runtime | Official Playwright MCP image, acquired by host Podman |
| Cookies/local storage | Isolated, non-persistent profile per MCP process |
| Workspace networking | Same network namespace as that workspace, including its localhost |
| Native host networking | Host networking; host localhost services are reachable |
| Files | Project at its absolute path, explicitly selected extra mounts with their access modes, session output directory |
| Credentials and desktop profiles | Not inherited from workspace credential mounts; no personal profile/cookie store mount |
| Artifacts | Automatically named files go to the session output directory; explicit relative filenames follow the MCP workspace root |
| Concurrency | Independent browser containers and profiles per agent session |

Workspace outputs live under the workspace runtime's `browser/<session>/output`;
host outputs under the host-session runtime's `browser/<session>/output`. These
roots follow agent-box's configured state location. Agent exit ends browser
processes; tmux detach keeps them alive. Explicit workspace stop also cleans up
owned browser containers left by an interrupted supervisor.

A file created only inside the workspace's writable layer or private `/tmp` is
not automatically visible to the sibling. Put uploads in the project or an
explicit shared mount. `--mount /path:ro` retains read-only access. The separate
host `/tmp` opt-in (`box.host_tmp` / `--host-tmp`) also shares host `/tmp`;
it is disabled by default. Additional
paths are not inferred from symlinks or copied from credential mounts. MCP file
operations may use the selected binds outside the client cwd; the container
mount set and read-only flags enforce host file access, rather than MCP roots.

## Trust boundary

Browser pages run in a separate rootless container with a smaller mount set than
the agent workspace. Project files and network access are still real capabilities:
read-write project mounts allow modification, and host mode can reach host
services. Do not treat page content as trusted instructions.

The runtime drops capabilities and uses `no-new-privileges`. The official image
runs Chromium without its internal sandbox; the container is the isolation
boundary. No host X11/Wayland socket, complete home, Podman socket, or personal
browser profile is mounted by default. An explicitly broader extra mount grants
the access its path and mode describe.

This feature does **not** attach to the user's authenticated desktop browser.
That is separate work with a separate opt-in. In particular, the Playwright
extension's tab chooser is not a one-tab permission boundary: its consent grants
browser-wide access. Isolated browsing never enables that extension or consent.

## Checking it

`agent-box doctor` performs a bounded offline Chromium render in the pinned
browser image when browsing is enabled. It uses no network and never pulls a
missing image. A missing image is diagnosed; enable browsing and start a session
online once to fetch it. An OK result proves that Chromium starts and renders,
not that any particular website or authenticated workflow succeeds.

Claude/Codex should show the `agent_box_browser` server for an enabled session.
Ask the agent to navigate to a local test page and save a screenshot to verify
the whole MCP path. A registration listing alone is not that functional test.

## Troubleshooting

- **No Podman or unusable rootless setup:** install/configure local rootless
  Podman as the launch diagnostic describes. Host agents require Podman only
  when the browser capability is enabled.
- **First-use download fails/offline:** retry online or use `--no-browser`.
  The pinned image is reused once cached; no engine/backend fallback occurs.
- **Workspace localhost:** it is the workspace's localhost. Host services need
  the workspace's existing explicit network configuration.
- **Upload not found:** move it into the project/shared mount. A workspace-only
  `/tmp` path is not a host bind source.
- **Broker unavailable:** restart the agent session; its private broker log is
  beside its output directory. An agent cannot reconnect to another session's
  browser by requesting an arbitrary host command.
- **Package changed while a workspace is running:** finish/stop and relaunch to
  adopt the new immutable runtime. Existing sessions are not rewritten.

## Limitation

Initial support is local Linux with local rootless Podman. Native host browsing
is headless and does not require a graphical desktop. Remote/VM-backed Podman,
SELinux relabeling requirements and architecture-specific image availability need
separate validation; a local amd64 test does not establish those combinations.
Proxy/custom-CA configuration inside the agent workspace is not automatically
copied into the browser container. No personal-browser or remote-desktop fallback
is attempted.

## Upgrading

Global and project schema 1 migrate `box.tools.browser` to
`box.browser.enabled` in schema 2, retaining explicit true/false and project
inheritance. Reading converts a copy; normal migration writes retain the original.
The browser no longer belongs to the workspace image tool list. New images omit
`playwright-mcp`, Chromium and the old browser smoke command; scripts that called
those inside the workspace must use the MCP capability or install their own
project tooling. Existing built images are not modified in place.

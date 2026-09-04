# Vikunja

Task tracking both Claude and Codex can read and update: projects, subprojects,
backlogs, priorities, dependencies, boards, comments, and structured imports.

Off by default. There is nothing to install or configure on the host first.

## Enabling it

```bash
agent-box config
```

Answer yes, then give it two things:

- **Server URL** — how *you* reach your Vikunja, e.g. `http://localhost:3456`
  or `https://tasks.example.com`.
- **API token** — created in Vikunja under *Settings → API tokens*.

Then apply and start a session:

```bash
agent-box install     # only if you edited config.json by hand
agent-box claude
```

That is the whole setup. agent-box installs its own copy of the Vikunja skill
into `~/.agents/skills/vikunja` and puts its CLI on the container's `PATH`, so
the agent has both the instructions and the tool.

> [!IMPORTANT]
> The token can read and rewrite your entire task tracker. Enabling this hands
> that ability to any agent running in the box.

## Where the settings live

In agent-box's own `~/.config/agent-box/config.json` (mode 0600), with every
other setting — not in a second configuration file you have to keep in step.

The container never sees that file. agent-box renders a minimal pair from it —

```text
~/.local/share/agent-box/runtime/vikunja/
    config.json     just the server URL
    token           just the token
```

— and mounts *that* read-only at `~/.config/vikunja`, which is where the skill's
CLI looks. Turning the integration off deletes the pair, so the credential stops
being reachable rather than merely being ignored. The same shape as
[notifications](telegram.md#notifications), for the same reason.

`~/.local/state/vikunja` is mounted read-write, so a partially applied
`vikunja import` stays resumable across boxes.

## Reaching a server on your own machine

A self-hosted Vikunja is usually addressed as `http://localhost:3456`. Inside a
container that address is the *container's* own loopback, and rootless Podman
hands the container the host's LAN addresses too — so forwarding the URL
verbatim would quietly address the wrong machine.

agent-box corrects the container's copy, never your setting:

| What you configured | What the container gets |
| --- | --- |
| `localhost`, `127.0.0.1`, `::1`, or an IP literal belonging to this machine | rewritten to `host.containers.internal`, keeping scheme, port and path |
| a DNS name that resolves to this machine, e.g. `http://pm.example.org:3456` | the URL unchanged, plus `--add-host=pm.example.org:host-gateway` |
| a genuinely remote server | unchanged |

The DNS case keeps the name rather than rewriting it because TLS certificate
validation and reverse-proxy virtual-host routing both depend on the server
seeing the name it was configured for — none of which survives a URL rewrite.

## Checking it

```bash
agent-box doctor
```

Reports the URL the container actually got, whether the token is present, and
the result of a real API call. When the call fails it also prints what the
hostname resolved to and what the container's own addresses are, which is the
difference between "the server is down" and "that address is this container".

# Web links and CLI callbacks

Inside the box, `xdg-open` (also set as `BROWSER`) displays HTTP(S) links on
stderr instead of failing, so CLIs that open a browser for login keep working
and their JSON on stdout stays clean. `agent-box links open URL` does the same
by hand. With host access enabled, agent-box can also open those links on your
desktop and route a CLI's localhost login callback back into the box, locally
or through an SSH or Coder tunnel.

The host link integration is separate from the [agent browser](browser.md) and
Remote Control. Both host access and automatic browser opening are off by
default. Configure **Workspace box → Web links** in `agent-box config`:

| Setting | Default | Purpose |
| --- | --- | --- |
| `box.links.enabled` | false | Expose the host broker and prepare temporary callback routes |
| `box.links.open_browser` | false | Request opening links with the local desktop browser |
| `box.links.ssh_destination` | empty | Workstation SSH alias or `user@server` for tunnel instructions |
| `box.links.coder_workspace` | empty | Coder workspace, optionally `owner/workspace.agent` |

These are global settings: a repository cannot enable host access. `config
show` and `config describe box.links` report them. A one-session override is:

```bash
agent-box --links --kubeconfigs --cloud-profiles
agent-box --no-links
```

The enabled broker is a capability of the **shared workspace box**, accessible
to all its processes. Enabling, disabling or changing its policy changes box
identity; an existing box refuses the change until `agent-box stop`. Detached
tmux sessions retain the broker. Stop or the box's own exit ends the broker
and its routes. Browser profiles, desktop sockets and host command execution
are not exposed to the box.

A box started inside a workspace box, through the `podman` tool group, gets no
broker: there is no host desktop inside the outer box, and a callback listener
there would sit on the outer box's loopback, not the workstation's. The launch
says so once, and links in the nested box are only displayed. A nested
launcher keeps its own broker directory, so it never reaches the outer box's
broker.

Once any session of a box is remote or headless, automatic opening stays off
for that box until it is stopped, and later sessions get manual instructions.

## Local workstation

The handler recognises a standard OAuth `redirect_uri` pointing to loopback,
or a direct loopback web URL. Before displaying or opening the link, it listens
on the same host loopback port and forwards connections to the CLI inside the
box, without publishing container ports. For `localhost` both IPv4 and IPv6
ports must be free.

With `open_browser=false`, open the displayed URL yourself. With it enabled,
agent-box requests the host's `xdg-open` only on a local graphical desktop.
An opener request does not prove that the page loaded or login succeeded.

## Remote server over SSH

The complete chain is:

```text
workstation browser → workstation loopback → SSH tunnel
  → server loopback → agent-box relay → container loopback → CLI
```

Set `ssh_destination` to an alias from the **workstation's** SSH config; it may
already include `ProxyJump`, an identity file and a custom port. If unset,
agent-box derives a suggestion from `SSH_CONNECTION`; without enough host
information it prints `USER@SERVER` and asks you to set the destination.

For a callback on port 42121 the handler displays a command such as:

```bash
ssh -N -o ExitOnForwardFailure=yes -L 127.0.0.1:42121:127.0.0.1:42121 dev-alias
```

Run it in another terminal **on the workstation**, then open the original
URL there. Keep the tunnel running until login finishes. For IPv6 callbacks
the command binds `[::1]` locally and targets `[::1]` on the server. The URL is
preserved; changing its redirect port/address could break provider checks.
`localhost` commands include both `-L` forwards so browser address selection
cannot send the callback to an unrelated workstation listener.

SSH, Coder, explicitly configured remote destinations and headless launches
never open the server's browser, even if `open_browser=true`.

## Coder workspace

Set `coder_workspace`, or use the workspace's `CODER_WORKSPACE_NAME` variable.
The handler displays, for example:

```bash
coder port-forward alice/myworkspace.main --tcp 127.0.0.1:42121:42121
```

Run this on the workstation and wait for Coder's `Ready!` before opening the
URL. Coder targets IPv4 loopback in the workspace; agent-box bridges that to
the CLI's actual callback address inside the nested box. An IPv6 callback uses
an explicit local address (`--tcp '[::1]:42121:42121'`) so a failed IPv6 bind
cannot be mistaken for a ready route. See [Coder port-forward](https://coder.com/docs/reference/cli/port-forward).
For `localhost`, the command includes both explicit `--tcp` forwards. Both
loopback families must be available; an IPv4-only host needs the CLI to use an
explicit `127.0.0.1` callback.

## Limits and diagnosis

- Routes expire after five minutes; requesting the same route renews it.
  There are at most 16 routes and 16 connections per route. Keep ordinary
  loopback web applications open through a deliberate port-forward for longer
  use; this integration is for opening links and completing CLI callbacks.
- A host port already in use is an error, not a usable callback. Free it or
  restart the CLI with another callback port. A workstation port collision is
  diagnosed by SSH/Coder; do not open the URL until the tunnel is ready.
- Unsupported schemes, file paths, credential-bearing URLs and ambiguous
  `redirect_uri` values are refused. Provider-specific callback parameters,
  custom URI schemes and CLIs launching a browser binary directly require
  their own documented flow. Device-code login simply displays its web URL.
- Authorization URLs stay in the live CLI output and memory; agent-box writes
  neither them nor callback contents into persistent logs or tracker comments.
- Without `--links`, displaying the URL starts no route. A fresh cached token
  can still work; an interactive remote login needs both the host route and
  the workstation tunnel.
- A CLI that discards child stderr and has no controlling terminal cannot
  display the handler's manual instructions. Run its interactive login from
  `agent-box shell` with a terminal; cached-credential opener discovery still
  works without one.

After upgrading the image, check the opener locally without cloud access:

```bash
agent-box shell -c 'command -v xdg-open; xdg-open https://example.invalid/'
```

How the broker is built is recorded in the
[integration design notes](../design/integrations.md#web-links).

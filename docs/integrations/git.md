# Git and GitHub

Deliberately conservative. Being logged in to `gh` on the host does **not**
give every workspace container a live GitHub credential.

```bash
agent-box config
```

| Setting | Default | What it exposes |
| --- | --- | --- |
| Git identity | on | `user.name` and `user.email` only |
| Full host Git config | off | your entire `~/.gitconfig` |
| GitHub token | off | `gh auth token` forwarded as `GH_TOKEN` |
| GitHub config copy | off | `~/.config/gh`, which stores that token on disk |

## Why identity is on and the rest is off

**Identity** is read one key at a time via `git config --get`. It is what commit
attribution actually needs and it is not a secret.

**The full config** is a different thing entirely. A real `~/.gitconfig`
routinely carries `credential.helper` entries, `http.<url>.extraHeader` values
holding bearer tokens, `url.insteadOf` rewrites, and `include.path` directives
pulling in further files. Handing all of that to an agent container is a
deliberate act.

**The GitHub token** is a live credential to everything your account can reach.
An earlier agent-box forwarded it automatically whenever `gh` happened to be
authenticated, which handed every container a credential nobody asked for.

## How it reaches the container

Git and `gh` configuration is copied into a per-workspace writable snapshot
rather than bind-mounted from your home directory. The host stays the source of
truth, and temporary changes made inside a box disappear with that box.

The token itself is forwarded by *name* (`--env=GH_TOKEN`), so its value never
lands in the Podman argv, which `/proc` exposes to other processes on the host.
Inside the box, `git` is pointed at `gh auth git-credential` for `github.com`
only — so the helper is asked for GitHub and for nothing else, and the token
value is never written into a config file.

Only HTTPS GitHub authentication is forwarded. SSH keys and the host SSH agent
are never mounted.

## One-off overrides

```bash
AGENT_BOX_GH_TOKEN=1 agent-box claude    # force it for this run
AGENT_BOX_GH_TOKEN=0 agent-box claude    # withhold it for this run
```

## Checking it

```bash
agent-box doctor
```

Reports the Git identity the box will commit with and which GitHub account, if
any, `gh` is authenticated as inside the container. `gh` being unauthenticated
there is the default state, not a fault.

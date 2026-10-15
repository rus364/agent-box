# Workspace and security model

A workspace box is one trust boundary. Claude, Codex, shells, hooks and
daemons inside the same box share one Unix user and PID namespace; agent-box
isolates the box from the host, not the agents within it from each other.

## What a box can reach

The workspace root is chosen simply:

1. Inside a Git worktree: its top-level directory.
2. Outside a Git worktree, including beneath a bare repository: the current
   directory.
3. `--workspace PATH` overrides both when you deliberately want a broader
   tree.

The box sees that root at the same absolute path as the host, plus explicit
`--mount` paths, the host-backed agent state (`~/.claude`, `~/.codex`, the
shared `~/.agents` tree) and what the integrations you enabled expose. Every
credential and every optional capability is a switch that starts off; see the
[integrations](integrations/README.md) for what each one exposes.

## Container hardening

Every workspace box runs with:

```text
--userns=keep-id
--cap-drop=all
--security-opt=no-new-privileges
```

The container user mirrors your passwd identity, so files created in the
workspace belong to you on the host rather than to root. Host paths come from
the passwd entry, never from `$HOME`, so a poisoned environment cannot move a
bind mount, a state directory or the container's home.

The opt-in `podman` image group keeps these flags and adds `SETUID`/`SETGID`,
`/dev/net/tun`, unmasked `/proc` paths and two seccomp syscall allowances for
nested namespace setup. It never enables privileged mode or exposes a host
container-engine socket; see [nested Podman](nested-podman.md).

## What agent-box is not

agent-box is a local workspace runtime, not an agent platform. It provides
isolated local Claude and Codex execution, workspace lifecycle, mounts and
identity handling, detachable sessions, controlled credential exposure and
local integrations. It does not implement a cloud scheduler, a multi-user
control plane, a Telegram bot platform or a remote-control protocol of its
own.

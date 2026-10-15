# Troubleshooting

## Diagnostics

```bash
agent-box doctor      # inside a real box: tools, credentials, integrations
agent-box selftest    # fast checks, no Podman, no network
python3 tests/run_suite.py
```

`tests/run_suite.py` runs the full test suite with isolated paths, so it never
touches your real configuration and also passes inside a workspace box.

## Common problems

**Podman is missing, too old or not rootless.** agent-box names the problem
and the fix: the install command for your distribution, the required version,
or the `usermod` line that adds a free subordinate UID/GID range. See
[Requirements](installation.md#requirements).

**Claude or Codex is not authenticated.** Log in with the CLI on the host.
agent-box mounts the host-backed agent state rather than keeping a second
credential store.

**`gh` is unauthenticated inside the box.** That is the default; see
[Git and GitHub](integrations/git.md).

**The workspace root rejects the current directory.** `--workspace` names a
directory that does not contain the current one. Change directory or pass
another path.

**A tool is missing from the box.** Check `agent-box config show`: its image
group may be off. Enable it, then run `agent-box build`. Project-specific
toolchains belong in the project, declared through `mise`.

**A shared skill is missing or points somewhere unexpected.** Run
`agent-box doctor`, then `agent-box config apply`. agent-box never overwrites
a skill directory or link it did not create; it reports it and keeps yours.

**A tmux session failed without output.** A wrapped session reports only its
exit status. Rerun with `--no-tmux` to see what the command printed.

## Known limitations

- Linux with rootless Podman 5.0 or newer only.
- Workspace boxes are transient. Detached sessions survive a lost terminal,
  but not a host reboot or a forced container kill.
- `~/.agents` is mounted as a whole tree. It is read-only except for the
  `skills/` subtree and its external symlink targets.
- The agent browser is amd64 and arm64 only, the architectures Playwright
  publishes Chromium for.
- Host audio needs a PulseAudio-compatible server on the host (PulseAudio or
  PipeWire's `pipewire-pulse`) at `/run/user/<uid>/pulse/native`, and exposes
  both microphone and speakers.
- The official Claude Telegram plugin is outside agent-box's control and may
  need compatibility updates when its internals change.
- There is no remote equivalent of `--mount`, and Telegram cannot create a new
  workspace box by itself.

# Integration design notes

For contributors. How individual integrations are wired, the evidence behind
their limits, and how to develop and test agent-box itself. User behaviour is
on the [integration pages](../integrations/README.md).

## Credential copies

Git, `gh` and `glab` configuration, registry logins, kubeconfigs and cloud CLI
profiles reach a box as copies, never as bind mounts of the host files. The
host stays the source of truth; changes made in the box disappear with it.

- Copies are rendered into private per-workspace directories (mode 0700, files
  0600) and mounted read-only where the tool allows it (registries,
  kubeconfigs) or writable where it must refresh tokens (`yc`, `aws`). Variables
  carry paths only, so no credential appears in Podman's argv. Turning a
  forwarding off or emptying its list removes the copies and the mount.
- A box keeps its creation-time switches and selections. Registry logins,
  kubeconfigs and cloud profiles are re-rendered from those choices at every
  launch, which is how renewed host logins (for example a 12-hour Yandex IAM
  token) reach a running box. Git, `gh` and `glab` configuration copies are seeded only
  when the box is created. The GitHub token is not a copy: it is forwarded per
  exec, but tmux sessions inherit the environment of the launch that started
  the box's tmux server.
- **Git.** Identity is read key by key with `git config --get`. The GitHub
  token is forwarded by name (`--env=GH_TOKEN`, or the existing
  `GH_TOKEN`/`GITHUB_TOKEN`) so its value never lands in argv, which `/proc`
  exposes. The box's Git uses `gh auth git-credential` for `github.com` only,
  so the token is never written into a config file, and with the token on the
  snapshot rewrites `git@github.com:` and `ssh://git@github.com/` remotes to
  HTTPS. An earlier design forwarded the token whenever `gh` was
  authenticated, which handed every box a credential nobody asked for.
- **Registries.** Each source file becomes its own filtered copy, never merged:
  Helm often holds registries Podman does not. Where both container auth files
  hold one registry, a key from the second counts unless the first has the
  same or a wider one, matching Podman's lookup. `repositories.yaml` is parsed
  without a YAML library, for the forms Helm writes (plain, quoted, wrapped and
  literal-block values); other forms are skipped with their line, and of two
  entries with one name the first counts, as in Helm. `REGISTRY_AUTH_FILE`,
  `HELM_REGISTRY_CONFIG` and `HELM_REPOSITORY_CONFIG` are honoured, which is
  also how a nested launch forwards what its box was given.
- **Kubeconfigs.** Files are never merged into one `KUBECONFIG`, because
  kubeconfigs often reuse names such as `default` and kubectl silently takes
  the first match.
- **Cloud profiles.** The box's `config.yaml` holds only the chosen profiles,
  copied line for line; `current` stays the host's when chosen, else the first
  chosen. Federated profiles bring `credentials/<profile>` with IAM and refresh
  tokens. agent-box never runs `yc` on the host or writes the host's directory.
- **AWS profiles.** Standard-library INI parsing preserves selected settings
  and follows role sources, SSO sessions and service sections. Token filenames
  follow AWS CLI v2's `SSOTokenLoader` (SHA-1 of session name or legacy start
  URL) and `generate_login_cache_key` (SHA-256 of `login_session`). These
  upstream cache formats are private conventions, tested against AWS CLI
  2.38.2 with synthetic credentials and networking disabled; changes require
  revalidation. Login refresh needs the cached DPoP key as well as the token.
  Directory-descriptor writes reuse the cloud renderer's no-follow and atomic
  replacement helpers, including cache cleanup. Live SSO, login refresh,
  role assumption and EKS authorization require a separately authorized check.

## Notifications

- The bot token lives in `config.json` (0600). agent-box derives a two-key
  `~/.local/share/agent-box/runtime/notify.json` only while notifications are
  usable and deletes it when they are off. Persistent boxes receive the values
  through the notification-capable exec environment rather than a mount, so
  they never enter the box's metadata.
- Claude's hooks and Codex's `notify` share one JSON formatter and Telegram
  sender: model output is sanitized, delivery-limited and falls back to plain
  text. Codex `notify` delivers only `agent-turn-complete`, needs no `/hooks`
  trust and sees no approval requests.
- The Codex block is prepended to `config.toml` without rewriting the rest and
  removed only while unchanged. Managed delegates run with
  `--ignore-user-config`, so they do not inherit it.
- `agent-box config` refuses to save an enabled Telegram channel before the
  plugin and token exist: the bootstrap order is necessarily run Claude,
  install the plugin, come back, and storing it early would only produce a
  setting every later run warns about.

## Remote Control

- Codex Remote Control runs a private app-server per session; the terminal
  connects over a private Unix socket. No public port is opened and the shared
  Codex daemon is never started, stopped or reconfigured. The server receives
  the launch's `-c` overrides, model and permission settings, in the same
  provider home, project directory and namespaces; `config.toml` and profiles
  are not rewritten.
- The one-session-per-`CODEX_HOME` slot is a lock. Detaching keeps it; a normal
  exit stops the server's process group, including its npm launcher and a
  server past the graceful timeout. A forced supervisor kill can leave the
  server holding the slot until it exits; no other session kills it. Natively
  managed Remote Control daemons do not take part in the lock.
- Codex rejects `/resume` of a remote thread when the TUI has explicit
  permission overrides (observed in 0.154.0; the diagnostic remains in 0.163.0).
  Container permission bypass is now opt-in for both agents, independently of
  Remote Control. Disabled adds no permission override. Enabled and explicit
  user flags remain on both Codex clients: dropping them only from the TUI would
  silently change new-thread permissions, because its thread/start settings
  override server defaults. Command construction alone does not establish
  authenticated remote resume or phone acceptance.

## Web links

- The broker socket lives in an owner-checked private directory under `/tmp`,
  which also works on Coder or headless hosts without `/run/user/<uid>`. The
  host policy file sits outside the mounted socket directory, so the box cannot
  change it; broker recovery keeps it, and only stop or recreation removes it.
  Broker exit closes every callback listener.
- Connection hints refresh when another session launches or attaches. New
  SSH or Coder details replace the previous transport, a headless attachment
  keeps the last hint, and a new callback request moves its listener to match.
- Callback routes bind the host loopback port and reach the CLI's loopback in
  the box through `podman exec`, so they need neither published ports nor
  pasta/slirp. `localhost` prepares IPv4 and IPv6 atomically.
- The offline smoke test uses a fake HTTP callback in real Podman. It proves the
  host-to-container route and remote-mode instructions, not a real identity
  provider or an external SSH/Coder deployment.

## External API providers

- Routes expose CLI, API name, model and an endpoint/catalog fingerprint, never
  keys or key fingerprints; an accepted route records `api_provider` and
  `api_fingerprint`.
- A native review is recorded on trust with the provider and model the
  coordinator names. Codex `spawn_agent` takes the session API's catalog
  `model` and `reasoning_effort`; Claude's Agent tool accepts only aliases,
  which `ext` maps to the session model, and the session's
  `CLAUDE_CODE_EFFORT_LEVEL` overrides a profile's effort.
- Local probes used Codex 0.154.0 and Claude Code 2.1.269 with temporary
  homes, fake keys and synthetic HTTP APIs. Codex TUI and exec worked while the
  Remote Control lock was held, with no Remote Control descendant observed.
  Codex `model/list` returned the configured display name, image modality and
  efforts, and an explicit `-c model_reasoning_effort="max"` overrode the
  model default. Claude `-p` against a loopback Messages stub reported a
  `[1m]` context as 1,000,000 tokens, stripped the suffix on the wire and sent
  `max` effort; an interactive PTY run showed the replacement `/model` picker.
  None of this proves live commercial APIs, automatic compaction at the
  threshold or phone-paired Remote Control.

## Host audio

Claude Code's `/voice` uses the ALSA default device, which the image routes
through the PulseAudio ALSA plugin to `$PULSE_SERVER`, the host socket
`/run/user/<uid>/pulse/native` bind-mounted into the box. The socket is part of
the box's identity, so its appearance or disappearance needs a new box.

## Nested Podman

- **Codex sandbox.** The image gives Codex's bundled `bwrap` an empty
  file-capability set when the `podman` group is on. Linux clears ambient
  capabilities when that file executes, so bwrap can start its unprivileged
  sandbox; no capability is granted, the binary is unchanged and Codex's digest
  check holds. Clearing capabilities around the whole Codex process instead
  would break creating a nested Podman user namespace under
  `no-new-privileges`. Replacing Codex by hand inside a box can drop the
  attribute. `python3 tests/smoke_codex_sandbox.py --podman on|off` checks the
  ambient capabilities, then offline reads, read-only and workspace-write
  boundaries and unchanged CLI settings; it invokes no model.
- **Doctor probe.** Each Podman command has a 20-second timeout. The probe uses
  the first local image of `AGENT_BOX_IMAGE`, `docker.io/library/alpine:3.23`
  or `docker.io/library/busybox:latest` with an explicit entrypoint, no network
  and `--pull=never`, checks a writable filesystem and removes only its
  uniquely named container. Errors are `FAILED` independently of CLI
  availability.

## Developing agent-box inside agent-box

The `agent-box` on PATH carries its build context and can build an image or
launch the next box from any project directory. Inside a box with the `podman`
group, keep the outer shell open. In an agent-box source checkout, create an
editable environment with its own entry point so the mounted launcher stays
available for the outer session:

```sh
uv venv .venv
uv pip install --python .venv/bin/python --editable .
.venv/bin/agent-box build
.venv/bin/agent-box shell -c 'id; podman info'
python3 tests/run_suite.py
.venv/bin/agent-box selftest
```

The inner launcher inherits the rendered image groups from the outer session.
Its builds and boxes use the inner engine and never touch the host's box. Build
markers, workspace records and generated policy live in a writable
`~/.local/share/agent-box-launcher-<namespace>` inside the container for its
lifetime; notification state and forwarded workflow state keep their shared
mounts, and an explicit `AGENT_BOX_STATE_DIR` still wins. The outer engine's
image store is not exposed, so the first inner launch builds an image and may
download packages. Configuration-applying tests must keep the runner's path
isolation: agent configuration directories mounted from the host are still the
host's files.

`sh tests/smoke_nested_podman.sh` in an enabled box checks a real build,
multiple-UID ownership, `run`/`exec`, networking, a published port, a bind mount
and named-volume reuse; it downloads Alpine packages and removes only what it
created.

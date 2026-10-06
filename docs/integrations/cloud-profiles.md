# Cloud CLI profiles

Off by default. Profiles you choose from the host's cloud CLIs can be forwarded
into the box, so agents run those CLIs there: `yc` itself, and kubeconfigs
whose users sign in with `exec: yc …` (see
[Kubernetes configs](kubeconfigs.md)). Yandex Cloud is the one provider so far;
others join the same section and the same switch.

**A profile is the whole cloud account.** A kubeconfig reaches one cluster; a
forwarded profile lets the agent do everything that account may do in the
cloud, and a federated profile comes with a refresh token valid for up to a
month. Forward a profile only to a box you would hand that account to.

```bash
agent-box config    # Credentials → Cloud CLI profiles
```

| Setting | Default | What it exposes |
| --- | --- | --- |
| Enabled | off | Whether the chosen profiles are forwarded at all |
| Yandex Cloud (yc) → yc profiles | none | The chosen profiles from `~/.config/yandex-cloud`, with their sign-in |

The menu lists the profiles in the host's `~/.config/yandex-cloud/config.yaml`
and ticks none for you. Each row shows how the profile signs in:
`federation`, `service account key`, `OAuth token` or
`instance service account` (that one works only on a Yandex Cloud VM). A
profile chosen earlier that the host no longer has stays listed, so you can
untick it. No token, key or other value is shown.

## The yc tool

`yc` is the image's `yc` tool group, off by default. It is installed on `PATH`
and at `~/yandex-cloud/bin/yc`, where yc's own installer puts it and so where
the kubeconfigs `yc managed-kubernetes cluster get-credentials` writes look for
it. Choosing a profile while forwarding is on ticks that group for you, and
`agent-box build` installs it;
the group can also be ticked alone, for `yc` without any profile. The menu
will not untick the group while profiles are forwarded. If forwarding is on
and the group is off anyway (through `config set` or `--cloud-profiles`), or
the image has not been rebuilt yet, the launch says so and still starts.

The same pairing holds for [kubeconfigs](kubeconfigs.md) and the `kubernetes`
group, which carries `kubectl` and Helm.

## In the box

The box gets its own `~/.config/yandex-cloud`, writable, holding:

- `config.yaml` with only the chosen profiles, each copied line for line from
  the host. `current` stays the host's current profile when it is chosen,
  otherwise it is the first chosen one.
- `credentials/<profile>` of each chosen federated profile: the IAM token and
  the refresh token.

The host's history, logs and every other profile stay on the host. agent-box
runs no `yc` command on the host and never writes to the host's
`~/.config/yandex-cloud`.

Inside the box `yc` works as usual, and renews a federated profile's IAM
token from its refresh token without a browser. It writes that token, its
history and its logs into the box's copy. At the next launch the copy is
rendered from the host again: the host is the source, so `yc config set` in
the box does not last, and a profile unticked or removed on the host leaves the
box.

### Federated profiles

A browser sign-in happens only on the host, with `yc` as usual. The box picks
up the renewed sign-in at its next launch. Once the refresh token expires
(31 days), `yc` in the box cannot sign in again: sign in on the host and launch
again.

Yandex Cloud reissues a refresh token used in its last seven days. Whether the
old one stays valid is not documented: if it does not, a box that renewed
it leaves the host's copy stale, and the host needs a browser sign-in once.

## How it reaches the container

The copies are written into a private per-workspace directory (mode 0700,
files 0600), and that directory is mounted as the box's
`~/.config/yandex-cloud`. No profile value appears in Podman's arguments.
Turning forwarding off or emptying the list removes the copies and the mount.

The mount is part of the box's identity. Turning forwarding on or off, or
emptying or filling the list, recreates the box; a box that is still running
refuses the change until `agent-box stop`. Changing which profiles are chosen
only changes the copies.

## One-off overrides

```bash
agent-box --cloud-profiles claude       # forward the chosen profiles for this session
agent-box --no-cloud-profiles claude    # withhold them for this session
```

Both act on the saved choice and save nothing; `--cloud-profiles` with
nothing chosen is refused.

# Container registries and Helm repositories

Off by default. Logins you made on the host with `podman login`,
`helm registry login`, `helm repo add` or a script such as `yc-cr-auth` can be
forwarded into the box, so agents reach private images and charts without
logging in again in every box.

```bash
agent-box config    # Credentials → Container registries
```

| Setting | Default | What it exposes |
| --- | --- | --- |
| Enabled | off | Whether the chosen logins are forwarded at all |
| Registry hosts | none | The chosen hosts' entries from the podman/skopeo and Helm OCI auth files |
| Helm repositories | none | The chosen entries of `repositories.yaml`, with their credentials |

The menu lists what it finds on the host and ticks nothing for you. Each host
says which file holds its login. A host or repository chosen earlier that the
host no longer has stays listed, so you can untick it. No credential value is
shown.

## Where logins come from

| Host file | Read by | In the box |
| --- | --- | --- |
| `$XDG_RUNTIME_DIR/containers/auth.json`, then `~/.config/containers/auth.json` | podman, skopeo, buildah | `REGISTRY_AUTH_FILE` |
| `~/.config/helm/registry/config.json` | helm (OCI) | `HELM_REGISTRY_CONFIG` |
| `~/.config/helm/repositories.yaml` | helm repositories | `HELM_REPOSITORY_CONFIG` |

Each file is filtered into its own copy and never merged with another: Helm
often holds registries podman does not. For a host present in both container
auth files, the runtime one wins, as in podman. agent-box copies entries
already written; it never runs a login command on the host. Logins kept by a
credential helper (`credHelpers`, `credsStore`) cannot be copied and are
reported at launch, as are chosen hosts or repositories the host no longer has
and files it cannot read. A launch never fails over them.

## How it reaches the container

The copies are rendered into a private per-workspace directory (mode 0700,
files 0600) and that directory is mounted read-only. The variables carry paths
only, so no credential appears in Podman's arguments. Turning forwarding off
or emptying the lists removes the copies and the mount.

The copies are rendered again at every launch. A login renewed on the host,
such as a Yandex IAM token after 12 hours, reaches an already running box at
the next `agent-box` launch for that project, without recreating it. The box
keeps its own Helm index cache; refresh it with `helm repo update`. Logging in
inside the box fails, because the copies are read-only.

The mount and the variables are part of the box's identity. Turning forwarding
on or off, or emptying or filling one of the two lists, recreates the box; a
box that is still running refuses the change until `agent-box stop`. Changing
which hosts or repositories are chosen only changes the copies.

## One-off overrides

```bash
agent-box --registries claude       # forward the chosen logins for this session
agent-box --no-registries claude    # withhold them for this session
```

Both act on the saved choice of hosts and repositories and save nothing.

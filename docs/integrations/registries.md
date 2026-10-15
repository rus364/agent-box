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

When `REGISTRY_AUTH_FILE`, `HELM_REGISTRY_CONFIG` or `HELM_REPOSITORY_CONFIG` is
set, that file is read instead, as the tools themselves do. This is also how a
launch nested inside a box forwards the copies that box was given.

Each file is filtered into its own copy containing only the chosen hosts and
repositories. agent-box copies logins already written; it never runs a login
command on the host. Logins kept by a credential helper (`credHelpers`,
`credsStore`), Helm repository entries in YAML forms Helm itself does not
write, chosen entries the host no longer has and unreadable files are reported
at launch, and the launch continues without them.

## How it reaches the container

The copies are mounted read-only, and the variables carry only their paths.
The copies are rendered again at every launch. A login renewed on the host,
such as a Yandex IAM token after 12 hours, reaches an already running box at
the next `agent-box` launch for that project, without recreating it. The box
keeps its own Helm index cache; refresh it with `helm repo update`. Logging in
inside the box fails, because the copies are read-only.

The running box retains the forwarding switch, chosen hosts and repositories,
mounts and variables from creation. Later launches refresh only those choices,
even if the saved selection changed. New choices take effect after
`agent-box stop` and the next launch; an explicit conflicting switch requires
that restart.

## One-off overrides

```bash
agent-box --registries claude       # forward the chosen logins for this session
agent-box --no-registries claude    # withhold them for this session
```

Both act on the saved choice of hosts and repositories and save nothing;
`--registries` with nothing chosen is refused.

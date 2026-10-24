# k3s cluster

Off by default. When enabled, every project gets its own Kubernetes cluster:
a single-node [k3s](https://k3s.io) in a sidecar container next to the
workspace box, in your rootless Podman. The agent uses it with `kubectl` and
`helm` from the box as it would any cluster: it can deploy, test and tear down
without a cluster of yours in reach.

```text
workspace box (unprivileged)             k3s sidecar (rootless Podman)
  kubectl ── 127.0.0.1:PORT ── pasta ──► host 127.0.0.1:PORT ──► API :6443
  KUBECONFIG (read-only)                  own network, own volume
```

No root on the host and no extra privilege for the box. The sidecar is
privileged only inside its own user namespace, which is as far as rootless
Podman reaches.

## Enabling it

In `agent-box config`, open **Workspace box → k3s cluster** and turn on `enabled`
(`box.k3s.enabled`). `kubectl` and `helm` come from the `kubernetes` tool
group, which is on by default; agent-box warns when the cluster is on and that
group is off.

Keep the global switch off when only some projects need a cluster. For one
launch, with or without a project configuration file:

```bash
agent-box --k3s codex
agent-box --no-k3s codex
```

These flags do not save settings. A running box keeps its cluster choice;
an opposite explicit flag requires `agent-box stop` before relaunching.
Both flags also work with `--host`. Host and container joins inherit the
active project choice, including when it is disabled; an explicit conflicting
flag requires stopping the project sessions and relaunching. Nested boxes still withhold k3s and
report why, including when `--k3s` was explicit.

```bash
agent-box build     # optional: fetches the k3s image (about 250 MB) ahead of time
agent-box claude
```

The first launch starts the cluster and waits for a Ready node, about ten
seconds once the image is present. Inside the box `KUBECONFIG` points at the
cluster, and the context is named `agent-box`:

```bash
kubectl get nodes
helm install ...
```

A project can switch it on for itself in `.agent-box.json`:

Use **Project settings → Workspace box → k3s cluster** in `agent-box config`,
or `agent-box config set --project box.k3s.enabled=true`. The file can also
include the other cluster settings:

```json
{"box": {"k3s": {"enabled": true, "traefik": true, "ports": ["8080:80"], "manifests": "deploy/k3s"}}}
```

`agent-box --host` uses the same `box.k3s.enabled` setting and cluster recipe.
With k3s enabled it starts or reuses the project's rootless-Podman sidecar,
waits for a Ready node and checks `/readyz` from the host before starting the
agent. Install `kubectl` on the host; install `helm` there if your work needs it.
Failure stops the launch instead of falling back to another Kubernetes context.

Both modes use the existing managed kubeconfig under
`~/.local/share/agent-box/runtime/workspaces/<id>/k3s/kubeconfig` (mode `0600`).
The host agent receives its path through its own `KUBECONFIG` environment;
there is no per-session copy or merge with your kubeconfig. Your shell's
variable and `~/.kube/config` stay unchanged. Disabled host sessions retain
ordinary host Kubernetes access; host mode is not a credential sandbox.

The first active session chooses the project policy. Later host/container
sessions inherit it even if saved enablement changes. A host-only project
created without k3s cannot gain it through another session's flag. End all
sessions before changing that choice. Running containers with an older runtime
must be explicitly stopped/relaunched before a host session can share them.

Native support requires local Linux/rootless Podman. Remote Podman and nested
host-k3s launches are refused. Over SSH, the API loopback belongs to the Linux
host executing agent-box, not your workstation; no tunnel or LAN publication
is added.

A box started inside a workspace box, through the `podman` tool group, gets
no cluster either: k3s cannot start in nested Podman. The launch says so,
`agent-box doctor` there reports `none in a nested box`, and `agent-box build`
there does not fetch the k3s image. The outer box keeps its own cluster.

## Settings

Settings live under **Workspace box → k3s cluster** (`box.k3s`), except the
port editor under **Workspace box → Network**. All may be set per project;
project port publications are restricted to loopback.

| Setting | Default | Effect |
| --- | --- | --- |
| `enabled` | off | Run the shared cluster and select its kubeconfig in both launch modes |
| `lifetime` | `persistent` | `persistent`: runs until `agent-box stop`, so deployments survive between sessions. `box`: stops after the box and last host session end |
| `persistent_data` | on | Off deletes the cluster's data at every stop: each start is a clean cluster |
| `traefik` | off | k3s's ingress controller |
| `servicelb` | on | Addresses for LoadBalancer services, Traefik's included |
| `metrics_server` | off | `kubectl top` and HorizontalPodAutoscalers |
| `local_storage` | on | The `local-path` StorageClass for PVCs |
| `network_policy` | on | NetworkPolicy enforcement |
| `cpus`, `memory` | no limit | Limits for the whole sidecar, such as `2` and `4g` |
| `ports` | none | `[IP:]HOST:CONTAINER[/tcp|udp]`; defaults to host `127.0.0.1` and TCP |
| `manifests` | none | A project directory k3s applies itself (YAML and `HelmChart`), mounted read-only |
| `registry_mirrors` | none | `{"docker.io": ["https://mirror.example"]}` rendered into k3s's `registries.yaml` |
| `extra_args` | none | Extra `k3s server` flags, such as `["--cluster-cidr=10.60.0.0/16"]` |
| `version` | pinned | A `rancher/k3s` tag such as `v1.33.5-k3s1`; empty is the release agent-box pins by digest |
| `ready_timeout` | 180 | Seconds a launch waits for a Ready node |

Changing anything but `lifetime`, `persistent_data` and `ready_timeout`
recreates the sidecar container on the same data volume. A recreated sidecar
publishes a new API port, so the box is recreated with it. While the box is
running, a launch with changed settings refuses instead: run `agent-box stop`
first.

**Opening the cluster from your browser.** With `traefik` on and
`"ports": ["8080:80"]`, an Ingress answers at `http://127.0.0.1:8080`. Ports
default to host loopback. Explicit non-loopback binds are available only in
global settings. See [workspace networking](../networking.md). From inside the box use
`kubectl port-forward` as with any cluster.

**Bootstrapping a project.** `manifests` names a directory of the project, such
as `deploy/k3s`. k3s applies its YAML and `HelmChart` resources at start and
again whenever a file changes. A path that leaves the project, directly or
through a symlink, is refused.

**Choosing a version.** k3s cannot downgrade existing data. Before moving to an
older `version`, delete the cluster with `agent-box stop --cluster-data`.

There is no choice of CNI: the cluster uses k3s's flannel and kube-proxy.
Cilium needs BPF privileges in the host's own user namespace, which no rootless
container can hold.

## Lifetime and data

| Event | Cluster | Data |
| --- | --- | --- |
| The box ends (`lifetime: persistent`) | keeps running | kept |
| Last box/host consumer ends (`lifetime: box`) | stopped by agent-box | kept unless `persistent_data` is off |
| A host session detaches | keeps running | kept |
| `agent-box --host stop` | stops after host sessions end, unless a box still uses it | follows creation-time retention policy |
| `agent-box stop` | stopped | kept, unless `persistent_data` is off |
| `agent-box stop --cluster-data` | removed | deleted |
| `agent-box workspaces rm` / `prune` | removed | deleted after you confirm |
| `enabled` turned off | stopped when the next box is created | kept |
| `agent-box uninstall` | removed | kept |
| `agent-box uninstall --purge` | removed | deleted after you confirm |

Ordinary `agent-box stop` refuses before changing anything while native host
sessions remain. End them with `agent-box --host stop` first. Workspace removal,
prune and uninstall likewise refuse to remove resources beneath tracked host
sessions. An exit of one session never stops a cluster used by another.

With `lifetime: box`, a box that ends on its own (its last detached session
finished) is noticed at the next agent-box command for the project. An abruptly killed host runner is likewise recovered by the next project
command; its surviving agent still counts as a consumer. Until
then the idle cluster uses about 500 MB of memory and a few percent of a core.

The data lives in the Podman volume `agent-box-k3s-<workspace id>`; the
container and network carry the same name. `agent-box workspaces` lists every
project's cluster with its data size and directory, and `agent-box workspaces
rm` deletes a cluster together with the rest of what that project keeps.

## Trust boundary

> [!IMPORTANT]
> The agent is cluster-admin in its cluster. A privileged pod, a `hostPath: /`
> volume, `hostPID` or `hostNetwork` all land in the **sidecar**: its
> filesystem, its processes, its network. Your home directory is not mounted.
> The sidecar has its own `/tmp`; workspace extra mounts are not inherited.
> Root in a pod is root in the sidecar's user namespace, which maps to your
> own user on the host.

What the cluster adds over the box alone:

- Pods have the same outbound network as the box.
- A privileged pod has a wider kernel surface than the unprivileged box,
  comparable to the `podman` tool group.
- Registry mirrors carry no credentials: a URL with a user or password is refused.
- The kubeconfig holds the cluster's admin client certificate. agent-box
  renders it under
  `~/.local/share/agent-box/runtime/workspaces/<id>/k3s/` with mode `0600` and
  mounts that directory read-only into the box.

## Requirements

- cgroup v2 with the `cpu`, `cpuset`, `io`, `memory` and `pids` controllers
  delegated to your user, because the kubelet creates cgroups for its pods.
  agent-box reads what Podman reports and does not depend on the init system;
  the first launch names any controller that is missing. On a systemd host,
  delegate them with a drop-in for `user@.service` containing
  `Delegate=cpu cpuset io memory pids`, then log in again.
- Rootless Podman with pasta networking (Podman 5 and later).
- amd64 or arm64.

## Checking it

```bash
agent-box doctor
```

The check runs inside the box through the same kubeconfig an agent uses:

```text
k3s cluster:         OK (agent-box-k3s-0123456789abcdef   Ready   control-plane   2d   v1.36.4+k3s1)
```

Disabled is a normal state:

```text
k3s cluster:         disabled (turn on box.k3s in 'agent-box config')
```

## Troubleshooting

**The launch says the cluster was not ready in time.** The message ends with
the sidecar's log. A slow first start, such as images still being pulled for
Traefik, can need a larger `ready_timeout`.

**`kubectl` says the connection was refused.** The sidecar was recreated or
stopped after the box started. End the box with `agent-box stop` and launch
again; the new box forwards the current port.

**A name is taken.** agent-box never touches a container, network or volume
named `agent-box-k3s-…` that it did not create for the project. Rename or
remove that object yourself.

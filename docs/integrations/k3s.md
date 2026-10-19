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
Both flags are refused with `--host`. Nested boxes still withhold k3s and
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

`agent-box --host` sessions get no cluster. A host agent works in your own
environment with your own kube contexts, and agent-box does not redirect its
`kubectl` to another cluster behind your back.

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
| `enabled` | off | Run the cluster and give the box its kubeconfig |
| `lifetime` | `persistent` | `persistent`: runs until `agent-box stop`, so deployments survive between sessions. `box`: stops when agent-box removes the box |
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
| The box ends (`lifetime: box`) | stopped by agent-box | kept |
| `agent-box stop` | stopped | kept, unless `persistent_data` is off |
| `agent-box stop --cluster-data` | removed | deleted |
| `agent-box workspaces rm` / `prune` | removed | deleted after you confirm |
| `enabled` turned off | stopped when the next box is created | kept |
| `agent-box uninstall` | removed | kept |
| `agent-box uninstall --purge` | removed | deleted after you confirm |

With `lifetime: box`, a box that ends on its own (its last detached session
finished) is noticed at the next agent-box command for the project. Until
then the idle cluster uses about 500 MB of memory and a few percent of a core.

The data lives in the Podman volume `agent-box-k3s-<workspace id>`; the
container and network carry the same name. `agent-box workspaces` lists every
project's cluster with its data size and directory, and `agent-box workspaces
rm` deletes a cluster together with the rest of what that project keeps.

## Trust boundary

> [!IMPORTANT]
> The agent is cluster-admin in its cluster. A privileged pod, a `hostPath: /`
> volume, `hostPID` or `hostNetwork` all land in the **sidecar**: its
> filesystem, its processes, its network. None of your home directory or other
> host files is mounted there. Root in a pod is root in the sidecar's user
> namespace, which maps to your own user on the host.

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

# Networking audit and proposed interface

Task [#48](https://github.com/rus364/agent-box/issues/48), audited on
2026-10-18 against `ed65554db71f524a67088a341f173db3b1feb84e`.
This is a design record, not documentation of available new settings or flags.
The owner selected both directions of traffic, loopback publication by default,
explicit user control of LAN exposure, and explicit reauthorization before joining
a workspace created with a temporary host-network override. Convenience means
describing the service and its destination rather than asking users to understand
pasta options. Product implementation follows this audit separately.

## Current topology

```text
desktop browser -- host published port --> workspace application
                    (not implemented by agent-box yet)

desktop browser -- 127.0.0.1:8080 --> k3s sidecar:80 --> ingress --> pod
workspace kubectl -- localhost:API_PORT -- pasta -T --> host:API_PORT
                                                     --> sidecar:6443

desktop browser -- outer publication --> workspace -- inner publication --> podman container
```

The host, workspace, k3s sidecar, cluster pods and nested Podman containers
normally have different network namespaces. A port number alone does not name
a route between them.

| Surface | Current implementation and consequence |
| --- | --- |
| Workspace network | `runtime.container_lifetime_args` supplies no general network selection or publication. Host Podman's configured default applies. With k3s enabled, `runtime.k3s_box_args` explicitly selects `pasta:-T,API_PORT`. |
| Outbound/DNS | Podman supplies workspace routing and resolver configuration. The audited host defaults to pasta. Ordinary external DNS and HTTPS work in the disposable workspace; this is not a guarantee for every VPN, proxy or host resolver. |
| Host aliases | `runtime.CONTAINER_HOST_ALIAS` is `host.containers.internal`. Podman supplies its address. It is not the workspace's localhost and did not reach a host listener bound only to `127.0.0.1` in this test. |
| k3s | `k3s.container_args` gives the sidecar a dedicated named network and volume, a random host-loopback API publication and `box.k3s.ports` publications. `runtime.k3s_box_args` makes the API reachable at the same loopback port inside the workspace. The kubeconfig is mounted read-only. |
| Cluster services | ClusterIP and service DNS belong to the cluster. API publication does not route the workspace into the pod/service network. `kubectl port-forward` is an additional API-mediated route, with its listener living wherever kubectl runs. |
| Nested Podman | `Containerfile`, `nested_podman.CONTAINERS_CONF` and `runtime.nested_podman_args` configure rootless slirp4netns, a separate storage volume and limited extra permissions. Inner `-p` publishes into the workspace. Inner host mode shares the workspace network. No host Podman socket is passed. |
| Browsers | The built-in isolated browser uses the workspace network. A desktop browser uses the host network. Opening a link does not publish its destination or attach authenticated desktop tabs; task #44 owns browser attachment. |

`box.k3s.ports` is currently under **Workspace box → k3s cluster**, globally
and per project. `K3sConfig.validate` accepts `HOST:CONTAINER` strings with
ports 1–65535 and rejects duplicate host ports. `k3s.container_args` renders
`--publish=127.0.0.1:HOST:CONTAINER`; TCP is Podman's default. There is no
explicit UDP/address syntax, generic workspace publishing, or workspace network
mode flag. `--k3s`/`--no-k3s` select the integration; native `--host` does not
create a workspace or k3s sidecar. These are feature boundaries, not demonstrated
product defects.

## Options

| Option | Decision |
| --- | --- |
| Move/rename only k3s ports | Reject: it neither publishes workspace applications nor explains the destination. It also needlessly changes a public configuration key. |
| One networking menu, separate destinations | Recommend: workspace publishing, k3s publishing, access to selected host services and network mode are distinct actions in one place. Reuse the existing editors and configuration ownership. |
| Replace everything with a generic target/routing schema | Defer: target selectors and migrations add complexity without helping the two common destinations. |
| Host mode for all networking needs | Reject as a default: shared localhost, port collisions and loss of network isolation are unnecessary for ordinary development servers. Retain as an explicit option. |

## Recommended configuration and commands

The following names are proposed, not implemented. All networking settings
appear under **Workspace box → Network**. The k3s port editor edits its existing
key; it must not maintain a second copy of the same list.

| Setting | Default | Scope and meaning |
| --- | --- | --- |
| `box.network.mode` | `default` | User only; `default` preserves the ordinary Podman backend, `host` shares the host network. |
| `box.network.ports` | `[]` | Workspace publications. Global or project; project entries may bind only loopback. |
| `box.k3s.ports` | `[]` | Keep existing sidecar publications and their interpretation. Global or project, with the same restriction on project exposure. |
| `box.network.host_ports` | `[]` | User only; selected host listeners forwarded into workspace localhost. |

For both publication lists, retain `HOST:CONTAINER` and add optional explicit
address/protocol: `[BIND_ADDRESS:]HOST:CONTAINER[/tcp|udp]`. Omission always means
`127.0.0.1` and TCP, independent of a host-wide Podman publication default.
Support bracketed IPv6 addresses. A non-loopback address, including `0.0.0.0`
or `[::]`, is an explicit host-user choice, unavailable in project settings.
The editor labels this choice **Accessible from other computers**, displaying
the actual bind address. No additional global allow-LAN switch should silently
reinterpret old loopback mappings or authorize arbitrary repository bindings.

`host_ports` uses `HOST:WORKSPACE[/tcp|udp]`: for example, `5432:15432`
means workspace `localhost:15432` reaches host `localhost:5432`. Keep host-first
notation consistent across settings, with a directional preview in the editor.
Translate that example to `pasta:-T,15432:5432`; UDP uses `-U`. Existing k3s API
forwarding is composed with these forwards, not supplied as a competing network
argument. Selected host forwarding requires pasta; diagnose an unsupported
backend rather than silently replacing it or enabling host mode. Do not add a
raw pasta-arguments setting.

Proposed CLI examples:

```sh
agent-box --publish 8080:3000 codex
agent-box --host-port 5432:15432 codex
agent-box --network=host codex
agent-box --network=default codex
```

Repeated publication/host-port flags replace the corresponding saved list for
that launch, avoiding accidental additive exposure; `--no-publish` and
`--no-host-ports` explicitly clear those lists. Reject simultaneous clear/set
flags. `--publish` targets the workspace, never k3s. k3s remains configured
through its named editor. CLI addresses are explicit user choices, not project
overrides. Reject these flags with native `--host`, which executes directly on
the host and has no container network to configure.

Validate exact ports and protocol, no ranges or automatic port allocation in
the initial interface. Those can be added later if a concrete use case justifies
the discovery/UI cost. Podman's ability to allocate random ports remains useful
for the internal k3s API and disposable tests.

## Container identity and conflicts

CLI mode overrides the saved user mode. A requested `default` overrides a saved
`host`. Repository configuration cannot select host mode or selected host forwards.
An existing running box does not change its network for a new exec session.

| Requested versus running box | Behavior |
| --- | --- |
| Same effective mode/mappings | Join after checking actual Podman state and the saved creation policy. Concurrent sessions share those settings. |
| Different mode or explicit mappings | Refuse with the current/requested values and the explicit stop/relaunch command. Never interrupt existing sessions automatically. |
| Ordinary launch after a temporary host override | Refuse unless the user explicitly requests host again, or the saved user mode now explicitly authorizes host. Do not silently inherit wider access. |
| Box has stopped/ended | The next creation uses current saved settings and that invocation's overrides. A previous temporary choice does not persist into config. |

Extend `workspace_box_policy`, `apply_box_policy`, `existing_workspace_spec`
and container-spec construction together. Inspect actual `NetworkMode` and
port bindings; a saved policy alone is not proof of compatibility. Lock the
creation/admission transition and recheck container identity before exec, as
the current lifecycle does. Do not infer authorization from journal text.

Host network is still a container: filesystem/user namespace and the existing
capability restrictions remain. It shares host localhost, listeners and port
space. Applications binding wildcard addresses can become LAN-visible even
without any publication setting. Inner Podman publication then lands on the
actual host, and inner host mode shares that same network. Explain this at the
host-mode control; do not advertise the loopback publishing default as protection
against host-mode applications binding `0.0.0.0`.

Reject workspace publications and selected host forwards in host mode; give
the user the corresponding clear flags instead of silently discarding settings.
k3s sidecar publications remain valid because the sidecar keeps its own network.
Host-mode workspace kubectl reaches the published API directly: omit the pasta
forward, preserve kubeconfig authentication, and make the current pasta-specific
compatibility check in `runtime` mode-aware. Never give the k3s sidecar host mode.

Check duplicate host bind-address/port/protocol tuples across workspace and
sidecar publications, including wildcard overlap. Check workspace listener ports
against selected forwards and the reserved API forward. TCP and UDP are separate
bindings. Different workspaces compete for the same host endpoints; the actual
bind result remains authoritative because preflight checks race with other
processes. Never kill an existing listener or silently change a requested port.

Diagnostics must name the destination container, bind address, protocol and
port. Show the browser URL and the required application listen address. For
example: `127.0.0.1:8080 -> workspace:3000/tcp; application must listen on
0.0.0.0:3000`. IPv6 publication requires a matching IPv6 listener. Distinguish
an occupied host port from a successful publication whose application is absent
or listening only on container loopback. DNS/host aliases are not substitutes
for an explicit host-loopback forwarding path.

## Executable evidence

Disposable tests used the local `localhost/agent-box:latest` image
`43f679881758`, host Podman `6.1.4-1.1`, pasta `2026_10_02.cba3570`,
and k3s `v1.36.4+k3s1` (`4dedb15b`). Nested Podman was `5.7.0`,
slirp4netns `1.3.3`, libslirp `4.9.1`. No product configuration was applied.

| Case | Observed result |
| --- | --- |
| Default isolation | Workspace and host network namespace IDs differ; workspace localhost does not reach the host-only listener. |
| Host alias | `host.containers.internal` resolves, but connecting to the test host-loopback TCP listener is refused. |
| DNS/outbound | External DNS resolves; HTTPS to example.com returns 200. docs.podman.io returns HTTP 403: transport works, but that request is not an application success. |
| Workspace TCP/UDP | Host clients receive `ab48-workspace` and `ab48-udp` through explicit loopback publications. |
| Selected host TCP/UDP | Workspace ports 15432/15433 reach different, ephemeral host ports through `-T`/`-U`; responses are `ab48-host` and `ab48-host-udp`. |
| Conflicting publication | A second container requesting the same host TCP endpoint fails with `Address already in use`; the first remains available. |
| Container loopback listener | Publication to an application listening only on container `127.0.0.1` resets; listening on the container interface succeeds. |
| IPv6 publication | Host `[::1]` receives the marker from an IPv6 listener in the workspace. The initial IPv4-only listener fails through that IPv6 mapping. |
| Explicit non-loopback publication | A connection to the host's own LAN address receives the marker. This does not prove reachability through a firewall from another physical machine. |
| Repeated/concurrent use | Two concurrent execs see the same namespace; stop/start retains the explicitly published endpoint and returns the marker again. |
| Host mode | Container and host namespace IDs match; the container reaches the host-loopback listener directly. |
| Nested pasta workspace | Both workspace and host clients receive `ab48-nested` through inner publication and outer publication respectively. |
| Nested host workspace | Inner publication is directly reachable from the physical host. Inner `--network=host` matches the outer namespace in both tested modes. |
| k3s API | Authenticated `kubectl get --raw=/readyz` returns `ok` from both a pasta workspace using the API forward and a host-network workspace without it. |
| k3s application | Host NodePort and Traefik Ingress requests return `ab48-k3s-workload`, not merely a successful TCP handshake. |
| Cluster DNS | After CoreDNS rollout, the application pod resolves `kubernetes.default.svc.cluster.local` to the cluster service address. |

The first k3s probe incorrectly invoked `/bin/k3s kubectl`; using the image's
`kubectl` command fixed the harness. Other intermediate failures were startup
races (the default service account/CoreDNS/Traefik not yet installed) and a probe
missing the workspace's `--userns=keep-id`, which prevented reading its private
kubeconfig. Corrected probes wait for controllers and use the actual user mapping.
These failures do not establish product bugs. They show why node Ready and an
unauthenticated API HTTP 401 are insufficient acceptance.

The first cleanup revealed three anonymous volumes declared by the k3s image.
Their absence in the baseline and creation times identified them as test-owned;
they were removed explicitly. Subsequent cleanup used `rm -fv`. Some intermediate
inventory comparisons overlapped other disposable probes; final restoration is
checked after every probe ends, against the original shared baseline.

Remaining limits: no second-machine LAN/firewall test, remote Podman machine,
VPN-specific DNS, or different backend/version/platform matrix. Proposed
agent-box mode admission, list overrides and configuration migrations do not
exist yet; their repeated/concurrent CLI acceptance belongs to implementation.
No general product networking defect was reproduced. The confirmed gaps are
missing workspace publishing/host controls and the need to explain listener
address, destination and direction in the UI.

Final restoration passed: container IDs and start times, network IDs and volume
names match the original baseline. No existing container was restarted. The
temporary test kubeconfig was deleted; the tracked project configuration is
unchanged, and the host config modification time predates the audit
(2026-10-17 12:45:36 UTC). The first probe also verified equal before/after
configuration hashes. No live workspace settings, external environments or
cloud CLI were used. The existing focused k3s suite passed all 42 tests.

## Compatibility and implementation boundaries

This audit changes documentation only. Adding disabled-by-default features
without changing old semantics qualifies for a MINOR release. Menu regrouping
does not require a persisted migration. Retain `box.k3s.ports`, old strings and
their loopback/TCP interpretation. A new parser must still reject malformed
and duplicate mappings. Removing/reinterpreting public keys would instead need
a migration and a MAJOR-contract assessment; this design avoids it.

Assess global/project schema versions and workspace creation-policy format before
implementation. Retain published fixtures, direct skipped-version upgrades,
failure/retry and data-preservation tests. Future schemas must be refused before
parsing/writing; originals must survive a schema-changing write. New policy
fields must not retrofit permissions into existing immutable runtimes. Apply
package integration changes through the existing single writer, with explicit
stop/relaunch to adopt a new runtime.

Bounded follow-ups, in dependency order:

1. Workspace publishing, the networking menu, compatible k3s syntax extension,
   user/project exposure checks and actionable diagnostics.
2. Selected host TCP/UDP forwarding, composition with the k3s API path and
   backend/listener conflicts.
3. Host mode, saved/CLI precedence, creation/admission policy, concurrent and
   repeated launches, k3s and nested-container integration.

Each includes its configuration/CLI tests, relevant live topology cases, upgrade
fixtures where needed and user documentation. These are design boundaries, not
automatically created or selected implementation tasks. The audit demonstrates
the underlying network mechanisms; it does not establish acceptance of flags
and lifecycle checks that have not been implemented.

## Sources and reproduction

Primary documentation checked on 2026-10-18:

- [Podman run](https://docs.podman.io/en/latest/markdown/podman-run.1.html):
  network namespaces, pasta options, host aliases, publishing and protocols.
- [pasta manual](https://passt.top/builds/latest/web/passt.1.html): `-t/-u`
  versus `-T/-U`, port translation and loopback delivery.
- [k3s networking services](https://docs.k3s.io/networking/networking-services):
  CoreDNS, Traefik and ServiceLB.
- Repository contracts: [k3s](../integrations/k3s.md),
  [nested Podman](../nested-podman.md), [security](../security.md),
  [host execution](../usage.md#host-mode) and
  [direct upgrades](../compatibility.md#direct-upgrades).

Reproduce with uniquely named disposable containers, no user home/config mounts,
and only test-service ports. Inventory existing container IDs, networks, volumes
and configuration hashes first. Use `--pull=never` for the existing workspace and
k3s images. Keep test kubeconfigs private and delete them at cleanup. Remove only
named test containers with `podman rm -fv` (k3s declares anonymous volumes), then
their dedicated networks/volumes; compare the final inventory to the baseline.
Never use a global prune.

Representative Podman arguments exercised:

```text
workspace publication: -p 127.0.0.1::18080/tcp -p 127.0.0.1::18080/udp
selected host TCP:    --network=pasta:-T,15432:HOST_PORT
selected host UDP:    --network=pasta:-U,15433:HOST_PORT
host network:         --network=host
explicit LAN bind:    -p 0.0.0.0::18080
IPv6 loopback bind:   -p [::1]::18080
nested publication:   inner -p 0.0.0.0:18080:8080 plus outer publication
k3s API:              sidecar -p 127.0.0.1::6443, workspace pasta:-T,API_PORT
k3s services:         sidecar -p 127.0.0.1::30080 and -p 127.0.0.1::80
```

Test listeners returned distinct fixed markers, rather than treating socket
creation as proof of application connectivity. Nested tests used the repository's
seccomp-profile extension and capability/device set with a fresh storage volume.
k3s used `k3s.ENTRYPOINT`/`server_args`, a dedicated network and storage volume,
a BusyBox HTTP pod, NodePort 30080 and a Traefik Ingress. Wait for the service
account and controller deployments as well as the node before probing them.

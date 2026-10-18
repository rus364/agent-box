# Workspace networking

Use **Workspace box → Network** in `agent-box config` for workspace publications,
k3s publications, selected host services and network mode. Settings affect a
container lifetime; stop the workspace before changing them. `config show`
reports the effective values, and a launch with network options prints the
traffic direction and required application listener.

## Publish a service

```sh
agent-box --publish 8080:3000 codex
agent-box --publish 5353:5353/udp shell
agent-box --publish '[::1]:8080:3000' codex
```

`--publish` targets an application in the workspace. The first example makes
it available on this computer at `http://127.0.0.1:8080`; the application must
listen on `0.0.0.0:3000` inside the workspace, not only on its localhost.
For an IPv6 publication, use an IPv6 listener such as `[::]:3000`.

Both `box.network.ports` and `box.k3s.ports` accept lists of
`[IP:]HOST:CONTAINER[/tcp|udp]`. The defaults are **127.0.0.1 and TCP**.
Ports must be 1–65535; ranges and automatic allocation are not accepted.
IPv6 addresses need brackets. Duplicate or overlapping binds, including
workspace/k3s conflicts when k3s is enabled, are refused. A busy host port
fails the launch; agent-box never kills its listener or chooses another port.

To make a workspace service accessible from other computers, explicitly bind
a host interface or wildcard, for example `--publish 0.0.0.0:8080:3000`.
Host routing and firewall rules still apply. Only user settings and launch
flags can authorize non-loopback publication. Project settings may publish
workspace and k3s ports on loopback only.

k3s ports are edited in the same Network menu but remain stored in
`box.k3s.ports`; for example, `["8080:80"]` publishes a configured cluster
ingress. `--publish` never targets k3s. See [k3s settings](integrations/k3s.md#settings).

## Reach a host service

```sh
agent-box --host-port 5432:15432 codex
agent-box --host-port 5353:15353/udp shell
```

`HOST:WORKSPACE[/tcp|udp]` forwards the selected host localhost listener into
workspace localhost. The first example lets a workspace client connect to
`127.0.0.1:15432` to reach the host's `127.0.0.1:5432`. All processes in that
workspace can use the service. `host.containers.internal` is a separate host
alias and does not guarantee access to services bound only to host loopback.

Save these mappings in `box.network.host_ports` globally. Projects cannot
request them. Forwarding requires the host's rootless backend to be pasta;
agent-box diagnoses other backends instead of silently replacing them.
TCP/UDP forwards and the internal k3s API forward share one pasta configuration.
The k3s API's selected workspace TCP port cannot also be used by a workspace
publication or user host forward.

## Share the host network

```sh
agent-box --network=host codex
agent-box --network=default codex
```

`box.network.mode` defaults to `default`: preserve the ordinary rootless
Podman backend. `host` is an explicit global or launch choice. It shares host
localhost and port space, while retaining the container's filesystem and user
namespace. Wildcard listeners may become accessible from other computers.
Nested Podman publication then lands directly on the host as well.

Host mode rejects workspace publications and selected host forwards because
there is no separate workspace network to map. Use `--no-publish
--no-host-ports` if saved lists need clearing for this launch. k3s retains its
separate network, publications and authenticated API; the workspace reaches
its loopback API directly, without pasta.

With web-link integration enabled, a host-network callback already listens on
host loopback, so the broker does not create a second listener. Remote tunnel
instructions still apply; Coder's IPv4 endpoint retains a bridge for an explicit
IPv6 callback.

`--network=host` is different from native `--host`, which runs the agent
outside a workspace container. Native `--host` refuses all these networking
flags.

## Defaults, overrides and running sessions

| Setting | Default | Where it can be set |
| --- | --- | --- |
| `box.network.mode` | `default` | Global, `--network` |
| `box.network.ports` | `[]` | Global, project loopback only, `--publish` |
| `box.network.host_ports` | `[]` | Global, `--host-port` |
| `box.k3s.ports` | `[]` | Global, project loopback only |

Repeated `--publish` or `--host-port` values replace that saved list for the
launch. `--no-publish` and `--no-host-ports` clear the corresponding list.
Setting and clearing the same list together is an error. Flags never save
configuration. `--network=default` overrides even a saved host mode.

A running box keeps its network. Matching effective settings can join it;
different mode/mappings are refused without interrupting any session. The
launcher verifies actual Podman mode and bindings against the creation policy.
Changes to enabled k3s publications also require stopping the workspace.
An ordinary launch after a temporary `--network=host` is refused: repeat the
flag, or explicitly run `agent-box stop` and relaunch normally. The same rule
prevents silently inheriting temporary publications or host forwards. A stopped
box's next creation uses the current saved settings and new launch flags.

For [nested containers](nested-podman.md#network-and-storage-boundaries), publish
at both layers: inner `-p 0.0.0.0:18080:8080` plus outer
`agent-box --publish 8080:18080 ...`. Inner loopback-only publication cannot be
reached by an outer publication. Desktop browsers use the host network; the
built-in browser uses the workspace network. Opening a URL does not publish it.

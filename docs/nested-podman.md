# Nested rootless Podman

Enable the `podman` group under **Image tool groups** in `agent-box config`,
then run `agent-box build`. The group is off by default. Changing it changes
the workspace definition. Finish active work and run `agent-box stop` before
launching a workspace with the new permissions.

Inside the workspace, use ordinary Podman commands:

```sh
podman info
podman build -t localhost/my-app .
podman run --rm -p 127.0.0.1:8080:8080 localhost/my-app
```

Podman and its OCI runtime run rootless. The image configures native overlay storage,
`crun`, disabled inner cgroups, file-based events, and `slirp4netns` for both
builds and containers. There is no daemon or host Podman socket to configure.
Image ownership is preserved across multiple UIDs; files are not squashed to
one UID. UID/GID allocations come from the building Podman's actual mappings,
so an image built inside a workspace can support another level of nesting.
Rebuild if the outer host's subordinate ID allocation changes.

## Network and storage boundaries

`slirp4netns` handles networking in userspace and connects it through a TAP
interface. The outer workspace receives `/dev/net/tun`; no host network
namespace is shared. A nested container's `-p` publishes into the **workspace**
network. For example, a service published at `127.0.0.1:8080` is reachable from
a shell in that workspace. It is not automatically published on the physical
host. `--network=host` on an inner container shares the workspace's network.

Bind mounts can use paths visible inside the workspace. Images, build cache,
containers and named volumes live in a dedicated outer volume named
`agent-box-podman-<workspace-id>`, independently of the host engine's existing
images and containers. This volume survives workspace removal and recreation;
running inner processes stop with the outer workspace. There is no automatic
volume deletion when the group is disabled. To discard that data, first end
the workspace's sessions, then explicitly remove its named volume with the
outer Podman. Source files stay in the mounted project directory.

Podman's pause and network helpers count as workspace workloads and can keep
the workspace alive after an interactive shell exits. Use `agent-box stop`
from its parent environment when that workspace and its nested processes are
no longer needed.

The dedicated volume avoids unsupported overlay-on-overlay storage without
passing `/dev/fuse` or sharing the host engine's graphroot. Its backing
filesystem must support native rootless overlay. Cgroup resource limits, systemd/Quadlet supervision and
privileged workloads are not supported in this mode. Ordinary image pulls,
builds with `RUN`, `run`/`exec`, named volumes and port publication are supported.

## Outer permissions

Enabling the group preserves `--userns=keep-id`, `--cap-drop=all` and
`--security-opt=no-new-privileges`, and adds:

- `SETUID` and `SETGID` for the image's `newuidmap`/`newgidmap` helpers, which
  use file capabilities instead of setuid-root permissions.
- `/dev/net/tun` for the userspace network backend.
- `unmask=/proc/*` so inner namespaces can mount their own proc filesystem.
- Unconditional `chroot` and `sethostname` allowances in a copy of the
  effective outer Podman seccomp profile. The kernel still checks namespace
  capabilities. Other profile rules remain in effect; seccomp is not disabled.

The profile copy lives in agent-box runtime state; launching does not edit
host Podman configuration. No `SYS_ADMIN`, privileged mode, host engine socket
or host engine graphroot is passed into the workspace. All processes within a
workspace remain one trust boundary, including nested containers.

The outer Podman must be rootless, have subordinate UID/GID mappings, an
errno-default seccomp allowlist, and access to `/dev/net/tun`. Host policies
such as SELinux or AppArmor may impose additional restrictions; agent-box
does not disable those policies or change host sysctls automatically.

## Developing agent-box inside agent-box

Keep the outer shell open and run the checkout's commands inside it:

```sh
./agent-box build
./agent-box update
./agent-box shell -c 'id; podman info'
python3 tests/run_suite.py
./agent-box selftest
```

The inner invocation inherits the rendered image-group settings from the
outer session, including `podman`. Its image build and workspace use the
inner engine. They do not update or recreate the physical host's workspace.
The test runner redirects state and integration paths. Tests of configuration
application must retain that isolation: agent configuration directories mounted
from the physical host are still host-owned files.

Run `sh tests/smoke_nested_podman.sh` inside an enabled workspace to check a
real build, multiple-UID ownership, `run`/`exec`, networking, a published HTTP
port, a bind mount and named-volume reuse. It downloads Alpine packages and
removes only the uniquely named resources it creates.

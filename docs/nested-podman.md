# Nested rootless Podman

Enable **Workspace box → Image tools → Podman** in `agent-box config`
(`box.tools.podman`, which a project may also set in `.agent-box.json`).
The next new workspace automatically builds the changed image with cache.
The group is off by default. Changing it changes
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
interface. The outer workspace receives `/dev/net/tun`; its default mode uses
a separate network namespace. A nested container's `-p` publishes into the **workspace**
network. For example, a service published at `127.0.0.1:8080` is reachable from
a shell in that workspace. It is not automatically published on the physical
host. `--network=host` on an inner container shares the workspace's network.
To publish through both layers, use an inner bind such as
`-p 0.0.0.0:18080:8080` and outer `agent-box --publish 8080:18080 ...`.
If the outer workspace uses `agent-box --network=host`, inner publication
lands on the physical host. See [workspace networking](networking.md).

Bind mounts can use paths visible inside the workspace. Images, build cache,
containers and named volumes live in a dedicated outer volume named
`agent-box-podman-<workspace id>`, independently of the host engine's existing
images and containers. agent-box creates it before the box with two labels:
`io.agent-box.podman` holds the workspace id and `io.agent-box.path` the
project directory, so `agent-box workspaces` shows every such volume, its
size and the project it belongs to. A volume of that name
that agent-box did not create is refused, never used or deleted. Source files
stay in the mounted project directory.

How long the data lives is `box.podman.persistent_data`, globally or per
project in `.agent-box.json`:

| Event | `persistent_data: false` (default) | `persistent_data: true` |
| --- | --- | --- |
| The box goes away (last session ended, `agent-box stop`) | deleted | kept |
| A box that ended on its own is replaced by a new one | deleted before the new box | kept |
| `agent-box stop --podman-data` | deleted | deleted |
| `agent-box uninstall` | listed, kept | listed, kept |
| `agent-box uninstall --purge` | deleted after you confirm | deleted after you confirm |

A box keeps the setting it was created with: changing it while the box runs,
or before a detached box is cleaned up, takes effect with the next box. Data
is carried into a new box only when both the old and the new box keep it.

With the default, each box starts with empty nested storage, so a scratch
project leaves nothing behind but pays for its pulls and builds every time.
Turn it on for a project whose images and build cache are worth keeping.
Running inner processes always stop with the outer workspace.

Podman's pause process, which holds the rootless user namespace and never
exits by itself, is not a workspace workload: a workspace with nothing else
left exits as usual. Running inner containers and their network helpers do
keep it alive; stop them, or run `agent-box stop` from the parent environment
when the workspace and its nested processes are no longer needed.

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

### Codex sandbox

With the `podman` group enabled, the image adjusts Codex's bundled sandbox
helper so Codex's command sandbox works alongside nested Podman; rebuild the
image and recreate the box after toggling the group. Sandbox restrictions still
apply: read-only commands cannot create Podman state, and nested Podman
operations may need to run outside Codex's sandbox under the session's
approval policy.

## Doctor

`agent-box doctor` checks Podman inside the workspace when the `podman` group
is enabled. It reports the version, rootless mode, storage driver/path, OCI
runtime, cgroup manager/version and network backend, then tests a user namespace.
Each Podman command has a 20-second timeout.

When a local image is already present (the agent-box image, `alpine:3.23` or
`busybox`), doctor also starts a throwaway container without network and checks
its filesystem; otherwise that probe is `SKIPPED`, since doctor never downloads
an image. Building, networking, ports and volumes are not covered by doctor.

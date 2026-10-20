# The workspace image

Every box runs the same local image, `localhost/agent-box:latest` (override
with `AGENT_BOX_IMAGE`), built from the installed package's `Containerfile` on
Ubuntu 26.04 LTS. It has a base set everyone gets plus optional tool groups
chosen under **Workspace box → Image tools** in `agent-box config`
(`box.tools.<group>`).

| Always | Included |
| --- | --- |
| Agents | Claude Code, Codex CLI |
| Languages | Python, uv, Node.js 22, Bun, mise |
| Source control | git, git-lfs, gh |
| Shell tooling | tmux, jq, ripgrep, fd, fzf, just, build essentials, network utilities |

| Optional group | Default | Included |
| --- | --- | --- |
| `analysis` | on | shellcheck, sqlite3, jscpd |
| `kubernetes` | on | kubectl, Helm, yq |
| `virtualization` | off | QEMU x86-64 and aarch64, OVMF; also gates `/dev/kvm` passthrough |
| `podman` | off | Nested rootless Podman, userspace networking and image builds; see [nested Podman](nested-podman.md) |
| `skopeo` | off | Inspecting, copying and listing registry images without Podman's namespace permissions |
| `yc` | off | Yandex Cloud CLI; selected for you when [cloud CLI profiles](integrations/cloud-profiles.md) are forwarded |
| `aws` | off | AWS CLI v2 for x86_64/aarch64; selected when AWS [cloud CLI profiles](integrations/cloud-profiles.md#aws-cli-v2) are forwarded |

**Workspace box → k3s cluster** is not an image group: it runs a per-project
[k3s cluster](integrations/k3s.md) in a sidecar container beside the box
(`box.k3s.enabled`, off by default).

Project-specific toolchains belong in the project, declared through `mise`,
not in an image everyone shares. Tools you add with `mise use -g` and project
configurations you `mise trust` survive box recreation: mise's installs,
global configuration and trust records live in agent-box state shared by
every box, separate from the host's own mise.

## Building and refreshing

```bash
agent-box build      # cached build
agent-box update     # refresh frequently updated tools
agent-box rebuild    # pull the base image and rebuild without layer cache
```

- `build` and `update` reuse the local base image. `update` refreshes Codex,
  Claude, Node.js 22 and npm, mise, uv, Bun and the enabled Kubernetes tools.
- Pinned jscpd sit above the refresh boundary and are
  reused while their inputs are unchanged.
- Ubuntu packages, including security fixes, are refreshed only by `rebuild`.
- APT archives and npm downloads use persistent builder cache mounts
  (`agent-box-apt`, `agent-box-npm`) outside the image; `rebuild` keeps them.

Before creating a new workspace, agent-box compares the image's build-input
fingerprint with the current recipe, copied assets, host identity and effective
build arguments (including tool groups and the explicit tool-refresh token).
Missing images, changed inputs and old images without this label trigger a cached
build automatically. Unrelated launcher/skill changes do not rebuild the image.
The build uses a private snapshot of those inputs, and its successful result is
selected by immutable image ID. Concurrent automatic and explicit builds share a
lock; a failed build retains the previous image and aborts the new launch with
instructions to fix and retry. There is no silent stale-image fallback.

The Python package includes the build inputs; no checkout is needed. Installer
upgrades do not run Podman. The next new workspace launch handles changed inputs.
Running workspaces keep their original image; stop and launch explicitly to adopt
an updated runtime or image. Native `--host` sessions do not build workspace images.
`config show` can report tool-group drift without building. `doctor` runs in a
workspace: joining an admitted running box does not build, but creating a new one
performs the same image reconciliation as an ordinary workspace launch.

Automatic reconciliation does not poll registries for a newer base or refresh
unpinned tool versions when the recipe is unchanged. Use `update` for fresh tools
and `rebuild` for base/OS updates as above; `build` can prepare the image ahead of a
launch. Builds may require network access even with cached layers.

The [agent browser](integrations/browser.md) uses a separate managed image. Its
setting does not participate in workspace build arguments or tool-group labels.

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
| `browser` | off | Playwright MCP and headless Chromium for every box session; see [agent browser](integrations/browser.md) |
| `yc` | off | Yandex Cloud CLI; selected for you when [cloud CLI profiles](integrations/cloud-profiles.md) are forwarded |

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
- Pinned Playwright/Chromium and jscpd sit above the refresh boundary and are
  reused while their inputs are unchanged.
- Ubuntu packages, including security fixes, are refreshed only by `rebuild`.
- APT archives and npm downloads use persistent builder cache mounts
  (`agent-box-apt`, `agent-box-npm`) outside the image; `rebuild` keeps them.

Changing a tool group takes effect on the next `agent-box build`. The image
records what it was built with, so `agent-box config show`, `agent-box doctor`
and every launch report when it no longer matches your configuration; none of
them rebuild it on their own.

The Python package includes the complete build context; no checkout is needed.
An installer upgrade changes those inputs but does not rebuild an image or
replace a running box.

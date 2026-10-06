# Kubernetes configs

Off by default. Kubeconfig files you choose from the host's `~/.kube` can be
forwarded into the box, so agents reach those clusters with `kubectl` and
`helm`. A kubeconfig usually reaches a real cluster, often a production one,
so nothing is forwarded until you tick it.

```bash
agent-box config    # Credentials → Kubernetes configs
```

| Setting | Default | What it exposes |
| --- | --- | --- |
| Enabled | off | Whether the chosen files are forwarded at all |
| Kubeconfig files | none | The chosen files from `~/.kube`, whole |

The menu lists the kubeconfigs at the top of `~/.kube` (`kind: Config` or a
`contexts` list; hidden files and directories such as `cache` are skipped;
a symlink to a file is listed under its own name) and ticks
nothing for you. Each row shows how the file authenticates: `token`,
`client certificate`, `basic auth`, `auth-provider: NAME` or
`exec: COMMAND --profile=NAME`. The cursor's file also shows its path and
contexts. A file chosen earlier that the host no longer has stays listed, so
you can untick it. No credential value and no other exec argument is shown.

## In the box

Each chosen file is copied byte for byte, under its own name, into the box's
own `~/.kube`, so paths and commands are the host's:

```bash
kubectl --kubeconfig ~/.kube/prod.kubeconfig get ns
k9s --kubeconfig ~/.kube/prod.kubeconfig
```

A symlink arrives as a plain file holding its target's content, wherever the
target lives on the host.

The files are never merged, and `KUBECONFIG` is not changed. Kubeconfigs from
different sources often reuse context and user names such as `default`, and
with a merged `KUBECONFIG` kubectl takes the first match without saying so,
so it could reach the wrong cluster.

Which cluster a bare `kubectl` reaches:

| | [k3s](k3s.md) on | k3s off |
| --- | --- | --- |
| `kubectl`, `k9s`, `helm` | the k3s cluster (`KUBECONFIG` is k3s's) | your `~/.kube/config`, if you chose it |
| `--kubeconfig ~/.kube/config` | your cluster | your cluster |

Choosing `config` without k3s therefore makes your cluster the agent's
default, as it is on the host.

While forwarding is on, the box's `~/.kube` is read-only: `kubectl config
use-context` and other kubeconfig edits fail, so pass `--context` instead.
kubectl's cache moves to `~/.cache/kube` through `KUBECACHEDIR`. With
forwarding off, the box's `~/.kube` is an ordinary directory.

## What does not work inside the box

The launch warns about these and still starts:

- **exec plugins** (`yc k8s create-token`, `kubectl-helper-*`, cloud CLIs) and
  **auth-providers**. They run a program with the host's own cloud login,
  and the box has neither the program nor that login. Forwarding the cloud
  login itself would give the agent the whole cloud account, not one cluster.
- **Files the kubeconfig names**: `certificate-authority`,
  `client-certificate`, `client-key` and `tokenFile`. The box has such a file
  only where its path is mounted, for example with `--mount`; a relative path
  no longer resolves next to the copy.
- **A chosen file the host no longer has**, or a symlink whose target is gone.
  Its copy is removed.

Files with an embedded token or certificate (`token`, `client-certificate-data`)
work as they are.

## How it reaches the container

The copies are written into a private per-workspace directory (mode 0700,
files 0600), and that directory is mounted as the box's `~/.kube`. No
kubeconfig content appears in Podman's arguments. Turning
forwarding off or emptying the list removes the copies and the mount.

The copies are written again at every launch. A file changed on the host
reaches an already running box at the next `agent-box` launch for that
project, without recreating the box. A launch nested inside a box finds the
copies that box was given in its `~/.kube`.

The mount and `KUBECACHEDIR` are part of the box's identity. Turning forwarding
on or off, or emptying or filling the list, recreates the box; a box that is
still running refuses the change until `agent-box stop`. Changing which files
are chosen only changes the copies.

## One-off overrides

```bash
agent-box --kubeconfigs claude       # forward the chosen files for this session
agent-box --no-kubeconfigs claude    # withhold them for this session
```

Both act on the saved choice of files and save nothing; `--kubeconfigs` with
nothing chosen is refused.

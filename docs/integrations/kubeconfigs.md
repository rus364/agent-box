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
`contexts` list; hidden files, `cache` and `http-cache` are skipped) and ticks
nothing for you. Each row shows how the file authenticates: `token`,
`client certificate`, `basic auth`, `auth-provider: NAME` or
`exec: COMMAND --profile=NAME`. The cursor's file also shows its path and
contexts. A file chosen earlier that the host no longer has stays listed, so
you can untick it. No credential value and no other exec argument is shown.

## In the box

Each chosen file is copied byte for byte under its own name into a directory
mounted read-only at `$AGENT_BOX_KUBECONFIGS`:

```bash
ls "$AGENT_BOX_KUBECONFIGS"
kubectl --kubeconfig "$AGENT_BOX_KUBECONFIGS/prod.kubeconfig" get ns
```

The files are never merged, and `KUBECONFIG` is not changed: it stays the k3s
cluster's when [k3s](k3s.md) is on and unset otherwise. Kubeconfigs from
different sources often reuse context and user names such as `default`, and
with a merged `KUBECONFIG` kubectl takes the first match without saying so,
so it could reach the wrong cluster. Because the copies are read-only,
`kubectl config use-context` fails; pass `--context` instead.

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
- **A chosen file the host no longer has.** Its copy is removed.

Files with an embedded token or certificate (`token`, `client-certificate-data`)
work as they are.

## How it reaches the container

The copies are written into a private per-workspace directory (mode 0700,
files 0600), and that directory is mounted. The variable carries only its
path, so no kubeconfig content appears in Podman's arguments. Turning
forwarding off or emptying the list removes the copies and the mount.

The copies are written again at every launch. A file changed on the host
reaches an already running box at the next `agent-box` launch for that
project, without recreating the box. A launch nested inside a box offers the
copies that box was given.

The mount and the variable are part of the box's identity. Turning forwarding
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

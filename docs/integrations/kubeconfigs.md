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

The files are never merged. With built-in k3s off, `KUBECONFIG` is not set.
With it on, `KUBECONFIG` selects `~/.kube/k3s.yaml`; a selected host `config`
keeps its name. Selecting a host `k3s.yaml` with k3s enabled is refused: rename
that host file or disable k3s. The name `cache` is reserved for kubectl's cache directory. Kubeconfigs from
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
kubectl uses its standard `~/.kube/cache`, overlaid writable without
`KUBECACHEDIR`. It is a private tmpfs by default, or a shared host-backed cache
when **Keep tool cache between boxes** is enabled. With forwarding and k3s off,
the box's `~/.kube` is an ordinary directory. An explicit mount at
`~/.kube/cache` conflicts with the managed overlay and is refused.

## What does not work inside the box

The launch warns about these and still starts:

- **`exec: yc` without its profile.** `yc k8s create-token --profile=NAME`
  works in the box once profile `NAME` is forwarded in
  [Cloud CLI profiles](cloud-profiles.md); the warning names the profile it
  is missing. Forwarding it gives the agent that whole cloud account, not one
  cluster. The command must be one the box has: `yc` on `PATH` or
  `~/yandex-cloud/bin/yc`; any other path is named in the warning.
- **`exec: aws eks get-token` without AWS access.** Enable the `aws` tool
  group and forward the matching AWS profile in Cloud CLI profiles. A
  `--profile NAME` or `--profile=NAME` argument wins over exec environment
  `AWS_PROFILE`, then `AWS_DEFAULT_PROFILE`; with none, the name is `default`.
  The box provides `aws`, `/usr/bin/aws` and `/usr/local/bin/aws`; other
  executable paths are unsupported. Missing/unsupported profile credentials
  are diagnosed. Other `AWS_*` exec environment overrides are warned about
  without printing their values, since they can change the credential source
  or endpoint. The kubeconfig itself is still copied unchanged.
  A successful offline profile check does not prove EKS authorization.
- **Other exec plugins** (`kubectl-helper-*`, other cloud CLIs) and
  **auth-providers**. They run a program with the host's own login, and the
  box has neither the program nor that login.
- **Files the kubeconfig names**: `certificate-authority`,
  `client-certificate`, `client-key` and `tokenFile`. The box has such a file
  only where its path is mounted, for example with `--mount`; a relative path
  no longer resolves next to the copy.
- **A chosen file the host no longer has**, or a symlink whose target is gone.
  Its copy is removed.

Files with an embedded token or certificate (`token`, `client-certificate-data`)
work as they are.

Forwarding kubeconfigs needs `kubectl`, which is the image's `kubernetes`
tool group (on by default). Turning forwarding on in the menu ticks that group,
and the menu will not untick it while kubeconfigs are forwarded; if it is off
anyway, the launch says so.

## How it reaches the container

The copies are mounted as the box's read-only `~/.kube`; turning forwarding
off or emptying the list removes them. They are written again at every launch. A file changed on the host
reaches an already running box at the next `agent-box` launch for that
project, without recreating the box. A launch nested inside a box finds the
copies that box was given in its `~/.kube`.

The running box retains the forwarding switch, selected files and cache mounts
from creation. Later launches refresh only those files, even if
the saved selection changed. New choices take effect after `agent-box stop`
and the next launch; an explicit conflicting switch requires that restart.

## One-off overrides

```bash
agent-box --kubeconfigs claude       # forward the chosen files for this session
agent-box --no-kubeconfigs claude    # withhold them for this session
```

Both act on the saved choice of files and save nothing; `--kubeconfigs` with
nothing chosen is refused.

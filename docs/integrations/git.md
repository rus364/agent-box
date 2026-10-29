# Git and GitHub

Being logged in to `gh` on the host does **not** give a workspace box a
GitHub credential. What reaches a session is chosen under **Credentials → Git**,
**Credentials → GitHub** and **Credentials → GitLab** in `agent-box config`.

| Setting | Default | What it exposes |
| --- | --- | --- |
| Git identity (`credentials.git.identity`) | on | `user.name` and `user.email` only |
| Full host Git config (`credentials.git.full_config`) | off | Your entire `~/.gitconfig` |
| GitHub token (`credentials.github.token`) | off | A live token for everything your account can reach |
| GitHub config copy (`credentials.github.config`) | off | `~/.config/gh`, which stores that token on disk |
| GitLab config copy (`credentials.gitlab.config`) | off | The glab configuration directory, including any file-stored tokens |

**Identity** is what commit attribution needs and is not a secret. **The full
Git config** often carries credential helpers, bearer-token headers, URL
rewrites and includes, so handing it to an agent is a deliberate choice.
**The token** is a live credential.

## How the token is used

With the GitHub token on, agent-box forwards `GH_TOKEN` or `GITHUB_TOKEN` if
your shell already sets one, and otherwise the token from `gh auth token` as
`GH_TOKEN` — always by name, so its value never appears in a process list.
Inside the box, Git asks `gh` for credentials for `github.com` only, and SSH
remotes on `github.com` are rewritten to HTTPS in the box's own Git
configuration; your repository's `origin` and host configuration stay
unchanged.

Only HTTPS GitHub authentication is forwarded. SSH keys and the host SSH agent
are never mounted, so `Host key verification failed` inside the box means a
remote still uses SSH — on `github.com` too while the token is off; use HTTPS
rather than mounting keys or disabling host-key checks.

Git, `gh` and `glab` configuration is copied into the box when it is created; changes
made inside the box disappear with it, and changed settings take effect for the
next box (`agent-box stop`, then launch). The forwarded token is read again at
every launch: direct commands (`--no-tmux`, pipes, `claude -p`) get the current
one, while interactive sessions share the box's tmux server, which keeps the
environment it started with, so a renewed `gh` login reaches them in a new box.

## One-session overrides

```bash
agent-box --gh-token claude        # forward the token for this session
agent-box --no-gh-token claude     # withhold it for this session
agent-box --gh-config codex        # also copy the gh configuration
agent-box --glab-config codex      # copy the glab configuration
agent-box --no-glab-config codex   # withhold it for a new box
agent-box --git-config codex       # also copy the full Git configuration
```

## GitLab CLI configuration

`glab` is included in every workspace image. Enable **Credentials → GitLab →
Copy glab config**, set `credentials.gitlab.config` to `true`, or launch with
`--glab-config`. The source follows [glab configuration lookup](https://docs.gitlab.com/cli/configuration/):
`GLAB_CONFIG_DIR` first; otherwise `~/.config/glab-cli` when it contains
`config.yml`, then `$XDG_CONFIG_HOME/glab-cli` (default `~/.config/glab-cli`).
Home paths use the account home, not the shell’s `HOME`. System-wide config
is not copied.

The container gets a private writable copy at `~/.config/glab-cli`, selected
by its own `GLAB_CONFIG_DIR`. Host files stay unchanged. This includes saved
self-managed GitLab hosts and tokens stored in the files. Credentials stored
only in the host OS keyring are not copied; authenticate inside the box with
`glab auth login` when needed. Shell token variables are not forwarded by this
switch. GitLab Git credential helpers and SSH rewrites are not configured
automatically.

## Checking it

`agent-box doctor` reports the installed `glab` version, the Git identity the box commits with and which
GitHub account, if any, `gh` is authenticated as inside the box. An
unauthenticated `gh` there is the default, not a fault.

The [tracker's GitHub Projects connection](tracker.md#github-projects) uses its
own token, configured separately.

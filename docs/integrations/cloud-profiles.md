# Cloud CLI profiles

Off by default. Profiles you choose from the host's cloud CLIs can be forwarded
into the box, so agents run `yc` and AWS CLI v2 there, including
[Kubernetes configs](kubeconfigs.md) that use these CLIs for authentication.
Both providers share the same forwarding switch.

**A profile is the whole cloud account.** A kubeconfig reaches one cluster; a
forwarded profile lets the agent do everything that account may do in the
cloud, and a federated profile comes with a refresh token valid for up to a
month. Forward a profile only to a box you would hand that account to.

```bash
agent-box config    # Credentials → Cloud CLI profiles
```

| Setting | Default | What it exposes |
| --- | --- | --- |
| Enabled | off | Whether the chosen profiles are forwarded at all |
| Yandex Cloud (yc) → yc profiles | none | The chosen profiles from `~/.config/yandex-cloud`, with their sign-in |
| Amazon Web Services (aws) → AWS profiles | none | Selected profiles, role sources and referenced SSO/login sessions from `~/.aws` |

The menu lists the profiles in the host's `~/.config/yandex-cloud/config.yaml`
and ticks none for you. Each row shows how the profile signs in:
`federation`, `service account key`, `OAuth token` or
`instance service account` (that one works only on a Yandex Cloud VM). A
profile chosen earlier that the host no longer has stays listed, so you can
untick it. No token, key or other value is shown.

## The yc tool

`yc` is the image's `yc` tool group, off by default. It is installed on `PATH`
and at `~/yandex-cloud/bin/yc`, where yc's own installer puts it and so where
the kubeconfigs `yc managed-kubernetes cluster get-credentials` writes look for
it. Choosing a profile while forwarding is on ticks that group for you, and
`agent-box build` installs it;
the group can also be ticked alone, for `yc` without any profile. The menu
will not untick the group while profiles are forwarded. If forwarding is on
and the group is off anyway (through `config set` or `--cloud-profiles`), or
the image has not been rebuilt yet, the launch says so and still starts.

The same pairing holds for [kubeconfigs](kubeconfigs.md) and the `kubernetes`
group, which carries `kubectl` and Helm.

## In the box

The box gets its own `~/.config/yandex-cloud`, writable, holding:

- `config.yaml` with only the chosen profiles, each copied line for line from
  the host. `current` stays the host's current profile when it is chosen,
  otherwise it is the first chosen one.
- `credentials/<profile>` of each chosen federated profile: the IAM token and
  the refresh token.

The host's history, logs and every other profile stay on the host. agent-box
runs no `yc` command on the host and never writes to the host's
`~/.config/yandex-cloud`.

Inside the box `yc` works as usual, and renews a federated profile's IAM
token from its refresh token without a browser. It writes that token, its
history and its logs into the box's copy. At the next launch the copy is
rendered from the host again, using the box's creation-time profile selection.
The host is the source, so `yc config set` in the box does not last. Changing
the saved switch or selection takes effect after `agent-box stop` and the next
launch; an explicit conflicting switch requires that restart. A selected
profile removed from the host cannot be refreshed.

### Federated profiles

Sign in on the host with `yc` as usual, then launch again to copy the renewed
credentials. Alternatively, enable [web links and CLI callbacks](links.md)
to complete a login started inside the box in your workstation browser;
remote launches also need the displayed SSH/Coder tunnel. That login updates
only the box's copy, which the next launch replaces from the host.

The image's `xdg-open` lets cached federated credentials work even without
host link access. A missing/expired IAM token with no usable refresh token
still needs interactive login. Refresh tokens may be absent; automatic
renewal is available only when the provider issued one and its supporting
authentication material is available. The link integration does not add
missing refresh-token or DPoP material to forwarded profiles.

Yandex Cloud reissues a refresh token used in its last seven days. Whether the
old one stays valid is not documented: if it does not, a box that renewed
it leaves the host's copy stale, and the host needs a browser sign-in once.

## AWS CLI v2

Choose AWS profiles under **Credentials → Cloud CLI profiles → Amazon Web
Services (aws)**, then enable forwarding. The menu enables the `aws` image
group; run `agent-box build` and stop/relaunch the workspace to adopt it.
The group is off by default and can also be installed without credentials.
It installs AWS CLI v2 for Linux x86_64 or aarch64 using
[AWS's signature-verifying installer](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html).
The image provides `aws`, `/usr/local/bin/aws` and `/usr/bin/aws`.
Existing installations gain no AWS access on upgrade.

Only the standard `~/.aws/config`, `~/.aws/credentials` and referenced caches
are read. Host `AWS_CONFIG_FILE`, `AWS_SHARED_CREDENTIALS_FILE`,
`AWS_LOGIN_CACHE_DIRECTORY`, `AWS_PROFILE` and credential environment variables
are not imported. Paths come from the real Unix home, not `$HOME`.
The list shows profile names and authentication types, never keys or tokens.
No profile is selected automatically, including `default`.

| Authentication | Forwarded material and limits |
| --- | --- |
| Static/session credentials | Selected credentials-file entries or inline config credentials; session credentials cannot renew themselves |
| IAM role with `source_profile` | The complete transitive source-profile chain; assumes roles inside the box. Self-source with static keys is supported |
| IAM Identity Center (SSO) | Referenced `sso-session` section and its token cache, including refresh/client registration material; legacy SSO uses its start-URL cache and needs login after expiry |
| `aws login` / `login_session` | Referenced login cache, including temporary credentials, refresh token and DPoP private key |

**A role choice exposes its source accounts too.** Their profile names appear
in the selection details. A shared SSO token may grant every account and role
available to that session, even when only one profile is selected. A shared
login session similarly exposes its full access. These warnings appear before
selection; copying a profile is not a permissions boundary.

The workspace gets a private writable `~/.aws`, containing only selected
profiles and their dependencies, referenced `services` endpoint sections,
and matching SSO/login cache files. Region, output and endpoint settings are
preserved. Profile names remain unchanged: a bare `aws` uses `default` only
if it was forwarded; use `aws --profile NAME ...` for another selection.
The default profile does not lend settings to named profiles.

Every launch replaces managed config, credentials and SSO/login caches from
the host using the box's creation-time selection. Missing or invalid files
remove stale copies and produce a diagnostic. Derived `cli/cache` role
credentials are cleared on refresh and can be recreated by AWS inside the
box; host role caches, history, plugins and unrelated sessions are never
copied. Login/refresh inside the box changes only its copy and may be lost
on the next launch. Refresh tokens may rotate: if a host copy becomes stale,
sign in again on the host. The image's CLI owns refresh behavior; successful
copying does not prove cloud authentication.

Profiles using `credential_process`, `credential_source` (environment,
EC2/ECS metadata), external web-identity token files or MFA-dependent roles
are diagnosed and omitted, along with roles that depend on them. Missing
sources, cycles and incomplete SSO settings are also diagnosed. External
`ca_bundle` files are not copied. This integration never executes a host
credential process or logs in on the user's behalf.

For interactive SSO inside the box, enable [web links and CLI callbacks](links.md)
and run `aws sso login --profile NAME`. For a remote browser, AWS also supports
`aws sso login --profile NAME --use-device-code`; follow its displayed URL/code.
For console-based `aws login`, use `aws login --profile NAME` or its documented
`--remote` flow when the browser is on another machine. Browser opening and
callback transport do not replace provider login or guarantee refresh.

AWS references:
[profiles and settings](https://docs.aws.amazon.com/cli/latest/userguide/cli-configure-files.html),
[role sources](https://docs.aws.amazon.com/cli/latest/userguide/cli-configure-role.html),
[SSO](https://docs.aws.amazon.com/cli/latest/userguide/cli-configure-sso.html),
[console login](https://docs.aws.amazon.com/cli/latest/userguide/cli-configure-sign-in.html).

## How it reaches the container

The copies are mounted as the box's `~/.config/yandex-cloud` and/or `~/.aws`; no profile value
appears in Podman's arguments. The mount is part of the box itself: turning
forwarding on or off, or emptying or filling the list, needs a new box, and a
running box refuses the change until `agent-box stop`. A different selection
of profiles also applies from the next box on, as described above.

## One-off overrides

```bash
agent-box --cloud-profiles claude       # forward the chosen profiles for this session
agent-box --no-cloud-profiles claude    # withhold them for this session
```

Both act on the saved choice and save nothing; `--cloud-profiles` with
nothing chosen is refused.

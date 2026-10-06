FROM docker.io/library/ubuntu:26.04

ARG DEBIAN_FRONTEND=noninteractive
# The container user mirrors the *building* host's real Unix identity
# (name/uid/gid/home, from agent_box.identity.detect_host_identity), so that
# host-absolute paths -- the project workspace, extra --mount targets, and
# host-authored symlinks such as ~/.claude/skills entries -- resolve
# identically inside the container.
#
# These ARGs deliberately have NO defaults. An earlier revision defaulted them
# to a fixed name, uid 1000, gid 1000 and an invented home directory, which
# meant a direct `podman build` (without agent-box's --build-arg values)
# silently produced an image whose user and home did not match the host at
# all: bind mounts landed at paths the
# container user could not own, $HOME pointed at a directory nothing was
# mounted into, and the mismatch only surfaced later as confusing runtime
# failures. Failing the build immediately is the safe behavior -- see the
# validation step in the "Container user" layer below.
ARG USER_NAME
ARG USER_UID
ARG USER_GID
ARG USER_HOME

#
# ---------------------------------------------------------------------------
# Optional tool groups
# ---------------------------------------------------------------------------
#
# The image carries one base set everybody needs -- the agents, Python, Node,
# git, and the ordinary shell tooling -- plus these groups, each of which is a
# real cost in build time and disk for people who never touch that domain.
# They are chosen in `agent-box config`, which passes them here and records
# the resulting set in the image label below, so `agent-box doctor` can say
# when a running image no longer matches what the configuration asks for.
#
# Anything not covered here belongs in the project, through mise: a language
# runtime or a per-project tool is not the image's business.
#
ARG TOOLS_ANALYSIS=1
ARG TOOLS_KUBERNETES=1
ARG TOOLS_VIRTUALIZATION=0
ARG TOOLS_BROWSER=0
ARG TOOLS_PODMAN=0
ARG TOOLS_SKOPEO=0
ARG TOOLS_YC=0
ARG PODMAN_UID_RANGES=[]
ARG PODMAN_GID_RANGES=[]
# The canonical, comma-separated set the flags above add up to. Passed
# whole rather than derived here, so the label and the configuration cannot
# disagree about spelling or order. Applied as a label at the very end of the
# file, where changing it cannot invalidate a single build layer.
ARG AGENT_BOX_TOOLS=""

ENV LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONUNBUFFERED=1 \
    UV_NO_PROGRESS=1 \
    UV_LINK_MODE=copy \
    DISABLE_AUTOUPDATER=1

#
# ---------------------------------------------------------------------------
# Base OS / development packages
# ---------------------------------------------------------------------------
#
# This layer is intentionally first and changes rarely.
#

RUN --mount=type=cache,id=agent-box-apt,target=/var/cache/apt,sharing=locked \
    set -eux; \
    rm -f /etc/apt/apt.conf.d/docker-clean; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        bash \
        bash-completion \
        ca-certificates \
        curl \
        wget \
        git \
        git-lfs \
        openssh-client \
        gnupg \
        gettext \
        \
        python3 \
        python3-dev \
        python3-venv \
        python3-pip \
        python-is-python3 \
        pipx \
        \
        build-essential \
        cmake \
        ninja-build \
        pkg-config \
        \
        libffi-dev \
        libssl-dev \
        libsqlite3-dev \
        libreadline-dev \
        libbz2-dev \
        liblzma-dev \
        zlib1g-dev \
        \
        jq \
        ripgrep \
        fd-find \
        fzf \
        less \
        vim-tiny \
        tree \
        just \
        \
        patch \
        diffutils \
        rsync \
        file \
        \
        unzip \
        zip \
        xz-utils \
        tar \
        \
        procps \
        psmisc \
        tmux \
        lsof \
        strace \
        \
        iproute2 \
        iputils-ping \
        bind9-dnsutils \
        netcat-openbsd \
        socat; \
    ln -sf /usr/bin/fdfind /usr/local/bin/fd; \
    git lfs install --system; \
    rm -rf /var/lib/apt/lists/*

#
# GitHub CLI
#
# GitHub's signed repository keeps gh current on rebuilds. Check the active
# account flag used by Claude's /install-github-app, not just its version.
#

RUN --mount=type=cache,id=agent-box-apt,target=/var/cache/apt,sharing=locked \
    set -eux; \
    install -m 0755 -d /etc/apt/keyrings; \
    curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
        -o /etc/apt/keyrings/githubcli-archive-keyring.gpg; \
    chmod 0644 /etc/apt/keyrings/githubcli-archive-keyring.gpg; \
    printf 'deb [arch=%s signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main\n' \
        "$(dpkg --print-architecture)" \
        > /etc/apt/sources.list.d/github-cli.list; \
    apt-get update; \
    apt-get install -y --no-install-recommends gh; \
    rm -rf /var/lib/apt/lists/*; \
    gh auth status --help | grep -q -- '--active'

#
# Analysis tooling (group: analysis)
#
# Small, and what an agent reaches for when reading a codebase rather than
# writing one. jscpd joins it from npm further down.
#

RUN --mount=type=cache,id=agent-box-apt,target=/var/cache/apt,sharing=locked \
    set -eux; \
    if [ "${TOOLS_ANALYSIS}" != "1" ]; then echo "agent-box: analysis tools skipped"; exit 0; fi; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        shellcheck \
        sqlite3; \
    rm -rf /var/lib/apt/lists/*

#
# Virtualization (group: virtualization)
#
# The largest optional group by a wide margin, and useless to most projects,
# so it is off unless asked for. agent-box also passes /dev/kvm through only
# when this group is present -- see runtime.kvm_args.
#

RUN --mount=type=cache,id=agent-box-apt,target=/var/cache/apt,sharing=locked \
    set -eux; \
    if [ "${TOOLS_VIRTUALIZATION}" != "1" ]; then echo "agent-box: virtualization skipped"; exit 0; fi; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        qemu-system-x86 \
        qemu-system-arm \
        qemu-utils \
        qemu-block-extra \
        ovmf \
        qemu-efi-aarch64; \
    rm -rf /var/lib/apt/lists/*

#
# skopeo (group: skopeo)
#
# Registry inspection and copying without a container engine. Separate from
# the podman group, which needs extra namespace permissions that skopeo does
# not, so a box can reach registries without them.
#

RUN --mount=type=cache,id=agent-box-apt,target=/var/cache/apt,sharing=locked \
    set -eux; \
    if [ "${TOOLS_SKOPEO}" != "1" ]; then echo "agent-box: skopeo skipped"; exit 0; fi; \
    apt-get update; \
    apt-get install -y --no-install-recommends skopeo; \
    rm -rf /var/lib/apt/lists/*

#
# ---------------------------------------------------------------------------
# Container user
# ---------------------------------------------------------------------------
#
# Ubuntu images may already contain UID/GID 1000 (usually user "ubuntu").
#
# Therefore:
#   - reuse an existing GID;
#   - rename an existing UID;
#   - create the user/group normally when UID/GID are free.
#

# Identity args are required: fail fast and explain, rather than inventing a
# fallback identity that will not match the host at runtime.
RUN set -eu; \
    missing=""; \
    [ -n "${USER_NAME:-}" ] || missing="${missing} USER_NAME"; \
    [ -n "${USER_UID:-}" ] || missing="${missing} USER_UID"; \
    [ -n "${USER_GID:-}" ] || missing="${missing} USER_GID"; \
    [ -n "${USER_HOME:-}" ] || missing="${missing} USER_HOME"; \
    if [ -n "${missing}" ]; then \
        echo "agent-box: missing required --build-arg:${missing}" >&2; \
        echo "agent-box: build this image with 'agent-box build', which derives" >&2; \
        echo "agent-box: USER_NAME/USER_UID/USER_GID/USER_HOME from the host passwd" >&2; \
        echo "agent-box: entry, or pass all four --build-arg values explicitly." >&2; \
        exit 1; \
    fi; \
    case "${USER_UID}" in ''|*[!0-9]*) echo "agent-box: USER_UID must be numeric: ${USER_UID}" >&2; exit 1;; esac; \
    case "${USER_GID}" in ''|*[!0-9]*) echo "agent-box: USER_GID must be numeric: ${USER_GID}" >&2; exit 1;; esac; \
    case "${USER_HOME}" in /*) ;; *) echo "agent-box: USER_HOME must be an absolute path: ${USER_HOME}" >&2; exit 1;; esac; \
    printf '%s\n' "agent-box: building for ${USER_NAME} (${USER_UID}:${USER_GID}) home=${USER_HOME}"

RUN set -eux; \
    existing_group="$(getent group "${USER_GID}" | cut -d: -f1 || true)"; \
    if [ -n "${existing_group}" ]; then \
        if [ "${existing_group}" != "${USER_NAME}" ]; then \
            groupmod --new-name "${USER_NAME}" "${existing_group}"; \
        fi; \
    else \
        groupadd --gid "${USER_GID}" "${USER_NAME}"; \
    fi; \
    existing_user="$(getent passwd "${USER_UID}" | cut -d: -f1 || true)"; \
    if [ -n "${existing_user}" ]; then \
        if [ "${existing_user}" != "${USER_NAME}" ]; then \
            usermod \
                --login "${USER_NAME}" \
                --home "${USER_HOME}" \
                --shell /bin/bash \
                "${existing_user}"; \
        fi; \
        usermod --gid "${USER_GID}" "${USER_NAME}"; \
    else \
        useradd \
            --uid "${USER_UID}" \
            --gid "${USER_GID}" \
            --home-dir "${USER_HOME}" \
            --shell /bin/bash \
            "${USER_NAME}"; \
    fi; \
    mkdir -p \
        "${USER_HOME}" \
        "${USER_HOME}/.cache" \
        "${USER_HOME}/.local/share/uv" \
        "${USER_HOME}/.local/share/mise"; \
    chown -R "${USER_UID}:${USER_GID}" "${USER_HOME}"

#
# Nested Podman (group: podman)
#
# File capabilities avoid setuid-root helpers and keep no-new-privileges usable.
# A dedicated workspace volume lets inner native overlay work without FUSE.
COPY agent_box/nested_podman.py /tmp/agent-box-nested-podman.py
RUN --mount=type=cache,id=agent-box-apt,target=/var/cache/apt,sharing=locked \
    set -eux; \
    if [ "${TOOLS_PODMAN}" = "1" ]; then \
        apt-get update; \
        apt-get install -y --no-install-recommends \
            podman crun slirp4netns uidmap libcap2-bin catatonit aardvark-dns nftables; \
        chmod 0755 /usr/bin/newuidmap /usr/bin/newgidmap; \
        setcap cap_setuid=ep /usr/bin/newuidmap; \
        setcap cap_setgid=ep /usr/bin/newgidmap; \
        python3 /tmp/agent-box-nested-podman.py \
            --user "${USER_NAME}" --uid "${USER_UID}" --gid "${USER_GID}" \
            --home "${USER_HOME}" --uid-ranges "${PODMAN_UID_RANGES}" --gid-ranges "${PODMAN_GID_RANGES}"; \
        rm -rf /var/lib/apt/lists/*; \
    fi; \
    rm /tmp/agent-box-nested-podman.py

#
# Node.js 22
#
# Both coding agents support the Node.js version used by this image.
#

RUN --mount=type=cache,id=agent-box-apt,target=/var/cache/apt,sharing=locked \
    set -eux; \
    curl -fsSL \
        https://deb.nodesource.com/setup_22.x \
        -o /tmp/nodesource-setup.sh; \
    bash /tmp/nodesource-setup.sh; \
    rm -f /tmp/nodesource-setup.sh; \
    apt-get update; \
    apt-get install -y --no-install-recommends nodejs; \
    rm -rf /var/lib/apt/lists/*

#
# Playwright MCP + headless Chromium
#
# The container-native browser backend behind `agent-box config`'s browser
# switch: a stdio MCP server (`playwright-mcp`) driving a headless Chromium
# that lives entirely inside the workspace box. No sidecar, no HTTP service,
# no host browser and no X11/Wayland socket -- see README.
#
# Node.js and pinned browser assets precede TOOLS_REFRESH so updates reuse
# Chromium. Node.js is refreshed separately below without reinstalling MCP.
#
# The version is pinned rather than tracking @latest because @playwright/mcp
# pins an exact `playwright` dependency, and the browser binaries downloaded
# below must be the ones that `playwright` build expects. Bumping this ARG is
# what upgrades both halves together.
ARG PLAYWRIGHT_MCP_VERSION=0.0.80

# A fixed system-wide location rather than the default ~/.cache/ms-playwright:
# agent-box gives the container its own XDG_CACHE_HOME backed by a bind mount,
# which would otherwise hide browsers installed at build time behind an empty
# runtime directory.
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright

# The `playwright` package that @playwright/mcp itself depends on is the one
# whose CLI downloads the matching browser revision, so it is resolved out of
# the installed MCP package rather than installed a second time.
# `playwright/cli.js` is not listed in that package's "exports", so the bin
# entry is read from its manifest by absolute path instead.

RUN --mount=type=cache,id=agent-box-apt,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,id=agent-box-npm,target=/root/.npm \
    set -eux; \
    if [ "${TOOLS_BROWSER}" != "1" ]; then echo "agent-box: browser skipped"; exit 0; fi; \
    arch="$(dpkg --print-architecture)"; \
    case "${arch}" in \
        amd64|arm64) ;; \
        *) \
            echo "agent-box: Playwright publishes no Chromium build for ${arch};" >&2; \
            echo "agent-box: the browser integration cannot be provided on this" >&2; \
            echo "agent-box: architecture. Turn the browser off in" >&2; \
            echo "agent-box: 'agent-box config' and rebuild." >&2; \
            exit 1; \
            ;; \
    esac; \
    npm install --global "@playwright/mcp@${PLAYWRIGHT_MCP_VERSION}"; \
    playwright_dir="$(node -e 'const p = require("path"); console.log(p.dirname(require.resolve("playwright/package.json", {paths: ["/usr/lib/node_modules/@playwright/mcp"]})))')"; \
    printf '%s\n' "${playwright_dir}" > /usr/local/lib/agent-box-playwright-dir; \
    node "${playwright_dir}/$(node -p "require('${playwright_dir}/package.json').bin.playwright")" install --with-deps chromium; \
    rm -rf /var/lib/apt/lists/*; \
    chmod -R a+rX /opt/ms-playwright

# Chromium needs a writable XDG config home: it creates its crash-reports
# database under $HOME/.config before any --user-data-dir applies, and dies
# with "chrome_crashpad_handler: --database is required" when it cannot.
#
# At runtime agent-box bind-mounts $HOME/.config/git and $HOME/.config/gh into
# the box. Podman creates missing bind-mount *parents* itself, root-owned, so
# without this the container user cannot write to its own $HOME/.config at
# all. Creating the directory here means Podman finds it already there and
# only adds the two leaves, leaving the ownership below intact.
#
# This is the whole of what Chromium needs in this environment. No Linux
# capability is added, --cap-drop=all and --security-opt=no-new-privileges are
# untouched, and nothing from the host is mounted.

RUN set -eux; \
    install -d -o "${USER_UID}" -g "${USER_GID}" -m 0755 "${USER_HOME}/.config"

# A bounded, entirely offline browser smoke test, with the resolved Playwright
# baked in: it renders a data: URL and reads the text back, so it proves a
# headless Chromium can actually start and exit without touching the network.
# Used by the build sanity check below and by `agent-box doctor`.
#
# Kept in its own layer so editing the check does not re-download the browser.

RUN set -eux; \
    if [ "${TOOLS_BROWSER}" != "1" ]; then echo "agent-box: browser smoke test skipped"; exit 0; fi; \
    playwright_dir="$(cat /usr/local/lib/agent-box-playwright-dir)"; \
    { \
        echo '#!/usr/bin/env node'; \
        echo "const { chromium } = require('${playwright_dir}');"; \
        echo 'const timer = setTimeout(() => { console.error("browser smoke test timed out"); process.exit(1); }, 20000);'; \
        echo '(async () => {'; \
        echo '  const browser = await chromium.launch({ headless: true });'; \
        echo '  try {'; \
        echo '    const page = await browser.newPage();'; \
        echo '    await page.goto("data:text/html,<h1>agent-box</h1>");'; \
        echo '    if ((await page.textContent("h1")) !== "agent-box") throw new Error("page did not render");'; \
        echo '    console.log(browser.version());'; \
        echo '  } finally {'; \
        echo '    await browser.close();'; \
        echo '    clearTimeout(timer);'; \
        echo '  }'; \
        echo '})().catch((error) => { console.error(String(error)); process.exit(1); });'; \
    } > /usr/local/bin/agent-box-browser-smoke; \
    chmod 0755 /usr/local/bin/agent-box-browser-smoke

# Pinned analysis package shares the stable Node.js/browser cache.
RUN --mount=type=cache,id=agent-box-npm,target=/root/.npm \
    set -eux; \
    if [ "${TOOLS_ANALYSIS}" != "1" ]; then echo "agent-box: jscpd skipped"; exit 0; fi; \
    npm install --global jscpd@5.0.16

#
# Host audio client
#
# Small and always present; the audio switch decides at box creation whether
# the host's PulseAudio socket is mounted. ALSA users such as Claude Code's
# voice capture reach it through the pulse plugin. Placed after the large
# stable layers so adding it does not invalidate them.
#

RUN --mount=type=cache,id=agent-box-apt,target=/var/cache/apt,sharing=locked \
    set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends libasound2-plugins; \
    rm -rf /var/lib/apt/lists/*; \
    printf 'pcm.!default { type pulse }\nctl.!default { type pulse }\n' > /etc/asound.conf; \
    ls /usr/lib/*/alsa-lib/libasound_module_pcm_pulse.so

#
# ---------------------------------------------------------------------------
# Frequently updated tools
# ---------------------------------------------------------------------------
#
# agent-box update changes this ARG.
# Stable layers above are reused with an unchanged local base and build args.
# Downloads below use builder-owned caches, separate from the final image.
#

ARG TOOLS_REFRESH=initial

#
# mise
#
# mise is not the foundation of the image. It is available for project-specific
# toolchains declared through mise.toml, .mise.toml, .tool-versions, and similar
# project files.
#

RUN --mount=type=cache,id=agent-box-apt,target=/var/cache/apt,sharing=locked \
    set -eux; \
    echo "tools-refresh=${TOOLS_REFRESH}"; \
    install -d -m 0755 /etc/apt/keyrings; \
    curl -fsSL \
        https://mise.jdx.dev/gpg-key.pub \
        -o /tmp/mise-key.pub; \
    gpg \
        --batch \
        --yes \
        --dearmor \
        --output /etc/apt/keyrings/mise-archive-keyring.gpg \
        /tmp/mise-key.pub; \
    rm -f /tmp/mise-key.pub; \
    chmod 0644 /etc/apt/keyrings/mise-archive-keyring.gpg; \
    echo \
        'deb [signed-by=/etc/apt/keyrings/mise-archive-keyring.gpg] https://mise.jdx.dev/deb stable main' \
        > /etc/apt/sources.list.d/mise.list; \
    apt-get update; \
    apt-get install -y --no-install-recommends mise; \
    rm -rf /var/lib/apt/lists/*

#
# uv
#
# Ubuntu 26.04 provides Python 3.14 as the system Python.
# uv can install other Python versions for individual projects when needed.
#

RUN set -eux; \
    curl -LsSf \
        https://astral.sh/uv/install.sh \
        -o /tmp/uv-install.sh; \
    env UV_UNMANAGED_INSTALL=/usr/local/bin \
        sh /tmp/uv-install.sh; \
    rm -f /tmp/uv-install.sh

# Refresh Node.js without invalidating the pinned browser installation.
RUN --mount=type=cache,id=agent-box-apt,target=/var/cache/apt,sharing=locked \
    set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends nodejs; \
    rm -rf /var/lib/apt/lists/*

#
# Bun
#
# Required by Anthropic's official Telegram channel plugin
# (telegram@claude-plugins-official): its MCP server is a TypeScript file run
# directly by Bun, and its own `bun install` step fetches the plugin's
# dependencies on first start. Claude Code passes `bun` as a bare command, so
# it has to be on PATH for every user of the image, not just in a shell that
# sourced an installer profile snippet.
#

RUN set -eux; \
    export BUN_INSTALL=/usr/local; \
    curl -fsSL https://bun.sh/install -o /tmp/bun-install.sh; \
    bash /tmp/bun-install.sh; \
    rm -f /tmp/bun-install.sh; \
    test -x /usr/local/bin/bun

#
# Coding agents and agent-facing analysis tools
#

RUN --mount=type=cache,id=agent-box-npm,target=/root/.npm \
    set -eux; \
    npm install --global \
        @openai/codex@latest \
        @anthropic-ai/claude-code@latest

# An empty file-capability set clears ambient privileges on exec of bwrap
# alone. Dropping them from Codex itself breaks nested Podman's ID helpers.
# Keep the bundled binary intact so Codex's digest verification still applies.
RUN set -eux; \
    if [ "${TOOLS_PODMAN}" != "1" ]; then exit 0; fi; \
    set -- "$(npm root --global)"/@openai/codex/node_modules/@openai/codex-*/vendor/*/codex-resources/bwrap; \
    test "$#" -eq 1; \
    test -x "$1"; \
    setcap '=' "$1"; \
    test "$(getcap "$1")" = "$1 ="

#
# yq v4 (Mike Farah)
#

RUN set -eux; \
    if [ "${TOOLS_KUBERNETES}" != "1" ]; then echo "agent-box: yq skipped"; exit 0; fi; \
    arch="$(dpkg --print-architecture)"; \
    case "${arch}" in \
        amd64|arm64) ;; \
        *) \
            echo "Unsupported architecture for yq: ${arch}" >&2; \
            exit 1; \
            ;; \
    esac; \
    curl -fsSL \
        https://api.github.com/repos/mikefarah/yq/releases/latest \
        -o /tmp/yq-release.json; \
    yq_version="$(jq -er '.tag_name' /tmp/yq-release.json)"; \
    rm -f /tmp/yq-release.json; \
    curl -fsSL \
        "https://github.com/mikefarah/yq/releases/download/${yq_version}/yq_linux_${arch}" \
        -o /usr/local/bin/yq; \
    chmod 0755 /usr/local/bin/yq

#
# kubectl
#

RUN set -eux; \
    if [ "${TOOLS_KUBERNETES}" != "1" ]; then echo "agent-box: kubectl skipped"; exit 0; fi; \
    arch="$(dpkg --print-architecture)"; \
    case "${arch}" in \
        amd64|arm64) ;; \
        *) \
            echo "Unsupported architecture for kubectl: ${arch}" >&2; \
            exit 1; \
            ;; \
    esac; \
    curl -fsSL \
        https://dl.k8s.io/release/stable.txt \
        -o /tmp/kubectl-version; \
    kubectl_version="$(cat /tmp/kubectl-version)"; \
    curl -fsSL \
        "https://dl.k8s.io/release/${kubectl_version}/bin/linux/${arch}/kubectl" \
        -o /usr/local/bin/kubectl; \
    curl -fsSL \
        "https://dl.k8s.io/release/${kubectl_version}/bin/linux/${arch}/kubectl.sha256" \
        -o /tmp/kubectl.sha256; \
    expected="$(cat /tmp/kubectl.sha256)"; \
    actual="$(sha256sum /usr/local/bin/kubectl | cut -d' ' -f1)"; \
    test "${actual}" = "${expected}"; \
    chmod 0755 /usr/local/bin/kubectl; \
    rm -f /tmp/kubectl-version /tmp/kubectl.sha256

#
# Helm
#

RUN set -eux; \
    if [ "${TOOLS_KUBERNETES}" != "1" ]; then echo "agent-box: Helm skipped"; exit 0; fi; \
    arch="$(dpkg --print-architecture)"; \
    case "${arch}" in \
        amd64|arm64) ;; \
        *) \
            echo "Unsupported architecture for Helm: ${arch}" >&2; \
            exit 1; \
            ;; \
    esac; \
    curl -fsSL \
        https://api.github.com/repos/helm/helm/releases/latest \
        -o /tmp/helm-release.json; \
    helm_version="$(jq -er '.tag_name' /tmp/helm-release.json)"; \
    rm -f /tmp/helm-release.json; \
    curl -fsSL \
        "https://get.helm.sh/helm-${helm_version}-linux-${arch}.tar.gz" \
        -o /tmp/helm.tar.gz; \
    tar -xzf /tmp/helm.tar.gz -C /tmp; \
    install \
        -m 0755 \
        "/tmp/linux-${arch}/helm" \
        /usr/local/bin/helm; \
    rm -rf \
        /tmp/helm.tar.gz \
        "/tmp/linux-${arch}"

#
# Yandex Cloud CLI (group: yc)
#
# The release binary itself, not the installer, which would also edit rc
# files and plant a self-updater under the home directory; `agent-box update`
# refreshes it with the image. Yandex publishes no checksum for it, and its
# own installer verifies none either, so HTTPS from Yandex's storage is the
# integrity there is.

RUN set -eux; \
    if [ "${TOOLS_YC}" != "1" ]; then echo "agent-box: yc skipped"; exit 0; fi; \
    arch="$(dpkg --print-architecture)"; \
    case "${arch}" in \
        amd64|arm64) ;; \
        *) \
            echo "Unsupported architecture for yc: ${arch}" >&2; \
            exit 1; \
            ;; \
    esac; \
    yc_version="$(curl -fsSL https://storage.yandexcloud.net/yandexcloud-yc/release/stable)"; \
    curl -fsSL \
        "https://storage.yandexcloud.net/yandexcloud-yc/release/${yc_version}/linux/${arch}/yc" \
        -o /usr/local/bin/yc; \
    chmod 0755 /usr/local/bin/yc

#
# ---------------------------------------------------------------------------
# Sanity checks
# ---------------------------------------------------------------------------
#

RUN set -eux; \
    python --version; \
    uv --version; \
    node --version; \
    npm --version; \
    bun --version; \
    codex --version; \
    claude --version; \
    mise --version; \
    git --version; \
    git-lfs --version; \
    gh --version; \
    jq --version; \
    rg --version; \
    fd --version; \
    just --version; \
    tmux -V

# Each group verifies only what it actually installed. A check that ran
# unconditionally would fail the build for everyone who left a group out,
# which is the whole point of being able to leave it out.

RUN set -eux; \
    if [ "${TOOLS_ANALYSIS}" != "1" ]; then exit 0; fi; \
    shellcheck --version; \
    sqlite3 --version; \
    jscpd --version

RUN set -eux; \
    if [ "${TOOLS_KUBERNETES}" != "1" ]; then exit 0; fi; \
    yq --version; \
    kubectl version --client; \
    helm version --short

RUN set -eux; \
    if [ "${TOOLS_VIRTUALIZATION}" != "1" ]; then exit 0; fi; \
    qemu-system-x86_64 --version; \
    qemu-system-aarch64 --version; \
    qemu-img --version; \
    test -f /usr/share/AAVMF/AAVMF_CODE.fd

RUN set -eux; \
    if [ "${TOOLS_BROWSER}" != "1" ]; then exit 0; fi; \
    playwright-mcp --version; \
    test -d /opt/ms-playwright; \
    agent-box-browser-smoke

RUN set -eux; \
    if [ "${TOOLS_SKOPEO}" != "1" ]; then exit 0; fi; \
    skopeo --version

RUN set -eux; \
    if [ "${TOOLS_YC}" != "1" ]; then exit 0; fi; \
    yc version

RUN set -eux; \
    if [ "${TOOLS_PODMAN}" != "1" ]; then exit 0; fi; \
    podman --version; \
    crun --version; \
    slirp4netns --version; \
    getcap /usr/bin/newuidmap | grep -q 'cap_setuid=ep'; \
    getcap /usr/bin/newgidmap | grep -q 'cap_setgid=ep'

#
# ---------------------------------------------------------------------------
# Runtime
# ---------------------------------------------------------------------------

# What this image actually contains, so agent-box can tell a running image
# that matches the configuration from one built before a group was turned on.
LABEL io.agent-box.tools="${AGENT_BOX_TOOLS}"

USER ${USER_NAME}

ENV HOME=${USER_HOME} \
    MISE_DATA_DIR=${USER_HOME}/.local/share/mise \
    MISE_STATE_DIR=${USER_HOME}/.local/share/mise/state \
    UV_CACHE_DIR=${USER_HOME}/.cache/uv

# No fixed WORKDIR: agent_box.runtime always passes an explicit --workdir
# matching the host's absolute project/cwd path, which is bind-mounted at
# that same path (see workspace.py). This WORKDIR is only a sane default
# for `podman run` invocations that don't set one (e.g. manual debugging).
WORKDIR ${USER_HOME}

CMD ["/bin/bash"]

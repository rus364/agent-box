FROM docker.io/library/ubuntu:24.04

ARG DEBIAN_FRONTEND=noninteractive
# The container user mirrors the *building* host's real Unix identity
# (name/uid/gid/home, from agent_box.identity.detect_host_identity), so that
# host-absolute paths -- the project workspace, extra --mount targets, and
# host-authored symlinks such as ~/.claude/skills entries -- resolve
# identically inside the container. These defaults are only used if the
# image is built without --build-arg (e.g. `podman build` run by hand).
ARG USER_NAME=agent
ARG USER_UID=1000
ARG USER_GID=1000
ARG USER_HOME=/home/agent

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

RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        bash \
        bash-completion \
        ca-certificates \
        curl \
        wget \
        git \
        git-lfs \
        gh \
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
        shellcheck \
        sqlite3 \
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
        lsof \
        strace \
        \
        qemu-system-x86 \
        qemu-system-arm \
        qemu-utils \
        qemu-block-extra \
        ovmf \
        qemu-efi-aarch64 \
        \
        iproute2 \
        iputils-ping \
        dnsutils \
        netcat-openbsd \
        socat; \
    ln -sf /usr/bin/fdfind /usr/local/bin/fd; \
    git lfs install --system; \
    apt-get clean; \
    rm -rf /var/lib/apt/lists/*

#
# ---------------------------------------------------------------------------
# Container user
# ---------------------------------------------------------------------------
#
# Ubuntu 24.04 may already contain UID/GID 1000 (usually user "ubuntu").
#
# Therefore:
#   - reuse an existing GID;
#   - rename an existing UID;
#   - create the user/group normally when UID/GID are free.
#

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
# ---------------------------------------------------------------------------
# Frequently updated tools
# ---------------------------------------------------------------------------
#
# agent-box update changes this ARG.
# Everything above stays in the build cache while this layer and later layers
# are rebuilt.
#

ARG TOOLS_REFRESH=initial

#
# mise
#
# mise is not the foundation of the image. It is available for project-specific
# toolchains declared through mise.toml, .mise.toml, .tool-versions, and similar
# project files.
#

RUN set -eux; \
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
    apt-get clean; \
    rm -rf /var/lib/apt/lists/*

#
# uv
#
# Ubuntu 24.04 provides Python 3.12 as the system Python.
# uv can install other Python versions for individual projects when needed.
#

RUN set -eux; \
    curl -LsSf \
        https://astral.sh/uv/install.sh \
        -o /tmp/uv-install.sh; \
    env UV_UNMANAGED_INSTALL=/usr/local/bin \
        sh /tmp/uv-install.sh; \
    rm -f /tmp/uv-install.sh

#
# Node.js 22
#
# Both coding agents support the Node.js version used by this image.
#

RUN set -eux; \
    curl -fsSL \
        https://deb.nodesource.com/setup_22.x \
        -o /tmp/nodesource-setup.sh; \
    bash /tmp/nodesource-setup.sh; \
    rm -f /tmp/nodesource-setup.sh; \
    apt-get update; \
    apt-get install -y --no-install-recommends nodejs; \
    apt-get clean; \
    rm -rf /var/lib/apt/lists/*

#
# Coding agents
#

RUN set -eux; \
    npm install --global \
        @openai/codex@latest \
        @anthropic-ai/claude-code@latest; \
    npm cache clean --force

#
# yq v4 (Mike Farah)
#

RUN set -eux; \
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
# ---------------------------------------------------------------------------
# Sanity checks
# ---------------------------------------------------------------------------
#

RUN set -eux; \
    python --version; \
    uv --version; \
    node --version; \
    npm --version; \
    codex --version; \
    claude --version; \
    mise --version; \
    git --version; \
    git-lfs --version; \
    gh --version; \
    jq --version; \
    yq --version; \
    rg --version; \
    fd --version; \
    just --version; \
    shellcheck --version; \
    kubectl version --client; \
    helm version --short; \
    qemu-system-x86_64 --version; \
    qemu-system-aarch64 --version; \
    qemu-img --version; \
    test -f /usr/share/AAVMF/AAVMF_CODE.fd

#
# ---------------------------------------------------------------------------
# Runtime
# ---------------------------------------------------------------------------

USER ${USER_NAME}

ENV HOME=${USER_HOME} \
    MISE_DATA_DIR=${USER_HOME}/.local/share/mise \
    UV_CACHE_DIR=${USER_HOME}/.cache/uv

# No fixed WORKDIR: agent_box.runtime always passes an explicit --workdir
# matching the host's absolute project/cwd path, which is bind-mounted at
# that same path (see workspace.py). This WORKDIR is only a sane default
# for `podman run` invocations that don't set one (e.g. manual debugging).
WORKDIR ${USER_HOME}

CMD ["/bin/bash"]

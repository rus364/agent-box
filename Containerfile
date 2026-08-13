FROM docker.io/library/ubuntu:24.04

ARG DEBIAN_FRONTEND=noninteractive
ARG USER_NAME=rus364
ARG USER_UID=1000
ARG USER_GID=1000

ENV LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONUNBUFFERED=1 \
    UV_NO_PROGRESS=1 \
    UV_LINK_MODE=copy

#
# ---------------------------------------------------------------------------
# Base OS / development packages
# ---------------------------------------------------------------------------
#
# Этот слой намеренно расположен первым и меняется редко.
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
# Ubuntu 24.04 уже может содержать UID/GID 1000 (обычно user "ubuntu").
#
# Поэтому:
#   - существующий GID переиспользуем;
#   - существующий UID переименовываем;
#   - если UID/GID свободны — создаём нормально.
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
                --home "/home/${USER_NAME}" \
                --shell /bin/bash \
                "${existing_user}"; \
        fi; \
        usermod --gid "${USER_GID}" "${USER_NAME}"; \
    else \
        useradd \
            --uid "${USER_UID}" \
            --gid "${USER_GID}" \
            --home-dir "/home/${USER_NAME}" \
            --shell /bin/bash \
            "${USER_NAME}"; \
    fi; \
    mkdir -p \
        "/home/${USER_NAME}" \
        "/home/${USER_NAME}/code" \
        "/home/${USER_NAME}/.cache" \
        "/home/${USER_NAME}/.local/share/uv" \
        "/home/${USER_NAME}/.local/share/mise"; \
    chown -R "${USER_UID}:${USER_GID}" "/home/${USER_NAME}"

#
# ---------------------------------------------------------------------------
# Frequently updated tools
# ---------------------------------------------------------------------------
#
# codex-box update меняет этот ARG.
# Всё выше остаётся в build cache, а этот слой и следующие пересобираются.
#

ARG TOOLS_REFRESH=initial

#
# mise
#
# Не является основой image. Нужен для project-specific toolchains:
# mise.toml, .mise.toml, .tool-versions и т.п.
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
# System Python в Ubuntu — 3.12.
# Другие версии Python uv сможет устанавливать для конкретных проектов сам.
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
# OpenAI использует Node 22 и в своём secure Codex devcontainer.
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
# Codex CLI
#

RUN set -eux; \
    npm install --global @openai/codex@latest; \
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

ENV HOME=/home/${USER_NAME} \
    MISE_DATA_DIR=/home/${USER_NAME}/.local/share/mise \
    UV_CACHE_DIR=/home/${USER_NAME}/.cache/uv

WORKDIR /home/${USER_NAME}/code

CMD ["/bin/bash"]

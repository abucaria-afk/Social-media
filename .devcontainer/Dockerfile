FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive

# Install build deps and utilities
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        libcurl4-openssl-dev \
        libsecret-1-dev \
        make \
        git \
        ca-certificates \
        curl \
        pkg-config \
        sudo \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user (matches common Codespaces/devcontainers defaults)
ARG USERNAME=vscode
ARG USER_UID=1000
ARG USER_GID=1000
RUN groupadd --gid ${USER_GID} ${USERNAME} \
    && useradd -s /bin/bash --uid ${USER_UID} --gid ${USER_GID} -m ${USERNAME} \
    && echo "${USERNAME} ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/${USERNAME} \
    && chmod 0440 /etc/sudoers.d/${USERNAME}

USER ${USERNAME}
WORKDIR /workspace

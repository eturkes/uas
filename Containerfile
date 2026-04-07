FROM docker.io/library/python:3.12-bookworm

USER root

# Install Node.js and npm
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl git \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# Configure git system-wide so it works on bind-mounted workspaces.
# The container runs as root, but /workspace is owned by the host UID, which
# triggers git's safe.directory ownership check (exit 128). The wildcard
# exemption is safe because the container is a disposable sandbox. We also
# pre-set a default identity so `git commit` succeeds without any per-project
# configuration from the user.
RUN git config --system --add safe.directory '*' \
    && git config --system user.email 'uas@local' \
    && git config --system user.name 'UAS Orchestrator' \
    && git config --system init.defaultBranch main

# Install Claude Code CLI globally
RUN npm install -g @anthropic-ai/claude-code

# Install framework into /uas (immutable application code)
WORKDIR /uas

COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt

# Install uv for fast package management (used by generated scripts)
RUN curl -LsSf https://astral.sh/uv/install.sh | sh \
    && ln -sf /root/.local/bin/uv /usr/local/bin/uv

COPY config.py .
COPY hooks.py .
COPY orchestrator/ ./orchestrator/
COPY architect/ ./architect/
COPY uas/ ./uas/
COPY entrypoint.sh .
RUN chmod +x entrypoint.sh

# /workspace is the user project mount point
VOLUME /workspace
WORKDIR /workspace

# The engine container itself is the sandbox — no nested containers.
# Steps run as subprocesses inside this container.
ENV IS_SANDBOX=1
ENV UAS_SANDBOX_MODE=local

ENTRYPOINT ["/uas/entrypoint.sh"]

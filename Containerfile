FROM docker.io/library/python:3.12-bookworm

USER root

# Install Node.js and npm
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl git \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# Install Claude Code CLI globally
RUN npm install -g @anthropic-ai/claude-code

# Install framework into /uas (immutable application code)
WORKDIR /uas

COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt

COPY orchestrator/ ./orchestrator/
COPY architect/ ./architect/
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

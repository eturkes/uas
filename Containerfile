FROM docker.io/library/python:3.12-bookworm

USER root

# Install Podman (for nested sandbox containers), Node.js, and npm
RUN apt-get update && apt-get install -y --no-install-recommends \
    podman crun slirp4netns ca-certificates curl \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# Install Claude Code CLI globally
RUN npm install -g @anthropic-ai/claude-code

# Configure Podman for in-container use: vfs storage (no kernel overlay
# support needed), cgroupfs (systemd unavailable), file-based logging.
RUN printf '[storage]\ndriver = "vfs"\n' > /etc/containers/storage.conf \
    && printf '[engine]\ncgroup_manager = "cgroupfs"\nevents_logger = "file"\n' \
       > /etc/containers/containers.conf

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

ENV IS_SANDBOX=1

ENTRYPOINT ["/uas/entrypoint.sh"]

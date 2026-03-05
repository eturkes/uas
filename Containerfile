FROM quay.io/podman/stable:latest

USER root

# Install Python 3, pip, Node.js, and npm
RUN dnf install -y python3 python3-pip nodejs npm && dnf clean all

# Install Claude Code CLI globally
RUN npm install -g @anthropic-ai/claude-code

# Use vfs storage driver -- most reliable for nested container scenarios
RUN printf '[storage]\ndriver = "vfs"\n' > /etc/containers/storage.conf

WORKDIR /orchestrator

COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt

COPY orchestrator/ ./orchestrator/
COPY architect/ ./architect/
COPY entrypoint.sh .
RUN chmod +x entrypoint.sh

ENTRYPOINT ["./entrypoint.sh"]

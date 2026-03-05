FROM quay.io/podman/stable:latest

USER root

# Install Python 3 and pip
RUN dnf install -y python3 python3-pip && dnf clean all

# Use vfs storage driver -- most reliable for nested container scenarios
RUN printf '[storage]\ndriver = "vfs"\n' > /etc/containers/storage.conf

WORKDIR /orchestrator

COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt

COPY orchestrator/ ./orchestrator/

ENTRYPOINT ["python3", "-m", "orchestrator.main"]

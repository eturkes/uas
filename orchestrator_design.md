# Orchestrator Design: Nested Container Architecture

## Overview

The UAS Orchestrator uses a **Podman-in-Podman** architecture to safely bridge an LLM API with code execution. The Orchestrator itself runs as a container and spawns ephemeral **Execution Sandboxes** as nested containers *within its own namespace*, without any access to the host's container runtime socket.

```
Host
 └─ Orchestrator Container  (quay.io/podman/stable + Python)
      ├─ Orchestrator Python process (main.py)
      │    ├─ LLM Client  ──► external API (Anthropic / OpenAI / mock)
      │    ├─ Parser       ──► extracts code blocks from LLM response
      │    └─ Sandbox      ──► invokes nested `podman run`
      │
      └─ Nested Podman Engine (independent, no host socket)
           └─ Execution Sandbox  (python:3.12-slim, ephemeral)
                └─ runs LLM-generated script
```

## Security Model

| Layer | Control |
|---|---|
| Host ↔ Orchestrator | `--privileged` grants the Orchestrator the kernel capabilities needed for nested namespaces. No host socket is mounted. |
| Orchestrator ↔ Sandbox | `--network=none` (no network), `--read-only` filesystem, `--memory=256m`, `--cpus=1`, 60 s timeout. The generated script is bind-mounted read-only. |
| LLM-generated code | Never executed in the Orchestrator process. Always runs inside the nested sandbox. |

### Why `--privileged`?

Podman-in-Podman requires the outer container to create new user/PID/mount namespaces for the inner container. This needs `CAP_SYS_ADMIN` and access to `/dev/fuse`. The `--privileged` flag is the simplest and most reliable way to grant these. An alternative minimal set:

```bash
--cap-add=SYS_ADMIN \
--cap-add=MKNOD \
--cap-add=NET_ADMIN \
--device=/dev/fuse \
--security-opt label=disable \
--security-opt seccomp=unconfined
```

This project defaults to `--privileged` for broad compatibility.

## Build-Run-Evaluate Loop

```
1. Receive task (CLI arg / env var / stdin)
2. Verify nested Podman works (run a trivial print statement)
3. For attempt = 1..3:
   a. Build prompt (include previous error if retrying)
   b. Send prompt to LLM → receive response
   c. Extract code block from response
   d. Write code to temp file inside Orchestrator
   e. `podman run --rm ...` mounts the temp file into a sandbox container
   f. Capture stdout/stderr and exit code
   g. If exit_code == 0 → SUCCESS, stop
   h. Else → feed error back into prompt, retry
4. If all 3 attempts fail → exit with error
```

## Container Images

| Image | Role | Source |
|---|---|---|
| `quay.io/podman/stable` | Orchestrator base | Fedora with Podman pre-installed |
| `python:3.12-slim` | Execution Sandbox | Pulled on-demand by the nested Podman |

The sandbox image is configurable via the `UAS_SANDBOX_IMAGE` environment variable.

## Storage Driver

The Orchestrator's internal Podman uses the **vfs** storage driver (`/etc/containers/storage.conf`). While slower than `overlay`, vfs requires no kernel module support and is the most reliable driver for nested container scenarios.

## Host Launch Command

```bash
# With an API key (live LLM):
ANTHROPIC_API_KEY="sk-..." ./start_orchestrator.sh "Write a script that computes the first 20 Fibonacci numbers"

# Mock mode (no API key needed):
./start_orchestrator.sh "Write a hello world script"

# Using environment variable for the task:
UAS_TASK="Sort a list of 10 random integers" ./start_orchestrator.sh
```

The `start_orchestrator.sh` wrapper runs:

```bash
podman run --rm -it \
    --privileged \
    -e ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY" \
    -e UAS_TASK="$UAS_TASK" \
    uas-orchestrator
```

## Environment Variables

| Variable | Purpose | Default |
|---|---|---|
| `ANTHROPIC_API_KEY` | Anthropic API key | *(mock mode if unset)* |
| `ANTHROPIC_MODEL` | Anthropic model ID | `claude-sonnet-4-20250514` |
| `ANTHROPIC_BASE_URL` | Anthropic API base URL | `https://api.anthropic.com` |
| `OPENAI_API_KEY` | OpenAI API key | *(mock mode if unset)* |
| `OPENAI_MODEL` | OpenAI model ID | `gpt-4o` |
| `OPENAI_BASE_URL` | OpenAI API base URL | `https://api.openai.com` |
| `UAS_TASK` | Task to execute | *(prompted via stdin)* |
| `UAS_SANDBOX_IMAGE` | Sandbox container image | `docker.io/library/python:3.12-slim` |
| `UAS_SANDBOX_TIMEOUT` | Sandbox execution timeout (seconds) | `60` |

## File Structure

```
.
├── Containerfile              # Orchestrator image (Podman + Python)
├── start_orchestrator.sh      # Host-side build & launch wrapper
├── requirements.txt           # Python dependencies (httpx)
├── orchestrator/
│   ├── __init__.py
│   ├── main.py                # Entry point, Build-Run-Evaluate loop
│   ├── llm_client.py          # Modular LLM interface (Anthropic/OpenAI/Mock)
│   ├── sandbox.py             # Nested Podman sandbox execution
│   └── parser.py              # Code extraction from LLM responses
└── orchestrator_design.md     # This document
```

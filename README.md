# Universal Agentic Specification (UAS)

An autonomous orchestrator that bridges an LLM API with a secure, nested-container execution sandbox. Uses a Podman-in-Podman architecture to generate, execute, and evaluate code without exposing the host container runtime.

## Quick Start

```bash
# Mock mode (no API key needed):
./start_orchestrator.sh "Write a script that prints hello world"

# With a live LLM:
ANTHROPIC_API_KEY="sk-..." ./start_orchestrator.sh "Compute the first 20 Fibonacci numbers"
```

The wrapper builds the Orchestrator image, launches it with `--privileged` for nested container support, and runs the Build-Run-Evaluate loop (up to 3 attempts).

## Requirements

[Podman](https://podman.io/) or [Docker](https://www.docker.com/) must be available on the host.

## Project Structure

```
.
├── Containerfile           # Orchestrator image (Podman + Python)
├── start_orchestrator.sh   # Host-side build & launch wrapper
├── requirements.txt        # Python dependencies (httpx)
├── orchestrator/
│   ├── main.py             # Entry point, Build-Run-Evaluate loop
│   ├── llm_client.py       # Modular LLM interface (Anthropic/OpenAI/Mock)
│   ├── sandbox.py          # Nested Podman sandbox execution
│   └── parser.py           # Code extraction from LLM responses
├── orchestrator_design.md  # Architecture documentation
└── stack_decisions.md      # Legacy stack rationale
```

## Architecture

- **Base image:** `quay.io/podman/stable` (Fedora + Podman)
- **Sandbox image:** `python:3.12-slim` (pulled on-demand inside the Orchestrator)
- **Storage driver:** `vfs` (most reliable for nested scenarios)
- **LLM clients:** Anthropic, OpenAI, or mock (selected via environment variables)

See [orchestrator_design.md](orchestrator_design.md) for the full nested-container architecture.

## License

Licensed under the Apache License v2.0 with LLVM Exceptions. See [LICENSE](LICENSE) for details.

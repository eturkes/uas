# Universal Agentic Specification (UAS)

A secure, containerized execution environment (sandbox) optimized for autonomous agent workflows. The sandbox executes arbitrary code, performs headless browser automation for web data extraction, and serves as a foundation for data science workloads.

## Quick Start

```bash
./run.sh
```

This builds the container image and runs the validation script, which navigates to `https://example.com`, extracts the page title, and saves a screenshot and DOM snapshot to `./output`.

## Requirements

A container engine must be available on the host. The wrapper script auto-detects:

- [Podman](https://podman.io/)
- [Docker](https://www.docker.com/)
- [nerdctl](https://github.com/containerd/nerdctl)

## Project Structure

```
.
├── Containerfile        # Container image definition
├── requirements.txt     # Python dependencies
├── validate.py          # Headless browser validation script
├── run.sh               # Build & run wrapper
├── stack_decisions.md   # Architecture rationale
└── output/              # Bind-mounted artifacts directory
```

## Stack

- **Base image:** `python:3.12-slim`
- **Headless browser:** [Playwright](https://playwright.dev/) with Chromium
- **Container format:** OCI (Containerfile)

See [stack_decisions.md](stack_decisions.md) for detailed rationale.

## License

Licensed under the Apache License v2.0 with LLVM Exceptions. See [LICENSE](LICENSE) for details.

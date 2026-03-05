# Universal Agentic Specification (UAS)

A two-layer autonomous system that takes abstract human goals and drives them to completion. The **Architect Agent** decomposes goals into atomic steps, generates UAS-compliant specs, and feeds them to the **Execution Orchestrator**, which generates and runs code in a secure Podman-in-Podman sandbox.

## Quick Start

```bash
# Full framework (Architect + Orchestrator):
ANTHROPIC_API_KEY="sk-..." ./run_framework.sh "Create a text file with 'Hello', then create a second script that reads that file and prints it reversed"

# Orchestrator only (single task, mock mode):
./start_orchestrator.sh "Write a script that prints hello world"
```

## Requirements

- Python 3 on the host (for the Architect Agent)
- [Podman](https://podman.io/) or [Docker](https://www.docker.com/) (for the Orchestrator)

## Project Structure

```
.
├── run_framework.sh          # Entry point: goal in, results out
├── architect/                # Architect Agent (host-side planner)
│   ├── main.py               # Controller loop
│   ├── planner.py            # LLM task decomposition + rewrite
│   ├── spec_generator.py     # UAS markdown spec writer
│   ├── executor.py           # Orchestrator subprocess interface
│   └── state.py              # JSON state persistence
├── orchestrator/             # Execution Orchestrator (containerized)
│   ├── main.py               # Build-Run-Evaluate loop
│   ├── llm_client.py         # LLM interface (Anthropic/OpenAI/Mock)
│   ├── sandbox.py            # Nested Podman sandbox execution
│   └── parser.py             # Code extraction from LLM responses
├── Containerfile             # Orchestrator image (Podman + Python)
├── start_orchestrator.sh     # Orchestrator-only launcher
├── architect_design.md       # Architect architecture documentation
├── orchestrator_design.md    # Orchestrator architecture documentation
└── stack_decisions.md        # Stack rationale
```

## Architecture

```
User
 └─ run_framework.sh
     └─ Architect Agent (host Python)
         ├─ Planner        -> LLM decomposes goal into steps
         ├─ Spec Generator  -> writes UAS markdown specs
         ├─ State Manager   -> tracks plan_state.json
         └─ Executor        -> invokes Orchestrator container
              └─ Orchestrator Container (Podman-in-Podman)
                  ├─ LLM Client -> generates Python code
                  └─ Sandbox    -> executes in nested container
```

The Architect handles multi-step goals by capturing stdout from each step and injecting it as context into dependent steps. If a step fails after all Orchestrator retries, the Architect rewrites the spec up to 2 times before halting with `ARCHITECT_BLOCKER.md`.

See [architect_design.md](architect_design.md) and [orchestrator_design.md](orchestrator_design.md) for full details.

## License

Licensed under the Apache License v2.0 with LLVM Exceptions. See [LICENSE](LICENSE) for details.

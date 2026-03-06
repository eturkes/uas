"""Standalone explanation CLI for post-hoc analysis of UAS runs.

Usage:
    python3 -m architect.explain [workspace_path]

Reads saved state, events, provenance, and code versions from a previous
run and prints the explanation to stdout.
"""

import argparse
import sys

from .explain import RunExplainer, load_run_data


def main():
    parser = argparse.ArgumentParser(
        description="Explain a previous UAS run",
    )
    parser.add_argument(
        "workspace",
        nargs="?",
        default="/workspace",
        help="Path to the workspace directory (default: /workspace)",
    )
    parser.add_argument(
        "--step", type=int, default=None,
        help="Explain a specific step by ID",
    )
    parser.add_argument(
        "--failure", type=int, default=None,
        help="Explain a specific failure by step ID",
    )
    parser.add_argument(
        "--critical-path", action="store_true",
        help="Explain the critical path",
    )
    parser.add_argument(
        "--cost", action="store_true",
        help="Show cost analysis",
    )
    args = parser.parse_args()

    try:
        state, events, provenance, code_versions = load_run_data(args.workspace)
    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    explainer = RunExplainer(state, events, provenance, code_versions)

    if args.step is not None:
        print(explainer.explain_step(args.step))
    elif args.failure is not None:
        print(explainer.explain_failure(args.failure))
    elif args.critical_path:
        print(explainer.explain_critical_path())
    elif args.cost:
        print(explainer.explain_cost())
    else:
        print(explainer.explain_run())


if __name__ == "__main__":
    main()

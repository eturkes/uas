"""LLM-based task decomposition into atomic steps."""

import json
import os
import re
import sys


def _get_llm_client():
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
    from orchestrator.llm_client import get_llm_client
    return get_llm_client()

DECOMPOSITION_PROMPT = """\
You are a task decomposition engine. Given a high-level goal, break it into \
a sequence of atomic, independently executable steps.

RULES:
1. Each step MUST be a self-contained Python script task.
2. Steps share a persistent workspace directory for file I/O. \
The path is available via os.environ.get('WORKSPACE', '/workspace'). \
Later steps can read files written by earlier steps from this directory.
3. Each step should be small and focused on one action.
4. Steps execute sequentially.
5. Keep the number of steps minimal.

Respond with ONLY a JSON array. Each element:
{{"title": "short name", "description": "detailed task for a code-generating LLM", \
"depends_on": [step_numbers]}}

Goal: {goal}
"""


def parse_steps_json(response: str) -> list[dict]:
    text = response.strip()

    # Direct parse
    try:
        result = json.loads(text)
        if isinstance(result, list):
            return result
    except json.JSONDecodeError:
        pass

    # Code fence extraction
    match = re.search(r"```(?:json)?\s*\n(.*?)```", text, re.DOTALL)
    if match:
        try:
            result = json.loads(match.group(1).strip())
            if isinstance(result, list):
                return result
        except json.JSONDecodeError:
            pass

    # Bracket extraction
    start = text.find("[")
    end = text.rfind("]")
    if start != -1 and end != -1:
        try:
            result = json.loads(text[start : end + 1])
            if isinstance(result, list):
                return result
        except json.JSONDecodeError:
            pass

    raise ValueError(f"Could not parse steps from LLM response:\n{text[:500]}")


def decompose_goal(goal: str) -> list[dict]:
    client = _get_llm_client()
    prompt = DECOMPOSITION_PROMPT.format(goal=goal)
    response = client.generate(prompt)
    steps = parse_steps_json(response)
    if not steps:
        raise ValueError("LLM returned an empty step list")
    for step in steps:
        if "title" not in step or "description" not in step:
            raise ValueError(f"Step missing required fields: {step}")
        step.setdefault("depends_on", [])
    return steps


def rewrite_task(step: dict, orchestrator_stdout: str, orchestrator_stderr: str) -> str:
    client = _get_llm_client()
    prompt = (
        "A code-generation task was sent to an orchestrator but failed after "
        "3 attempts. Analyze the failure and provide an improved task description.\n\n"
        f"Original task:\n{step['description']}\n\n"
        f"Orchestrator stdout (last 2000 chars):\n{orchestrator_stdout[-2000:]}\n\n"
        f"Orchestrator stderr (last 1000 chars):\n{orchestrator_stderr[-1000:]}\n\n"
        "Provide ONLY the improved task description. Be more specific and explicit "
        "about what the Python code should do. Do not include any explanation."
    )
    return client.generate(prompt).strip()

"""Tests for ``orchestrator.worker.spawn_worker`` (Phase 3 §2).

The end-to-end test spawns a real headless Claude Code worker against
the ``uas-engine:latest`` image, which requires:

- A container engine on PATH (podman / docker), provided by the
  ``uas_engine`` fixture in ``conftest.py``.
- Valid Claude Max OAuth credentials under ``<repo>/.uas_auth/``,
  provided by the ``require_auth`` fixture.

The test is therefore marked ``integration``; the default pytest
config (``addopts = -m "not integration"``) skips it. Run with
``pytest -m integration -k orchestrator_worker`` to exercise the
real spawn path.

The pure-Python helpers in ``orchestrator.worker`` (``_safe_segment``,
``_build_command``) are covered without ``integration`` so they
exercise on every CI run.
"""

import os
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from conftest import find_engine  # noqa: E402

from orchestrator import container, worker  # noqa: E402


# ---------------------------------------------------------------------------
# Pure-Python helpers (always run)
# ---------------------------------------------------------------------------

class TestSafeSegment:
    """Container-name segments must survive arbitrary task / subtask ids."""

    def test_alnum_passthrough(self):
        assert worker._safe_segment("task01") == "task01"

    def test_collapses_unsafe_runs(self):
        assert worker._safe_segment("a / b / c") == "a-b-c"

    def test_empty_segment_replaced(self):
        # Empty string would yield an unnamed segment; sanitiser falls
        # back to "x" so the assembled container name remains valid.
        assert worker._safe_segment("") == "x"

    def test_strips_leading_trailing_dashes(self):
        assert worker._safe_segment("///foo///") == "foo"


class TestBuildCommand:
    """Argv assembly is independent of any container being available."""

    def test_includes_required_flags(self, tmp_path, monkeypatch):
        monkeypatch.delenv("UAS_HOST_UID", raising=False)
        monkeypatch.delenv("UAS_HOST_GID", raising=False)
        cmd = worker._build_command(
            engine="podman",
            name="uas-orchestrator-t-s-deadbeef",
            workspace=str(tmp_path),
            prompt="hello world",
        )
        assert cmd[0] == "podman"
        # --storage-driver=vfs is the podman-specific flag carried over
        # from orchestrator/sandbox.py; the docker branch drops it.
        assert "--storage-driver=vfs" in cmd
        assert "--rm" in cmd
        assert "--network=host" in cmd
        assert "--name" in cmd
        assert "uas-orchestrator-t-s-deadbeef" in cmd
        assert f"{tmp_path}:/workspace:Z" in cmd
        assert "--entrypoint" in cmd
        assert "/bin/bash" in cmd
        assert container.IMAGE_TAG in cmd
        # Prompt is forwarded via env, not as a positional shell arg.
        assert "UAS_WORKER_PROMPT=hello world" in cmd
        # The shell payload runs claude with the documented flags.
        shell_payload = cmd[-1]
        assert "claude --print --dangerously-skip-permissions" in shell_payload
        assert "--output-format stream-json --verbose" in shell_payload

    def test_forwards_host_uid_when_set(self, tmp_path, monkeypatch):
        monkeypatch.setenv("UAS_HOST_UID", "1000")
        monkeypatch.setenv("UAS_HOST_GID", "1000")
        cmd = worker._build_command(
            engine="podman",
            name="uas-orchestrator-x-y-cafef00d",
            workspace=str(tmp_path),
            prompt="p",
        )
        assert "UAS_HOST_UID=1000" in cmd
        assert "UAS_HOST_GID=1000" in cmd

    def test_docker_omits_storage_driver_flag(self, tmp_path, monkeypatch):
        # docker rejects --storage-driver=vfs; the engine gate must
        # drop it for docker so eval.py-style invocations keep working
        # on docker-only hosts.
        monkeypatch.delenv("UAS_HOST_UID", raising=False)
        monkeypatch.delenv("UAS_HOST_GID", raising=False)
        cmd = worker._build_command(
            engine="/usr/bin/docker",
            name="uas-orchestrator-d-d-12345678",
            workspace=str(tmp_path),
            prompt="p",
        )
        assert cmd[0] == "/usr/bin/docker"
        assert "--storage-driver=vfs" not in cmd


# ---------------------------------------------------------------------------
# Live integration test
# ---------------------------------------------------------------------------

@pytest.mark.integration
class TestSpawnWorkerLive:
    """End-to-end spawn against real Claude Code + uas-engine."""

    def test_trivial_done_prompt(
        self, require_auth, uas_engine, tmp_path,
    ):
        """Spawn a worker that should reply with 'done' and verify shape."""
        result = worker.spawn_worker(
            "Reply with only the literal word: done",
            workspace=str(tmp_path),
            task_id="phase3-section2",
            subtask_id="trivial-done",
            timeout_seconds=300,
        )

        # Surface the captured raw lines on failure so the cause is
        # readable in the pytest report rather than buried in stderr.
        diag = "\n".join(result.get("raw_lines", [])[-20:])

        assert result["exit_code"] == 0, (
            f"Worker exited {result['exit_code']}; tail:\n{diag}"
        )
        assert result["result"] is not None, (
            f"Missing terminal result event; tail:\n{diag}"
        )
        usage = result["result"].get("usage") or {}
        assert usage.get("input_tokens", 0) > 0, (
            f"Expected nonzero input_tokens; usage={usage}"
        )
        assert len(result["rate_limit_events"]) >= 1, (
            "Expected at least one rate_limit_event; got "
            f"{result['rate_limit_events']!r}"
        )
        assert "done" in result["output"].lower(), (
            f"Expected 'done' in output; got {result['output']!r}"
        )

        # Confirm cleanup left no stale orchestrator-managed containers.
        engine = find_engine()
        proc = subprocess.run(
            [engine, "ps", "-a",
             "--filter", "name=uas-orchestrator-",
             "--format", "{{.Names}}"],
            capture_output=True, text=True, timeout=15,
            stdin=subprocess.DEVNULL,
        )
        survivors = [
            n for n in proc.stdout.split() if n.strip()
        ]
        assert not survivors, (
            f"Stale containers survived spawn_worker: {survivors!r}"
        )

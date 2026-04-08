"""Tests for the hooks module (Section 8 of PLAN.md)."""

import json
import os
import stat
import textwrap

import pytest

from uas_hooks import (
    HookEvent,
    HookConfig,
    load_hooks,
    run_hook,
)


# ---------------------------------------------------------------------------
# HookConfig basics
# ---------------------------------------------------------------------------

class TestHookConfig:
    def test_default_timeout(self):
        h = HookConfig(event=HookEvent.PRE_STEP, command="echo hi")
        assert h.timeout == 30

    def test_custom_timeout(self):
        h = HookConfig(event=HookEvent.POST_STEP, command="true", timeout=5)
        assert h.timeout == 5


# ---------------------------------------------------------------------------
# load_hooks
# ---------------------------------------------------------------------------

class TestLoadHooks:
    def test_no_file_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.setenv("UAS_WORKSPACE", str(tmp_path))
        assert load_hooks() == []

    def test_loads_from_hooks_toml(self, tmp_path, monkeypatch):
        monkeypatch.setenv("UAS_WORKSPACE", str(tmp_path))
        hooks_dir = tmp_path / ".uas"
        hooks_dir.mkdir()
        hooks_file = hooks_dir / "hooks.toml"
        hooks_file.write_text(textwrap.dedent("""\
            [[hooks]]
            event = "PRE_STEP"
            command = "echo pre"
            timeout = 5

            [[hooks]]
            event = "POST_STEP"
            command = "echo post"
        """))
        result = load_hooks()
        assert len(result) == 2
        assert result[0].event == HookEvent.PRE_STEP
        assert result[0].command == "echo pre"
        assert result[0].timeout == 5
        assert result[1].event == HookEvent.POST_STEP
        assert result[1].timeout == 30  # default

    def test_loads_from_explicit_path(self, tmp_path, monkeypatch):
        monkeypatch.setenv("UAS_WORKSPACE", str(tmp_path))
        hooks_file = tmp_path / "custom_hooks.toml"
        hooks_file.write_text(textwrap.dedent("""\
            [[hooks]]
            event = "RUN_START"
            command = "echo start"
        """))
        result = load_hooks(config_path=str(hooks_file))
        assert len(result) == 1
        assert result[0].event == HookEvent.RUN_START

    def test_loads_from_config_toml_hooks_section(self, tmp_path, monkeypatch):
        monkeypatch.setenv("UAS_WORKSPACE", str(tmp_path))
        uas_dir = tmp_path / ".uas"
        uas_dir.mkdir()
        config_file = uas_dir / "config.toml"
        config_file.write_text(textwrap.dedent("""\
            [other]
            key = "value"

            [[hooks]]
            event = "POST_PLAN"
            command = "echo plan"
        """))
        result = load_hooks()
        assert len(result) == 1
        assert result[0].event == HookEvent.POST_PLAN

    def test_skips_unknown_events(self, tmp_path, monkeypatch):
        monkeypatch.setenv("UAS_WORKSPACE", str(tmp_path))
        hooks_dir = tmp_path / ".uas"
        hooks_dir.mkdir()
        hooks_file = hooks_dir / "hooks.toml"
        hooks_file.write_text(textwrap.dedent("""\
            [[hooks]]
            event = "UNKNOWN_EVENT"
            command = "echo bad"

            [[hooks]]
            event = "PRE_STEP"
            command = "echo good"
        """))
        result = load_hooks()
        assert len(result) == 1
        assert result[0].event == HookEvent.PRE_STEP

    def test_skips_missing_command(self, tmp_path, monkeypatch):
        monkeypatch.setenv("UAS_WORKSPACE", str(tmp_path))
        hooks_dir = tmp_path / ".uas"
        hooks_dir.mkdir()
        hooks_file = hooks_dir / "hooks.toml"
        hooks_file.write_text(textwrap.dedent("""\
            [[hooks]]
            event = "PRE_STEP"
        """))
        result = load_hooks()
        assert len(result) == 0


# ---------------------------------------------------------------------------
# run_hook
# ---------------------------------------------------------------------------

class TestRunHook:
    def test_no_matching_hooks(self):
        hooks = [HookConfig(event=HookEvent.PRE_STEP, command="echo hi")]
        result = run_hook(HookEvent.POST_STEP, {"key": "val"}, hooks)
        assert result is None

    def test_empty_hooks_list(self):
        result = run_hook(HookEvent.PRE_STEP, {"key": "val"}, [])
        assert result is None

    def test_hook_receives_json_stdin(self, tmp_path):
        script = tmp_path / "hook.sh"
        script.write_text("#!/bin/bash\ncat\n")
        script.chmod(script.stat().st_mode | stat.S_IEXEC)

        hooks = [HookConfig(
            event=HookEvent.PRE_STEP,
            command=str(script),
        )]
        result = run_hook(HookEvent.PRE_STEP, {"step_id": 1}, hooks)
        assert result is not None
        assert result["event"] == "PRE_STEP"
        assert result["step_id"] == 1

    def test_abort_response(self, tmp_path):
        script = tmp_path / "hook.sh"
        script.write_text(
            '#!/bin/bash\necho \'{"abort": true, "reason": "blocked"}\'\n'
        )
        script.chmod(script.stat().st_mode | stat.S_IEXEC)

        hooks = [HookConfig(
            event=HookEvent.PRE_STEP,
            command=str(script),
        )]
        result = run_hook(HookEvent.PRE_STEP, {"step_id": 1}, hooks)
        assert result is not None
        assert result["abort"] is True
        assert result["reason"] == "blocked"

    def test_hook_timeout(self, tmp_path):
        script = tmp_path / "hook.sh"
        script.write_text("#!/bin/bash\nsleep 60\n")
        script.chmod(script.stat().st_mode | stat.S_IEXEC)

        hooks = [HookConfig(
            event=HookEvent.PRE_STEP,
            command=str(script),
            timeout=1,
        )]
        result = run_hook(HookEvent.PRE_STEP, {"step_id": 1}, hooks)
        assert result is None

    def test_hook_nonzero_exit_ignored(self, tmp_path):
        script = tmp_path / "hook.sh"
        script.write_text("#!/bin/bash\nexit 1\n")
        script.chmod(script.stat().st_mode | stat.S_IEXEC)

        hooks = [HookConfig(
            event=HookEvent.PRE_STEP,
            command=str(script),
        )]
        result = run_hook(HookEvent.PRE_STEP, {"step_id": 1}, hooks)
        assert result is None

    def test_hook_invalid_json_stdout_ignored(self, tmp_path):
        script = tmp_path / "hook.sh"
        script.write_text("#!/bin/bash\necho 'not json'\n")
        script.chmod(script.stat().st_mode | stat.S_IEXEC)

        hooks = [HookConfig(
            event=HookEvent.PRE_STEP,
            command=str(script),
        )]
        result = run_hook(HookEvent.PRE_STEP, {"step_id": 1}, hooks)
        assert result is None

    def test_hook_empty_stdout(self, tmp_path):
        hooks = [HookConfig(
            event=HookEvent.PRE_STEP,
            command="true",
        )]
        result = run_hook(HookEvent.PRE_STEP, {"step_id": 1}, hooks)
        assert result is None

    def test_post_plan_steps_override(self, tmp_path):
        new_steps = [{"id": 1, "title": "overridden", "depends_on": []}]
        script = tmp_path / "hook.sh"
        script.write_text(
            f'#!/bin/bash\necho \'{json.dumps({"steps": new_steps})}\'\n'
        )
        script.chmod(script.stat().st_mode | stat.S_IEXEC)

        hooks = [HookConfig(
            event=HookEvent.POST_PLAN,
            command=str(script),
        )]
        result = run_hook(HookEvent.POST_PLAN, {"goal": "test"}, hooks)
        assert result is not None
        assert result["steps"] == new_steps

    def test_multiple_matching_hooks_merged(self, tmp_path):
        script1 = tmp_path / "hook1.sh"
        script1.write_text('#!/bin/bash\necho \'{"a": 1}\'\n')
        script1.chmod(script1.stat().st_mode | stat.S_IEXEC)

        script2 = tmp_path / "hook2.sh"
        script2.write_text('#!/bin/bash\necho \'{"b": 2}\'\n')
        script2.chmod(script2.stat().st_mode | stat.S_IEXEC)

        hooks = [
            HookConfig(event=HookEvent.POST_STEP, command=str(script1)),
            HookConfig(event=HookEvent.POST_STEP, command=str(script2)),
        ]
        result = run_hook(HookEvent.POST_STEP, {}, hooks)
        assert result == {"a": 1, "b": 2}

    def test_abort_short_circuits(self, tmp_path):
        script1 = tmp_path / "hook1.sh"
        script1.write_text(
            '#!/bin/bash\necho \'{"abort": true, "reason": "stop"}\'\n'
        )
        script1.chmod(script1.stat().st_mode | stat.S_IEXEC)

        script2 = tmp_path / "hook2.sh"
        script2.write_text('#!/bin/bash\necho \'{"ran": true}\'\n')
        script2.chmod(script2.stat().st_mode | stat.S_IEXEC)

        hooks = [
            HookConfig(event=HookEvent.PRE_STEP, command=str(script1)),
            HookConfig(event=HookEvent.PRE_STEP, command=str(script2)),
        ]
        result = run_hook(HookEvent.PRE_STEP, {}, hooks)
        assert result["abort"] is True
        assert "ran" not in result

    def test_missing_script_doesnt_crash(self):
        hooks = [HookConfig(
            event=HookEvent.PRE_STEP,
            command="/nonexistent/path/hook.sh",
        )]
        result = run_hook(HookEvent.PRE_STEP, {"step_id": 1}, hooks)
        assert result is None


# ---------------------------------------------------------------------------
# Zero overhead when no hooks configured
# ---------------------------------------------------------------------------

class TestZeroOverhead:
    def test_run_hook_with_no_hooks(self):
        """run_hook with empty list does nothing (no subprocess)."""
        result = run_hook(HookEvent.PRE_STEP, {"step_id": 1}, [])
        assert result is None

    def test_load_hooks_no_config(self, tmp_path, monkeypatch):
        monkeypatch.setenv("UAS_WORKSPACE", str(tmp_path))
        hooks = load_hooks()
        assert hooks == []

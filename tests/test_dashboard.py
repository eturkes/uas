"""Tests for architect.dashboard module."""

import io
import threading

from architect.dashboard import Dashboard, STATUS_ICONS, _RICH_AVAILABLE, LOG_PANEL_HEIGHT


def _make_state(steps=None, goal="Test goal", status="executing", total_elapsed=0.0):
    if steps is None:
        steps = [
            {
                "id": 1,
                "title": "Step one",
                "description": "Do something",
                "depends_on": [],
                "status": "completed",
                "elapsed": 5.0,
                "timing": {"llm_time": 2.0, "sandbox_time": 3.0, "total_time": 5.0},
                "error": "",
                "rewrites": 0,
            },
            {
                "id": 2,
                "title": "Step two",
                "description": "Do something else",
                "depends_on": [1],
                "status": "executing",
                "elapsed": 0.0,
                "timing": {"llm_time": 0.0, "sandbox_time": 0.0, "total_time": 0.0},
                "error": "",
                "rewrites": 0,
            },
            {
                "id": 3,
                "title": "Step three",
                "description": "Final thing",
                "depends_on": [1],
                "status": "pending",
                "elapsed": 0.0,
                "timing": {"llm_time": 0.0, "sandbox_time": 0.0, "total_time": 0.0},
                "error": "",
                "rewrites": 0,
            },
        ]
    return {"goal": goal, "steps": steps, "status": status, "total_elapsed": total_elapsed}


class TestDashboardFallback:
    """Test plain-text fallback mode (non-TTY)."""

    def test_non_tty_uses_fallback(self):
        buf = io.StringIO()
        state = _make_state()
        dash = Dashboard(state, file=buf)
        assert not dash.use_rich

    def test_fallback_update_prints_executing(self):
        buf = io.StringIO()
        state = _make_state()
        dash = Dashboard(state, file=buf)
        dash.update(state)
        output = buf.getvalue()
        assert "Step 2" in output
        assert "Step two" in output

    def test_fallback_update_no_output_for_non_executing(self):
        buf = io.StringIO()
        steps = [
            {
                "id": 1,
                "title": "Done step",
                "description": "Already done",
                "depends_on": [],
                "status": "completed",
                "elapsed": 1.0,
                "timing": {"llm_time": 0.5, "sandbox_time": 0.5, "total_time": 1.0},
                "error": "",
                "rewrites": 0,
            },
        ]
        state = _make_state(steps=steps)
        dash = Dashboard(state, file=buf)
        dash.update(state)
        assert buf.getvalue() == ""

    def test_print_plan_plain(self):
        buf = io.StringIO()
        state = _make_state()
        dash = Dashboard(state, file=buf)
        dash.print_plan(state)
        output = buf.getvalue()
        assert "Goal:" in output
        assert "Step one" in output
        assert "Step two" in output
        assert "Level" in output

    def test_report_progress_plain(self):
        buf = io.StringIO()
        state = _make_state()
        dash = Dashboard(state, file=buf)
        step = state["steps"][1]
        dash.report_progress(step, total=3, completed=1, failed=0, attempt=1)
        output = buf.getvalue()
        assert "Step 2" in output
        assert "attempt 1" in output

    def test_finish_plain(self):
        buf = io.StringIO()
        state = _make_state(total_elapsed=10.5)
        dash = Dashboard(state, file=buf)
        dash.finish(state)
        output = buf.getvalue()
        assert "Step one" in output
        assert "Step two" in output
        assert "TOTAL" in output
        assert "10.5s" in output

    def test_finish_plain_shows_timing(self):
        buf = io.StringIO()
        state = _make_state(total_elapsed=5.0)
        dash = Dashboard(state, file=buf)
        dash.finish(state)
        output = buf.getvalue()
        assert "2.0s" in output  # llm_time
        assert "3.0s" in output  # sandbox_time


class TestDashboardRich:
    """Test rich mode rendering (using Console with StringIO capture)."""

    def test_rich_available(self):
        assert _RICH_AVAILABLE, "rich must be installed for these tests"

    def test_rich_print_plan(self):
        from rich.console import Console

        buf = io.StringIO()
        console = Console(file=buf, force_terminal=True)
        state = _make_state()
        dash = Dashboard(state, file=buf)
        # Override to use our console
        dash._use_rich = True
        dash._console = console
        dash.print_plan(state)
        output = buf.getvalue()
        assert "Step one" in output or "Plan" in output

    def test_rich_finish(self):
        from rich.console import Console

        buf = io.StringIO()
        console = Console(file=buf, force_terminal=True)
        state = _make_state(total_elapsed=15.0)
        dash = Dashboard(state, file=buf)
        dash._use_rich = True
        dash._console = console
        dash._live = None  # No live display to stop
        dash.finish(state)
        output = buf.getvalue()
        assert "Step one" in output
        assert "TOTAL" in output

    def test_rich_report_progress_suppressed(self):
        buf = io.StringIO()
        state = _make_state()
        dash = Dashboard(state, file=buf)
        dash._use_rich = True
        step = state["steps"][1]
        dash.report_progress(step, total=3, completed=1, failed=0)
        # Rich mode suppresses line-by-line progress
        assert buf.getvalue() == ""

    def test_render_returns_layout(self):
        from rich.layout import Layout

        buf = io.StringIO()
        state = _make_state()
        dash = Dashboard(state, file=buf)
        dash._use_rich = True
        result = dash._render()
        assert isinstance(result, Layout)

    def test_render_dag_panel(self):
        from rich.console import Console
        from rich.panel import Panel

        buf = io.StringIO()
        state = _make_state()
        dash = Dashboard(state, file=buf)
        dash._use_rich = True
        panel = dash._render_dag()
        assert isinstance(panel, Panel)

    def test_render_header_panel(self):
        from rich.panel import Panel

        buf = io.StringIO()
        state = _make_state()
        dash = Dashboard(state, file=buf)
        dash._use_rich = True
        panel = dash._render_header()
        assert isinstance(panel, Panel)

    def test_render_timing_table(self):
        from rich.table import Table

        buf = io.StringIO()
        state = _make_state()
        dash = Dashboard(state, file=buf)
        dash._use_rich = True
        table = dash._render_timing()
        assert isinstance(table, Table)


class TestDashboardSetPhase:
    def test_set_phase(self):
        buf = io.StringIO()
        state = _make_state()
        dash = Dashboard(state, file=buf)
        dash.set_phase("executing")
        assert dash._phase == "executing"
        dash.set_phase("done")
        assert dash._phase == "done"


class TestDashboardThreadSafety:
    def test_concurrent_updates(self):
        buf = io.StringIO()
        state = _make_state()
        dash = Dashboard(state, file=buf)
        errors = []

        def updater():
            try:
                for _ in range(50):
                    dash.update(state)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=updater) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors

    def test_concurrent_set_phase(self):
        buf = io.StringIO()
        state = _make_state()
        dash = Dashboard(state, file=buf)
        errors = []

        def setter(phase):
            try:
                for _ in range(50):
                    dash.set_phase(phase)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=setter, args=(f"phase_{i}",)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors


class TestStatusIcons:
    def test_all_statuses_have_icons(self):
        for status in ("pending", "executing", "completed", "failed"):
            assert status in STATUS_ICONS
            icon, style = STATUS_ICONS[status]
            assert len(icon) > 0
            assert len(style) > 0


class TestDashboardEmptyState:
    def test_empty_steps(self):
        buf = io.StringIO()
        state = {"goal": "test", "steps": [], "status": "planning", "total_elapsed": 0.0}
        dash = Dashboard(state, file=buf)
        dash.update(state)
        dash.finish(state)

    def test_render_dag_empty(self):
        buf = io.StringIO()
        state = {"goal": "test", "steps": [], "status": "planning", "total_elapsed": 0.0}
        dash = Dashboard(state, file=buf)
        dash._use_rich = True
        panel = dash._render_dag()
        assert panel is not None


class TestDashboardScroll:
    """Test scrolling and panel focus."""

    def test_initial_scroll_state(self):
        buf = io.StringIO()
        state = _make_state()
        dash = Dashboard(state, file=buf)
        assert dash._focused_panel == "dag"
        assert dash._scroll_offsets == {"dag": 0, "log": 0, "output": 0}
        assert all(dash._auto_scroll.values())

    def test_scroll_changes_offset(self):
        buf = io.StringIO()
        dash = Dashboard(_make_state(), file=buf)
        dash._scroll(5)
        assert dash._scroll_offsets["dag"] == 5
        assert not dash._auto_scroll["dag"]

    def test_scroll_clamps_to_zero(self):
        buf = io.StringIO()
        dash = Dashboard(_make_state(), file=buf)
        dash._scroll(-100)
        assert dash._scroll_offsets["dag"] == 0

    def test_scroll_to_top(self):
        buf = io.StringIO()
        dash = Dashboard(_make_state(), file=buf)
        dash._scroll(10)
        dash._scroll_to_top()
        assert dash._scroll_offsets["dag"] == 0
        assert not dash._auto_scroll["dag"]

    def test_scroll_to_bottom_enables_auto_scroll(self):
        buf = io.StringIO()
        dash = Dashboard(_make_state(), file=buf)
        dash._scroll(5)
        assert not dash._auto_scroll["dag"]
        dash._scroll_to_bottom()
        assert dash._auto_scroll["dag"]

    def test_cycle_focus_dag_log(self):
        buf = io.StringIO()
        dash = Dashboard(_make_state(), file=buf)
        assert dash._focused_panel == "dag"
        dash._cycle_focus()
        assert dash._focused_panel == "log"
        dash._cycle_focus()
        assert dash._focused_panel == "dag"

    def test_cycle_focus_includes_output(self):
        buf = io.StringIO()
        dash = Dashboard(_make_state(), file=buf)
        dash.add_output_line("test output")
        assert dash._focused_panel == "dag"
        dash._cycle_focus()
        assert dash._focused_panel == "log"
        dash._cycle_focus()
        assert dash._focused_panel == "output"
        dash._cycle_focus()
        assert dash._focused_panel == "dag"

    def test_scroll_respects_focused_panel(self):
        buf = io.StringIO()
        dash = Dashboard(_make_state(), file=buf)
        dash._focused_panel = "log"
        dash._scroll(5)
        assert dash._scroll_offsets["log"] == 5
        assert dash._scroll_offsets["dag"] == 0

    def test_apply_scroll_all_visible(self):
        buf = io.StringIO()
        dash = Dashboard(_make_state(), file=buf)
        lines = ["a", "b", "c"]
        visible, offset, total = dash._apply_scroll(lines, "dag", 10)
        assert visible == lines
        assert offset == 0
        assert total == 3

    def test_apply_scroll_auto_scroll(self):
        buf = io.StringIO()
        dash = Dashboard(_make_state(), file=buf)
        lines = list(range(20))
        visible, offset, total = dash._apply_scroll(lines, "dag", 5)
        assert len(visible) == 5
        assert visible == list(range(15, 20))
        assert offset == 15
        assert total == 20

    def test_apply_scroll_manual_offset(self):
        buf = io.StringIO()
        dash = Dashboard(_make_state(), file=buf)
        dash._auto_scroll["dag"] = False
        dash._scroll_offsets["dag"] = 3
        lines = list(range(20))
        visible, offset, total = dash._apply_scroll(lines, "dag", 5)
        assert visible == list(range(3, 8))
        assert offset == 3

    def test_apply_scroll_clamps_manual_offset(self):
        buf = io.StringIO()
        dash = Dashboard(_make_state(), file=buf)
        dash._auto_scroll["dag"] = False
        dash._scroll_offsets["dag"] = 999
        lines = list(range(10))
        visible, offset, total = dash._apply_scroll(lines, "dag", 5)
        # Max offset is 10-5=5
        assert offset == 5
        assert visible == list(range(5, 10))

    def test_large_log_buffer_preserved(self):
        buf = io.StringIO()
        dash = Dashboard(_make_state(), file=buf)
        for i in range(100):
            dash.log(f"msg {i}")
        with dash._lock:
            assert len(dash._log_lines) == 100

    def test_render_dag_with_scroll(self):
        from rich.panel import Panel

        buf = io.StringIO()
        dash = Dashboard(_make_state(), file=buf)
        dash._use_rich = True
        panel = dash._render_dag()
        assert isinstance(panel, Panel)

    def test_render_log_with_many_lines(self):
        from rich.panel import Panel

        buf = io.StringIO()
        dash = Dashboard(_make_state(), file=buf)
        dash._use_rich = True
        for i in range(50):
            dash.log(f"message {i}")
        panel = dash._render_log()
        assert isinstance(panel, Panel)

    def test_render_output_with_many_lines(self):
        from rich.panel import Panel

        buf = io.StringIO()
        dash = Dashboard(_make_state(), file=buf)
        dash._use_rich = True
        for i in range(50):
            dash.add_output_line(f"output {i}")
        panel = dash._render_output()
        assert isinstance(panel, Panel)

    def test_focused_panel_bold_border(self):
        from rich.console import Console

        buf = io.StringIO()
        console = Console(file=buf, force_terminal=True, width=80)
        dash = Dashboard(_make_state(), file=buf)
        dash._use_rich = True
        dash._console = console

        dash._focused_panel = "dag"
        dag_panel = dash._render_dag()
        assert dag_panel.border_style == "bold green"

        dash._focused_panel = "log"
        dag_panel = dash._render_dag()
        assert dag_panel.border_style == "green"

    def test_concurrent_scroll(self):
        buf = io.StringIO()
        dash = Dashboard(_make_state(), file=buf)
        errors = []

        def scroller():
            try:
                for _ in range(50):
                    dash._scroll(1)
                    dash._scroll(-1)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=scroller) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors

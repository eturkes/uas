"""Rich terminal dashboard for real-time UAS execution visualization."""

import collections
import sys
import threading
import time

from .planner import topological_sort

_RICH_AVAILABLE = False
try:
    from rich.console import Console
    from rich.layout import Layout
    from rich.live import Live
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text
    from rich.tree import Tree

    _RICH_AVAILABLE = True
except ImportError:
    pass

STATUS_ICONS = {
    "pending": ("[ ]", "dim"),
    "executing": ("[>]", "bold cyan"),
    "completed": ("[+]", "green"),
    "failed": ("[x]", "red"),
}

MAX_LOG_LINES = 12
MAX_OUTPUT_LINES = 20


class Dashboard:
    """Rich Live terminal dashboard showing DAG structure, step statuses, and timing.

    Falls back to plain print-based reporting when stdout is not a TTY or
    rich is not installed.
    """

    def __init__(self, state: dict, file=None):
        self._state = state
        self._phase = "initializing"
        self._active_steps: list[int] = []
        self._step_activities: dict[int, str] = {}
        self._log_lines: collections.deque[str] = collections.deque(maxlen=MAX_LOG_LINES)
        self._output_lines: collections.deque[str] = collections.deque(maxlen=MAX_OUTPUT_LINES)
        self._start_time = time.monotonic()
        self._lock = threading.Lock()
        self._file = file if file is not None else sys.stderr
        self._use_rich = _RICH_AVAILABLE and hasattr(self._file, "isatty") and self._file.isatty()
        self._live = None
        self._console = None

        if self._use_rich:
            self._console = Console(file=self._file)
            self._live = Live(
                self._render(),
                console=self._console,
                refresh_per_second=4,
                transient=True,
            )

    @property
    def use_rich(self) -> bool:
        return self._use_rich

    def start(self):
        if self._live:
            self._live.start()

    def stop(self):
        if self._live:
            self._live.stop()

    def set_phase(self, phase: str):
        with self._lock:
            self._phase = phase

    def set_step_activity(self, step_id: int, activity: str):
        """Set the current sub-activity for a step (e.g. 'Generating code', 'Running sandbox')."""
        with self._lock:
            self._step_activities[step_id] = activity
        if self._live:
            try:
                self._live.update(self._render())
            except Exception:
                pass
        else:
            print(f"  Step {step_id}: {activity}", file=self._file)

    def log(self, message: str):
        """Append a message to the activity log shown in the dashboard."""
        with self._lock:
            self._log_lines.append(message)
        if self._live:
            try:
                self._live.update(self._render())
            except Exception:
                pass
        else:
            print(f"  {message}", file=self._file)

    def add_output_line(self, line: str):
        """Append a line of live orchestrator/LLM output to the output panel."""
        with self._lock:
            self._output_lines.append(line)
        if self._live:
            try:
                self._live.update(self._render())
            except Exception:
                pass

    def update(self, state: dict):
        with self._lock:
            self._state = state
            self._active_steps = [
                s["id"] for s in state.get("steps", []) if s["status"] == "executing"
            ]
        if self._live:
            try:
                self._live.update(self._render())
            except Exception:
                pass
        else:
            self._fallback_update(state)

    def _fallback_update(self, state: dict):
        """Plain-text fallback when rich is unavailable or not a TTY."""
        steps = state.get("steps", [])
        total = len(steps)
        completed = sum(1 for st in steps if st["status"] == "completed")
        failed = sum(1 for st in steps if st["status"] == "failed")
        for s in steps:
            if s["status"] == "executing":
                activity = self._step_activities.get(s["id"], "")
                activity_str = f" - {activity}" if activity else ""
                print(
                    f"[{s['id']}/{total}] Step {s['id']}: \"{s['title']}\" "
                    f"({completed} completed, {failed} failed){activity_str}",
                    file=self._file,
                )
            elif s["status"] == "completed" and s.get("summary"):
                print(
                    f"  Step {s['id']} completed: {s['summary'][:120]}",
                    file=self._file,
                )

    def print_plan(self, state: dict):
        if self._use_rich and self._console:
            self._rich_print_plan(state)
        else:
            self._plain_print_plan(state)

    def _rich_print_plan(self, state: dict):
        steps = state["steps"]
        levels = topological_sort(steps)
        step_by_id = {s["id"]: s for s in steps}

        tree = Tree(f"[bold]Plan: {len(steps)} steps, {len(levels)} levels[/bold]")
        for level_idx, level in enumerate(levels, 1):
            branch = tree.add(f"[bold]Level {level_idx}[/bold] (parallel)")
            for sid in level:
                step = step_by_id[sid]
                deps = step["depends_on"]
                deps_str = f" [dim](depends on: {deps})[/dim]" if deps else ""
                branch.add(f"Step {sid}: {step['title']}{deps_str}")

        panel = Panel(tree, title=f"[bold]Goal:[/bold] {state['goal'][:80]}", border_style="blue")
        self._console.print(panel)

    def _plain_print_plan(self, state: dict):
        steps = state["steps"]
        levels = topological_sort(steps)
        step_by_id = {s["id"]: s for s in steps}

        print(f"Goal: {state['goal']}\n", file=self._file)
        print(f"Steps: {len(steps)}", file=self._file)
        print(f"Execution levels: {len(levels)}\n", file=self._file)

        for level_idx, level in enumerate(levels, 1):
            print(f"--- Level {level_idx} (parallel) ---", file=self._file)
            for sid in level:
                step = step_by_id[sid]
                deps = step["depends_on"]
                deps_str = f" [depends on: {deps}]" if deps else ""
                print(f"  Step {sid}: {step['title']}{deps_str}", file=self._file)
                print(f"    {step['description']}", file=self._file)
            print(file=self._file)

    def report_progress(self, step: dict, total: int, completed: int,
                        failed: int, attempt: int = 1):
        msg = (f"Step {step['id']}/{total}: \"{step['title']}\" "
               f"(attempt {attempt}, {completed} done, {failed} failed)")
        with self._lock:
            self._log_lines.append(msg)
        if self._use_rich:
            if self._live:
                try:
                    self._live.update(self._render())
                except Exception:
                    pass
        else:
            print(f"[{step['id']}/{total}] {msg}", file=self._file)

    def finish(self, state: dict):
        self.stop()
        if self._use_rich and self._console:
            self._rich_finish(state)
        else:
            self._plain_finish(state)

    def _rich_finish(self, state: dict):
        table = Table(title="Execution Summary", show_lines=False)
        table.add_column("Step", justify="right", style="bold", width=5)
        table.add_column("Title", width=40)
        table.add_column("Status", width=12)
        table.add_column("Elapsed", justify="right", width=9)
        table.add_column("LLM", justify="right", width=9)
        table.add_column("Sandbox", justify="right", width=9)

        for s in state.get("steps", []):
            elapsed = s.get("elapsed", 0.0)
            timing = s.get("timing", {})
            llm_t = timing.get("llm_time", 0.0)
            sandbox_t = timing.get("sandbox_time", 0.0)
            title = s["title"][:40]

            status_style = STATUS_ICONS.get(s["status"], ("?", ""))[1]
            table.add_row(
                str(s["id"]),
                title,
                f"[{status_style}]{s['status']}[/{status_style}]",
                f"{elapsed:.1f}s",
                f"{llm_t:.1f}s",
                f"{sandbox_t:.1f}s",
            )

        total_elapsed = state.get("total_elapsed", 0.0)
        table.add_section()
        table.add_row("", "TOTAL", "", f"{total_elapsed:.1f}s", "", "")

        self._console.print(table)

    def _plain_finish(self, state: dict):
        steps = state.get("steps", [])
        print(file=self._file)
        print(
            f"{'Step':>4}  {'Title':<40}  {'Status':<12}  {'Elapsed':>8}  {'LLM':>8}  {'Sandbox':>8}",
            file=self._file,
        )
        print(
            f"{'─' * 4}  {'─' * 40}  {'─' * 12}  {'─' * 8}  {'─' * 8}  {'─' * 8}",
            file=self._file,
        )
        for s in steps:
            elapsed = s.get("elapsed", 0.0)
            timing = s.get("timing", {})
            llm_t = timing.get("llm_time", 0.0)
            sandbox_t = timing.get("sandbox_time", 0.0)
            title = s["title"][:40]
            print(
                f"{s['id']:>4}  {title:<40}  {s['status']:<12}  "
                f"{elapsed:>7.1f}s  {llm_t:>7.1f}s  {sandbox_t:>7.1f}s",
                file=self._file,
            )
        total_elapsed = state.get("total_elapsed", 0.0)
        print(
            f"{'─' * 4}  {'─' * 40}  {'─' * 12}  {'─' * 8}  {'─' * 8}  {'─' * 8}",
            file=self._file,
        )
        print(
            f"{'':>4}  {'TOTAL':<40}  {'':12}  {total_elapsed:>7.1f}s",
            file=self._file,
        )

    def _render(self):
        """Build the rich renderable for the Live display."""
        layout = Layout()

        with self._lock:
            has_output = len(self._output_lines) > 0

        if has_output:
            layout.split_column(
                Layout(name="header", size=3),
                Layout(name="middle"),
                Layout(name="log", size=min(MAX_LOG_LINES + 2, 8)),
                Layout(name="footer", size=8),
            )
            layout["middle"].split_row(
                Layout(name="body", ratio=1),
                Layout(name="output", ratio=1),
            )
            layout["output"].update(self._render_output())
        else:
            layout.split_column(
                Layout(name="header", size=3),
                Layout(name="middle"),
                Layout(name="log", size=min(MAX_LOG_LINES + 2, 8)),
                Layout(name="footer", size=8),
            )
            layout["middle"].update(self._render_dag())

        layout["header"].update(self._render_header())
        if has_output:
            layout["middle"]["body"].update(self._render_dag())
        layout["log"].update(self._render_log())
        layout["footer"].update(self._render_timing())

        return layout

    def _render_header(self) -> Panel:
        with self._lock:
            goal = self._state.get("goal", "")[:80]
            phase = self._phase
            steps = self._state.get("steps", [])
        total = len(steps)
        completed = sum(1 for s in steps if s["status"] == "completed")
        failed = sum(1 for s in steps if s["status"] == "failed")
        executing = sum(1 for s in steps if s["status"] == "executing")
        elapsed = time.monotonic() - self._start_time

        progress = f"{completed}/{total} done"
        if executing:
            progress += f", {executing} running"
        if failed:
            progress += f", {failed} failed"

        text = Text.assemble(
            ("Goal: ", "bold"),
            (goal, ""),
            ("  |  ", "dim"),
            ("Phase: ", "bold"),
            (phase, "cyan"),
            ("  |  ", "dim"),
            ("Progress: ", "bold"),
            (progress, "green" if not failed else "yellow"),
            ("  |  ", "dim"),
            (f"Elapsed: {elapsed:.0f}s", ""),
        )
        return Panel(text, style="blue")

    def _render_dag(self) -> Panel:
        with self._lock:
            state = self._state
            step_activities = dict(self._step_activities)
        steps = state.get("steps", [])
        if not steps:
            return Panel("[dim]No steps yet[/dim]", title="DAG")

        step_by_id = {s["id"]: s for s in steps}
        try:
            levels = topological_sort(steps)
        except ValueError:
            return Panel("[red]Invalid DAG[/red]", title="DAG")

        tree = Tree("[bold]Execution DAG[/bold]")
        for level_idx, level in enumerate(levels, 1):
            level_branch = tree.add(f"[bold]Level {level_idx}[/bold]")
            for sid in level:
                step = step_by_id[sid]
                icon, style = STATUS_ICONS.get(step["status"], ("?", ""))
                elapsed = step.get("elapsed", 0.0)
                elapsed_str = f" ({elapsed:.1f}s)" if elapsed > 0 else ""

                label = f"[{style}]{icon}[/{style}] Step {sid}: \"{step['title']}\"{elapsed_str}"

                step_node = level_branch.add(label)

                if step["status"] == "executing":
                    rewrites = step.get("rewrites", 0)
                    attempt = rewrites + 1
                    activity = step_activities.get(sid, "")
                    activity_str = f" - {activity}" if activity else ""
                    step_node.add(f"[cyan]Attempt {attempt}{activity_str}[/cyan]")
                    if step.get("error"):
                        err_preview = step["error"][:120]
                        step_node.add(f"[dim red]Last error: {err_preview}[/dim red]")

                elif step["status"] == "completed":
                    summary = step.get("summary") or (step.get("output") or "")[:100]
                    if summary:
                        step_node.add(f"[dim green]{summary[:120]}[/dim green]")
                    files = step.get("files_written", [])
                    if files:
                        step_node.add(f"[dim]Files: {', '.join(files[:5])}"
                                      f"{'...' if len(files) > 5 else ''}[/dim]")

                elif step["status"] == "failed":
                    if step.get("error"):
                        err_preview = step["error"][:120]
                        step_node.add(f"[dim red]{err_preview}[/dim red]")

        return Panel(tree, title="DAG", border_style="green")

    def _render_log(self) -> Panel:
        """Build the activity log panel."""
        with self._lock:
            lines = list(self._log_lines)
        if not lines:
            return Panel("[dim]Waiting for activity...[/dim]", title="Activity Log",
                         border_style="dim")
        text = Text()
        for i, line in enumerate(lines):
            if i > 0:
                text.append("\n")
            text.append(line, style="dim" if i < len(lines) - 1 else "")
        return Panel(text, title="Activity Log", border_style="dim")

    def _render_output(self) -> Panel:
        """Build the live LLM/orchestrator output panel."""
        with self._lock:
            lines = list(self._output_lines)
        if not lines:
            return Panel("[dim]Waiting for output...[/dim]", title="Claude Code Output",
                         border_style="magenta")
        text = Text()
        for i, line in enumerate(lines):
            if i > 0:
                text.append("\n")
            # Dim older lines, highlight the latest
            style = "dim" if i < len(lines) - 1 else ""
            text.append(line[:200], style=style)
        return Panel(text, title="Claude Code Output", border_style="magenta")

    def _render_timing(self) -> Table:
        with self._lock:
            state = self._state
        steps = state.get("steps", [])

        table = Table(title="Timing", expand=True, show_lines=False)
        table.add_column("Step", justify="right", width=5)
        table.add_column("Title", width=30)
        table.add_column("Status", width=10)
        table.add_column("LLM", justify="right", width=8)
        table.add_column("Sandbox", justify="right", width=8)
        table.add_column("Total", justify="right", width=8)

        for s in steps:
            if s["status"] not in ("completed", "failed"):
                continue
            timing = s.get("timing", {})
            status_style = STATUS_ICONS.get(s["status"], ("?", ""))[1]
            table.add_row(
                str(s["id"]),
                s["title"][:30],
                f"[{status_style}]{s['status']}[/{status_style}]",
                f"{timing.get('llm_time', 0.0):.1f}s",
                f"{timing.get('sandbox_time', 0.0):.1f}s",
                f"{s.get('elapsed', 0.0):.1f}s",
            )

        return table

"""Rich terminal dashboard for real-time UAS execution visualization."""

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


class Dashboard:
    """Rich Live terminal dashboard showing DAG structure, step statuses, and timing.

    Falls back to plain print-based reporting when stdout is not a TTY or
    rich is not installed.
    """

    def __init__(self, state: dict, file=None):
        self._state = state
        self._phase = "initializing"
        self._active_steps: list[int] = []
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
        for s in state.get("steps", []):
            if s["status"] == "executing":
                total = len(state.get("steps", []))
                completed = sum(1 for st in state["steps"] if st["status"] == "completed")
                failed = sum(1 for st in state["steps"] if st["status"] == "failed")
                print(
                    f"[{s['id']}/{total}] Step {s['id']}: \"{s['title']}\" "
                    f"({completed} completed, {failed} failed)",
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
        if self._use_rich:
            return
        print(
            f"[{step['id']}/{total}] Step {step['id']}: \"{step['title']}\" "
            f"(attempt {attempt}, {completed} completed, {failed} failed)",
            file=self._file,
        )

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
        layout.split_column(
            Layout(name="header", size=3),
            Layout(name="body"),
            Layout(name="footer", size=8),
        )

        layout["header"].update(self._render_header())
        layout["body"].update(self._render_dag())
        layout["footer"].update(self._render_timing())

        return layout

    def _render_header(self) -> Panel:
        with self._lock:
            goal = self._state.get("goal", "")[:80]
            phase = self._phase
        elapsed = time.monotonic() - self._start_time
        text = Text.assemble(
            ("Goal: ", "bold"),
            (goal, ""),
            ("  |  ", "dim"),
            ("Phase: ", "bold"),
            (phase, "cyan"),
            ("  |  ", "dim"),
            (f"Elapsed: {elapsed:.0f}s", ""),
        )
        return Panel(text, style="blue")

    def _render_dag(self) -> Panel:
        with self._lock:
            state = self._state
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

                # Show detail for executing steps
                if step["status"] == "executing":
                    rewrites = step.get("rewrites", 0)
                    attempt = rewrites + 1
                    step_node.add(f"[cyan]Attempt {attempt}[/cyan]")
                    if step.get("error"):
                        err_preview = step["error"][:100]
                        step_node.add(f"[dim red]Last error: {err_preview}[/dim red]")

        return Panel(tree, title="DAG", border_style="green")

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

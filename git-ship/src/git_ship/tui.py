"""The live step list, used on an interactive terminal unless `--plain` is given. Every step ahead stays
pinned at the bottom of the terminal and shows which one runs, what it does right now and how long it took.
Logs scroll above it."""

import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager

from rich.console import Console, RenderableType
from rich.live import Live
from rich.spinner import Spinner
from rich.table import Table
from rich.text import Text

from .ui import PlannedStep, Progress, Step

ICONS = {
    "pending": Text("·", style="dim"),
    "done": Text("✓", style="green"),
    "skipped": Text("-", style="dim"),
    "failed": Text("✗", style="bold red"),
}


class TerminalUi:
    def __init__(self, console: Console | None = None):
        self.console = console or Console()
        self.progress = Progress()
        self._spinners: dict[str, Spinner] = {}
        # The refresh thread draws while the pipeline changes the steps.
        self._lock = threading.Lock()
        # Transient, so it can be taken down for a question without leaving a copy behind.
        self._live = Live(
            get_renderable=self._render, console=self.console, refresh_per_second=8, transient=True
        )

    @contextmanager
    def session(self) -> Iterator[None]:
        self._live.start()
        try:
            yield
        except BaseException:
            with self._lock:
                self.progress.fail_running()
            raise
        finally:
            self._live.stop()
            self.console.print(self._render())

    def plan(self, steps: Sequence[PlannedStep]) -> None:
        with self._lock:
            self.progress.plan(steps)

    def start(self, key: str, title: str) -> None:
        with self._lock:
            self.progress.start(key, title)
            self._spinners.setdefault(key, Spinner("dots", style="cyan"))

    def done(self, key: str, note: str = "") -> None:
        with self._lock:
            self.progress.done(key, note)

    def skip(self, key: str, note: str = "") -> None:
        with self._lock:
            self.progress.skip(key, note)

    def status(self, text: str) -> None:
        with self._lock:
            self.progress.status(text)

    def log(self, text: str) -> None:
        # Not markup: findings like "[auto-fix] ..." would be read as style tags.
        self.console.print(Text(text))

    def ask(self, question: str) -> str:
        self._live.stop()
        try:
            return self.console.input(Text(question, style="bold"))
        finally:
            self._live.start()

    def _render(self) -> RenderableType:
        with self._lock:
            now = self.progress.now()
            table = Table.grid(padding=(0, 1))
            table.add_column(width=1)
            table.add_column(no_wrap=True)
            table.add_column(justify="right", style="dim", no_wrap=True)
            table.add_column(overflow="ellipsis", no_wrap=True)
            for step in self.progress.steps:
                table.add_row(
                    self._icon(step),
                    Text(step.title, style=_title_style(step)),
                    _duration(step.elapsed(now)),
                    Text(step.note, style="red" if step.state == "failed" else "dim"),
                )
            return table

    def _icon(self, step: Step) -> RenderableType:
        if step.state == "running":
            return self._spinners.setdefault(step.key, Spinner("dots", style="cyan"))
        return ICONS[step.state]


def _title_style(step: Step) -> str:
    return {"running": "bold", "pending": "dim", "skipped": "dim", "failed": "bold red"}.get(step.state, "")


def _duration(seconds: float | None) -> str:
    if seconds is None:
        return ""
    minutes, seconds = divmod(int(seconds), 60)
    return f"{minutes}:{seconds:02d}"

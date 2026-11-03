"""The live step list, used on an interactive terminal unless `--plain` is given. Every step ahead stays
pinned at the bottom of the terminal and shows which one runs, what it does right now and how long it took.
What a step found, e.g. the review's findings, shows under it. When the pipeline starts over after a fix,
the finished attempt is kept in the scrollback and the same steps run again from the top."""

import re
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager

from rich.cells import cell_len
from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.spinner import Spinner
from rich.table import Table
from rich.text import Text

from .ui import PlannedStep, Progress, Step

LIVE_DETAIL_LINES = 6
"""Per step. The live list can't be taller than the terminal, the copy in the scrollback has them all."""
MORE = "... "

TAG = re.compile(r"\[([\w-]+)(?: #\d+)?\] ")
"""What a detail line is, e.g. `[auto-fix]`."""
TAG_STYLES = {"auto-fix": "bold cyan", "ask-user": "bold yellow", "conflict": "bold red", "failed": "bold red"}

ICONS = {
    "pending": Text("·", style="dim"),
    "done": Text("✓", style="green"),
    "fixed": Text("↻", style="yellow"),
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
        self._live = self._new_live()

    @contextmanager
    def session(self) -> Iterator[None]:
        self._live.start()
        try:
            yield
        except BaseException:
            with self._lock:
                self.progress.end_running("failed")
            raise
        finally:
            self._live.stop()
            self.console.print(self._render(all_details=True))

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

    def details(self, key: str, lines: Sequence[str]) -> None:
        with self._lock:
            self.progress.details(key, lines)

    def restart(self, attempt: int, attempts: int, reason: str) -> None:
        with self._lock:
            self.progress.end_running("fixed", f"{reason}, starting over")
        self.console.print(self._render(all_details=True))
        self.console.print()
        with self._lock:
            self.progress.restart(attempt, attempts, reason)

    def log(self, text: str) -> None:
        # Not markup: findings like "[auto-fix] ..." would be read as style tags.
        self.console.print(Text(text))

    def ask(self, question: str, details: Sequence[str] = ()) -> str:
        """Once answered, the question is erased again, so the step list is back in focus. The answer shows
        in the steps, e.g. as kept findings under the review."""
        self._live.stop()
        try:
            lines = 0
            if details:
                about = _details(list(details), all_details=True)
                lines += len(self.console.render_lines(about, pad=False))
                self.console.print(about)
            prompt = _question(question)
            answer = self.console.input(prompt)
            lines += _wrapped_lines(prompt.cell_len + cell_len(answer), self.console.width)
            self._erase(lines)
            return answer
        finally:
            # A new one: a restarted Live still knows the height of its old area and would draw over the question.
            self._live = self._new_live()
            self._live.start()

    def _erase(self, lines: int) -> None:
        """Erases the last `lines` lines printed. Those that scrolled out of the screen stay."""
        if not self.console.is_terminal or lines <= 0:
            return
        # To the start of the line `lines` up, then clear everything below.
        self.console.file.write(f"\x1b[{min(lines, self.console.height)}F\x1b[J")
        self.console.file.flush()

    def _new_live(self) -> Live:
        # Transient, so it can be taken down for a question without leaving a copy behind.
        return Live(get_renderable=self._render, console=self.console, refresh_per_second=8, transient=True)

    def _render(self, all_details: bool = False) -> RenderableType:
        """One row per step. A step's details go under it on their own lines, between table segments
        of the same column widths, so a long finding doesn't widen the columns."""
        with self._lock:
            now = self.progress.now()
            title_width = max((len(step.title) for step in self.progress.steps), default=0)
            parts: list[RenderableType] = []
            if self.progress.attempt:
                parts.append(Text(self.progress.attempt, style="bold yellow"))
            table = _table(title_width)
            for step in self.progress.steps:
                table.add_row(
                    self._icon(step),
                    Text(step.title, style=_title_style(step)),
                    _duration(step.elapsed(now)),
                    Text(step.note, style="red" if step.state == "failed" else "dim"),
                )
                if step.details:
                    parts += [table, _details(step.details, all_details)]
                    table = _table(title_width)
            if table.row_count:
                parts.append(table)
            return Group(*parts)

    def _icon(self, step: Step) -> RenderableType:
        if step.state == "running":
            return self._spinners.setdefault(step.key, Spinner("dots", style="cyan"))
        return ICONS[step.state]


def _table(title_width: int) -> Table:
    """The fixed columns keep their width, the note gets what is left and is cut to fit."""
    table = Table.grid(padding=(0, 1), expand=True)
    table.add_column(width=1, min_width=1)
    table.add_column(width=title_width, min_width=min(title_width, 40), no_wrap=True, overflow="ellipsis")
    table.add_column(width=5, min_width=5, justify="right", style="dim", no_wrap=True)
    table.add_column(ratio=1, overflow="ellipsis", no_wrap=True)
    return table


def _details(lines: list[str], all_details: bool) -> RenderableType:
    """Indented under the step. Live they are cut to one line each, in the scrollback they wrap."""
    return Group(*(_detail(line, all_details) for line in _shown(lines, all_details)))


def _detail(line: str, all_details: bool) -> Table:
    """A line starting with a tag like `[auto-fix]` gets the tag colored and a hanging indent, so where it
    wraps, the text continues under the text and the next tag shows where the next item starts."""
    table = Table.grid(padding=(0, 1), expand=True)
    table.add_column(width=3, min_width=3)
    overflow = "fold" if all_details else "ellipsis"
    tag = TAG.match(line)
    if tag:
        table.add_column(no_wrap=True)
        table.add_column(ratio=1, overflow=overflow, no_wrap=not all_details)
        style = TAG_STYLES.get(tag.group(1), "bold")
        table.add_row("", Text(tag.group(0).rstrip(), style=style), Text(line[tag.end():]))
    else:
        table.add_column(ratio=1, overflow=overflow, no_wrap=not all_details)
        table.add_row("", Text(line, style="dim" if line.startswith(MORE) else ""))
    return table


def _wrapped_lines(cells: int, width: int) -> int:
    """How many lines a line of `cells` takes in a terminal `width` wide, with the Enter after it."""
    return max(1, -(-cells // width))


def _question(question: str) -> Text:
    """Plain, with the answer keys like `[f]` highlighted."""
    text = Text(question)
    text.highlight_regex(r"\[\w\]", "bold cyan")
    return text


def _shown(details: list[str], all_details: bool) -> list[str]:
    if all_details or len(details) <= LIVE_DETAIL_LINES:
        return details
    return [*details[: LIVE_DETAIL_LINES - 1], f"{MORE}{len(details) - LIVE_DETAIL_LINES + 1} more"]


def _title_style(step: Step) -> str:
    return {"running": "bold", "pending": "dim", "skipped": "dim", "failed": "bold red"}.get(step.state, "")


def _duration(seconds: float | None) -> str:
    if seconds is None:
        return ""
    minutes, seconds = divmod(int(seconds), 60)
    return f"{minutes}:{seconds:02d}"

"""How git-ship shows its progress: a live step list on a terminal (tui.py), otherwise a plain log."""

import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field
from typing import Literal, Protocol

StepState = Literal["pending", "running", "done", "fixed", "skipped", "failed"]
"""fixed: the step fixed something, so the pipeline started over."""


@dataclass(frozen=True)
class PlannedStep:
    key: str
    title: str


class Ui(Protocol):
    def session(self) -> AbstractContextManager[None]:
        """While the pipeline runs. A step still running when it raises is marked failed."""
        ...

    def plan(self, steps: Sequence[PlannedStep]) -> None:
        """The steps ahead, in order. Called again when more is known, e.g. the checks after detecting them."""

    def start(self, key: str, title: str) -> None: ...

    def done(self, key: str, note: str = "") -> None: ...

    def skip(self, key: str, note: str = "") -> None: ...

    def status(self, text: str) -> None:
        """What the running step does right now. The TUI replaces it with the next status."""

    def details(self, key: str, lines: Sequence[str]) -> None:
        """What a step found, e.g. the review's findings. The TUI shows them under the step."""

    def log(self, text: str) -> None:
        """Output to keep that belongs to no step, e.g. the output of a failing check."""

    def restart(self, attempt: int, attempts: int, reason: str) -> None:
        """The pipeline starts over from the top after a fix."""

    def ask(self, question: str) -> str: ...


class PlainUi:
    """A log of the steps as they happen. This is what the /ship skill reads."""

    @contextmanager
    def session(self) -> Iterator[None]:
        yield

    def plan(self, steps: Sequence[PlannedStep]) -> None:
        pass

    def start(self, key: str, title: str) -> None:
        print(f"\n\033[1m==> {title}\033[0m", flush=True)

    def done(self, key: str, note: str = "") -> None:
        pass

    def skip(self, key: str, note: str = "") -> None:
        pass

    def status(self, text: str) -> None:
        print(text, flush=True)

    def details(self, key: str, lines: Sequence[str]) -> None:
        for line in lines:
            print(line, flush=True)

    def log(self, text: str) -> None:
        print(text, flush=True)

    def restart(self, attempt: int, attempts: int, reason: str) -> None:
        print(f"\n\033[1m=== {reason} - starting over (attempt {attempt}/{attempts})\033[0m", flush=True)

    def ask(self, question: str) -> str:
        return input(question)


@dataclass
class Step:
    key: str
    title: str
    state: StepState = "pending"
    note: str = ""
    details: list[str] = field(default_factory=list)
    started: float | None = None
    finished: float | None = None

    def elapsed(self, now: float) -> float | None:
        if self.started is None:
            return None
        return (self.finished or now) - self.started


class Progress:
    """The steps of a run and their state, what the TUI draws."""

    def __init__(self, now: Callable[[], float] = time.monotonic):
        self.steps: list[Step] = []
        self.attempt = ""
        """Which attempt runs and why, empty on the first."""
        self._now = now

    def plan(self, planned: Sequence[PlannedStep]) -> None:
        known = {step.key: step for step in self.steps}
        steps = []
        for item in planned:
            step = known.pop(item.key, None) or Step(item.key, item.title)
            if step.state == "pending":
                step.title = item.title
            steps.append(step)
        # Steps that already ran stay, even if the new plan doesn't have them.
        steps += [step for step in known.values() if step.state != "pending"]
        self.steps = steps

    def start(self, key: str, title: str) -> None:
        step = self._get(key, title)
        step.title = title
        step.state = "running"
        step.note = ""
        step.details = []
        step.started = self._now()
        step.finished = None

    def done(self, key: str, note: str = "") -> None:
        self._finish(key, "done", note)

    def skip(self, key: str, note: str = "") -> None:
        self._finish(key, "skipped", note)

    def details(self, key: str, lines: Sequence[str]) -> None:
        self._get(key, key).details = list(lines)

    def end_running(self, state: StepState, note: str | None = None) -> None:
        for step in self.steps:
            if step.state == "running":
                step.state = state
                step.finished = self._now()
                if note is not None:
                    step.note = note

    def restart(self, attempt: int, attempts: int, reason: str) -> None:
        """Every step is ahead again, in the same order."""
        self.attempt = f"attempt {attempt}/{attempts} - {reason}"
        for step in self.steps:
            step.state = "pending"
            step.note = ""
            step.details = []
            step.started = None
            step.finished = None

    def status(self, text: str) -> None:
        running = self.running()
        if running:
            running.note = text

    def running(self) -> Step | None:
        """The step started last of those still running."""
        running = [step for step in self.steps if step.state == "running"]
        return max(running, key=lambda step: step.started or 0, default=None)

    def now(self) -> float:
        return self._now()

    def _finish(self, key: str, state: StepState, note: str) -> None:
        step = self._get(key, key)
        if step.state == "running":
            step.finished = self._now()
        step.state = state
        step.note = note

    def _get(self, key: str, title: str) -> Step:
        for step in self.steps:
            if step.key == key:
                return step
        step = Step(key, title)
        self.steps.append(step)
        return step

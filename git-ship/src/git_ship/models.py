from dataclasses import dataclass
from pathlib import Path
from typing import Literal

Risk = Literal["low", "medium", "high"]
CheckState = Literal["pending", "passed", "failed"]


@dataclass(frozen=True)
class Finding:
    file: str
    line: int | None
    summary: str
    """The effect in a few plain words, readable without looking at the code."""
    needs_user: bool
    detail: str = ""
    """The evidence and the fix, for the fixer, the PR and a closer look."""
    question: str | None = None
    """The decision to make, for findings that need the user."""

    def __str__(self) -> str:
        """In full, e.g. for the fixer."""
        return f"{self.file}:{self.line or '-'} - {self.summary} {self.detail}".rstrip()

    def tagged(self) -> str:
        """Short, with what to do about it in front, e.g. `[auto-fix] m.py:2 - off by one`.
        The decision follows the summary, e.g. `[ask-user] m.py:5 - may raise → Catch it or let it crash?`"""
        tag = "ask-user" if self.needs_user else "auto-fix"
        text = f"{self.summary} → {self.question}" if self.needs_user and self.question else self.summary
        return f"[{tag}] {Path(self.file).name}:{self.line or '-'} - {text}"

    def explained(self) -> str:
        """The short form, then where exactly and the detail, for a closer look before deciding."""
        return "\n".join(part for part in (self.tagged(), f"{self.file}:{self.line or '-'}", self.detail) if part)


@dataclass(frozen=True)
class Review:
    findings: list[Finding]
    risk: Risk
    risk_rationale: str


@dataclass(frozen=True)
class Assessment:
    """The outcome of the review step, as the PR reviewer should see it."""

    risk: Risk
    risk_rationale: str
    kept: list[Finding]


@dataclass(frozen=True)
class PullRequest:
    title: str
    body: str


@dataclass(frozen=True)
class Check:
    name: str
    state: CheckState
    ref: str
    """Host-specific handle to fetch the failure log with."""


@dataclass(frozen=True)
class Detection:
    checks: list[str]
    sources: list[str]
    """Files the checks were derived from."""
    tests_in_ci: bool = False
    """The tests were left out of the checks because the PR's CI runs them."""


@dataclass(frozen=True)
class Ticket:
    id: str
    title: str
    description: str

    def __str__(self) -> str:
        return f"#{self.id} {self.title}\n\n{self.description}"


@dataclass(frozen=True)
class Change:
    """What is being shipped, as the agents need to know it."""

    diff_range: str
    ticket: Ticket | None

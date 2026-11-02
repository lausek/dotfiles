from dataclasses import dataclass
from typing import Literal

Risk = Literal["low", "medium", "high"]
CheckState = Literal["pending", "passed", "failed"]


@dataclass(frozen=True)
class Finding:
    file: str
    line: int | None
    summary: str
    needs_user: bool

    def __str__(self) -> str:
        return f"{self.file}:{self.line or '-'} - {self.summary}"


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

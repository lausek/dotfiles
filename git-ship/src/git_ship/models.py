from dataclasses import dataclass


@dataclass(frozen=True)
class Finding:
    file: str
    line: int | None
    summary: str
    needs_user: bool

    def __str__(self) -> str:
        return f"{self.file}:{self.line or '-'} - {self.summary}"


@dataclass(frozen=True)
class PullRequest:
    title: str
    body: str


@dataclass(frozen=True)
class Change:
    """What is being shipped, as the agents need to know it."""

    diff_range: str
    ticket: str | None

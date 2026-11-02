from dataclasses import dataclass

from .git import Git
from .shell import ShipError


@dataclass(frozen=True)
class Config:
    base: str
    rounds: int
    checks: tuple[str, ...]


def load_config(repo: Git) -> Config:
    """Reads the per-repo `ship.*` git config."""
    return Config(
        base=repo.config("ship.base") or _default_base(repo),
        rounds=_parse_rounds(repo.config("ship.rounds")),
        checks=tuple(repo.config_all("ship.check")),
    )


def _default_base(repo: Git) -> str:
    head = repo.try_("symbolic-ref", "--short", "refs/remotes/origin/HEAD")
    if head is None:
        raise ShipError("can't detect the base branch, set it: git config ship.base <branch>")
    return head.removeprefix("origin/")


def _parse_rounds(value: str | None) -> int:
    if value is None:
        return 3
    if not value.isdigit() or int(value) < 1:
        raise ShipError(f"ship.rounds must be a positive number, got '{value}'")
    return int(value)

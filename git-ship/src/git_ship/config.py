"""Settings live in `.git/ship.json`: per clone, never committed. Example:

    {
      "checks": ["npm ci", "npm run lint", "npm test"],   detected by claude, see checks.py
      "sources": {"package.json": "<sha256>"},            re-detects when these change
      "detection": 2,                                     re-detects when the detection rules change
      "base": "main",                                     optional, default: origin/HEAD
      "rounds": 3,                                        optional, fix attempts per step
      "ci_timeout_minutes": 60                            optional, 0 disables watching CI
    }
"""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .git import Git
from .shell import ShipError


@dataclass(frozen=True)
class Config:
    base: str
    rounds: int
    ci_timeout_minutes: int


def config_path(repo: Git) -> Path:
    # The common dir is shared by all worktrees of the clone.
    return Path(repo("rev-parse", "--path-format=absolute", "--git-common-dir")) / "ship.json"


def read_config_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as error:
        raise ShipError(f"{path} is not valid JSON: {error}") from error
    if not isinstance(data, dict):
        raise ShipError(f"{path} must contain a JSON object")
    return data


def write_config_file(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data, indent=2) + "\n")


def load_config(repo: Git, data: dict[str, Any]) -> Config:
    return Config(
        base=data.get("base") or _default_base(repo),
        rounds=_number(data, "rounds", default=3, minimum=1),
        ci_timeout_minutes=_number(data, "ci_timeout_minutes", default=60, minimum=0),
    )


def _default_base(repo: Git) -> str:
    head = repo.try_("symbolic-ref", "--short", "refs/remotes/origin/HEAD")
    if head is None:
        raise ShipError('can\'t detect the base branch, set "base" in .git/ship.json')
    return head.removeprefix("origin/")


def _number(data: dict[str, Any], key: str, default: int, minimum: int) -> int:
    value = data.get(key, default)
    if not isinstance(value, int) or value < minimum:
        raise ShipError(f'"{key}" in .git/ship.json must be a number >= {minimum}, got {value!r}')
    return value

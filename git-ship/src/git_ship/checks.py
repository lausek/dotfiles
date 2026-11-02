import hashlib
from collections.abc import Callable, Iterable
from pathlib import Path

from .agent import Agent
from .config import read_config_file, write_config_file
from .pipeline import step
from .shell import ShipError

Approve = Callable[[str], bool]


def resolve_checks(config_file: Path, checkout: Path, agent: Agent, approve: Approve, force: bool) -> list[str]:
    """Returns the saved checks, or detects them again if the files they came from changed."""
    data = read_config_file(config_file)
    saved = data.get("checks")
    sources = data.get("sources") or {}
    if saved is not None and not (isinstance(saved, list) and all(isinstance(c, str) for c in saved)):
        raise ShipError(f'"checks" in {config_file} must be a list of commands')
    if saved is not None and not force and fingerprint(checkout, sources) == sources:
        return saved

    step("detect checks")
    detection = agent.detect_checks()
    for command in detection.checks:
        print(f"  {command}")
    if not detection.checks:
        print("  (none found)")

    if detection.checks != saved and not approve(f"save these checks to {config_file}?"):
        raise ShipError(f"aborted - you can write the checks into {config_file} yourself")

    write_config_file(config_file, data | {"checks": detection.checks, "sources": fingerprint(checkout, detection.sources)})
    return detection.checks


def fingerprint(checkout: Path, files: Iterable[str]) -> dict[str, str]:
    return {file: _hash(checkout / file) for file in sorted(files)}


def _hash(path: Path) -> str:
    if path.is_file():
        return hashlib.sha256(path.read_bytes()).hexdigest()
    if path.is_dir():
        digest = hashlib.sha256()
        for file in sorted(p for p in path.rglob("*") if p.is_file()):
            digest.update(str(file.relative_to(path)).encode())
            digest.update(file.read_bytes())
        return digest.hexdigest()
    return "missing"

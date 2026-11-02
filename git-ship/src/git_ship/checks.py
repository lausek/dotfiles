import hashlib
from collections.abc import Callable, Iterable
from pathlib import Path

from .agent import Agent
from .config import read_config_file, write_config_file
from .models import Detection
from .pipeline import step
from .shell import ShipError

Approve = Callable[[str], bool]

DETECTION_VERSION = 2
"""Bump when the detection rules change, so saved checks are detected again."""


def resolve_checks(config_file: Path, checkout: Path, agent: Agent, approve: Approve, force: bool) -> Detection:
    """Returns the saved checks, or detects them again if the files they came from changed."""
    data = read_config_file(config_file)
    saved = data.get("checks")
    sources = data.get("sources") or {}
    if saved is not None and not (isinstance(saved, list) and all(isinstance(c, str) for c in saved)):
        raise ShipError(f'"checks" in {config_file} must be a list of commands')
    current = data.get("detection") == DETECTION_VERSION and fingerprint(checkout, sources) == sources
    if saved is not None and not force and current:
        return Detection(checks=saved, sources=list(sources), tests_in_ci=data.get("tests_in_ci") is True)

    step("detect checks")
    detection = agent.detect_checks()
    for command in detection.checks:
        print(f"  {command}")
    if not detection.checks:
        print("  (none found)")

    if detection.checks != saved and not approve(f"save these checks to {config_file}?"):
        raise ShipError(f"aborted - you can write the checks into {config_file} yourself")

    sources = fingerprint(checkout, detection.sources)
    write_config_file(
        config_file,
        data
        | {
            "checks": detection.checks,
            "sources": sources,
            "tests_in_ci": detection.tests_in_ci,
            "detection": DETECTION_VERSION,
        },
    )
    if detection.tests_in_ci:
        print("  tests are left to the PR's CI")
    return detection


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

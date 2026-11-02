import subprocess
from pathlib import Path


class ShipError(Exception):
    pass


class NeedsDecision(ShipError):
    """Stopped on findings only a human can decide on. Exits with code 2."""


def run(*args: str, cwd: Path | None = None) -> str:
    result = subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        output = result.stderr.strip() or result.stdout.strip()
        raise ShipError(f"`{' '.join(args[:3])} ...` failed:\n{output}")
    return result.stdout.strip()


def try_run(*args: str, cwd: Path | None = None) -> str | None:
    result = subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=False)
    return result.stdout.strip() if result.returncode == 0 else None

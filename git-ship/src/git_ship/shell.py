import re
import subprocess
import time
from pathlib import Path


class ShipError(Exception):
    pass


class NeedsDecision(ShipError):
    """Stopped on findings only a human can decide on. Exits with code 2."""


# The SSH server dropped the connection before the handshake, so nothing was sent yet and a retry is safe.
# Azure DevOps does this a lot when several connections open in a short time.
_DROPPED_BEFORE_HANDSHAKE = re.compile(r"kex_exchange_identification|Connection reset by .* port 22")
RETRY_DELAYS = (2, 5, 10)


def run(*args: str, cwd: Path | None = None) -> str:
    result = _run(args, cwd)
    if result.returncode != 0:
        output = result.stderr.strip() or result.stdout.strip()
        raise ShipError(f"`{' '.join(args[:3])} ...` failed:\n{output}")
    return result.stdout.strip()


def try_run(*args: str, cwd: Path | None = None) -> str | None:
    result = _run(args, cwd)
    return result.stdout.strip() if result.returncode == 0 else None


def _run(args: tuple[str, ...], cwd: Path | None) -> subprocess.CompletedProcess[str]:
    for delay in (*RETRY_DELAYS, None):
        result = subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=False)
        if result.returncode == 0 or delay is None or not _DROPPED_BEFORE_HANDSHAKE.search(result.stderr):
            return result
        time.sleep(delay)
    raise AssertionError("unreachable")

import shutil
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager


@contextmanager
def stay_awake() -> Iterator[None]:
    """Blocks suspend (idle, sleep and lid close) while shipping, like `claude-awake`.
    The lock is held by a child process and released when it is terminated."""
    if shutil.which("systemd-inhibit") is None:
        _warn("systemd-inhibit not found, the machine may go to sleep while shipping")
        yield
        return

    lock = subprocess.Popen(
        [
            "systemd-inhibit",
            "--what=idle:sleep:handle-lid-switch",
            "--who=git-ship",
            "--why=Shipping a branch",
            "--mode=block",
            "sleep", "infinity",
        ],
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        # systemd-inhibit only exits on its own if it could not take the lock.
        lock.wait(timeout=1)
        _warn(f"could not block sleep: {lock.stderr.read().strip() if lock.stderr else lock.returncode}")
    except subprocess.TimeoutExpired:
        pass

    try:
        yield
    finally:
        lock.terminate()
        lock.wait()


def _warn(message: str) -> None:
    print(f"ship: warning: {message}", file=sys.stderr)

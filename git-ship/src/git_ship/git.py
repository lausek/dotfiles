import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .shell import run, try_run


@dataclass(frozen=True)
class Git:
    cwd: Path

    def __call__(self, *args: str) -> str:
        return run("git", *args, cwd=self.cwd)

    def try_(self, *args: str) -> str | None:
        return try_run("git", *args, cwd=self.cwd)

    def config(self, key: str) -> str | None:
        return self.try_("config", "--get", key)

    def config_all(self, key: str) -> list[str]:
        values = self.try_("config", "--get-all", key)
        return values.splitlines() if values else []

    def is_clean(self) -> bool:
        return self("status", "--porcelain") == ""

    def untracked(self) -> set[str]:
        return set(self("ls-files", "--others", "--exclude-standard").splitlines())

    def has_staged_changes(self) -> bool:
        return self.try_("diff", "--cached", "--quiet") is None

    def subjects(self, rev_range: str) -> list[str]:
        log = self("log", "--reverse", "--format=%s", rev_range)
        return log.splitlines() if log else []


@contextmanager
def worktree(repo: Git) -> Iterator[Git]:
    """A detached checkout of HEAD that is removed afterwards, whatever happens."""
    with tempfile.TemporaryDirectory(prefix="git-ship-") as tmp:
        path = Path(tmp) / "wt"
        repo("worktree", "add", "-q", "--detach", str(path), "HEAD")
        try:
            yield Git(path)
        finally:
            repo("worktree", "remove", "--force", str(path))

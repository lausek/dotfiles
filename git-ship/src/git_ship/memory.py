"""What git-ship remembers between runs, per branch, in `.git/ship-state.json` (per clone, never committed):

- the last review and the commit it reviewed, so a rerun on the same commit asks about the same findings
  instead of reviewing again
- the findings you kept, so later reviews of the branch don't report them again

It is only a cache: if the file is unreadable, git-ship starts from scratch.
"""

import json
from collections.abc import Iterable, Sequence
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .models import Finding, Review

VERSION = 1


class Memory:
    def __init__(self, path: Path, branch: str):
        self.path = path
        self.branch = branch

    def kept(self) -> list[Finding]:
        try:
            return [_finding(item) for item in self._entry().get("kept", [])]
        except (KeyError, TypeError, AttributeError):
            return []

    def remember_kept(self, kept: Sequence[Finding]) -> None:
        self._update(kept=[asdict(finding) for finding in kept])

    def review(self, key: str) -> Review | None:
        """The last review, if it reviewed `key`."""
        saved = self._entry().get("review")
        if not isinstance(saved, dict) or saved.get("key") != key:
            return None
        try:
            return Review(
                findings=[_finding(item) for item in saved["findings"]],
                risk=saved["risk"],
                risk_rationale=saved["risk_rationale"],
            )
        except (KeyError, TypeError, AttributeError):
            return None

    def remember_review(self, key: str, review: Review) -> None:
        self._update(review={"key": key, **asdict(review)})

    def forget_other_branches(self, branches: Iterable[str]) -> None:
        """Drops what it remembers of branches that no longer exist."""
        keep = set(branches) | {self.branch}
        data = self._read()
        data["branches"] = {name: entry for name, entry in data["branches"].items() if name in keep}
        self._write(data)

    def _entry(self) -> dict[str, Any]:
        entry = self._read()["branches"].get(self.branch)
        return entry if isinstance(entry, dict) else {}

    def _update(self, **values: Any) -> None:
        data = self._read()
        data["branches"][self.branch] = self._entry() | values
        self._write(data)

    def _read(self) -> dict[str, Any]:
        empty: dict[str, Any] = {"version": VERSION, "branches": {}}
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return empty
        if not isinstance(data, dict) or data.get("version") != VERSION or not isinstance(data.get("branches"), dict):
            return empty
        return data

    def _write(self, data: dict[str, Any]) -> None:
        # Written whole and renamed, so an interrupted run never leaves half a file.
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2) + "\n")
        tmp.replace(self.path)


def _finding(item: dict[str, Any]) -> Finding:
    return Finding(file=item["file"], line=item.get("line"), summary=item["summary"], needs_user=item["needs_user"])

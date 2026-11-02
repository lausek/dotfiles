from pathlib import Path
from typing import Protocol

from .models import PullRequest
from .shell import run


class Host(Protocol):
    def find_open_pr(self, branch: str) -> str | None: ...

    def create_pr(self, branch: str, base: str, pr: PullRequest) -> str: ...


class GitHub:
    def __init__(self, cwd: Path):
        self.cwd = cwd

    def find_open_pr(self, branch: str) -> str | None:
        url = run("gh", "pr", "list", "--head", branch, "--state", "open", "--json", "url", "-q", ".[0].url", cwd=self.cwd)
        return url or None

    def create_pr(self, branch: str, base: str, pr: PullRequest) -> str:
        return run(
            "gh", "pr", "create",
            "--head", branch, "--base", base,
            "--title", pr.title, "--body", pr.body,
            cwd=self.cwd,
        )


class AzureDevOps:
    URL_QUERY = "join('', [repository.webUrl, '/pullrequest/', to_string(pullRequestId)])"

    def __init__(self, cwd: Path):
        self.cwd = cwd

    def find_open_pr(self, branch: str) -> str | None:
        url = run(
            "az", "repos", "pr", "list",
            "--source-branch", branch, "--status", "active",
            "--query", f"[0].{self.URL_QUERY}", "-o", "tsv",
            cwd=self.cwd,
        )
        return url or None

    def create_pr(self, branch: str, base: str, pr: PullRequest) -> str:
        return run(
            "az", "repos", "pr", "create",
            "--source-branch", branch, "--target-branch", base,
            "--title", pr.title, "--description", pr.body,
            "--query", self.URL_QUERY, "-o", "tsv",
            cwd=self.cwd,
        )


def detect_host(remote_url: str, cwd: Path) -> Host | None:
    if "github.com" in remote_url:
        return GitHub(cwd)
    if "dev.azure.com" in remote_url or "visualstudio.com" in remote_url:
        return AzureDevOps(cwd)
    return None

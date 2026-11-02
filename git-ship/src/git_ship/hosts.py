import json
import re
import tempfile
from pathlib import Path
from typing import Any, ClassVar, Protocol

from .models import Check, CheckState, PullRequest, Ticket
from .shell import ShipError, run


class Host(Protocol):
    def fetch_ticket(self, ticket_id: str) -> Ticket: ...

    def find_open_pr(self, branch: str) -> str | None: ...

    def create_pr(self, branch: str, base: str, pr: PullRequest, ticket: Ticket | None) -> str:
        """Links the ticket so merging the PR closes it."""
        ...

    def checks(self, branch: str, head: str) -> list[Check]:
        """CI checks of the branch's PR. Checks that ran on another commit than `head` are pending."""
        ...

    def failure_log(self, check: Check) -> str: ...


class GitHub:
    def __init__(self, cwd: Path):
        self.cwd = cwd

    def fetch_ticket(self, ticket_id: str) -> Ticket:
        issue = json.loads(self._gh("issue", "view", ticket_id, "--json", "title,body"))
        return Ticket(id=ticket_id, title=issue["title"], description=issue["body"])

    def find_open_pr(self, branch: str) -> str | None:
        url = self._gh("pr", "list", "--head", branch, "--state", "open", "--json", "url", "-q", ".[0].url")
        return url or None

    def create_pr(self, branch: str, base: str, pr: PullRequest, ticket: Ticket | None) -> str:
        body = f"{pr.body}\n\nCloses #{ticket.id}" if ticket else pr.body
        return self._gh("pr", "create", "--head", branch, "--base", base, "--title", pr.title, "--body", body)

    def checks(self, branch: str, head: str) -> list[Check]:
        pr = json.loads(self._gh("pr", "view", branch, "--json", "headRefOid,statusCheckRollup"))
        if pr["headRefOid"] != head:
            return []
        return [_github_check(item) for item in pr["statusCheckRollup"]]

    def failure_log(self, check: Check) -> str:
        job = re.search(r"/actions/runs/\d+/job/(\d+)", check.ref)
        if job is None:
            return f"No log available, see {check.ref}"
        return self._gh("run", "view", "--job", job.group(1), "--log-failed")

    def _gh(self, *args: str) -> str:
        return run("gh", *args, cwd=self.cwd)


def _github_check(item: dict[str, Any]) -> Check:
    if item["__typename"] == "CheckRun":
        if item["status"] != "COMPLETED":
            state: CheckState = "pending"
        elif item["conclusion"] in ("SUCCESS", "NEUTRAL", "SKIPPED"):
            state = "passed"
        else:
            state = "failed"
        return Check(name=item["name"], state=state, ref=item.get("detailsUrl") or "")

    states: dict[str, CheckState] = {"SUCCESS": "passed", "PENDING": "pending", "EXPECTED": "pending"}
    return Check(name=item["context"], state=states.get(item["state"], "failed"), ref=item.get("targetUrl") or "")


class AzureDevOps:
    URL_QUERY = "join('', [repository.webUrl, '/pullrequest/', to_string(pullRequestId)])"
    POLICY_STATES: ClassVar[dict[str, CheckState]] = {
        "approved": "passed",
        "notApplicable": "passed",
        "rejected": "failed",
        "broken": "failed",
    }

    def __init__(self, cwd: Path):
        self.cwd = cwd

    def fetch_ticket(self, ticket_id: str) -> Ticket:
        item = json.loads(self._az(
            "boards", "work-item", "show", "--id", ticket_id, "-o", "json",
            "--query", "{title: fields.\"System.Title\", description: fields.\"System.Description\", "
                       "criteria: fields.\"Microsoft.VSTS.Common.AcceptanceCriteria\", "
                       "repro: fields.\"Microsoft.VSTS.TCM.ReproSteps\"}",
        ))
        sections = [
            ("Description", item["description"]),
            ("Acceptance criteria", item["criteria"]),
            ("Repro steps", item["repro"]),
        ]
        description = "\n\n".join(f"{name}:\n{text}" for name, text in sections if text)
        return Ticket(id=ticket_id, title=item["title"], description=description)

    def find_open_pr(self, branch: str) -> str | None:
        pr = self._find_pr(branch)
        return self._web_url(pr["id"]) if pr else None

    def create_pr(self, branch: str, base: str, pr: PullRequest, ticket: Ticket | None) -> str:
        link = ["--work-items", ticket.id, "--transition-work-items", "true"] if ticket else []
        pr_id = self._az("repos", "pr", "create", "--source-branch", branch, "--target-branch", base,
                         "--title", pr.title, "--description", pr.body, *link, "--query", "pullRequestId", "-o", "tsv")
        return self._web_url(int(pr_id))

    def checks(self, branch: str, head: str) -> list[Check]:
        """Build policies of the PR. Other policies (reviewers, work items) are not CI."""
        pr = self._find_pr(branch)
        if pr is None:
            raise ShipError(f"no open PR for {branch}")
        policies = json.loads(self._az("repos", "pr", "policy", "list", "--id", str(pr["id"]), "-o", "json"))
        return [
            self._check(policy, head, pr["project"])
            for policy in policies
            if policy["configuration"]["type"]["displayName"] == "Build"
        ]

    def failure_log(self, check: Check) -> str:
        project, build_id = check.ref.split(":")
        timeline = json.loads(self._az(
            "devops", "invoke", "--area", "build", "--resource", "timeline",
            "--route-parameters", f"project={project}", f"buildId={build_id}", "-o", "json",
        ))
        failed = [r for r in timeline["records"] if r["type"] == "Task" and r["result"] == "failed"]
        if not failed:
            return f"No failed task found in build {build_id}"
        return "\n\n".join(self._task_log(project, build_id, record) for record in failed)

    def _check(self, policy: dict[str, Any], head: str, project: str) -> Check:
        context = policy.get("context") or {}
        name = context.get("buildDefinitionName") or policy["configuration"]["settings"].get("displayName") or "build"
        current = context.get("lastMergeSourceCommitId") == head and not context.get("isExpired")
        state = self.POLICY_STATES.get(policy["status"], "pending") if current else "pending"
        return Check(name=name, state=state, ref=f"{project}:{context.get('buildId')}")

    def _task_log(self, project: str, build_id: str, record: dict[str, Any]) -> str:
        issues = "\n".join(issue["message"] for issue in record.get("issues") or [])
        log = record.get("log")
        if log is None:
            return f"## {record['name']}\n{issues}"

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "task.log"  # az refuses to write to an existing file
            self._az(
                "devops", "invoke", "--area", "build", "--resource", "logs",
                "--route-parameters", f"project={project}", f"buildId={build_id}", f"logId={log['id']}",
                "--accept-media-type", "text/plain", "--out-file", str(out),
            )
            return f"## {record['name']}\n{issues}\n\n{out.read_text()}"

    def _find_pr(self, branch: str) -> dict[str, Any] | None:
        return json.loads(self._az(
            "repos", "pr", "list", "--source-branch", branch, "--status", "active",
            "--query", "[0].{id: pullRequestId, project: repository.project.name}", "-o", "json",
        ) or "null")

    def _web_url(self, pr_id: int) -> str:
        # `pr list` leaves out the repository's web URL, `pr show` has it.
        return self._az("repos", "pr", "show", "--id", str(pr_id), "--query", self.URL_QUERY, "-o", "tsv")

    def _az(self, *args: str) -> str:
        return run("az", *args, cwd=self.cwd)


def detect_host(remote_url: str, cwd: Path) -> Host | None:
    if "github.com" in remote_url:
        return GitHub(cwd)
    if "dev.azure.com" in remote_url or "visualstudio.com" in remote_url:
        return AzureDevOps(cwd)
    return None

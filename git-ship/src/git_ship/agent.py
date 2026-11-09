import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Protocol

from .models import Change, Detection, Finding, PullRequest, Review
from .shell import ShipError, run

READ_ONLY_TOOLS = "Read,Grep,Glob,Bash(git diff:*),Bash(git log:*),Bash(git show:*)"

OPINIONS = Path.home() / ".claude" / "OPINIONS.md"
"""The author's views on how code should be. The reviewer reads them when they exist."""
EDIT_TOOLS = "Read,Grep,Glob,Edit,Write"

FINDINGS_SCHEMA = {
    "type": "object",
    "required": ["findings", "risk_level", "risk_rationale"],
    "properties": {
        "risk_level": {"type": "string", "enum": ["low", "medium", "high"]},
        "risk_rationale": {"type": "string"},
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["file", "summary", "detail", "action"],
                "properties": {
                    "file": {"type": "string"},
                    "line": {"type": "integer"},
                    "summary": {"type": "string"},
                    "detail": {"type": "string"},
                    "question": {"type": "string"},
                    "action": {"type": "string", "enum": ["auto-fix", "ask-user"]},
                },
            },
        }
    },
}

PR_SCHEMA = {
    "type": "object",
    "required": ["title", "body"],
    "properties": {"title": {"type": "string"}, "body": {"type": "string"}},
}


PR_BODY_TARGET = 800

DETECTION_SCHEMA = {
    "type": "object",
    "required": ["checks", "sources", "tests_in_ci"],
    "properties": {
        "checks": {"type": "array", "items": {"type": "string"}},
        "sources": {"type": "array", "items": {"type": "string"}},
        "tests_in_ci": {"type": "boolean"},
    },
}


class Agent(Protocol):
    def detect_checks(self) -> Detection: ...

    def review(self, change: Change, kept: Sequence[Finding], since: str | None) -> Review:
        """Reviews the full change, or with `since` only the commits after it."""
        ...

    def fix(self, instructions: str) -> None: ...

    def write_pr(self, change: Change) -> PullRequest: ...


class Claude:
    """Every call is a fresh `claude -p` session, so the reviewer never sees the fixer's reasoning."""

    def __init__(self, cwd: Path):
        self.cwd = cwd

    def detect_checks(self) -> Detection:
        prompt = """Find the checks a developer of this repository runs locally before pushing.
They will run in order from the repository root, in a fresh checkout without installed dependencies.
- First install dependencies if needed, respecting lockfiles (e.g. `npm ci`, `uv sync`, `poetry install`).
- Then fast checks: format check, lint, typecheck.
- Then the tests, but only if no CI pipeline runs them on pull requests. When CI runs them, leave them
  out: they cost a lot locally, and failures in the PR's CI are fixed from its logs.
- Leave out slow or environment-dependent things (e2e, deployments, docker builds, anything needing secrets).
  The PR's CI covers those.
Take the commands from what the repo already defines: package.json scripts, Makefile, pyproject.toml,
CI pipeline files, README, CLAUDE.md / AGENTS.md. If there are tests but no command for them,
use the language's standard runner (e.g. `python3 -m pytest`, `go test ./...`, `cargo test`).
sources: repo-relative paths of the files that define the commands (package.json, Makefile, CI pipeline
files, ...). Detection reruns whenever one of them changes, so leave out files that change for other
reasons: lockfiles, docs, CLAUDE.md. Also list such defining files you looked for that don't exist yet,
so detection reruns when they are added. Return empty checks if the repo has nothing to check.
tests_in_ci: true if you left the tests out because CI runs them on pull requests."""
        output = self._structured(prompt, DETECTION_SCHEMA, READ_ONLY_TOOLS)
        return Detection(checks=output["checks"], sources=output["sources"], tests_in_ci=output["tests_in_ci"])

    def review(self, change: Change, kept: Sequence[Finding], since: str | None) -> Review:
        if since is None:
            scope = f"""You are reviewing a change you did not write. Review the full diff `git diff {change.diff_range}`.
This is the only full review of the change: later reviews only look at the fixes for your findings. So report
every problem you find now, all in this one list. Don't hold anything back for a later round."""
        else:
            scope = f"""You are reviewing a change you did not write. The change, `git diff {change.diff_range}`, was reviewed
up to commit {since} already. Review only what the commits after it changed: `git log -p --cc {since}..HEAD`
(a merge commit shows only how its conflicts were resolved). Read the full change only for context and report
only problems these commits bring in. Report all of them now, in this one list."""
        prompt = f"""{scope}
Report only real problems: bugs, broken edge cases, security issues, missing error handling,
violations of the repo's conventions (CLAUDE.md / AGENTS.md). No style nitpicks, no praise.
Mark a finding 'auto-fix' only if the fix is mechanical and unambiguous. If the fix changes
behavior or intent in a way the author has to decide, mark it 'ask-user'.
Return an empty list if the change is fine.

The author decides on the findings from a terminal prompt, without opening the code. Write each so it is
understood at a glance:
- summary: what goes wrong for users or operators, in plain words, at most 12 words. No code identifiers,
  file names or ids. E.g. "Deploy fails: the migrations have two heads after merging main".
- question: only for 'ask-user'. The decision in one short question naming the options, at most 15 words.
  E.g. "Migrate the old chat events, or accept that old chats lose their visuals?"
- detail: everything else - evidence, affected code, the suggested fix. The fixer works from it.

Then assess how carefully a human has to review the full change before merging:
- 'low': well-bounded, mostly cosmetic or straightforward with little ambiguity. A quick look is enough.
- 'medium': touches behavior that matters, safe to merge once the risky parts were checked.
- 'high': fundamental, risky or ambiguous. Must not be merged without an in-depth review.
risk_rationale is one sentence explaining the level and where a reviewer should look."""
        if change.ticket:
            prompt += f"""

The change implements this ticket. If it misses or contradicts what the ticket asks for,
report that as an 'ask-user' finding.

{change.ticket}"""
        if change.context:
            prompt += f"""

The author gave you this context for the review, in their own words. Follow it, e.g. where to focus or what to
look out for. It doesn't lift the rules above: still report every real problem you find.

{change.context}"""
        if kept:
            prompt += "\n\nThe author decided to keep these as they are, don't report them again:\n"
            prompt += _bullets(kept)

        tools = READ_ONLY_TOOLS
        if OPINIONS.is_file():
            prompt += f"""

Before reviewing, read the author's opinions on how code should be from `{OPINIONS}`.
Report code in the change that goes against them as 'ask-user' findings."""
            tools += "," + _read_only(OPINIONS)

        output = self._structured(prompt, FINDINGS_SCHEMA, tools)
        findings = [
            Finding(
                file=item["file"],
                line=item.get("line"),
                summary=item["summary"],
                needs_user=item["action"] == "ask-user",
                detail=item.get("detail", ""),
                question=item.get("question"),
            )
            for item in output["findings"]
        ]
        return Review(findings=findings, risk=output["risk_level"], risk_rationale=output["risk_rationale"])

    def fix(self, instructions: str) -> None:
        prompt = f"""Fix the following in this repository. Keep the change minimal, don't touch unrelated code.

{instructions}"""
        run("claude", "-p", prompt, "--permission-mode", "acceptEdits", "--allowedTools", EDIT_TOOLS, cwd=self.cwd)

    def write_pr(self, change: Change) -> PullRequest:
        prompt = f"""Write a pull request for `git diff {change.diff_range}`, for a reviewer who wasn't there.
Title: a conventional commit describing the effect in plain lowercase words. The scope is the product
that changed in a monorepo, e.g. 'fix(dpt): ...', and left out in a single-product repo, e.g. 'feat: ...'.
No ticket numbers in the title.
{f"The change implements this ticket:{chr(10)}{change.ticket}" if change.ticket else ""}
{f"The author's context for the change, use what helps a reviewer understand it:{chr(10)}{change.context}" if change.context else ""}
Body (markdown): concise. One to three plain sentences on the problem and the fix, or a short bullet list
when the change has several parts. Name what is left out only if a reviewer would expect it in this PR.
No headings, no template, no lists of files or checks, no attribution or "Generated with" footer.
Stay under {PR_BODY_TARGET} characters."""
        output = self._structured(prompt, PR_SCHEMA, READ_ONLY_TOOLS)
        return PullRequest(title=output["title"], body=output["body"])

    def _structured(self, prompt: str, schema: dict[str, Any], tools: str) -> dict[str, Any]:
        raw = run(
            "claude", "-p", prompt,
            "--output-format", "json",
            "--json-schema", json.dumps(schema),
            "--allowedTools", tools,
            cwd=self.cwd,
        )
        response = json.loads(raw)
        output = response.get("structured_output")
        if response.get("is_error") or output is None:
            raise ShipError(f"claude returned no structured output: {response.get('result')}")
        return output


def _read_only(path: Path) -> str:
    """Permission to read this one file outside the repo, also through a symlink.
    `//` marks an absolute path in permission rules."""
    paths = dict.fromkeys([path, path.resolve()])
    return ",".join(f"Read(/{file})" for file in paths)


def _bullets(items: Sequence[object]) -> str:
    return "\n".join(f"- {item}" for item in items)

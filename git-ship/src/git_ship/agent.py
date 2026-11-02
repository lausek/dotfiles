import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Protocol

from .models import Change, Finding, PullRequest
from .shell import ShipError, run

READ_ONLY_TOOLS = "Read,Grep,Glob,Bash(git diff:*),Bash(git log:*),Bash(git show:*)"
EDIT_TOOLS = "Read,Grep,Glob,Edit,Write"

FINDINGS_SCHEMA = {
    "type": "object",
    "required": ["findings"],
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["file", "summary", "action"],
                "properties": {
                    "file": {"type": "string"},
                    "line": {"type": "integer"},
                    "summary": {"type": "string"},
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


class Agent(Protocol):
    def review(self, change: Change, kept: Sequence[Finding]) -> list[Finding]: ...

    def fix(self, instructions: str) -> None: ...

    def write_pr(self, change: Change, fixes: Sequence[str], checks: Sequence[str]) -> PullRequest: ...


class Claude:
    """Every call is a fresh `claude -p` session, so the reviewer never sees the fixer's reasoning."""

    def __init__(self, cwd: Path):
        self.cwd = cwd

    def review(self, change: Change, kept: Sequence[Finding]) -> list[Finding]:
        prompt = f"""You are reviewing a change you did not write. Review the full diff `git diff {change.diff_range}`.
Report only real problems: bugs, broken edge cases, security issues, missing error handling,
violations of the repo's conventions (CLAUDE.md / AGENTS.md). No style nitpicks, no praise.
Mark a finding 'auto-fix' only if the fix is mechanical and unambiguous. If the fix changes
behavior or intent in a way the author has to decide, mark it 'ask-user'.
Return an empty list if the change is fine."""
        if kept:
            prompt += "\n\nThe author decided to keep these as they are, don't report them again:\n"
            prompt += _bullets(kept)

        output = self._structured(prompt, FINDINGS_SCHEMA, READ_ONLY_TOOLS)
        return [
            Finding(
                file=item["file"],
                line=item.get("line"),
                summary=item["summary"],
                needs_user=item["action"] == "ask-user",
            )
            for item in output["findings"]
        ]

    def fix(self, instructions: str) -> None:
        prompt = f"""Fix the following in this repository. Keep the change minimal, don't touch unrelated code.

{instructions}"""
        run("claude", "-p", prompt, "--permission-mode", "acceptEdits", "--allowedTools", EDIT_TOOLS, cwd=self.cwd)

    def write_pr(self, change: Change, fixes: Sequence[str], checks: Sequence[str]) -> PullRequest:
        prompt = f"""Write a pull request for `git diff {change.diff_range}`, for a reviewer who wasn't there.
Title: conventional commit style. Scope is the ticket number '{change.ticket or ""}' if set,
otherwise the product or area that changed, e.g. 'feat(1234): ...' or 'fix(api): ...'.
Body (markdown): what changed and why, how it was verified, risks.
No attribution, signature or "Generated with" footer.
Fixes applied by the validation pipeline:
{_bullets(fixes) or "none"}
Checks that passed:
{_bullets(checks) or "none"}"""
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


def _bullets(items: Sequence[object]) -> str:
    return "\n".join(f"- {item}" for item in items)

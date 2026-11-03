import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from itertools import count
from pathlib import Path
from typing import Literal

from .agent import Agent
from .git import Git
from .hosts import Host
from .models import Assessment, Change, Check, Finding
from .shell import ShipError

Decision = Literal["fix", "keep"]
Decide = Callable[[Sequence[Finding]], Decision]
"""Decides what to do with ask-user findings. Raises to abort."""

CI_POLL_SECONDS = 30
CI_NO_CHECKS_GRACE_SECONDS = 180
CI_LOG_LINES = 300


CONFLICT_MARKERS = ("<<<<<<< ", "||||||| ", ">>>>>>> ")


class MergeConflict(ShipError):
    """The PR has merge conflicts with its base, so its CI doesn't run."""


@dataclass(frozen=True)
class Run:
    worktree: Git
    agent: Agent
    change: Change
    rounds: int
    decide: Decide
    base: str
    """Branch the PR merges into, e.g. 'main'."""


def step(title: str) -> None:
    print(f"\n\033[1m==> {title}\033[0m", flush=True)


def merge_step(run: Run) -> bool:
    """The PR's CI doesn't run while the branch conflicts with the base. If it does, the base is merged in
    and the agent resolves the conflicts. A clean merge is left to the host, the branch stays as it is.
    Returns whether it merged."""
    base = f"origin/{run.base}"
    run.worktree("fetch", "-q", "origin", run.base)
    conflicted = run.worktree.merge_conflicts(base)
    if not conflicted:
        return False

    step(f"resolve merge conflicts with {base}")
    for path in conflicted:
        print(f"[conflict] {path}")
    untracked_before = run.worktree.untracked()
    for round in count(1):
        run.agent.fix(
            f"`{base}` is being merged into this branch and these files have conflicts:\n"
            + "\n".join(f"- {path}" for path in conflicted)
            + "\n\nResolve every conflict so that the branch's change applies on top of what changed on the base, "
            "keeping the intent of both sides. The markers are in diff3 style: between `|||||||` and `=======` "
            "is the common ancestor, above it the branch, below it the base. Remove all conflict markers. "
            "Don't change anything beyond the conflicts."
        )
        remaining = [path for path in conflicted if _has_conflict_markers(run.worktree.cwd / path)]
        if not remaining:
            break
        if round > run.rounds:
            run.worktree("merge", "--abort")
            raise ShipError(f"merge conflicts with {base} still unresolved after {run.rounds} rounds: {', '.join(remaining)}")
        conflicted = remaining
    # Committed even without content changes: the merge commit makes the base a parent, which resolves the PR.
    _stage(run.worktree, untracked_before)
    run.worktree("commit", "-q", "-m", f"chore: merge {base} and resolve conflicts")
    return True


def _has_conflict_markers(path: Path) -> bool:
    if not path.is_file():
        return False
    lines = path.read_text(errors="replace").splitlines()
    return any(line.startswith(CONFLICT_MARKERS) for line in lines)


def review_step(run: Run) -> Assessment:
    """Reviews the full change until it is clean. Each round of fixes is reviewed again."""
    kept: list[Finding] = []
    for round in count(1):
        step(f"review (round {round})")
        review = run.agent.review(run.change, kept)
        print(f"risk: {review.risk} - {review.risk_rationale}")
        assessment = Assessment(risk=review.risk, risk_rationale=review.risk_rationale, kept=kept)
        if not review.findings:
            print("no findings")
            return assessment

        for finding in review.findings:
            print(f"[{'ask-user' if finding.needs_user else 'auto-fix'}] {finding}")
        if round > run.rounds:
            raise ShipError(f"still findings after {run.rounds} fix rounds")

        to_fix = select_fixes(review.findings, run.decide)
        kept += [finding for finding in review.findings if finding not in to_fix]
        if not to_fix:
            return assessment

        fix(run, "fix: address review findings", "\n".join(f"- {finding}" for finding in to_fix))
    raise AssertionError("unreachable")


def select_fixes(findings: Sequence[Finding], decide: Decide) -> list[Finding]:
    asks = [finding for finding in findings if finding.needs_user]
    if asks and decide(asks) == "fix":
        return list(findings)
    return [finding for finding in findings if not finding.needs_user]


def checks_step(run: Run, commands: Sequence[str]) -> None:
    """Runs each check in order. A failing check is handed to the agent to fix, then rerun."""
    for command in commands:
        for round in count(1):
            step(f"check: {command}")
            result = subprocess.run(
                command, shell=True, cwd=run.worktree.cwd, check=False,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            )
            if result.returncode == 0:
                break

            print(_tail(result.stdout, 40))
            if round > run.rounds:
                raise ShipError(f"'{command}' still failing after {run.rounds} fix rounds")

            fix(
                run,
                f"fix: make '{command}' pass",
                f"The command `{command}` fails. Make it pass by fixing the code, "
                f"not by weakening or skipping tests. Output (tail):\n\n{_tail(result.stdout, 200)}",
            )


@dataclass(frozen=True)
class Clock:
    now: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep


SYSTEM_CLOCK = Clock()


def ci_step(
    run: Run, host: Host, branch: str, publish: Callable[[], None], timeout: float, clock: Clock = SYSTEM_CLOCK
) -> list[Check]:
    """Waits for the PR's CI. Failures are handed to the agent with their logs, fixed and published.
    Returns the passed checks, empty if none showed up."""
    for round in count(1):
        step(f"CI (round {round})")
        try:
            checks = wait_for_checks(host, branch, run.worktree("rev-parse", "HEAD"), timeout, clock)
        except MergeConflict:
            if round > run.rounds:
                raise
            print("the base moved and the PR has merge conflicts now")
            if not merge_step(run):
                raise ShipError(f"the PR reports merge conflicts, but origin/{run.base} merges cleanly here - check the PR yourself")
            publish()
            continue

        failed = [check for check in checks if check.state == "failed"]
        if not failed:
            print("CI passed" if checks else "no CI checks showed up, skipping")
            return checks

        for check in failed:
            print(f"[failed] {check.name}")
        if round > run.rounds:
            raise ShipError(f"CI still failing after {run.rounds} fix rounds")

        logs = "\n\n".join(f"# {check.name}\n{_tail(host.failure_log(check), CI_LOG_LINES)}" for check in failed)
        fix(
            run,
            f"fix: make CI pass ({', '.join(check.name for check in failed)})",
            "These CI checks fail. Make them pass by fixing the code, not by weakening or skipping tests. "
            "Only change CI configuration if it is clearly the cause. Logs (tail):\n\n" + logs,
        )
        publish()
    raise AssertionError("unreachable")


def wait_for_checks(host: Host, branch: str, head: str, timeout: float, clock: Clock) -> list[Check]:
    started = clock.now()
    last_status = ""
    while True:
        checks = host.checks(branch, head)
        elapsed = clock.now() - started
        if checks and all(check.state != "pending" for check in checks):
            return checks
        # Right after a push the host may still report the conflicts of the previous commit.
        if elapsed >= CI_POLL_SECONDS and host.has_conflicts(branch):
            raise MergeConflict("the PR has merge conflicts with its base, so CI won't run")
        if not checks and elapsed > CI_NO_CHECKS_GRACE_SECONDS:
            return []
        if elapsed > timeout:
            raise ShipError(f"CI still running after {int(timeout // 60)} min - check the PR yourself")

        status = _ci_status(checks)
        if status != last_status:
            print(status, flush=True)
            last_status = status
        clock.sleep(CI_POLL_SECONDS)


def fix(run: Run, message: str, instructions: str) -> None:
    """Lets the agent fix and commits the result."""
    untracked_before = run.worktree.untracked()
    run.agent.fix(instructions)
    _commit(run.worktree, untracked_before, message)


def _commit(wt: Git, untracked_before: set[str], message: str) -> None:
    _stage(wt, untracked_before)
    if not wt.has_staged_changes():
        raise ShipError(f"the agent made no changes for: {message}")
    wt("commit", "-q", "-m", message)


def _stage(wt: Git, untracked_before: set[str]) -> None:
    """Stages the agent's changes. Untracked files that existed before, e.g. build output of a check, are left out."""
    created = sorted(wt.untracked() - untracked_before)
    wt("add", "-u")
    if created:
        wt("add", "--", *created)


def push(worktree: Git, branch: str) -> None:
    """Never forces. If the branch moved on the remote, the push fails and nothing is lost."""
    worktree("push", "-q", "-u", "origin", f"HEAD:refs/heads/{branch}")


def _ci_status(checks: Sequence[Check]) -> str:
    if not checks:
        return "waiting for CI to pick up the commit..."
    pending = [check.name for check in checks if check.state == "pending"]
    return f"{len(checks) - len(pending)}/{len(checks)} checks done, waiting for: {', '.join(pending)}"


def _tail(text: str, lines: int) -> str:
    return "\n".join(text.splitlines()[-lines:])

import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from itertools import count
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


@dataclass(frozen=True)
class Run:
    worktree: Git
    agent: Agent
    change: Change
    rounds: int
    decide: Decide


def step(title: str) -> None:
    print(f"\n\033[1m==> {title}\033[0m", flush=True)


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
        checks = wait_for_checks(host, branch, run.worktree("rev-parse", "HEAD"), timeout, clock)
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
    """Lets the agent fix and commits the result. Untracked files that existed before,
    e.g. build output of a check, are left out of the commit."""
    wt = run.worktree
    untracked_before = wt.untracked()
    run.agent.fix(instructions)

    created = sorted(wt.untracked() - untracked_before)
    wt("add", "-u")
    if created:
        wt("add", "--", *created)
    if not wt.has_staged_changes():
        raise ShipError(f"the agent made no changes for: {message}")
    wt("commit", "-q", "-m", message)


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

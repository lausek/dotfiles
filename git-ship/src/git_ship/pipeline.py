import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from itertools import count
from pathlib import Path
from typing import NoReturn

from .agent import Agent
from .git import Git
from .hosts import Host
from .memory import Memory
from .models import Assessment, Change, Check, Detection, Finding
from .shell import ShipError
from .ui import PlannedStep, Ui

Answer = str | None
"""What the author wants done about an ask-user finding, in their own words. None keeps it as it is."""
Decide = Callable[[Sequence[Finding]], list[Answer]]
"""Answers the ask-user findings among a review's findings, one per finding in their order. Raises to abort."""

CI_POLL_SECONDS = 30
CI_NO_CHECKS_GRACE_SECONDS = 180
CI_LOG_LINES = 300


CONFLICT_MARKERS = ("<<<<<<< ", "||||||| ", ">>>>>>> ")


class MergeConflict(ShipError):
    """The PR has merge conflicts with its base, so its CI doesn't run."""


class Restart(Exception):
    """A step committed a fix. The pipeline starts over, so every fix is reviewed and checked like the rest."""

    def __init__(self, reason: str, conflicts: bool = False):
        super().__init__(reason)
        self.reason = reason
        self.conflicts = conflicts
        """The PR reported merge conflicts, so the next merge step has to find them."""


@dataclass(frozen=True)
class Run:
    worktree: Git
    agent: Agent
    change: Change
    rounds: int
    """How often the pipeline may fix something and start over."""
    decide: Decide
    base: str
    """Branch the PR merges into, e.g. 'main'."""
    ui: Ui
    memory: Memory
    """What earlier runs on this branch found and decided."""
    attempt: int = 1

    @property
    def attempts(self) -> int:
        return self.rounds + 1


@dataclass(frozen=True)
class Clock:
    now: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep


SYSTEM_CLOCK = Clock()


@dataclass(frozen=True)
class Target:
    """Where the validated branch goes."""

    branch: str
    host: Host | None
    ci_timeout: float
    """Seconds to wait for CI per attempt, 0 doesn't watch it."""
    resolve_checks: Callable[[], Detection]
    """The saved checks, detected again when the files they came from changed, e.g. by a fix."""
    open_pr: Callable[[Assessment], str]
    """Opens the PR, or finds the open one. Returns its link."""


@dataclass(frozen=True)
class Shipped:
    detection: Detection
    ci_checks: list[Check]
    """The CI checks that passed, empty if none showed up or CI wasn't watched."""


def ship_pipeline(run: Run, target: Target, clock: Clock = SYSTEM_CLOCK) -> Shipped:
    """merge -> detect checks -> review -> checks -> push -> PR -> CI. A step that fixes something commits
    the fix and starts the pipeline over from the top."""
    kept = run.memory.kept()
    restart: Restart | None = None
    run.ui.plan(plan(run.base, target.branch, checks=None))
    for attempt in count(1):
        if restart:
            run.ui.restart(attempt, run.attempts, restart.reason)
        try:
            return _attempt(replace(run, attempt=attempt), target, kept, restart is not None and restart.conflicts, clock)
        except Restart as error:
            restart = error
    raise AssertionError("unreachable")


def _attempt(run: Run, target: Target, kept: list[Finding], conflicts_expected: bool, clock: Clock) -> Shipped:
    if not merge_step(run):
        if conflicts_expected:
            raise ShipError(f"the PR reports merge conflicts, but origin/{run.base} merges cleanly here - check the PR yourself")
        run.ui.skip("merge", "no conflicts")

    detection = target.resolve_checks()
    run.ui.plan(plan(run.base, target.branch, detection.checks))
    if not detection.checks:
        run.ui.skip("checks", "none detected")
    assessment = review_step(run, kept)
    checks_step(run, detection.checks)

    run.ui.start("push", f"push {target.branch}")
    push(run.worktree, target.branch)
    run.ui.done("push")

    if target.host is None:
        run.ui.skip("pr", "unknown host, open it yourself")
        run.ui.skip("ci", "unknown host")
        return Shipped(detection, [])

    run.ui.start("pr", "pull request")
    run.ui.done("pr", target.open_pr(assessment))
    if target.ci_timeout <= 0:
        run.ui.skip("ci", "disabled (ci_timeout_minutes: 0)")
        return Shipped(detection, [])
    return Shipped(detection, ci_step(run, target.host, target.branch, target.ci_timeout, clock))


def plan(base: str, branch: str, checks: Sequence[str] | None) -> list[PlannedStep]:
    """The steps ahead. The checks are one step until they are known."""
    if checks:
        check_steps = [PlannedStep(check_key(index), f"check: {command}") for index, command in enumerate(checks)]
    else:
        check_steps = [PlannedStep("checks", "checks")]
    return [
        PlannedStep("merge", f"merge origin/{base}"),
        PlannedStep("detect", "detect checks"),
        PlannedStep("review", "review"),
        *check_steps,
        PlannedStep("push", f"push {branch}"),
        PlannedStep("pr", "pull request"),
        PlannedStep("ci", "CI"),
    ]


def check_key(index: int) -> str:
    return f"check:{index}"


def merge_step(run: Run) -> bool:
    """The PR's CI doesn't run while the branch conflicts with the base. If it does, the base is merged in
    and the agent resolves the conflicts. A clean merge is left to the host, the branch stays as it is.
    Returns whether it merged."""
    base = f"origin/{run.base}"
    run.worktree("fetch", "-q", "origin", run.base)
    conflicted = run.worktree.merge_conflicts(base)
    if not conflicted:
        return False

    run.ui.start("merge", f"resolve merge conflicts with {base}")
    run.ui.details("merge", [f"[conflict] {path}" for path in conflicted])
    untracked_before = run.worktree.untracked()
    for round in count(1):
        run.ui.status(f"claude resolves {len(conflicted)} conflicted file(s)")
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
    # The pipeline goes on with the review, which sees the merge as part of the change.
    _stage(run.worktree, untracked_before)
    run.worktree("commit", "-q", "-m", f"chore: merge {base} and resolve conflicts")
    run.ui.done("merge", "conflicts resolved")
    return True


def _has_conflict_markers(path: Path) -> bool:
    if not path.is_file():
        return False
    lines = path.read_text(errors="replace").splitlines()
    return any(line.startswith(CONFLICT_MARKERS) for line in lines)


def review_step(run: Run, kept: list[Finding]) -> Assessment:
    """Reviews the full change. Findings to fix are fixed, then the pipeline starts over.
    Findings the author keeps are added to `kept`, so later reviews don't report them again."""
    run.ui.start("review", "review")
    key = _review_key(run)
    review = run.memory.review(key)
    if review is None:
        run.ui.status("claude reviews the change")
        review = run.agent.review(run.change, kept)
        run.memory.remember_review(key, review)
        source = []
    else:
        source = ["(the last run reviewed this commit already)"]
    # A review remembered from before you kept some of its findings still has them.
    open_findings = [finding for finding in review.findings if finding not in kept]
    findings = numbered(open_findings)
    run.ui.details(
        "review", [*source, f"risk: {review.risk} - {review.risk_rationale}", *(findings or ["no findings"])]
    )
    assessment = Assessment(risk=review.risk, risk_rationale=review.risk_rationale, kept=kept)
    if not open_findings:
        run.ui.done("review", f"risk: {review.risk}")
        return assessment

    to_fix = select_fixes(open_findings, run.decide)
    fixed = [finding for finding, _ in to_fix]
    kept += [finding for finding in open_findings if finding not in fixed]
    run.memory.remember_kept(kept)
    if not to_fix:
        run.ui.done("review", f"risk: {review.risk}, {len(kept)} finding(s) kept")
        return assessment

    fix(
        run,
        problem="still findings",
        status=f"claude fixes {len(to_fix)} finding(s)",
        message="fix: address review findings",
        instructions="\n".join(_instruction(finding, answer) for finding, answer in to_fix),
        reason=f"fixed {len(to_fix)} review finding(s)",
    )
    raise AssertionError("unreachable")


def _review_key(run: Run) -> str:
    """What the review depends on: the commit, where it meets the base and the ticket."""
    head = run.worktree("rev-parse", "HEAD")
    merge_base = run.worktree("merge-base", "HEAD", f"origin/{run.base}")
    ticket = run.change.ticket.id if run.change.ticket else ""
    return f"{head} {merge_base} {ticket}".strip()


def numbered(findings: Sequence[Finding]) -> list[str]:
    """The findings in short, the ask-user ones numbered, so an answer can refer to them."""
    asks = iter(range(1, len(findings) + 1))
    return [finding.tagged(next(asks) if finding.needs_user else None) for finding in findings]


def select_fixes(findings: Sequence[Finding], decide: Decide) -> list[tuple[Finding, Answer]]:
    """The findings to fix, each with the author's answer if it needed one. Unanswered ask-user findings are kept."""
    asks = [finding for finding in findings if finding.needs_user]
    answers = dict(zip(asks, decide(findings))) if asks else {}
    return [
        (finding, answers.get(finding))
        for finding in findings
        if not finding.needs_user or answers.get(finding) is not None
    ]


def _instruction(finding: Finding, answer: Answer) -> str:
    if answer is None:
        return f"- {finding}"
    return f"""- {finding}
  The author was asked: {finding.question or "How should this be fixed?"}
  The author answered, do what the answer says: {answer}"""


def checks_step(run: Run, commands: Sequence[str]) -> None:
    """Runs each check in order. A failing check is fixed, then the pipeline starts over."""
    for index, command in enumerate(commands):
        key = check_key(index)
        run.ui.start(key, f"check: {command}")
        result = subprocess.run(
            command, shell=True, cwd=run.worktree.cwd, check=False,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        if result.returncode == 0:
            run.ui.done(key)
            continue

        run.ui.log(_tail(result.stdout, 40))
        fix(
            run,
            problem=f"'{command}' still failing",
            status="claude fixes the failure",
            message=f"fix: make '{command}' pass",
            instructions=f"The command `{command}` fails. Make it pass by fixing the code, "
            f"not by weakening or skipping tests. Output (tail):\n\n{_tail(result.stdout, 200)}",
            reason=f"fixed '{command}'",
        )


def ci_step(run: Run, host: Host, branch: str, timeout: float, clock: Clock = SYSTEM_CLOCK) -> list[Check]:
    """Waits for the PR's CI. Failures are fixed with their logs, then the pipeline starts over.
    Returns the passed checks, empty if none showed up."""
    run.ui.start("ci", "CI")
    try:
        checks = wait_for_checks(host, branch, run.worktree("rev-parse", "HEAD"), timeout, run.ui, clock)
    except MergeConflict:
        if run.attempt >= run.attempts:
            raise
        run.ui.log("the base moved and the PR has merge conflicts now")
        raise Restart("the base moved, the PR has merge conflicts", conflicts=True)

    failed = [check for check in checks if check.state == "failed"]
    if not failed:
        run.ui.log("CI passed" if checks else "no CI checks showed up, skipping")
        if checks:
            run.ui.done("ci", f"{len(checks)} check(s) passed")
        else:
            run.ui.skip("ci", "no CI checks showed up")
        return checks

    names = ", ".join(check.name for check in failed)
    run.ui.details("ci", [f"[failed] {check.name}" for check in failed])
    if run.attempt >= run.attempts:
        raise ShipError(f"CI still failing after {run.rounds} fix rounds")
    run.ui.status("fetching the logs")
    logs = "\n\n".join(f"# {check.name}\n{_tail(host.failure_log(check), CI_LOG_LINES)}" for check in failed)
    fix(
        run,
        problem="CI still failing",
        status=f"claude fixes {len(failed)} failed check(s)",
        message=f"fix: make CI pass ({names})",
        instructions="These CI checks fail. Make them pass by fixing the code, not by weakening or skipping tests. "
        "Only change CI configuration if it is clearly the cause. Logs (tail):\n\n" + logs,
        reason=f"fixed CI ({names})",
    )
    raise AssertionError("unreachable")


def wait_for_checks(host: Host, branch: str, head: str, timeout: float, ui: Ui, clock: Clock) -> list[Check]:
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
            ui.status(status)
            last_status = status
        clock.sleep(CI_POLL_SECONDS)


def fix(run: Run, problem: str, status: str, message: str, instructions: str, reason: str) -> NoReturn:
    """Lets the agent fix, commits the result and starts the pipeline over. Gives up with `problem` when
    the pipeline already started over as often as it may."""
    if run.attempt >= run.attempts:
        raise ShipError(f"{problem} after {run.rounds} fix rounds")
    run.ui.status(status)
    untracked_before = run.worktree.untracked()
    run.agent.fix(instructions)
    _commit(run.worktree, untracked_before, message)
    raise Restart(reason)


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

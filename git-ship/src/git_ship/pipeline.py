import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from itertools import count

from .agent import Agent
from .git import Git
from .models import Change, Finding
from .shell import ShipError

Ask = Callable[[str], bool]


@dataclass(frozen=True)
class Run:
    worktree: Git
    agent: Agent
    change: Change
    rounds: int
    ask: Ask


def step(title: str) -> None:
    print(f"\n\033[1m==> {title}\033[0m", flush=True)


def review_step(run: Run) -> None:
    """Reviews the full change until it is clean. Each round of fixes is reviewed again."""
    kept: list[Finding] = []
    for round in count(1):
        step(f"review (round {round})")
        findings = run.agent.review(run.change, kept)
        if not findings:
            print("no findings")
            return

        for finding in findings:
            print(f"[{'ask-user' if finding.needs_user else 'auto-fix'}] {finding}")
        if round > run.rounds:
            raise ShipError(f"still findings after {run.rounds} fix rounds")

        to_fix = select_fixes(findings, run.ask)
        kept += [finding for finding in findings if finding not in to_fix]
        if not to_fix:
            return

        fix(run, "fix: address review findings", "\n".join(f"- {finding}" for finding in to_fix))


def select_fixes(findings: Sequence[Finding], ask: Ask) -> list[Finding]:
    auto = [finding for finding in findings if not finding.needs_user]
    decisions = len(findings) - len(auto)
    if decisions == 0:
        return auto
    if ask(f"fix the {decisions} ask-user finding(s) too?"):
        return list(findings)
    if ask("keep them as they are and continue?"):
        return auto
    raise ShipError("aborted")


def checks_step(run: Run, checks: Sequence[str]) -> None:
    """Runs each check in order. A failing check is handed to the agent to fix, then rerun."""
    for check in checks:
        for round in count(1):
            step(f"check: {check}")
            result = subprocess.run(
                check, shell=True, cwd=run.worktree.cwd,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            )
            if result.returncode == 0:
                break

            print(_tail(result.stdout, 40))
            if round > run.rounds:
                raise ShipError(f"'{check}' still failing after {run.rounds} fix rounds")

            fix(
                run,
                f"fix: make '{check}' pass",
                f"The command `{check}` fails. Make it pass by fixing the code, "
                f"not by weakening or skipping tests. Output (tail):\n\n{_tail(result.stdout, 200)}",
            )


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


def _tail(text: str, lines: int) -> str:
    return "\n".join(text.splitlines()[-lines:])

"""git ship - validate the current branch in a disposable worktree, then push, open a PR and watch CI.

  detect checks -> review -> fix -> checks -> fix -> push -> PR -> CI -> fix (GitHub or Azure DevOps)

On the first run, claude detects the local checks (install, lint, tests) and saves them to
.git/ship.json. They are detected again when the files they came from change.
"""

import argparse
import re
import sys
from collections.abc import Sequence
from pathlib import Path

from .agent import Claude
from .awake import stay_awake
from .checks import Approve, resolve_checks
from .config import Config, config_path, load_config, read_config_file
from .git import Git, worktree
from .hosts import Host, detect_host
from .models import Assessment, Change, Finding
from .pipeline import (
    Decide,
    Decision,
    Run,
    checks_step,
    ci_step,
    push,
    review_step,
    step,
)
from .report import with_assessment
from .shell import NeedsDecision, ShipError, run


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="git ship", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "-y", "--yes", action="store_true",
        help="don't ask: save detected checks right away, exit with 2 on findings that need a human decision",
    )
    asks = parser.add_mutually_exclusive_group()
    asks.add_argument("--fix-asks", action="store_true", help="fix findings that need a human decision too")
    asks.add_argument("--keep-asks", action="store_true", help="keep findings that need a human decision as they are")
    parser.add_argument("--setup", action="store_true", help="detect the checks again")
    args = parser.parse_args()

    try:
        with stay_awake():
            ship(decide=_decider(args), approve=accept if args.yes else confirm, redetect=args.setup)
    except NeedsDecision as error:
        print(f"ship: {error}", file=sys.stderr)
        return 2
    except ShipError as error:
        print(f"ship: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


def _decider(args: argparse.Namespace) -> Decide:
    if args.fix_asks:
        return lambda asks: "fix"
    if args.keep_asks:
        return lambda asks: "keep"
    if args.yes:
        return needs_decision
    return prompt_decision


def ship(decide: Decide, approve: Approve, redetect: bool) -> None:
    repo = Git(Path(run("git", "rev-parse", "--show-toplevel")))
    config_file = config_path(repo)
    config = load_config(repo, read_config_file(config_file))
    branch = _current_branch(repo, config)
    remote_url = repo("remote", "get-url", "origin")
    host = detect_host(remote_url, repo.cwd)

    repo("fetch", "-q", "origin", config.base)
    merge_base = repo("merge-base", "HEAD", f"origin/{config.base}")
    if not repo("rev-list", f"{merge_base}..HEAD"):
        raise ShipError(f"no commits ahead of origin/{config.base}")

    change = Change(diff_range=f"{merge_base}..HEAD", ticket=ticket_from_branch(branch))

    with worktree(repo) as wt:
        start = wt("rev-parse", "HEAD")
        validation = Run(worktree=wt, agent=Claude(wt.cwd), change=change, rounds=config.rounds, decide=decide)
        checks = resolve_checks(config_file, wt.cwd, validation.agent, approve, force=redetect)
        assessment = review_step(validation)
        checks_step(validation, checks)

        step(f"push {branch}")
        push(wt, branch)

        if host is None:
            print(f"unknown host for {remote_url} - pushed, open the PR yourself")
        else:
            step("pull request")
            open_pr(host, validation, assessment, branch, config.base, checks, wt.subjects(f"{start}..HEAD"))
            if config.ci_timeout_minutes > 0:
                ci_step(validation, host, branch, lambda: push(wt, branch), config.ci_timeout_minutes * 60)

        fixes = wt.subjects(f"{start}..HEAD")
        if fixes:
            print(f"\nship added {len(fixes)} commit(s) - run 'git pull --ff-only' on {branch}")


def open_pr(
    host: Host, validation: Run, assessment: Assessment, branch: str, base: str, checks: list[str], fixes: list[str]
) -> None:
    existing = host.find_open_pr(branch)
    if existing:
        print(f"PR already open: {existing}")
        return

    pr = validation.agent.write_pr(validation.change, fixes, checks)
    print(host.create_pr(branch, base, with_assessment(pr, assessment)))


def ticket_from_branch(branch: str) -> str | None:
    match = re.search(r"\d{3,}", branch)
    return match.group() if match else None


def confirm(question: str) -> bool:
    return input(f"{question} [y/N] ").strip().lower() == "y"


def accept(question: str) -> bool:
    return True


def prompt_decision(asks: Sequence[Finding]) -> Decision:
    answer = input(f"{len(asks)} ask-user finding(s): [f]ix them, [k]eep them as they are, [a]bort? ")
    match answer.strip().lower():
        case "f":
            return "fix"
        case "k":
            return "keep"
    raise ShipError("aborted")


def needs_decision(asks: Sequence[Finding]) -> Decision:
    raise NeedsDecision(f"{len(asks)} finding(s) need a human decision - rerun with --fix-asks or --keep-asks")


def _current_branch(repo: Git, config: Config) -> str:
    branch = repo.try_("symbolic-ref", "--short", "HEAD")
    if branch is None:
        raise ShipError("not on a branch")
    if branch == config.base:
        raise ShipError(f"you are on the base branch '{config.base}'")
    if not repo.is_clean():
        raise ShipError("uncommitted changes - commit them first")
    return branch

"""git ship - validate the current branch in a disposable worktree, then push and open a PR.

  review -> fix -> checks -> fix -> push -> PR (GitHub or Azure DevOps)

Per-repo config (all optional):
  git config --add ship.check "npm ci"     commands run in the worktree, in order
  git config --add ship.check "npm test"
  git config ship.base main                 default: origin/HEAD
  git config ship.rounds 3                  fix attempts per step, default 3
"""

import argparse
import re
import sys
from pathlib import Path

from .agent import Claude
from .config import Config, load_config
from .git import Git, worktree
from .hosts import detect_host
from .models import Change
from .pipeline import Ask, Run, checks_step, review_step, step
from .shell import ShipError, run


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="git ship", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("-y", "--yes", action="store_true", help="don't ask, abort on findings that need a human call")
    args = parser.parse_args()

    try:
        ship(refuse if args.yes else confirm)
    except ShipError as error:
        print(f"ship: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


def ship(ask: Ask) -> None:
    repo = Git(Path(run("git", "rev-parse", "--show-toplevel")))
    config = load_config(repo)
    branch = _current_branch(repo, config)

    repo("fetch", "-q", "origin", config.base)
    merge_base = repo("merge-base", "HEAD", f"origin/{config.base}")
    if not repo("rev-list", f"{merge_base}..HEAD"):
        raise ShipError(f"no commits ahead of origin/{config.base}")

    change = Change(diff_range=f"{merge_base}..HEAD", ticket=ticket_from_branch(branch))

    with worktree(repo) as wt:
        start = wt("rev-parse", "HEAD")
        validation = Run(worktree=wt, agent=Claude(wt.cwd), change=change, rounds=config.rounds, ask=ask)
        review_step(validation)
        checks_step(validation, config.checks)

        step(f"push {branch}")
        wt("push", "-q", "-u", "origin", f"HEAD:refs/heads/{branch}")
        fixes = wt.subjects(f"{start}..HEAD")
        if fixes:
            print(f"ship added {len(fixes)} commit(s) - run 'git pull --ff-only' on {branch}")

        step("pull request")
        open_pr(repo, validation, branch, config, fixes)


def open_pr(repo: Git, validation: Run, branch: str, config: Config, fixes: list[str]) -> None:
    remote_url = repo("remote", "get-url", "origin")
    host = detect_host(remote_url, repo.cwd)
    if host is None:
        print(f"unknown host for {remote_url} - pushed, open the PR yourself")
        return

    existing = host.find_open_pr(branch)
    if existing:
        print(f"PR already open: {existing}")
        return

    pr = validation.agent.write_pr(validation.change, fixes, config.checks)
    print(host.create_pr(branch, config.base, pr))


def ticket_from_branch(branch: str) -> str | None:
    match = re.search(r"\d{3,}", branch)
    return match.group() if match else None


def confirm(question: str) -> bool:
    return input(f"{question} [y/N] ").strip().lower() == "y"


def refuse(question: str) -> bool:
    raise ShipError(f"needs a human decision ({question}) - rerun without -y")


def _current_branch(repo: Git, config: Config) -> str:
    branch = repo.try_("symbolic-ref", "--short", "HEAD")
    if branch is None:
        raise ShipError("not on a branch")
    if branch == config.base:
        raise ShipError(f"you are on the base branch '{config.base}'")
    if not repo.is_clean():
        raise ShipError("uncommitted changes - commit them first")
    return branch

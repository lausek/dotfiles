"""git ship - validate the current branch in a disposable worktree, then push, open a PR and watch CI.

  resolve conflicts -> detect checks -> review -> checks -> push -> PR -> CI (GitHub or Azure DevOps)

When a step fixes something, the fix is committed and the pipeline starts over from the top.

On the first run, claude detects the local checks (install, lint, tests) and saves them to
.git/ship.json. They are detected again when the files they came from change.
"""

import argparse
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from .agent import Claude
from .awake import stay_awake
from .checks import Approve, resolve_checks
from .config import Config, config_path, load_config, read_config_file
from .git import Git, worktree
from .hosts import Host, detect_host
from .memory import Memory
from .models import Assessment, Change, Check, Detection, Finding, Ticket
from .pipeline import Answer, Decide, Run, Target, numbered, ship_pipeline
from .report import with_assessment
from .shell import NeedsDecision, ShipError, run
from .ui import PlainUi, Ui


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="git ship", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "-y", "--yes", action="store_true",
        help="don't ask: save detected checks right away, exit with 2 on findings that need a human decision",
    )
    parser.add_argument(
        "--answer", action="append", type=_answer, default=[], metavar="N=TEXT",
        help="what to do about the finding [ask-user #N], in your words, or `keep` to keep it as it is. Repeatable",
    )
    parser.add_argument(
        "--keep-asks", action="store_true", help="keep the findings that need a human decision and got no --answer",
    )
    parser.add_argument("--ticket", type=_ticket_id, help="GitHub issue or Azure DevOps work item the change implements")
    parser.add_argument(
        "--context", default="", metavar="TEXT",
        help="context for the review in your words, e.g. what to focus on. Pass the same on reruns",
    )
    parser.add_argument("--setup", action="store_true", help="detect the checks again")
    parser.add_argument(
        "--plain", action="store_true",
        help="log the steps as they happen instead of the live step list. Always the case off a terminal",
    )
    args = parser.parse_args()

    ui = _ui(args.plain)
    try:
        with stay_awake():
            ship(
                ui=ui,
                decide=_decider(args, ui),
                approve=accept if args.yes else lambda question: confirm(ui, question),
                redetect=args.setup,
                ticket_id=args.ticket,
                context=args.context.strip(),
            )
    except NeedsDecision as error:
        print(f"ship: {error}", file=sys.stderr)
        return 2
    except ShipError as error:
        print(f"ship: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


def _ui(plain: bool) -> Ui:
    """The live step list only on an interactive terminal. Agents like the /ship skill and anything else
    reading the output run without one and get the plain log."""
    if not plain and _interactive():
        from .tui import TerminalUi

        return TerminalUi()
    return PlainUi()


def _interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty() and os.environ.get("TERM") != "dumb"


def _decider(args: argparse.Namespace, ui: Ui) -> Decide:
    if args.answer or args.keep_asks:
        given = dict(args.answer)
        return lambda findings: given_answers(findings, given, args.keep_asks)
    if args.yes:
        return needs_decision
    return lambda findings: prompt_decision(ui, findings)


def _answer(value: str) -> tuple[int, str]:
    number, sep, text = value.partition("=")
    if not sep or not number.strip().isdigit() or not text.strip():
        raise argparse.ArgumentTypeError(f"expected N=TEXT, e.g. 1=migrate them, got {value!r}")
    return int(number), text.strip()


def ship(
    ui: Ui, decide: Decide, approve: Approve, redetect: bool, ticket_id: str | None, context: str
) -> None:
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

    # Three dots: the diff from where the branch last met the base, so a merge of the base doesn't show up in it.
    change = Change(
        diff_range=f"origin/{config.base}...HEAD", ticket=_fetch_ticket(host, ticket_id), context=context
    )

    memory = Memory(config_file.with_name("ship-state.json"), branch)
    memory.forget_other_branches(repo("for-each-ref", "--format=%(refname:short)", "refs/heads").splitlines())

    with worktree(repo) as wt:
        start = wt("rev-parse", "HEAD")
        validation = Run(
            worktree=wt, agent=Claude(wt.cwd), change=change, rounds=config.rounds, decide=decide, base=config.base,
            ui=ui, memory=memory,
        )
        target = Target(
            branch=branch,
            host=host,
            ci_timeout=config.ci_timeout_minutes * 60,
            resolve_checks=lambda: resolve_checks(config_file, wt.cwd, validation.agent, approve, redetect, ui),
            open_pr=lambda assessment: open_pr(ui, host, validation, assessment, branch, config.base),
        )
        try:
            with ui.session():
                shipped = ship_pipeline(validation, target)
        finally:
            # Also when it fails or you abort it, so a rerun goes on from the fixes instead of redoing them.
            # After the session, so it shows below the TUI's steps.
            kept_fixes = keep_fixes(repo, branch, start, wt("rev-parse", "HEAD"))
            if kept_fixes:
                print(f"\n{kept_fixes}")

        if host is None:
            print(f"unknown host for {remote_url} - pushed, open the PR yourself")
        warning = untested_warning(shipped.detection, host, config, shipped.ci_checks)
        if warning:
            print(f"\nwarning: {warning}", file=sys.stderr)


def keep_fixes(repo: Git, branch: str, start: str, head: str) -> str | None:
    """Puts the commits ship added in its worktree onto the branch. Only fast-forwards, and only while the
    branch is still checked out where ship started. Otherwise they go to `ship/<branch>`.
    Returns what it did, None if ship added nothing."""
    added = repo.subjects(f"{start}..{head}")
    if not added:
        return None
    summary = f"ship added {len(added)} commit(s)"
    unmoved = repo.try_("symbolic-ref", "--short", "HEAD") == branch and repo("rev-parse", "HEAD") == start
    if unmoved and repo.try_("merge", "--ff-only", "-q", head) is not None:
        return f"{summary} to {branch}"
    aside = f"ship/{branch}"
    repo("branch", "-f", aside, head)
    return (
        f"{summary}, but they can't go onto {branch}: it moved or uncommitted changes are in the way. "
        f"They are on {aside}, merge them yourself"
    )


def untested_warning(detection: Detection, host: Host | None, config: Config, ci_checks: list[Check]) -> str | None:
    """The tests were left to the PR's CI, but no CI result came back that shows they ran."""
    if not detection.tests_in_ci or ci_checks:
        return None
    if host is None:
        reason = "git-ship can't watch CI on this host"
    elif config.ci_timeout_minutes <= 0:
        reason = "watching CI is disabled (ci_timeout_minutes: 0)"
    else:
        reason = "no CI checks showed up on the PR"
    return f"the tests did not run - they are left to the PR's CI, but {reason}. Check that CI runs them."


def open_pr(ui: Ui, host: Host, validation: Run, assessment: Assessment, branch: str, base: str) -> str:
    """Returns the PR's link."""
    existing = host.find_open_pr(branch)
    if existing:
        ui.log(f"PR already open: {existing}")
        return existing

    ui.status("claude writes the description")
    pr = validation.agent.write_pr(validation.change)
    url = host.create_pr(branch, base, with_assessment(pr, assessment), validation.change.ticket)
    ui.log(url)
    return url


def _ticket_id(value: str) -> str:
    ticket_id = value.removeprefix("#")
    if not ticket_id.isdigit():
        raise argparse.ArgumentTypeError(f"expected a ticket number like 1234 or #1234, got '{value}'")
    return ticket_id


def _fetch_ticket(host: Host | None, ticket_id: str | None) -> Ticket | None:
    if ticket_id is None:
        return None
    if host is None:
        raise ShipError("--ticket needs a GitHub or Azure DevOps remote")
    return host.fetch_ticket(ticket_id)


def confirm(ui: Ui, question: str) -> bool:
    return ui.ask(f"{question} [y/N] ").strip().lower() == "y"


def accept(question: str) -> bool:
    return True


def prompt_decision(ui: Ui, findings: Sequence[Finding]) -> list[Answer]:
    """Asks about each ask-user finding in turn. The answer is free text, handed to the fixer as it is."""
    asks = [finding for finding in findings if finding.needs_user]
    # The findings come with each question: the TUI takes its step list down while it asks.
    details = numbered(findings)
    answers: list[Answer] = []
    for number, finding in enumerate(asks, 1):
        while True:
            reply = ui.ask(
                f"#{number} {finding.question or finding.summary} "
                "Answer in your words, or [k]eep as is, show [d]etails, [a]bort: ",
                details=details,
            ).strip()
            match reply.lower():
                case "":
                    continue
                case "k":
                    answers.append(None)
                case "a":
                    raise ShipError("aborted")
                case "d":
                    ui.log(finding.explained(number))
                    continue
                case _:
                    answers.append(reply)
            break
    return answers


def given_answers(findings: Sequence[Finding], given: dict[int, str], keep_rest: bool) -> list[Answer]:
    """The answers from the command line. `keep` keeps a finding, as does --keep-asks for those without one."""
    count = sum(finding.needs_user for finding in findings)
    unknown = sorted(set(given) - set(range(1, count + 1)))
    if unknown:
        raise ShipError(f"no finding [ask-user #{unknown[0]}], there are {count}")
    missing = [number for number in range(1, count + 1) if number not in given]
    if missing and not keep_rest:
        raise NeedsDecision(f"no answer for [ask-user {', '.join(f'#{n}' for n in missing)}] - add --answer or --keep-asks")
    texts = [given.get(number, "keep") for number in range(1, count + 1)]
    return [None if text.lower() == "keep" else text for text in texts]


def needs_decision(findings: Sequence[Finding]) -> list[Answer]:
    """Without a terminal, e.g. under the /ship skill: lists the questions in full for whoever relays them."""
    asks = [finding for finding in findings if finding.needs_user]
    questions = "\n\n".join(finding.explained(number) for number, finding in enumerate(asks, 1))
    raise NeedsDecision(
        f"{len(asks)} finding(s) need a human decision - rerun with --answer N=TEXT for each [ask-user #N], "
        f"TEXT being what to do in the author's words or `keep`:\n\n{questions}"
    )


def _current_branch(repo: Git, config: Config) -> str:
    branch = repo.try_("symbolic-ref", "--short", "HEAD")
    if branch is None:
        raise ShipError("not on a branch")
    if branch == config.base:
        raise ShipError(f"you are on the base branch '{config.base}'")
    if not repo.is_clean():
        raise ShipError("uncommitted changes - commit them first")
    return branch

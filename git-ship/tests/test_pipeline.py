import subprocess
import tempfile
import unittest
from collections.abc import Sequence
from pathlib import Path

from git_ship.cli import ticket_from_branch
from git_ship.git import Git, worktree
from git_ship.hosts import AzureDevOps, GitHub, detect_host
from git_ship.models import Change, Finding, PullRequest
from git_ship.pipeline import Run, checks_step, review_step, select_fixes
from git_ship.shell import ShipError

BUG = Finding(file="m.py", line=2, summary="off by one", needs_user=False)
DECISION = Finding(file="m.py", line=5, summary="should this raise?", needs_user=True)


class FakeAgent:
    """Returns scripted review rounds and fixes by writing a file."""

    def __init__(self, cwd: Path, reviews: list[list[Finding]]):
        self.cwd = cwd
        self.reviews = reviews
        self.kept_per_round: list[list[Finding]] = []
        self.fixes: list[str] = []

    def review(self, change: Change, kept: Sequence[Finding]) -> list[Finding]:
        self.kept_per_round.append(list(kept))
        return self.reviews.pop(0) if self.reviews else []

    def fix(self, instructions: str) -> None:
        self.fixes.append(instructions)
        (self.cwd / "fixed").write_text(str(len(self.fixes)))

    def write_pr(self, change: Change, fixes: Sequence[str], checks: Sequence[str]) -> PullRequest:
        return PullRequest(title="t", body="b")


def answer(*replies: bool):
    queue = list(replies)
    return lambda question: queue.pop(0)


class PipelineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Git(Path(self.tmp.name))
        self.repo("init", "-q")
        self.repo("config", "user.email", "test@example.com")
        self.repo("config", "user.name", "test")
        self.repo("config", "commit.gpgsign", "false")
        (self.repo.cwd / "a").write_text("a")
        self.repo("add", "-A")
        self.repo("commit", "-qm", "init")

    def tearDown(self):
        self.tmp.cleanup()

    def make_run(self, wt: Git, agent: FakeAgent, ask=answer()) -> Run:
        return Run(worktree=wt, agent=agent, change=Change("HEAD~1..HEAD", None), rounds=2, ask=ask)

    def test_auto_fixes_are_committed_and_rereviewed(self):
        with worktree(self.repo) as wt:
            agent = FakeAgent(wt.cwd, [[BUG]])
            review_step(self.make_run(wt, agent))
            self.assertEqual(wt.subjects("HEAD~1..HEAD"), ["fix: address review findings"])
            self.assertEqual(len(agent.kept_per_round), 2)

    def test_kept_findings_are_passed_to_next_review(self):
        with worktree(self.repo) as wt:
            agent = FakeAgent(wt.cwd, [[BUG, DECISION]])
            review_step(self.make_run(wt, agent, ask=answer(False, True)))
            self.assertEqual(agent.kept_per_round[1], [DECISION])
            self.assertNotIn("should this raise", agent.fixes[0])

    def test_gives_up_after_rounds(self):
        with worktree(self.repo) as wt:
            agent = FakeAgent(wt.cwd, [[BUG], [BUG], [BUG]])
            with self.assertRaisesRegex(ShipError, "after 2 fix rounds"):
                review_step(self.make_run(wt, agent))

    def test_failing_check_is_fixed_and_rerun(self):
        with worktree(self.repo) as wt:
            agent = FakeAgent(wt.cwd, [])
            checks_step(self.make_run(wt, agent), ["test -f fixed"])
            self.assertEqual(wt.subjects("HEAD~1..HEAD"), ["fix: make 'test -f fixed' pass"])

    def test_check_artifacts_are_not_committed(self):
        with worktree(self.repo) as wt:
            agent = FakeAgent(wt.cwd, [])
            checks_step(self.make_run(wt, agent), ["touch artifact; test -f fixed"])
            self.assertEqual(wt("show", "--name-only", "--format=", "HEAD"), "fixed")

    def test_worktree_is_removed(self):
        with worktree(self.repo) as wt:
            path = wt.cwd
        self.assertFalse(path.exists())
        self.assertNotIn(str(path), self.repo("worktree", "list"))


class SelectFixesTest(unittest.TestCase):
    def test_only_auto_fixes_without_asking(self):
        self.assertEqual(select_fixes([BUG], ask=answer()), [BUG])

    def test_user_can_include_decisions(self):
        self.assertEqual(select_fixes([BUG, DECISION], ask=answer(True)), [BUG, DECISION])

    def test_user_can_abort(self):
        with self.assertRaises(ShipError):
            select_fixes([DECISION], ask=answer(False, False))


class HelpersTest(unittest.TestCase):
    def test_ticket_from_branch(self):
        self.assertEqual(ticket_from_branch("feat/1234-avg"), "1234")
        self.assertIsNone(ticket_from_branch("fix/v2-typo"))

    def test_detect_host(self):
        cwd = Path(".")
        self.assertIsInstance(detect_host("git@github.com:a/b.git", cwd), GitHub)
        self.assertIsInstance(detect_host("https://org@dev.azure.com/org/p/_git/r", cwd), AzureDevOps)
        self.assertIsInstance(detect_host("git@ssh.dev.azure.com:v3/org/p/r", cwd), AzureDevOps)
        self.assertIsNone(detect_host("git@gitlab.com:a/b.git", cwd))


if __name__ == "__main__":
    unittest.main()

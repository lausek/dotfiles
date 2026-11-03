import json
import tempfile
import unittest
from collections.abc import Sequence
from pathlib import Path

from git_ship.checks import fingerprint, resolve_checks
from git_ship.cli import needs_decision, untested_warning
from git_ship.config import Config
from git_ship.git import Git, worktree
from git_ship.hosts import AzureDevOps, GitHub, detect_host
from git_ship.models import (
    Assessment,
    Change,
    Check,
    Detection,
    Finding,
    PullRequest,
    Review,
    Ticket,
)
from git_ship.pipeline import (
    Clock,
    Run,
    checks_step,
    ci_step,
    merge_step,
    review_step,
    select_fixes,
)
from git_ship.report import fit, with_assessment
from git_ship.shell import NeedsDecision, ShipError

BUG = Finding(file="m.py", line=2, summary="off by one", needs_user=False)
DECISION = Finding(file="m.py", line=5, summary="should this raise?", needs_user=True)


class FakeAgent:
    """Returns scripted review rounds and fixes by writing a file."""

    def __init__(self, cwd: Path, reviews: list[list[Finding]]):
        self.cwd = cwd
        self.reviews = reviews
        self.kept_per_round: list[list[Finding]] = []
        self.fixes: list[str] = []
        self.detections = 0

    def detect_checks(self) -> Detection:
        self.detections += 1
        return Detection(checks=["make test"], sources=["Makefile"])

    def review(self, change: Change, kept: Sequence[Finding]) -> Review:
        self.kept_per_round.append(list(kept))
        findings = self.reviews.pop(0) if self.reviews else []
        return Review(findings=findings, risk="medium", risk_rationale="touches m.py")

    def fix(self, instructions: str) -> None:
        self.fixes.append(instructions)
        (self.cwd / "fixed").write_text(str(len(self.fixes)))

    def write_pr(self, change: Change) -> PullRequest:
        return PullRequest(title="t", body="b")


class ResolvingAgent(FakeAgent):
    """Resolves conflicts by writing `resolution` to every file, or leaves them when it is None."""

    def __init__(self, cwd: Path, resolution: str | None):
        super().__init__(cwd, [])
        self.resolution = resolution

    def fix(self, instructions: str) -> None:
        self.fixes.append(instructions)
        if self.resolution is not None:
            for line in instructions.splitlines():
                if line.startswith("- "):
                    (self.cwd / line[2:]).write_text(self.resolution)


class FakeHost:
    """Reports scripted check states, one list per poll."""

    def __init__(self, polls: list[list[Check]], conflicts: bool = False):
        self.polls = polls
        self.conflicts = conflicts

    def fetch_ticket(self, ticket_id: str) -> Ticket:
        return Ticket(id=ticket_id, title="t", description="d")

    def find_open_pr(self, branch: str) -> str | None:
        return None

    def create_pr(self, branch: str, base: str, pr: PullRequest, ticket: Ticket | None) -> str:
        return "url"

    def has_conflicts(self, branch: str) -> bool:
        return self.conflicts

    def checks(self, branch: str, head: str) -> list[Check]:
        return self.polls.pop(0) if self.polls else []

    def failure_log(self, check: Check) -> str:
        return f"log of {check.name}"


class FakeClock(Clock):
    def __init__(self):
        self.time = 0.0
        super().__init__(now=lambda: self.time, sleep=self._sleep)

    def _sleep(self, seconds: float) -> None:
        self.time += seconds


def answer(decision=None):
    def decide(asks):
        if decision is None:
            raise AssertionError(f"unexpected decision on {asks}")
        return decision
    return decide


class PipelineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Git(Path(self.tmp.name))
        self.repo("init", "-q")
        self.repo("checkout", "-q", "-b", "main")
        self.repo("remote", "add", "origin", str(self.repo.cwd))
        self.repo("config", "user.email", "test@example.com")
        self.repo("config", "user.name", "test")
        self.repo("config", "commit.gpgsign", "false")
        (self.repo.cwd / "a").write_text("a")
        self.repo("add", "-A")
        self.repo("commit", "-qm", "init")

    def tearDown(self):
        self.tmp.cleanup()

    def make_run(self, wt: Git, agent: FakeAgent, decide=None) -> Run:
        change = Change("HEAD~1..HEAD", None)
        return Run(worktree=wt, agent=agent, change=change, rounds=2, decide=decide or answer(), base="main")

    def diverge(self, ours: str = "ours", theirs: str = "theirs"):
        """Puts the repo on a branch whose change to `a` conflicts with main's."""
        self.repo("checkout", "-q", "-b", "feature")
        (self.repo.cwd / "a").write_text(ours)
        self.repo("commit", "-qam", "ours")
        self.repo("checkout", "-q", "main")
        (self.repo.cwd / "a").write_text(theirs)
        (self.repo.cwd / "b").write_text("b")
        self.repo("add", "-A")
        self.repo("commit", "-qm", "theirs")
        self.repo("checkout", "-q", "feature")

    def test_auto_fixes_are_committed_and_rereviewed(self):
        with worktree(self.repo) as wt:
            agent = FakeAgent(wt.cwd, [[BUG]])
            review_step(self.make_run(wt, agent))
            self.assertEqual(wt.subjects("HEAD~1..HEAD"), ["fix: address review findings"])
            self.assertEqual(len(agent.kept_per_round), 2)

    def test_kept_findings_are_passed_to_next_review(self):
        with worktree(self.repo) as wt:
            agent = FakeAgent(wt.cwd, [[BUG, DECISION]])
            review_step(self.make_run(wt, agent, decide=answer("keep")))
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

    def test_review_returns_risk_and_kept_findings(self):
        with worktree(self.repo) as wt:
            agent = FakeAgent(wt.cwd, [[DECISION]])
            assessment = review_step(self.make_run(wt, agent, decide=answer("keep")))
            self.assertEqual(assessment, Assessment(risk="medium", risk_rationale="touches m.py", kept=[DECISION]))

    def test_ci_failure_is_fixed_and_published(self):
        pending = Check(name="build", state="pending", ref="")
        failed = Check(name="build", state="failed", ref="")
        passed = Check(name="build", state="passed", ref="")
        published = []
        with worktree(self.repo) as wt:
            agent = FakeAgent(wt.cwd, [])
            host = FakeHost([[pending], [failed], [], [passed]])
            ci_step(self.make_run(wt, agent), host, "b", lambda: published.append(True), 600, FakeClock())
            self.assertEqual(wt.subjects("HEAD~1..HEAD"), ["fix: make CI pass (build)"])
            self.assertIn("log of build", agent.fixes[0])
            self.assertEqual(published, [True])

    def test_ci_without_checks_is_skipped_after_grace(self):
        with worktree(self.repo) as wt:
            ci_step(self.make_run(wt, FakeAgent(wt.cwd, [])), FakeHost([]), "b", lambda: None, 600, FakeClock())

    def test_ci_times_out(self):
        pending = Check(name="build", state="pending", ref="")
        with worktree(self.repo) as wt:
            host = FakeHost([[pending]] * 100)
            with self.assertRaisesRegex(ShipError, "still running"):
                ci_step(self.make_run(wt, FakeAgent(wt.cwd, [])), host, "b", lambda: None, 600, FakeClock())

    def test_merge_step_leaves_a_branch_without_conflicts_alone(self):
        with worktree(self.repo) as wt:
            head = wt("rev-parse", "HEAD")
            self.assertFalse(merge_step(self.make_run(wt, FakeAgent(wt.cwd, []))))
            self.assertEqual(wt("rev-parse", "HEAD"), head)
            self.assertTrue(wt.is_clean())

    def test_merge_step_resolves_conflicts_in_a_merge_commit(self):
        self.diverge()
        with worktree(self.repo) as wt:
            agent = ResolvingAgent(wt.cwd, "both")
            self.assertTrue(merge_step(self.make_run(wt, agent)))
            self.assertIn("- a\n", agent.fixes[0])
            self.assertEqual(wt.subjects("HEAD~1..HEAD"), ["chore: merge origin/main and resolve conflicts"])
            self.assertEqual(wt("rev-parse", "HEAD^2"), wt("rev-parse", "main"))
            self.assertEqual((wt.cwd / "a").read_text(), "both")
            self.assertEqual((wt.cwd / "b").read_text(), "b")
            self.assertTrue(wt.is_clean())

    def test_merge_step_commits_a_resolution_without_content_changes(self):
        self.diverge()
        with worktree(self.repo) as wt:
            merge_step(self.make_run(wt, ResolvingAgent(wt.cwd, "ours")))
            self.assertEqual(wt("rev-parse", "HEAD^2"), wt("rev-parse", "main"))

    def test_merge_step_gives_up_on_leftover_markers(self):
        self.diverge()
        with worktree(self.repo) as wt:
            head = wt("rev-parse", "HEAD")
            agent = ResolvingAgent(wt.cwd, None)
            with self.assertRaisesRegex(ShipError, "still unresolved after 2 rounds: a"):
                merge_step(self.make_run(wt, agent))
            self.assertEqual(len(agent.fixes), 3)
            self.assertEqual(wt("rev-parse", "HEAD"), head)
            self.assertTrue(wt.is_clean())

    def test_merge_conflicts_fails_on_unknown_rev(self):
        with worktree(self.repo) as wt:
            with self.assertRaises(ShipError):
                wt.merge_conflicts("nope")
            self.assertTrue(wt.is_clean())

    def test_ci_resolves_conflicts_and_publishes(self):
        self.diverge()
        pending = Check(name="build", state="pending", ref="")
        passed = Check(name="build", state="passed", ref="")
        published = []
        with worktree(self.repo) as wt:
            host = FakeHost([[pending], [pending], [passed]], conflicts=True)
            publish = lambda: (published.append(True), setattr(host, "conflicts", False))
            ci_step(self.make_run(wt, ResolvingAgent(wt.cwd, "both")), host, "b", publish, 600, FakeClock())
            self.assertEqual(wt.subjects("HEAD~1..HEAD"), ["chore: merge origin/main and resolve conflicts"])
            self.assertEqual(published, [True])

    def test_ci_stops_when_host_conflicts_do_not_reproduce(self):
        pending = Check(name="build", state="pending", ref="")
        with worktree(self.repo) as wt:
            host = FakeHost([[pending]] * 100, conflicts=True)
            with self.assertRaisesRegex(ShipError, "merges cleanly here"):
                ci_step(self.make_run(wt, FakeAgent(wt.cwd, [])), host, "b", lambda: None, 600, FakeClock())

    def test_worktree_is_removed(self):
        with worktree(self.repo) as wt:
            path = wt.cwd
        self.assertFalse(path.exists())
        self.assertNotIn(str(path), self.repo("worktree", "list"))


class ResolveChecksTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.checkout = Path(self.tmp.name)
        self.config_file = self.checkout / "ship.json"
        (self.checkout / "Makefile").write_text("test:\n")
        self.agent = FakeAgent(self.checkout, [])

    def tearDown(self):
        self.tmp.cleanup()

    def resolve(self, approve=lambda q: True, force=False):
        return resolve_checks(self.config_file, self.checkout, self.agent, approve, force)

    def test_detects_and_saves_on_first_run(self):
        self.assertEqual(self.resolve().checks, ["make test"])
        saved = json.loads(self.config_file.read_text())
        self.assertEqual(saved["checks"], ["make test"])
        self.assertEqual(list(saved["sources"]), ["Makefile"])

    def test_uses_saved_checks_while_sources_are_unchanged(self):
        self.resolve()
        self.resolve()
        self.assertEqual(self.agent.detections, 1)

    def test_remembers_that_tests_are_left_to_ci(self):
        detection = Detection(checks=["make lint"], sources=["Makefile"], tests_in_ci=True)
        self.agent.detect_checks = lambda: detection
        self.resolve()
        self.agent.detect_checks = lambda: self.fail("detected again")
        self.assertEqual(self.resolve(), detection)

    def test_redetects_when_a_source_changes(self):
        self.resolve()
        (self.checkout / "Makefile").write_text("test:\n\tpytest\n")
        self.resolve()
        self.assertEqual(self.agent.detections, 2)

    def test_redetects_when_a_source_appears_or_a_directory_changes(self):
        self.agent.detect_checks = lambda: Detection(checks=["make test"], sources=["ci", "Makefile.local"])
        self.resolve()
        before = json.loads(self.config_file.read_text())["sources"]
        (self.checkout / "ci").mkdir()
        (self.checkout / "ci" / "pipeline.yml").write_text("steps: []")
        (self.checkout / "Makefile.local").write_text("")
        after = fingerprint(self.checkout, before)
        self.assertNotEqual(after["ci"], before["ci"])
        self.assertNotEqual(after["Makefile.local"], before["Makefile.local"])

    def test_redetects_when_the_detection_rules_changed(self):
        self.resolve()
        saved = json.loads(self.config_file.read_text())
        self.config_file.write_text(json.dumps(saved | {"detection": 1}))
        self.resolve()
        self.assertEqual(self.agent.detections, 2)

    def test_keeps_other_settings(self):
        self.config_file.write_text(json.dumps({"base": "develop"}))
        self.resolve()
        self.assertEqual(json.loads(self.config_file.read_text())["base"], "develop")

    def test_declining_saves_nothing(self):
        with self.assertRaises(ShipError):
            self.resolve(approve=lambda q: False)
        self.assertFalse(self.config_file.exists())


class SelectFixesTest(unittest.TestCase):
    def test_only_auto_fixes_without_asking(self):
        self.assertEqual(select_fixes([BUG], answer()), [BUG])

    def test_user_can_include_decisions(self):
        self.assertEqual(select_fixes([BUG, DECISION], answer("fix")), [BUG, DECISION])

    def test_user_can_keep_decisions(self):
        self.assertEqual(select_fixes([BUG, DECISION], answer("keep")), [BUG])

    def test_needs_decision_exits_with_2(self):
        with self.assertRaises(NeedsDecision):
            select_fixes([DECISION], needs_decision)


class HelpersTest(unittest.TestCase):
    def test_pr_body_starts_with_review_depth_and_ends_with_kept(self):
        pr = with_assessment(PullRequest("t", "body"), Assessment("high", "rewrites auth.", [DECISION]))
        self.assertTrue(pr.body.startswith("> 🚨 **High risk** - needs an in-depth review before merging. rewrites auth."))
        self.assertTrue(pr.body.endswith("- m.py:5 - should this raise?"))

    def test_warns_when_tests_left_to_ci_did_not_run(self):
        left_to_ci = Detection(checks=[], sources=[], tests_in_ci=True)
        watched, unwatched = Config("main", 3, 60), Config("main", 3, 0)
        github = GitHub(Path("."))
        passed = [Check("tests", "passed", "")]
        self.assertIsNone(untested_warning(left_to_ci, github, watched, passed))
        self.assertIsNone(untested_warning(Detection(checks=[], sources=[]), None, unwatched, []))
        self.assertIn("can't watch CI", untested_warning(left_to_ci, None, watched, []))
        self.assertIn("disabled", untested_warning(left_to_ci, github, unwatched, []))
        self.assertIn("no CI checks showed up", untested_warning(left_to_ci, github, watched, []))

    def test_fit_cuts_long_bodies_at_a_line_break(self):
        self.assertEqual(fit("short", 4000), "short")
        body = "\n".join(f"- finding {i}" for i in range(1000))
        cut = fit(body, 4000)
        self.assertLessEqual(len(cut), 4000)
        self.assertRegex(cut, r"- finding \d+\n\n\[\.\.\.\] cut")

    def test_detect_host(self):
        cwd = Path(".")
        self.assertIsInstance(detect_host("git@github.com:a/b.git", cwd), GitHub)
        self.assertIsInstance(detect_host("https://org@dev.azure.com/org/p/_git/r", cwd), AzureDevOps)
        self.assertIsInstance(detect_host("git@ssh.dev.azure.com:v3/org/p/r", cwd), AzureDevOps)
        self.assertIsNone(detect_host("git@gitlab.com:a/b.git", cwd))


if __name__ == "__main__":
    unittest.main()

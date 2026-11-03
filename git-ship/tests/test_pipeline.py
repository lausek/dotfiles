import importlib.util
import io
import json
import tempfile
import unittest
import unittest.mock
from collections.abc import Sequence
from pathlib import Path

from git_ship import shell
from git_ship.checks import fingerprint, resolve_checks
from git_ship.cli import keep_fixes, needs_decision, prompt_decision, untested_warning
from git_ship.config import Config
from git_ship.git import Git, worktree
from git_ship.hosts import AzureDevOps, GitHub, detect_host
from git_ship.memory import Memory
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
    Shipped,
    Target,
    ci_step,
    merge_step,
    plan,
    review_step,
    select_fixes,
    ship_pipeline,
)
from git_ship.report import fit, with_assessment
from git_ship.shell import NeedsDecision, ShipError
from git_ship.ui import PlainUi, PlannedStep, Progress

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
        return Run(
            worktree=wt, agent=agent, change=change, rounds=2, decide=decide or answer(), base="main", ui=PlainUi(),
            memory=self.memory(),
        )

    def memory(self) -> Memory:
        return Memory(self.repo.cwd / ".git" / "ship-state.json", "feature")

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

    def ship(self, wt: Git, agent: FakeAgent, checks=(), host=None, decide=None, open_pr=None) -> Shipped:
        target = Target(
            branch="b",
            host=host,
            ci_timeout=600,
            resolve_checks=lambda: Detection(checks=list(checks), sources=[]),
            open_pr=open_pr or (lambda assessment: "url"),
        )
        return ship_pipeline(self.make_run(wt, agent, decide), target, FakeClock())

    def test_auto_fixes_are_committed_and_reviewed_again(self):
        with worktree(self.repo) as wt:
            agent = FakeAgent(wt.cwd, [[BUG]])
            self.ship(wt, agent)
            self.assertEqual(wt.subjects("HEAD~1..HEAD"), ["fix: address review findings"])
            self.assertEqual(len(agent.kept_per_round), 2)
            self.assertEqual(self.repo("rev-parse", "b"), wt("rev-parse", "HEAD"))

    def test_kept_findings_are_passed_to_next_review(self):
        with worktree(self.repo) as wt:
            agent = FakeAgent(wt.cwd, [[BUG, DECISION]])
            self.ship(wt, agent, decide=answer("keep"))
            self.assertEqual(agent.kept_per_round[1], [DECISION])
            self.assertNotIn("should this raise", agent.fixes[0])

    def test_gives_up_after_rounds(self):
        with worktree(self.repo) as wt:
            agent = FakeAgent(wt.cwd, [[BUG], [BUG], [BUG]])
            with self.assertRaisesRegex(ShipError, "still findings after 2 fix rounds"):
                self.ship(wt, agent)
            self.assertEqual(len(agent.fixes), 2)

    def test_check_fix_is_reviewed_and_checked_again(self):
        with worktree(self.repo) as wt:
            agent = FakeAgent(wt.cwd, [])
            self.ship(wt, agent, checks=["true", "test -f fixed"])
            self.assertEqual(wt.subjects("HEAD~1..HEAD"), ["fix: make 'test -f fixed' pass"])
            self.assertEqual(len(agent.kept_per_round), 2)

    def test_check_artifacts_are_not_committed(self):
        with worktree(self.repo) as wt:
            agent = FakeAgent(wt.cwd, [])
            self.ship(wt, agent, checks=["touch artifact; test -f fixed"])
            self.assertEqual(wt("show", "--name-only", "--format=", "HEAD"), "fixed")

    def test_review_returns_risk_and_kept_findings(self):
        with worktree(self.repo) as wt:
            wt("fetch", "-q", "origin", "main")
            agent = FakeAgent(wt.cwd, [[DECISION]])
            assessment = review_step(self.make_run(wt, agent, decide=answer("keep")), [])
            self.assertEqual(assessment, Assessment(risk="medium", risk_rationale="touches m.py", kept=[DECISION]))

    def test_ci_fix_is_reviewed_and_pushed(self):
        pending = Check(name="build", state="pending", ref="")
        failed = Check(name="build", state="failed", ref="")
        passed = Check(name="build", state="passed", ref="")
        with worktree(self.repo) as wt:
            agent = FakeAgent(wt.cwd, [])
            host = FakeHost([[pending], [failed], [], [passed]])
            shipped = self.ship(wt, agent, host=host)
            self.assertEqual(shipped.ci_checks, [passed])
            self.assertEqual(wt.subjects("HEAD~1..HEAD"), ["fix: make CI pass (build)"])
            self.assertIn("log of build", agent.fixes[0])
            self.assertEqual(len(agent.kept_per_round), 2)
            self.assertEqual(self.repo("rev-parse", "b"), wt("rev-parse", "HEAD"))

    def test_ci_without_checks_is_skipped_after_grace(self):
        with worktree(self.repo) as wt:
            self.assertEqual(ci_step(self.make_run(wt, FakeAgent(wt.cwd, [])), FakeHost([]), "b", 600, FakeClock()), [])

    def test_ci_times_out(self):
        pending = Check(name="build", state="pending", ref="")
        with worktree(self.repo) as wt:
            host = FakeHost([[pending]] * 100)
            with self.assertRaisesRegex(ShipError, "still running"):
                ci_step(self.make_run(wt, FakeAgent(wt.cwd, [])), host, "b", 600, FakeClock())

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

    def test_conflicts_from_a_moved_base_are_resolved_from_the_top(self):
        pending = Check(name="build", state="pending", ref="")
        passed = Check(name="build", state="passed", ref="")
        with worktree(self.repo) as wt:
            host = FakeHost([[pending], [pending], [passed]], conflicts=True)
            opened = []

            def open_pr(assessment):
                if not opened:  # the base moves into a conflict while CI runs
                    self.repo("checkout", "-q", "main")
                    (self.repo.cwd / "a").write_text("theirs")
                    self.repo("commit", "-qam", "theirs")
                    self.repo("checkout", "-q", "-")
                else:
                    host.conflicts = False
                opened.append(True)
                return "url"

            agent = ResolvingAgent(wt.cwd, "both")
            (wt.cwd / "a").write_text("ours")
            wt("commit", "-qam", "ours")
            self.ship(wt, agent, host=host, open_pr=open_pr)
            self.assertEqual(wt.subjects("HEAD~1..HEAD"), ["chore: merge origin/main and resolve conflicts"])
            self.assertEqual(len(agent.kept_per_round), 2)
            self.assertEqual(self.repo("rev-parse", "b"), wt("rev-parse", "HEAD"))

    def test_stops_when_host_conflicts_do_not_reproduce(self):
        pending = Check(name="build", state="pending", ref="")
        with worktree(self.repo) as wt:
            host = FakeHost([[pending]] * 100, conflicts=True)
            with self.assertRaisesRegex(ShipError, "merges cleanly here"):
                self.ship(wt, FakeAgent(wt.cwd, []), host=host)

    def test_rerun_on_the_same_commit_reuses_the_review(self):
        with worktree(self.repo) as wt:
            agent = FakeAgent(wt.cwd, [[DECISION]])
            with self.assertRaisesRegex(ShipError, "aborted"):
                self.ship(wt, agent, decide=lambda asks: (_ for _ in ()).throw(ShipError("aborted")))
            self.ship(wt, agent, decide=answer("keep"))
            self.assertEqual(len(agent.kept_per_round), 1)

    def test_kept_findings_are_remembered_across_runs(self):
        with worktree(self.repo) as wt:
            self.ship(wt, FakeAgent(wt.cwd, [[DECISION]]), decide=answer("keep"))
            (wt.cwd / "new").write_text("new")
            wt("add", "-A")
            wt("commit", "-qm", "new")
            agent = FakeAgent(wt.cwd, [[DECISION]])
            self.ship(wt, agent)  # would fail on being asked again
            self.assertEqual(agent.kept_per_round, [[DECISION]])

    def test_fixes_go_onto_the_branch(self):
        self.repo("checkout", "-q", "-b", "feature")
        start = self.repo("rev-parse", "HEAD")
        with worktree(self.repo) as wt:
            (wt.cwd / "fix").write_text("fix")
            wt("add", "-A")
            wt("commit", "-qm", "fix: x")
            self.assertEqual(keep_fixes(self.repo, "feature", start, wt("rev-parse", "HEAD")), "ship added 1 commit(s) to feature")
            self.assertEqual(self.repo("rev-parse", "HEAD"), wt("rev-parse", "HEAD"))
            self.assertTrue((self.repo.cwd / "fix").exists())

    def test_fixes_go_aside_when_the_branch_moved(self):
        self.repo("checkout", "-q", "-b", "feature")
        start = self.repo("rev-parse", "HEAD")
        with worktree(self.repo) as wt:
            (wt.cwd / "fix").write_text("fix")
            wt("add", "-A")
            wt("commit", "-qm", "fix: x")
            (self.repo.cwd / "mine").write_text("mine")
            self.repo("add", "-A")
            self.repo("commit", "-qm", "mine")
            message = keep_fixes(self.repo, "feature", start, wt("rev-parse", "HEAD"))
            self.assertIn("they are on ship/feature", message.lower())
            self.assertEqual(self.repo("rev-parse", "ship/feature"), wt("rev-parse", "HEAD"))
            self.assertEqual(self.repo.subjects("HEAD~1..HEAD"), ["mine"])

    def test_nothing_to_keep_without_fixes(self):
        start = self.repo("rev-parse", "HEAD")
        self.assertIsNone(keep_fixes(self.repo, "main", start, start))

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
        return resolve_checks(self.config_file, self.checkout, self.agent, approve, force, PlainUi())

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



class ProgressTest(unittest.TestCase):
    def setUp(self):
        self.time = 0.0
        self.progress = Progress(now=lambda: self.time)
        self.progress.plan([PlannedStep("review", "review"), PlannedStep("checks", "checks"), PlannedStep("ci", "CI")])

    def states(self) -> list[tuple[str, str]]:
        return [(step.key, step.state) for step in self.progress.steps]

    def test_replanning_keeps_what_ran_and_replaces_what_is_ahead(self):
        self.progress.start("review", "review (round 1)")
        self.progress.plan([PlannedStep("review", "review"), PlannedStep("check:0", "check: make"), PlannedStep("ci", "CI")])
        self.assertEqual(self.states(), [("review", "running"), ("check:0", "pending"), ("ci", "pending")])
        self.assertEqual(self.progress.steps[0].title, "review (round 1)")

    def test_restart_runs_the_same_steps_from_the_top(self):
        self.progress.start("review", "review")
        self.progress.details("review", ["[auto-fix] m.py:2 - off by one"])
        self.progress.done("review", "risk: low")
        self.progress.start("checks", "checks")
        self.progress.restart(2, 3, "fixed 1 review finding(s)")
        self.assertEqual(self.states(), [("review", "pending"), ("checks", "pending"), ("ci", "pending")])
        self.assertEqual(self.progress.attempt, "attempt 2/3 - fixed 1 review finding(s)")
        self.assertEqual([(step.note, step.details, step.started) for step in self.progress.steps], [("", [], None)] * 3)

    def test_details_belong_to_a_step_until_it_starts_again(self):
        self.progress.start("review", "review")
        self.progress.details("review", ["no findings"])
        self.progress.done("review")
        self.assertEqual(self.progress.steps[0].details, ["no findings"])
        self.progress.start("review", "review")
        self.assertEqual(self.progress.steps[0].details, [])

    def test_status_goes_to_the_step_started_last(self):
        self.progress.start("ci", "CI (round 1)")
        self.time = 1
        self.progress.start("merge", "resolve merge conflicts")
        self.progress.status("claude resolves 1 conflicted file(s)")
        self.progress.done("merge")
        self.progress.status("waiting for CI")
        self.assertEqual(self.progress.steps[2].note, "waiting for CI")
        self.assertEqual(self.progress.steps[3].note, "")

    def test_failure_marks_running_steps(self):
        self.progress.start("review", "review (round 1)")
        self.progress.end_running("failed")
        self.assertEqual(self.states(), [("review", "failed"), ("checks", "pending"), ("ci", "pending")])

    def test_plan_lists_each_check_once_known(self):
        keys = [step.key for step in plan("main", "b", None)]
        self.assertEqual(keys, ["merge", "detect", "review", "checks", "push", "pr", "ci"])
        keys = [step.key for step in plan("main", "b", ["make lint", "make test"])]
        self.assertEqual(keys, ["merge", "detect", "review", "check:0", "check:1", "push", "pr", "ci"])



@unittest.skipUnless(importlib.util.find_spec("rich"), "rich is not installed")
class TerminalUiTest(unittest.TestCase):
    def render(self, ui) -> str:
        ui.console.print(ui._render())
        return ui.console.export_text()

    def make_ui(self):
        from rich.console import Console

        from git_ship.tui import TerminalUi

        ui = TerminalUi(Console(width=60, record=True, file=io.StringIO()))
        ui.plan(plan("main", "b", ["make lint"]))
        return ui

    def test_details_show_under_their_step_without_shifting_the_columns(self):
        ui = self.make_ui()
        ui.start("review", "review")
        ui.details("review", ["[auto-fix] " + "x" * 100, *[f"[auto-fix] {n}" for n in range(10)]])
        ui.done("review", "risk: low")
        lines = self.render(ui).splitlines()
        review = next(index for index, line in enumerate(lines) if "review" in line)
        self.assertIn("risk: low", lines[review])
        self.assertTrue(lines[review + 1].startswith("    [auto-fix] xxx"))
        self.assertIn("... 6 more", lines[review + 6])
        self.assertEqual(lines[review + 7].index("check"), lines[review].index("review"))
        self.assertTrue(all(len(line) <= 60 for line in lines))

    def test_wrapped_details_hang_under_their_text(self):
        ui = self.make_ui()
        ui.start("review", "review")
        ui.details("review", ["[auto-fix] " + "word " * 30, "[ask-user] short"])
        ui.done("review")
        ui.console.print(ui._render(all_details=True))
        lines = ui.console.export_text().splitlines()
        first = next(index for index, line in enumerate(lines) if "[auto-fix]" in line)
        text_column = lines[first].index("word")
        self.assertEqual(lines[first + 1][:text_column].strip(), "")
        self.assertEqual(lines[first + 1][text_column:text_column + 4], "word")
        self.assertTrue(any(line.startswith("    [ask-user] short") for line in lines))

    def test_counts_the_lines_a_wrapped_answer_takes(self):
        from git_ship.tui import _wrapped_lines

        self.assertEqual([_wrapped_lines(cells, 60) for cells in (0, 59, 60, 61, 121)], [1, 1, 1, 2, 3])

    def test_restart_shows_the_attempt_and_resets_the_steps(self):
        ui = self.make_ui()
        ui.start("review", "review")
        ui.restart(2, 4, "fixed 1 review finding(s)")
        text = self.render(ui)
        self.assertIn("↻ review", text)
        self.assertIn("attempt 2/4 - fixed 1 review finding(s)", text)
        self.assertIn("· review", text.split("attempt 2/4")[1])



class PromptDecisionTest(unittest.TestCase):
    def test_question_lists_the_findings(self):
        class Asking(PlainUi):
            def ask(self, question: str, details=()) -> str:
                self.question = question
                self.details = list(details)
                return "k"

        ui = Asking()
        self.assertEqual(prompt_decision(ui, [BUG, DECISION]), "keep")
        self.assertEqual(ui.details, ["[auto-fix] m.py:2 - off by one", "[ask-user] m.py:5 - should this raise?"])
        self.assertIn("1 finding(s) need your decision, the 1 auto-fix one(s) are fixed either way", ui.question)



class MemoryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "ship-state.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_remembers_the_last_review_of_a_commit(self):
        review = Review(findings=[BUG, DECISION], risk="low", risk_rationale="small")
        Memory(self.path, "a").remember_review("sha1", review)
        self.assertEqual(Memory(self.path, "a").review("sha1"), review)
        self.assertIsNone(Memory(self.path, "a").review("sha2"))
        self.assertIsNone(Memory(self.path, "b").review("sha1"))

    def test_remembers_kept_findings_per_branch(self):
        Memory(self.path, "a").remember_kept([DECISION])
        self.assertEqual(Memory(self.path, "a").kept(), [DECISION])
        self.assertEqual(Memory(self.path, "b").kept(), [])

    def test_forgets_branches_that_are_gone(self):
        Memory(self.path, "gone").remember_kept([DECISION])
        Memory(self.path, "a").forget_other_branches(["main"])
        self.assertEqual(Memory(self.path, "gone").kept(), [])

    def test_starts_from_scratch_on_a_broken_file(self):
        self.path.write_text("{nope")
        memory = Memory(self.path, "a")
        self.assertEqual(memory.kept(), [])
        memory.remember_kept([BUG])
        self.assertEqual(memory.kept(), [BUG])
        self.path.write_text(json.dumps({"version": 1, "branches": {"a": {"kept": [{"file": 1}], "review": {"key": "k"}}}}))
        self.assertEqual(memory.kept(), [])
        self.assertIsNone(memory.review("k"))



class ReadOnlyTest(unittest.TestCase):
    def test_allows_reading_the_file_and_its_symlink_target(self):
        from git_ship.agent import _read_only

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "real.md"
            target.write_text("x")
            link = Path(tmp) / "OPINIONS.md"
            link.symlink_to(target)
            self.assertEqual(_read_only(link), f"Read(/{link}),Read(/{target.resolve()})")
            self.assertEqual(_read_only(target), f"Read(/{target.resolve()})")


class RetryTest(unittest.TestCase):
    """A command that fails the first `fails` times with `error`, counting its attempts in a file."""

    def _flaky(self, tmp: Path, fails: int, error: str) -> tuple[str, Path]:
        attempts = tmp / "attempts"
        script = (
            f"n=$(( $(cat {attempts} 2>/dev/null || echo 0) + 1 )); echo $n > {attempts}; "
            f"[ $n -gt {fails} ] && echo ok && exit 0; echo '{error}' >&2; exit 128"
        )
        return script, attempts

    def setUp(self) -> None:
        patcher = unittest.mock.patch.object(shell, "RETRY_DELAYS", (0, 0, 0))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_retries_a_connection_dropped_before_the_handshake(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            script, attempts = self._flaky(Path(tmp), 2, "kex_exchange_identification: read: Connection reset by peer")
            self.assertEqual(shell.run("sh", "-c", script), "ok")
            self.assertEqual(attempts.read_text().strip(), "3")

    def test_gives_up_after_the_last_retry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            script, attempts = self._flaky(Path(tmp), 9, "Connection reset by 20.166.41.2 port 22")
            self.assertIsNone(shell.try_run("sh", "-c", script))
            self.assertEqual(attempts.read_text().strip(), "4")

    def test_does_not_retry_other_failures(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            script, attempts = self._flaky(Path(tmp), 1, "! [rejected] main -> main (non-fast-forward)")
            with self.assertRaises(ShipError):
                shell.run("sh", "-c", script)
            self.assertEqual(attempts.read_text().strip(), "1")


if __name__ == "__main__":
    unittest.main()

# git-ship

Validates the current branch in a disposable worktree, then pushes it, opens a PR on GitHub or Azure DevOps and watches CI.

```
resolve conflicts -> review -> checks -> push -> PR -> CI
```

Whenever a step fixes something, the fix is committed and the pipeline starts over from the top. So every
commit git-ship pushes was reviewed and checked, including fixes for review findings, failing checks and CI.

- The review runs in a fresh `claude -p` session that can only read. Findings are `auto-fix` (fixed and committed) or `ask-user` (you decide). Each shows as one short line you can decide on without opening the code. An `ask-user` finding ends with a question, you answer it in your own words and the fixer does what you said. `[d]etails` in the prompt shows the evidence and the suggested fix.
- Each commit is reviewed once. The first review covers the full change and has to report everything in one go.
  After that, the review only looks at commits added since and only reports problems they bring in: the fixes of
  its findings, of failing checks or CI, a merge that resolved conflicts, new commits on a rerun. A rerun without
  new commits doesn't review at all. If the reviewed commit is no longer part of the branch (rebase, amend) or the
  ticket changed, the full change is reviewed again.
- The review never fails the run. If it still has findings when the fix rounds are used up, they are kept without
  asking and listed in the PR.
- Findings you choose to keep are passed to the next reviews, so they aren't reported again. This holds across runs too.
- The commits git-ship adds go onto your local branch, also when it fails or you abort it. A rerun goes on from there.
  If the branch moved in the meantime or uncommitted changes are in the way, they go to `ship/<branch>` instead.
- A rerun on the same commit reuses the last review instead of reviewing again, so you decide about the same findings.
- If `~/.claude/OPINIONS.md` exists, the reviewer reads it and reports code that goes against those opinions as `ask-user`.
- The review also rates how carefully a human has to look at the PR (low / medium / high risk). That rating heads the PR description.
- A failing check is handed to Claude to fix.
- CI of the PR is watched. Failing checks are handed to Claude with their logs.
- The machine doesn't go to sleep while shipping, the same way `claude-awake` does it (`systemd-inhibit`).
- Pushes never force. If you committed in the meantime, the push fails and nothing is lost.
- PRs with merge conflicts get no CI. If the branch conflicts with the base, git-ship merges the base in and Claude resolves the conflicts (markers in diff3 style, so it sees the common ancestor). The merge commit then goes through the review and the checks like any other change. If the base moves into a conflict while CI is watched, the pipeline starts over and resolves it the same way. A branch that merges cleanly is left as it is.

## Install

The dotfiles' `install` script installs it (`uv tool install -e`).

## Usage

```sh
git ship                # asks about each finding that needs a human decision, you answer in your words
git ship -y             # never asks, exits with 2 on those instead and lists their questions
git ship -y --answer "1=migrate the old events" --answer 2=keep  # answers [ask-user #1] and #2
git ship -y --keep-asks # keep them as they are, they are listed in the PR
git ship --ticket 1234  # the change implements this issue / work item
git ship --plain        # log the steps instead of the live step list
```

On a terminal, git-ship shows every step ahead as a list pinned to the bottom and updates it in place:
which step runs, what it does right now and how long each took. The review's findings show under the review.
When the pipeline starts over, the finished attempt stays in the scrollback and the list runs from the top again.
Without a terminal, e.g. when an agent like the `/ship` skill runs it or the output is piped, it logs
the steps as they happen instead. No setting needed, the same check git uses for its pager.

`--ticket` hands the GitHub issue or Azure DevOps work item to the reviewer, which then also checks the
change against it, and links it to the PR so merging closes it.

Exit codes: `0` shipped, `1` failed, `2` needs a human decision. The `/ship` Claude Code skill
(`claude/skills/ship`) drives git-ship with these and asks you about the decisions.

No setup needed. On the first run, Claude detects the local checks (install, lint, typecheck, unit tests)
from package.json, Makefile, CI pipelines etc. and saves them to `.git/ship.json`, together with a hash of
the files it derived them from. When one of those files changes, the checks are detected again.
Tests only run locally if no CI pipeline runs them on PRs. Otherwise the PR's CI runs them and failures
are fixed from its logs. If no CI result comes back (unknown host, `ci_timeout_minutes: 0`, no checks on
the PR), git-ship warns that the tests did not run.
If the detected commands differ from the saved ones, you are asked before they are saved (`-y` saves right away).

```sh
git ship --setup    # detect the checks again
```

`.git/ship.json` is per clone and never committed. What git-ship remembers between runs (the last review and the findings
you kept, per branch) is in `.git/ship-state.json` next to it. Delete that to start from scratch.

You can edit `.git/ship.json`, optional keys:

```json
{
  "checks": ["npm ci", "npm run lint", "npm test"],
  "sources": {"package.json": "<sha256>"},
  "detection": 2,
  "base": "main",
  "rounds": 3,
  "ci_timeout_minutes": 60
}
```

- `base`: default `origin/HEAD`
- `rounds`: how often the pipeline may fix something and start over, default 3
- `ci_timeout_minutes`: how long to wait for CI per round, `0` disables watching CI, default 60

## Develop

```sh
uv run python -m unittest discover -s tests
```

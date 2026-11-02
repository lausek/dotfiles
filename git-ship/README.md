# git-ship

Validates the current branch in a disposable worktree, then pushes it, opens a PR on GitHub or Azure DevOps and watches CI.

```
review -> fix -> checks -> fix -> push -> PR -> CI -> fix
```

- The review runs in a fresh `claude -p` session that can only read. Findings are `auto-fix` (fixed and committed) or `ask-user` (you decide).
- Findings you choose to keep are passed to the next review round, so they aren't reported again.
- The review also rates how carefully a human has to look at the PR (low / medium / high risk). That rating heads the PR description.
- A failing check is handed to Claude to fix, then rerun.
- CI of the PR is watched. Failing checks are handed to Claude with their logs, fixed, pushed and watched again.
- The machine doesn't go to sleep while shipping, the same way `claude-awake` does it (`systemd-inhibit`).
- Pushes never force. If you committed in the meantime, the push fails and nothing is lost.

## Install

The dotfiles' `install` script installs it (`uv tool install -e`).

## Usage

```sh
git ship                # asks about findings that need a human decision
git ship -y             # never asks, exits with 2 on those instead
git ship -y --fix-asks  # fix them too
git ship -y --keep-asks # keep them as they are, they are listed in the PR
git ship --ticket 1234  # the change implements this issue / work item
```

`--ticket` hands the GitHub issue or Azure DevOps work item to the reviewer, which then also checks the
change against it, and links it to the PR so merging closes it.

Exit codes: `0` shipped, `1` failed, `2` needs a human decision. The `/ship` Claude Code skill
(`claude/skills/ship`) drives git-ship with these and asks you about the decisions.

No setup needed. On the first run, Claude detects the local checks (install, lint, typecheck, unit tests)
from package.json, Makefile, CI pipelines etc. and saves them to `.git/ship.json`, together with a hash of
the files it derived them from. When one of those files changes, the checks are detected again.
If the detected commands differ from the saved ones, you are asked before they are saved (`-y` saves right away).

```sh
git ship --setup    # detect the checks again
```

`.git/ship.json` is per clone and never committed. You can edit it, optional keys:

```json
{
  "checks": ["npm ci", "npm run lint", "npm test"],
  "sources": {"package.json": "<sha256>"},
  "base": "main",
  "rounds": 3,
  "ci_timeout_minutes": 60
}
```

- `base`: default `origin/HEAD`
- `rounds`: fix attempts per step, default 3
- `ci_timeout_minutes`: how long to wait for CI per round, `0` disables watching CI, default 60

## Develop

```sh
PYTHONPATH=src python3 -m unittest discover -s tests
```

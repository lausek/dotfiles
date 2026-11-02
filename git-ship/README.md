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

```sh
uv tool install -e ~/dev/lausek/dotfiles/git-ship
```

## Usage

```sh
git ship        # asks about findings that need a human call
git ship -y     # never asks, aborts on those instead
```

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

# git-ship

Validates the current branch in a disposable worktree, then pushes it and opens a PR on GitHub or Azure DevOps.

```
review -> fix -> checks -> fix -> push -> PR
```

- The review runs in a fresh `claude -p` session that can only read. Findings are `auto-fix` (fixed and committed) or `ask-user` (you decide).
- Findings you choose to keep are passed to the next review round, so they aren't reported again.
- A failing check is handed to Claude to fix, then rerun.
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

Per-repo config:

```sh
git config --add ship.check "npm ci"     # the worktree starts without dependencies
git config --add ship.check "npm test"
git config ship.base main                 # default: origin/HEAD
git config ship.rounds 3                  # fix attempts per step, default 3
```

## Develop

```sh
PYTHONPATH=src python3 -m unittest discover -s tests
```

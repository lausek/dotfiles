---
name: ship
description: Validate and ship a branch with git-ship (independent review, local checks, push, PR, CI fixes). Use when the user says "ship", "/ship", "ship it", "open a PR", or wants a task implemented and shipped.
argument-hint: "[task]"
---

# Ship

`git ship` validates the current branch in a disposable worktree: an independent Claude session reviews and fixes it, local checks run, then it pushes, opens a PR (GitHub or Azure DevOps) and fixes failing CI. Run `git ship --help` if you need details.

## 1. Prepare the branch

With a task argument:
- If the task references a ticket, fetch it the way the `fix-ticket` skill does.
- Create a branch from the up-to-date base branch, named `<type>/<ticket>-<slug>`, e.g. `feat/1234-date-parsing`. Without a ticket: `<type>/<slug>`.
- Implement the task and commit it.

Without a task argument, ship the current branch as it is:
- If there are uncommitted changes, ask the user whether to commit them. Don't commit on your own.
- If the current branch is the base branch, stop and ask for a branch name.

## 2. Run git-ship

```bash
git ship -y
```

- Always run it with `run_in_background: true`. Waiting for CI can take up to an hour. You get notified when it exits, don't poll.
- Don't review or fix the change yourself, before or after. git-ship validates in sessions that didn't write the code, that's the point.
- If `git-ship` is not installed, tell the user to run `uv tool install -e ~/dev/lausek/dotfiles/git-ship`.

## 3. Act on the exit code

**0 - shipped.** Report:
- the PR link and the risk line (`risk: low|medium|high - ...` from the last review round)
- the checks it detected, if a `detect checks` section shows up in the output (they were saved to `.git/ship.json`)
- the commits git-ship added. If it added any, run `git pull --ff-only` on the branch so the local branch has them.

**2 - needs a human decision.** The review found `[ask-user]` findings and stopped. Show the user these findings, then use AskUserQuestion:
- "Fix them" -> rerun `git ship -y --fix-asks` in the background
- "Keep them as they are" -> rerun `git ship -y --keep-asks` in the background

The rerun reviews again from scratch. If it stops with exit 2 once more, ask again. Never pick an option for the user.

**1 - failed.** Report the error from the output and stop. Don't retry or work around it on your own. Common cases:
- "still findings after N fix rounds" / "still failing after N fix rounds": the fixer couldn't solve it. Show the remaining findings or failure.
- "CI still running after N min": CI is slow, not broken. The PR exists, the user can rerun `/ship` later.
- a rejected push: the remote branch moved. Suggest pulling and rerunning.

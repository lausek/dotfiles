---
name: ship
description: Validate and ship a branch with git-ship (independent review, local checks, push, PR, CI fixes). Use when the user says "ship", "/ship", "ship it", "open a PR", or wants a task implemented and shipped.
argument-hint: "[task]"
---

# Ship

`git ship` validates the current branch in a disposable worktree: an independent Claude session reviews and fixes it, local checks run, then it pushes, opens a PR (GitHub or Azure DevOps) and fixes failing CI. Run `git ship --help` if you need details.

## 1. Prepare the branch

With a task argument, implement it following the git workflow from your global instructions (fresh base, worktree, `<type>/<summary>` branch, ticket read first), then commit it.

Without a task argument, ship the current branch as it is:
- If there are uncommitted changes, ask the user whether to commit them. Don't commit on your own.
- If the current branch is the base branch, stop and ask for a branch name.

## 2. Run git-ship

```bash
git ship -y [--ticket <n>]
```

Pass `--ticket` when the change implements a GitHub issue or Azure DevOps work item. git-ship gives it to the reviewer, which then also checks the change against it, and links it to the PR so merging closes it.

- Always run it with `run_in_background: true`. Waiting for CI can take up to an hour. You get notified when it exits, don't poll.
- Don't review or fix the change yourself, before or after. git-ship validates in sessions that didn't write the code, that's the point.
- If `git-ship` is not installed, tell the user to run `install` from their dotfiles repo.

## 3. Act on the exit code

**0 - shipped.** Report:
- the PR link and the risk line (`risk: low|medium|high - ...` from the last review)
- the checks it detected, if a `detect checks` section shows up in the output (they were saved to `.git/ship.json`)
- any `warning:` line, verbatim. "the tests did not run" means nothing tested the change.
- the commits git-ship added, including a `chore: merge origin/<base> and resolve conflicts` if it resolved conflicts. Ask the user to look at that resolution in the PR. git-ship puts them onto the local branch itself. If it says they are on `ship/<branch>` instead, tell the user, they have to merge them.

**2 - needs a human decision.** The review found `[ask-user #N]` findings and stopped. The error lists each with its question (after the `→`), the location and the detail. Ask the user about each finding with AskUserQuestion, up to 4 per call, one question per finding:
- the question: the finding's question as git-ship wrote it, with the short summary as context. Use the detail only to make the options concrete, the user decides without reading code.
- the options: the choices the question names, then "Keep as is". The user can always answer in their own words via "Other".

Then rerun in the background with one `--answer N=<answer>` per finding: the chosen option or the user's own words as they wrote them, `keep` for "Keep as is". Quote each argument for the shell.

The rerun reuses the review it stopped on, so the numbers refer to the same findings. If it stops with exit 2 once more, ask again. Never answer for the user.

**1 - failed.** Report the error from the output and stop. Don't retry or work around it on your own. Common cases:
- "still findings after N fix rounds" / "still failing after N fix rounds": the fixer couldn't solve it. Show the remaining findings or failure.
- "CI still running after N min": CI is slow, not broken. The PR exists, the user can rerun `/ship` later.
- a rejected push: the remote branch moved. Suggest pulling and rerunning.
- "merge conflicts ... still unresolved": git-ship merges the base in and resolves conflicts on its own, this means it couldn't. Show the files and the error.
- "reports merge conflicts, but ... merges cleanly here": the host and git disagree. Show the PR link and stop.

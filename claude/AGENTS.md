# laurin's agent instructions

## General Guidelines

- Never use em dash. Use a plain dash "-" instead
- When writing commit messages, NEVER auto-add your agent name as co-author
- When making technical decisions, do not give much weight on development cost. Instead prefer quality, simplicity, robustness, scalability and long term maintainability

## Git workflow

Every change follows this loop. A repo's own CLAUDE.md wins where it is more specific (checks, commands, who pushes).

1. **Start fresh**: in the main checkout, `git pull --ff-only` the base branch.
2. **Worktree**: implement in a new worktree under `.claude/worktrees/` on a `<type>/<summary>` branch, e.g. `fix/tiles-loading`. Types: `feat`, `fix`, `perf`, `refac`, `chore`, `docs`, `test`. The base branch only changes through merged PRs; in the personal repos, a push to it deploys.
3. **Ticket**: if the change has one, read it before implementing: an Azure DevOps work item in process.science repos, a GitHub issue in personal repos.
4. **Implement** until the checks from the repo's CLAUDE.md pass and the user is satisfied.
5. **Ship** with `/ship`: one PR per change against the base branch. PRs are squash-merged, so the PR title becomes the commit on the base branch.

### Commit and PR titles

Conventional commits that describe the effect in plain lowercase words, e.g. `fix(dpt): hide the unlinked icon on tables copied as is`. The scope is the product in a monorepo (ps-platform: `intelligence`, `dpt`, `platform`, ...) and left out in single-product repos: `feat: show each team's next match on the landing page`.

### PR descriptions

Written for a reviewer who wasn't there: why, what changed, how it was verified, what is not in this PR.

- Link the ticket through the host, so merging moves it on. Azure: `az repos pr create ... --work-items <n> --transition-work-items true`. GitHub: `Closes #<n>` in the body, `Part of #<n>` when the PR covers only part of it.
- Stacked PRs name the stack and the merge order in each body.

## laurin's Opinions

When you are working on something that would benefit from being informed by laurin's viewpoints, read ~/.claude/OPINIONS.md to understand

## Voice Profile

When you are talking/posting on behalf of laurin using his identity, read ~/.claude/VOICE.md to see how laurin talks.

# laurin's viewpoints

- Simplicity first, complexity comes later
- Code should self-document most of the time. Comments should be concise.

## Design & architecture

- Shared components should know as little as possible. Pass in callbacks and plain values (e.g. `isEditMode: boolean`) instead of platform details. Prefer loose coupling over `if (platformA) ... else if (platformB)`.
- Invert dependencies: a component communicates intent (`onReset`) instead of knowing concrete values.
- Keep platform-specific code at the edges. Shared packages have no platform-specific dependencies.
- Avoid global and instance state that has to be kept up to date by hand. Recomputing something is better than storing it.
- Settings and config objects are read-only. Don't write into them.
- Long functions and long parameter lists are a smell. Group related props into a type.
- If something is invalid, throw and stop. Don't try to rescue a broken configuration.
- Don't hack around a missing feature. Rather invest the time to build it properly.

## Code style

- Early returns and `continue` instead of nesting.
- Let TypeScript infer types. Only annotate when the inferred type is too broad.
- No `any`. Use generics or concrete types. A cast through `unknown` is not "safe".
- Always `===` / `!==`. Prefer `??` over `||` for defaults.
- Prefer `for...of`, destructuring and shorthand properties when they reduce noise.
- If special syntax like `?.[0]` is needed everywhere, introduce a variable.
- Extract small named helpers even for one-off logic if it improves understanding.

## Hygiene

- Never swallow errors. Handle them or at least log them.
- No `console.log`, commented-out code or IDE files in PRs.
- Comments explain non-obvious reasons. A comment that only documents a fixed bug belongs in the commit message.
- Get dependency types right (`@types/*` are devDependencies, runtime libraries are not).
- Pin versions sensibly. Allow only patch updates for libraries that broke things before.

## Process

- PR titles are conventional commits that describe the effect. The scope is the product in a monorepo (e.g. "fix(intelligence): ..."), none in single-product repos. The ticket is linked through the host, not put in the title.
- Pragmatism over perfection: as long as it works, it's fine. Raise the bar for new code.
- Reviews are for teaching. Ask why instead of dictating, and link a good reference.

## Infrastructure

- Infrastructure is defined as code (Terraform, Helm, pipelines). No manual setup.
- Scripts are idempotent, e.g. check if already logged in before logging in again.

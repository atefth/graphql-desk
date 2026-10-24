# Agent instructions

## Project workflow

- Read the relevant issue, acceptance criteria, and nearby docs or code before changing files.
- Keep changes focused on the assigned issue. Prefer existing project code and maintained open source libraries over new implementations; check compatibility and licensing before adding a dependency.
- Before parallel work, agree on file ownership and interfaces. Do not have agents edit the same files at the same time.
- Keep generated files, caches, and local test artifacts out of commits unless the issue explicitly requires them.
- Never commit secrets, credentials, or private user data.

## Worktrees

- Use the primary checkout by default. Create a worktree only when isolation or genuinely parallel work needs it, and follow any more specific user instruction.
- Keep at most **five active worktrees for this repository at a time**, including the primary checkout. Count existing worktrees before creating one. If the limit is reached, reuse an existing checkout or sequence the work.
- Remove a temporary worktree when its task is complete. After its changes are safely committed or preserved, remove the checkout and prune stale registrations from the primary checkout.
- Before cleanup, check tracked, untracked, and ignored files. Never discard uncommitted work or user data: preserve it and verify the backup before removing the checkout. Remove only worktrees created for the task.

## Changes and pull requests

- Use issue-linked pull requests sized as coherent, reviewable slices. Bundle related foundation work when it can be reviewed together; split unrelated work or changes with conflicting ownership.
- Run the focused checks for changed behavior and report what was and was not verified. Do not claim a feature or parity gate passed without evidence for its acceptance criteria.
- Use Ponytail for implementation and review. Fix review findings before publishing a pull request; keep CI green and follow repository branch protections.
- Update the linked issue when a pull request completes. Close an issue only when all of its acceptance criteria are met; otherwise record the partial progress and leave it open.
- Keep documentation and issue status aligned with the behavior that is actually implemented and verified.

# Agent instructions

## Project workflow

- Read the relevant issue, acceptance criteria, and nearby docs or code before changing files.
- Use the primary checkout on the checked-out `main` branch as the default workspace. Pull from `origin/main` before starting a task and refresh it regularly, especially between tasks and before opening a pull request. Inspect and preserve local changes before pulling; never overwrite user work to update the branch.
- Keep changes focused on the assigned issue. Prefer existing project code and maintained open source libraries over new implementations; check compatibility and licensing before adding a dependency.
- Before parallel work, agree on file ownership and interfaces. Do not have agents edit the same files at the same time.
- Keep generated files, caches, and local test artifacts out of commits unless the issue explicitly requires them.
- Never commit secrets, credentials, or private user data.

## Worktrees

- Use worktrees only when they enable independent tasks to run in parallel. Otherwise, work in the primary checkout. Follow any more specific user instruction.
- Keep every temporary worktree under the primary checkout's `.agent-worktrees` directory, never in system temp, sibling projects, or another drive.
- Keep at most **five active worktrees for this repository at a time**, including the primary checkout. Count existing worktrees before creating one. If the limit is reached, reuse an existing checkout or sequence the work.
- Remove a temporary worktree when its task is complete. After its changes are safely committed or preserved, remove the checkout and prune stale registrations from the primary checkout.
- Before cleanup, check tracked, untracked, and ignored files. Never discard uncommitted work or user data: preserve it and verify the backup before removing the checkout. Remove only worktrees created for the task.

## Local storage

- Keep task temp files, caches, and editor profiles inside the primary repository directory, including when working in a linked worktree. Redirect `TEMP`/`TMP` and tool-specific caches such as npm, uv, and pip for the current process where supported; do not change global user settings.
- Redirect managed Python installations with `UV_PYTHON_INSTALL_DIR` and installed uv tools with `UV_TOOL_DIR`/`UV_TOOL_BIN_DIR` into `.graphql-desk-local/tools` before running uv. `TEMP` and `UV_CACHE_DIR` do not redirect those installations. Check a reused environment's `pyvenv.cfg` and `sys.base_prefix`: an environment on E: can still use a base interpreter on C:. Reuse a verified project-local base for task-owned environments; preserve unrelated global installations until their users are known.
- Reuse shared pinned tool caches when possible to avoid duplicate downloads. After success or failure, remove only task-created artifacts and worktree-specific caches after verifying their resolved paths. Preserve tracked, untracked, and ignored user data, and do not follow or delete junction targets.
- These repository instructions cannot prevent operating-system or application components from making writes outside the repository; inspect configured paths and report any such limitation honestly.

## Changes and pull requests

- Use issue-linked pull requests sized as coherent, reviewable slices. Bundle related foundation work when it can be reviewed together; split unrelated work or changes with conflicting ownership.
- Run the focused checks for changed behavior and report what was and was not verified. Do not claim a feature or parity gate passed without evidence for its acceptance criteria.
- Use Ponytail for implementation and review. Fix review findings before publishing a pull request; keep CI green and follow repository branch protections.
- Update the linked issue when a pull request completes. Close an issue only when all of its acceptance criteria are met; otherwise record the partial progress and leave it open.
- Keep documentation and issue status aligned with the behavior that is actually implemented and verified.

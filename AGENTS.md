# Agent Info

- Read the documentation in `docs/_build/html` for a high level understanding of `otto` (if they don't exist, run `make docs`).
- Consider scalability and maintainability whenever possible.
- If the user reports a bug, reproduce it with unit/integration tests first, then fix it, then run the tests again to prove it is fixed.
- Do not use threads combined with asyncio. also, an event loop is guaranteed within the Typer subcommands (e.g. run, test, monitor)
- `ty` is the type checker available here — run `make typecheck` to verify type correctness
- Always run the entire test suite under tests/unit
- Documentation is a high priority. See docs/contributing.md for documentation guidelines.
- Do not worry about backwards compatibility at this time.
- If a work item from the `todo` directory is completed, delete the file when 100% complete. If a todo file is only partially copmlete, mark the items as done with a checkmark emoji and summarize what is left to do
- When working in a git worktree (`.claude/worktrees/*`), treat the main checkout at the repo root as READ-ONLY: every edit, test run, and commit happens inside the worktree. Never write through absolute paths into the main checkout — stray edits there create unstaged noise and block the eventual merge with untracked-file conflicts.

# Agent Info

- Read the documentation in `docs/_build/html` for a high level understanding of `otto` (if they don't exist, run `make docs`).
- Consider scalability and maintainability whenever possible.
- If the user reports a bug, reproduce it with unit/integration tests first, then fix it, then run the tests again to prove it is fixed.
- Do not use threads combined with asyncio. also, an event loop is guaranteed within the Typer subcommands (e.g. run, test, monitor)
- `ty` is the type checker available here — run `make typecheck` to verify type correctness
- Gates are squash-aware. If the branch you are on will be SQUASHED into a single commit before landing (agent worktrees headed for `main` normally are), intermediate commits may gate on a targeted, uninstrumented run: the tests covering the touched area plus `ruff check` on every changed file (plain `uv run pytest tests/unit -q` for cross-cutting changes; `make docs` for docs changes). The full gate suite (`make coverage`, `nox -s tests_hostless-3.14`, `make typecheck`, applicable bed lanes) then runs ONCE, before the squash, and must be green. If commits land individually (no squash planned), every commit is a bisect point and keeps the full gate — run the entire test suite under tests/unit at minimum. Unsure whether a squash is planned? Ask; default to full gates.
- Documentation is a high priority. See docs/contributing.md for documentation guidelines.
- Do not worry about backwards compatibility at this time.
- If a work item from the `todo` directory is completed, delete the file when 100% complete. If a todo file is only partially copmlete, mark the items as done with a checkmark emoji and summarize what is left to do
- When working in a git worktree (`.claude/worktrees/*`), treat the main checkout at the repo root as READ-ONLY: every edit, test run, and commit happens inside the worktree. Never write through absolute paths into the main checkout — stray edits there create unstaged noise and block the eventual merge with untracked-file conflicts.
- When work in a worktree is ready to hand back — and always before any squash onto `main` — run `make gate-fresh`. Both the main checkout and your worktree accumulate gitignored build artifacts (notably `src/otto/_webassets/*/`, built only by `make web`) that CI does not have, so a green local run can certify an environment CI will never reproduce. `gate-fresh` re-runs CI's assets-absent Python lanes against your **committed** tree in a throwaway pristine worktree. It refuses if tracked files are modified or staged — commit first, then gate.

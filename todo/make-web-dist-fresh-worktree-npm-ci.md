# Fresh worktree/checkout: `make coverage`/`dashboard` dies Error 127 — web dist build doesn't ensure `web/node_modules`

**Filed:** 2026-07-05 (surfaced running the full gate for the shell-liveness branch in a fresh worktree)

## Symptom

In a fresh `git worktree` (or a fresh clone) that has never installed the web
toolchain, `make coverage` fails before any test runs:

```
make[1]: *** [Makefile:222: web] Error 127
make:    *** [Makefile:361: src/otto/monitor/static/dist/index.html] Error 2
```

`make coverage` depends on `dashboard`, which depends on the two dist sentinels
`$(DASHBOARD_DIST)` / `$(COVREPORT_DIST)`; the `&:` grouped rule (Makefile ~360)
builds them by calling `make web`; `make web` (Makefile ~215) ends in
`cd web && npm run build`, which exits **127 (command not found)** because
`vite`/`tsc` aren't found — the worktree's `web/node_modules` was never
installed. `node`/`npm` themselves ARE on PATH; only the local dev-deps are
missing.

## Root cause

The self-healing dist rule heals the **dist** but not the **toolchain it needs**.
`release` / `all` / `ci` already `npm ci` the web deps up front (commit
`d0adef0`), so they don't hit this — but `make coverage` and `make dashboard`
invoked directly in a fresh worktree do. The `&:` dist rule / `make web` assume
`web/node_modules` (and the covreport bundle's deps) already exist.

## Proposed fix

Make `make web` (or the `&:` dist rule) self-sufficient in a fresh worktree, so
*any* entry point (coverage, dashboard, web) heals the same way the dist already
does:

- Add a `web/node_modules` sentinel target built by `npm ci` (in `web/`), and
  make the dist `&:` rule / `web` target depend on it — Make then installs deps
  once, only when missing, before `npm run build`. Prefer `npm ci` for
  reproducibility (mirrors `d0adef0`); fall back to `npm install` if no lockfile.
- Same applies to whatever installs the covreport bundle's deps (built by the
  same `make web`) — confirm it shares `web/`'s `node_modules` or gets its own
  sentinel.

Keep it air-gap-friendly (labs have no network — but this runs on the dev/CI
host, which does). Guard on a missing `node_modules` so warm worktrees pay
nothing.

## Workarounds (today)

- Per worktree, once: `cd web && npm ci` (then `make coverage` works).
- Or copy a prebuilt dist from another checkout into
  `src/otto/monitor/static/dist/` and `src/otto/coverage/renderer/static/dist/`
  (both are gitignored build artifacts) — the `&:` rule has no prerequisites, so
  it treats present sentinels as up-to-date and skips the build. This is what the
  shell-liveness gate run did to unblock, since `web/` was unchanged on that
  branch.

## Not a blocker for

Any host-only branch: the web build is orthogonal to `src/otto/host/` changes.
This is a repo build-DX papercut for fresh worktrees, not a product bug.

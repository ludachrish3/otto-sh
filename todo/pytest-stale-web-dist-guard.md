# Guard bare `pytest` against certifying a stale (or missing) web dist

> Raised 2026-07-30 while running the host-default-command-timeout branch.
> **Not** a gap in `make coverage-python` — see "Already handled" below, which
> is the question that prompted this. The real gap is the bare-`pytest` path.

## Already handled — do not "fix" this part

`make coverage-python` needs no change. The Make dependency chain is complete
and correct today:

```
coverage-python:  dashboard
dashboard:        $(DASHBOARD_DIST) $(COVAPP_DIST)
$(DASHBOARD_DIST) $(COVAPP_DIST) &:  $(WEB_SRCS) $(WEB_NODE_MODULES)   → make web
$(WEB_NODE_MODULES):                 web/package.json web/package-lock.json   → make web-install (npm ci)
```

(`Makefile` ~499, ~600, ~556, ~348.) A cold worktree running
`make coverage-python` therefore installs `web/node_modules` and builds both
dist bundles automatically. Running `npm ci` by hand first is redundant.

## The actual gap

`pytest` invoked **directly** bypasses Make entirely, so nothing builds or
freshness-checks the dist. A developer (or an agent) running

```bash
uv run pytest tests/e2e/monitor/dashboard/...
```

against a missing dist gets a confusing failure, and — far worse — against a
**stale** dist gets a **green run that certifies the wrong artifact**. The
browser tests happily exercise whatever bundle happens to be on disk. This has
bitten before: a `make docs` Playwright timeout was root-caused to a stale web
dist, and the "grep the built artifact when a selector doesn't exist" debugging
note comes from the same failure mode.

Silence is the problem: a stale-bundle pass is indistinguishable from a real
pass in the output.

## Proposed fix

Add a session-scoped autouse fixture in the dashboard/covapp e2e conftest that
**fails fast** when the dist is missing or older than `web/src/`:

- Compare the newest mtime under `web/src/` against `$(DASHBOARD_DIST)` /
  `$(COVAPP_DIST)` — the same source-stamp comparison the Makefile already
  encodes, so the two cannot disagree about what "stale" means.
- On failure, error with the remedy in the message: run `make web` (or
  `make dashboard`, which does it for you).
- **Fail, do not auto-build.** A pytest run that silently kicks off a vite
  build is a surprising side effect, slow, and racy under `-n auto`; and the
  project rule is to fail loudly with a named cause rather than self-heal
  invisibly. Erroring also keeps the guard cheap.

Scope it to the browser lanes only — the rest of the suite has no dist
dependency and must not pay for the check.

## Verification

Prove the guard red first, both ways:

1. With no dist at all → the guard fires (not a Playwright timeout).
2. With a dist present but `touch web/src/<some file>` making it stale → the
   guard fires. This second case is the one that matters; it is the case that
   currently produces a false green.

Then confirm `make dashboard` / `make coverage-python` still pass unchanged,
since their Make prerequisites already guarantee freshness and the guard should
be a no-op there.

## Related

- `docs/superpowers/plans/` — the host-default-command-timeout branch, whose
  Task 12 gate decision surfaced this.
- The wheel-embedding guard (`make wheel-check`) is the analogous
  "did the artifact actually get built" check for release, already in place.

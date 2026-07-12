# Monitor sessionized producer (Plan 5a) — ship-and-note follow-ups

Triaged by the final whole-branch review. Everything here was consciously
deferred; nothing blocks the merge.

## Worth doing next time these files are touched

1. **`session_meta(interval=...)` should be a required keyword.** The default
   (`None`) still lets a future construction-time caller silently omit it —
   the exact seam that burned this phase once (every `--db` archive persisted
   `interval: null`, leaving `health.ts`'s `cadenceMs()` unresolvable). The
   call-site guards catch a raw-dump revert but cannot catch an omitted
   argument. Make it required; existing callers pass `None` explicitly where
   they mean it.

2. **`MetricPoint.meta` is lost on the `--db` path.** Per-point hover metadata
   (e.g. proc detail) rides only in JSON exports — `MetricDB.write_point` has
   no `meta` column, so a `--db` replay drops what the same run's
   `/api/export/json` would keep. Deliberate for now (documented in the model
   docstring); wants a schema bump to close.

3. **Review mode ignores live-only flags silently.** `otto monitor x.db
   --label y --interval 9` is accepted and the flags are discarded. The
   `--live`-plus-source case already errors; these could too.

4. **`chart_map` write amplification.** A new label rewrites the whole map
   (one `UPDATE` per distinct label — ~90 across a real session, front-loaded
   into the early ticks). The eager write IS the crash-safety mechanism, so
   this is accepted; debounce to once-per-tick only if archive write volume
   ever matters.

5. **Duplicate `review_dash` fixture name** (dashboard `conftest.py` vs
   `test_harness.py`'s module-local one). Shadowing is deliberate, documented,
   and fails loud rather than silently — but a distinct name
   (`hydrated_review_dash`) would remove the two-hit grep trap.

6. **`MetricRecord.source`'s docstring** says the SQLite column arrives "with
   the backend catch-up" — this branch *is* that catch-up. Stale by one phase.

## Resolved

7. **Review mode printed nothing to the console — FIXED.** `lab_free` is
   all-or-nothing in `command_preamble`: making review mode lab-free (the
   live-bed bug fix that dropped the `--lab` requirement) also skipped
   `ensure_cli_session` entirely — no banner, no `init_cli_logging`, no
   console handler on the `'otto'` logger. The concrete, user-visible
   consequence: `otto monitor <source>` (review mode) printed **zero
   bytes**, not even the "Server running at `<url>`" line
   (`MonitorServer.serve()`, `src/otto/monitor/server.py`) a review-mode
   user needs to open the dashboard — `logger.info` calls vanished into
   Python's `lastResort` (WARNING+ only) handler.

   Chris's ruling: **console logging is required for review mode; no
   file-trail (no per-invocation output dir, no log file) is an accepted
   tradeoff.** Shipped: `otto.cli.monitor.monitor()`'s review branch now
   calls the already-shared `otto.cli.invoke.ensure_cli_session(ctx)` (the
   banner + `init_cli_logging` slice `ensure_lab_session` was already
   composed from, extracted by `48a2d20` for `--live`'s own lazy lab pull)
   before serving — guarded on `ctx.meta["_otto_root_options"]` presence so
   hand-built unit-test contexts are unaffected. It deliberately does NOT
   call `ensure_lab_session`, which would also load a lab and create a
   per-invocation output dir under `xdir` — neither is needed for a local
   read, and both are now confirmed unnecessary: `init_cli_logging` attaches
   the console `RichHandler` on its own, with no output-dir dependency.

## Known, by design

- **Any `.db` captured on this branch before the meta/chart_map fixes lacks
  chart specs and interval.** There is no migration (legacy read support is
  dropped by design). Re-capture.

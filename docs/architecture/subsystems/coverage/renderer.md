# The renderer

`SpaRenderer` (`otto.coverage.renderer.spa_renderer`) is the reporter's only
renderer: the report `otto cov report` emits is the covapp single-page app.
It replaced a server-rendered Jinja `HtmlRenderer`, which was deleted —
module, templates, and `report.css` — once the SPA became the reporter's
renderer.

`SpaRenderer.render` does two things, in order:

1. **Copy the bundle.** `make web` builds a second Vite app
   (`web/src/covapp/`) into
   `src/otto/coverage/renderer/static/covapp/` — a classic-script IIFE
   bundle with no ES modules, no inline scripts, and only relative asset
   paths, so it boots the same from `file://`, a CI artifacts browser, or
   behind Jenkins' minimal CSP. That directory isn't committed; it's built
   by `make web` and wheel-embedded (`make wheel-check` asserts the
   bundle's `index.html` is present in the built wheel). `SpaRenderer` copies
   it into the report directory as-is (sourcemaps excluded — kept out of
   every emitted report, used only by the TS coverage fold in CI). A
   checkout that skipped `make web` has no bundle to copy: `_copy_bundle`
   warns, names `make web` as the fix, and still emits the data chunks
   rather than failing the whole report.
2. **Emit the data chunks** (`otto.coverage.renderer.spa_data`, pure Python)
   — `cov_data/index.js`, one classic-script
   assignment to `window.__OTTO_COV__` carrying the report-wide payload:
   config (thresholds, tier colors/labels, state colors), the run table and
   its per-run contribution rollup (`run_contrib`: lines credited, lines
   revoked, and the top files per run),
   and a directory tree with **per-directory rollup stats precomputed in
   Python** (hit/total per stat type, per-tier breakdowns, stale/aging/
   excluded flag counts) so the frontend never re-aggregates the whole
   store client-side. Alongside it, one `cov_data/files/<mangled-path>.js`
   classic-script chunk per source file, each calling
   `window.__OTTO_COV_FILE__({...})` with that file's annotated source and
   per-line hit/branch/state/run data. Chunks load lazily on navigation to
   a file page, not up front, so page-load cost stays constant regardless
   of report size. Every index and file chunk carries the same **stamp** (a
   UTC timestamp plus a short random suffix, freshly generated per
   `render()` call) and the same `OTTO_COV_DATA_FORMAT` constant — the
   frontend's `EXPECTED_DATA_FORMAT` must match it exactly
   (`otto.coverage.renderer.spa_data.OTTO_COV_DATA_FORMAT` and the
   TypeScript constant are bumped together or never). A stamp mismatch
   between the index and a stale cached chunk, or a format the running
   bundle doesn't recognize, renders a "this report needs to be
   regenerated" guard screen instead of a wrong or partial report.

Chunk ids come from `mangle_path` over the file's **canonical** path, never
its display path, so `--prefix` changes what a report shows without changing
what any of its files are called.

Exclusion display stays render-time, not store-time, for the same reason it
always has: a single-valued `LineRecord.state` can't express "excluded
always wins" over covered/stale/aging, so the reporter never bakes
`state == "excluded"` into the store — it only forwards the configured
extra marker strings. `emit_chunks` re-scans each file's source for
exclusion markers and annotates `FileRecord.excluded_lines` on the store as
a side effect of building that file's chunk. This is why `otto cov report`
calls `store.save(...)` for `store.json` **after** `renderer.render(store)`
returns — saving first would miss every file's excluded-line list.

# Monitor UI scaffold (Plan 2) — ship-and-note follow-ups

Consolidated from the whole-branch final review at `2e02d04` (2026-07-11).
Items 1–3 belong at the top of Plan 3; the rest are opportunistic.

1. **Plan 3, first commit: densify `NormalizedSession.meta`** — dense
   `NormalizedMeta` type + per-field `??` defaults in `normalizeSession`
   (`web/src/data/exportDoc.ts`) + a partial-meta test, BEFORE anything
   iterates `meta.charts`. Today the `NonNullable<…>` alias keeps
   `charts?`/`tabs?` optional (type-guarded only), which invites `?? []`
   sprinkled at use sites against the module's own densify-once contract.
2. Consume or delete the remaining dead forward-API: `clampRange`
   (`web/src/data/exportDoc.ts`), `formatSpan` (`web/src/data/time.ts`).
3. Unenforced contract testids — add assertions when their views land:
   EmptyState's `import-error` render site, `empty-import-btn`,
   `topology-page` (structural `app-bar`/`brand`/`status-dot` are fine as
   forward contract).
4. Decide "Historical" (current) vs UX-spec §7's literal "HISTORICAL" for
   the app-bar status text before Plan 3 multiplies assertions pinning the
   casing.
5. `/host/:id` also serves element subjects — revisit URL vocabulary
   (e.g. `/subject/:id`) when Plan 3 makes elements linkable from the grid.
6. Housekeeping when next touched: dead `_run_isolated` in
   `tests/e2e/monitor/dashboard/conftest.py`; redundant
   `as Record<string,string>` cast on `chart_map` in `exportDoc.ts`;
   `web/vite.config.ts` ratchet comment's "~15-18 points across the board"
   (branches actually +24.5, functions +13.4).
7. Watch item: `test_import_error_banner_in_loaded_state`'s post-dismiss
   `count() == 0` is Playwright's non-waiting form — deterministic in
   practice; switch to `expect(locator).to_have_count(0)` if it ever flakes.
8. Recorded, no action: `exportLoadedDocument` unit-level gap (covered e2e
   by the export-download spec); unreachable `sourceName ??` fallback.

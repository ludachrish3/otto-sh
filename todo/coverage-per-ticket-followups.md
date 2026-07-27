# Per-ticket coverage — follow-ups

Carried from the per-ticket coverage branch (spec
`docs/superpowers/specs/2026-07-26-per-ticket-coverage-design.md`). Everything here was
triaged by the final whole-branch review as safe to carry — no blockers. Grouped by theme.

## 1. The ambient-git seam (the branch's weakest area)

The final review's assessment: the attribution engine is oracle-pinned against `git blame`,
mutation-hardened, and budgeted at a constant 4 subprocesses — but it parses **porcelain**
`git log -p` / `git diff` output, and every committed test runs in a hermetic git
environment. That makes this whole class structurally invisible to the suite; *the first
user with a decorated `~/.gitconfig` is the test*.

Config pins (`-c diff.mnemonicprefix=false --no-ext-diff --no-show-signature --no-color`)
and one config-hostile regression test now cover the known shapes. Remaining:

- `diff_no_index_u0` / `diff_no_index_dir_u0` are unpinned (they take no repo context, so
  the exposure is different, but they were never audited).
- `name_status_walk_u0` is deliberately unpinned. Its immunity was **verified empirically**:
  signature pollution appears in its raw stream, but the `\x00` block count is preserved and
  `parse_rename_records`' `startswith("R")` + 3-tab-field filter ignores it. Residual: a
  hypothetical signature verifier emitting `R<x>\t<y>\t<z>` lines would defeat it. No
  observed gpg/ssh output does.
- Consider a broader audit: any new porcelain-parsing git call needs the same pins, and
  nothing enforces that today.

## 2. Cross-language drift (no compiler, no guard)

Three hand-maintained Python↔TypeScript mirrors held up field-for-field under review but
have nothing enforcing them:

- `web/src/covapp/types.ts` mirrors the emitted dict keys.
- `TicketsPage.tsx` hardcodes `"(no ticket)"` / `"(uncommitted)"` as TS literals mirroring
  the Python sentinel constants.
- The chunk-callback contract (`window.__OTTO_COV_TICKET__`).

A drift guard over any one of these would be cheap insurance.

## 3. Error-surface polish

- A malformed `[coverage.tickets]` block raises an uncaught `TicketConfigError` from the CLI
  preflight on **every** `otto cov report`, not only `--tickets-json` runs. Loud, nonzero and
  descriptive — matches existing precedent — but wrapping it in `typer.BadParameter` at both
  entry points would be cleaner.
- An unbalanced brace in `[coverage.tickets] url` surfaces a bare `ValueError` from
  `string.Formatter` rather than `TicketConfigError`. Still config-load-time.

## 4. Data-shape notes

- `mangle_path` on ticket ids: two ids that mangle to the same chunk name (e.g. `PROJ/1` and
  `PROJ_1`) would **silently overwrite** each other in the chunks dict, so both summaries
  would show one ticket's detail. Far-fetched trigger; no traversal guard for a pathological
  id such as `..` either.
- `sorted(record.commits)` trades chronological first-parent order for hash order.
  Deterministic either way; chronological may read better in the export.

## 5. UI scale and polish

- AppShell's "Pin ticket" menu lists every ticket flat — a mature repo yields hundreds. The
  tickets page rows have no pin affordance of their own.
- Composed-mode `keyColumnLabel` differs by page ("Ticket" on the directory page, "Context"
  on the file page) when both filters are active.
- No line-% bar column on the tickets table (present in the original mock).
- A `?lines=` change that alters only the range on the *same* file does not refresh the
  highlight (unreachable via produced links, which always remount).

## 6. Deferred enhancements

- `TicketChunk` carries no per-file per-tier covered counts, so tier columns render "no data"
  under ticket-only scope. Python walks the per-line data already and could emit it cheaply —
  this would replace an honest decline with a real number.
- The oracle suite covers linear histories plus a dedicated first-parent divergence case; a
  whitespace-only change is not among its timeline cases (both sides use `-w`, so it would
  pin that equivalence).

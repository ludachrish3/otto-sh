# Per-ticket coverage — follow-ups

Carried from the per-ticket coverage branch (spec
`docs/superpowers/specs/2026-07-26-per-ticket-coverage-design.md`), then triaged against the
merged code on 2026-07-27. Everything actionable has since been done; this file is kept as
the record of what was found, what shipped, and what was deliberately closed.

## Done

**The ambient-git seam.** Triage found the remaining bullet was not polish but a live
silent-wrong-answer defect. `diff_no_index_u0` / `diff_no_index_dir_u0` were exempted from
the config pins on the reasoning that they "run outside any repo" — but git still loads the
invoking user's global `~/.gitconfig`. `color.ui = always` breaks both (ANSI escapes defeat
every prefix match) and `diff.mnemonicPrefix = true` breaks the batched one via `1/`/`2/`
prefixes — *different letters* from the `c/`/`w/` a repo diff emits, so the shipped
blocker-1 fix did not cover it. Both land on `AnchorResolver`'s "absent from the `-w` diff,
therefore whitespace-only" branch, so a changed file reads as verbatim and stale manual
coverage stays valid. Fixed with a `--no-index`-safe pin (`_pin` cannot be reused verbatim:
`--no-index` has a restricted option parser that rejects `--no-show-signature`), a
config-hostile regression test that sets the keys *globally* and parametrises one at a time,
and an AST guard so a new porcelain call cannot skip the pins.

**Cross-language drift.** All three mirrors now pin to one shared table
(`tests/_fixtures/covapp_ticket_contract.json`), asserted from both languages, following the
`format_outage_cases.json` precedent. The Python half reads its keys off real emitted
payloads; the TS half adds a compile-time layer via `Record<keyof X, true>`.

**Per-file per-tier counts.** `TicketChunk.files[]` now carries a per-tier breakdown, so a
ticket-scoped subtree renders real tier rows instead of one aggregate row. The composed
ticket+context case still declines honestly — that cross-tab does not exist at tree
granularity.

**Oracle whitespace case.** Added, asserting the reindent sha is absent outright rather than
relying on oracle-equality (which both engines could satisfy while both being wrong).

**UI.** Composed-mode key-column header unified behind one shared function; Line % column
added to the tickets table; ticket pinning moved out of the flat ⋮ menu into an app-bar
search box (left of the ⋮, `/` to focus, capped option list) plus per-row pin controls.

## Closed — will not do

Triaged as not worth doing; do not re-open without new evidence.

- **`mangle_path` traversal.** Not exploitable: every `/` and `\` is replaced, so `..`
  yields the literal file `...js` and `../../evil` yields `.._.._evil`. The sibling
  collision (`PROJ/1` vs `PROJ_1`) is real but needs a repo whose ticket regex matches both
  forms.
- **`name_status_walk_u0` staying unpinned.** Its immunity was re-verified under
  `color.ui = always` as well as signature pollution — git does not colourise
  `--name-status`, and the `\x00` block count survives.
- **Wrapping `TicketConfigError` in `typer.BadParameter`.** Matches existing precedent
  (`load_tiers` raises a bare `ValueError` the same way). Wrapping only the ticket path
  would make the CLI *less* consistent; this is a repo-wide config-error decision or
  nothing. Same for the `string.Formatter` `ValueError` on an unbalanced brace.
- **`sorted(record.commits)` chronological ordering.** Cosmetic; deterministic either way.
- **`?lines=` not refreshing on a same-file range change.** Unreachable via produced links,
  which always remount.

## Not carried here

`tickets.json`'s own `files[]` entries still have no per-tier breakdown. That export is an
independently versioned public schema, so adding one is a deliberate schema change rather
than a follow-up — raise it as its own piece of work if a consumer wants it.

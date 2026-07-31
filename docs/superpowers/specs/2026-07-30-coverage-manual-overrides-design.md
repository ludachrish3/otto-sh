# Coverage manual-testing overrides — design

Successor to `2026-07-26-per-ticket-coverage-design.md` (merged `8309aea9`, store v5).
Builds directly on its attribution engine (line → commit → ticket) and the declarative
`[coverage.tiers]` model.

## 1. Goal

Two related capabilities, one file:

1. **Asserted manual coverage.** Legacy projects carry code that was manually tested
   before otto existed; re-running all of it just to record it in otto is not worth the
   cost. A project can declare "the work of ticket X / commit Y was manually tested" and
   have those lines count toward a named manual tier's line coverage — while remaining
   visibly distinct from coverage proven by a recorded run.
2. **Ticket reattribution (break-glass).** Work sometimes lands under the wrong ticket id.
   An entry maps a commit sha to its definitive ticket list, replacing what was parsed
   from the commit message — applied at the attribution layer, so every consumer (tickets
   page, file-page gutter, ticket context, `tickets.json`, and asserted-coverage entries
   keyed on the corrected ticket) sees the fix consistently.

The file is TOML so it carries comments: a reader can tell at a glance what was tested
manually via this bypass and why. Each entry additionally carries a structured, required
`reason`, which the report surfaces — comments serve the file's readers, `reason` serves
the report's readers.

### Non-goals

- **No new CLI.** The file is hand-edited — it is a deliberate, commented, PR-reviewed
  record. otto validates it loud at report time. Helper verbs can come later if editing
  proves error-prone (recorded in §9).
- **No denominator changes.** Overrides add hits to lines already in the coverage store;
  they never make an un-instrumented file appear. The parent spec's "no retrofitting
  attribution onto un-instrumented files" non-goal stands.
- **No indistinguishable coverage.** Rejected by ruling: synthesizing entries that look
  like recorded manual runs would leave the override file as the only record of what was
  actually proven. Asserted lines are always marked (§6).

## 2. The override file

Default location: `.otto/coverage-overrides.toml`, next to `settings.toml`, so it is
versioned and PR-reviewed alongside the SUT repo whose history it makes claims about.
The path is overridable:

```toml
# settings.toml
[coverage.overrides]
file = "somewhere-else/coverage-overrides.toml"   # relative to sut_dir
```

Absent file *and* absent key → feature off: no walk changes, no store changes, report
byte-identical to today (pinned, §8).

Format — **top-level table names are manual-tier names** (ruling), plus the reserved
`reattribute` table:

```toml
# Legacy manual-test record for the 3.x line. See the release sign-off docs.

[[bench]]                          # section name = the manual tier it maps to
ticket = "PROJ-412"
as_of = "a1b2c3d4e5"               # required for ticket entries (§3)
reason = "Full regression on bench rig 2, 2024-11 release sign-off"

[[bench]]
commit = "deadbeef1234"            # commit entries need no as_of — the sha is the bound
reason = "Hotfix verified by hand on the lab rig before ship"

[[field-trial]]
ticket = "PROJ-101"
as_of = "0badc0ffee0"
reason = "Covered by the customer field trial, spring 2025"

[[reattribute]]
commit = "cafe4321beef"
tickets = ["PROJ-500"]             # replaces the parsed set, everywhere
reason = "Committed under PROJ-388 by mistake; this is PROJ-500's work"
```

### Validation — loud at load, never rendered around

- Every top-level table name is either `reattribute` or a tier declared in
  `[coverage.tiers]` **with `kind = "manual"`**. Anything else — a typo'd tier, a
  non-manual tier, an undeclared tier — is a config error. A manual tier literally named
  `reattribute` is a config error (the name is reserved).
- Each asserted entry has **exactly one** of `ticket` / `commit`, and a non-empty
  `reason`.
- `ticket` entries require `as_of`; `commit` entries must not carry one.
- Every sha (`commit`, `as_of`) must resolve in the SUT repo. Abbreviated shas are
  accepted if unambiguous.
- A ticket entry's id must appear in at least one commit at/before its `as_of`. Owning
  zero *current* lines is legal — that is full aging (§3), not a typo; never having
  appeared at all is a typo and fails loud.
- `reattribute` entries: `commit`, `tickets` (a list; **empty is legal** and means the
  commit's lines land in `(no ticket)` — "this should never have named a ticket" is a
  real mistake), and a non-empty `reason`.
- Unknown keys in any entry are errors, matching the settings model's posture.
- **An override file requires `[coverage.tickets]` to be configured.** Both halves
  operate on the attribution walk (reattribution rewrites its ticket extraction;
  asserted entries resolve against its line→commit map), and the walk only runs when
  the tickets feature is on. A present override file without the block is a config
  error, not a silent no-op.

## 3. Semantics

**Reattribution applies first**, at the single extraction site
(`_extract_real_tickets`, `coverage/attribution.py`): for a listed commit the
message-parsed id set is replaced by the entry's `tickets` list before anything
downstream runs. One hook, every consumer.

**An asserted entry covers the lines currently attributed** — same `-w -M
--first-parent` rules as everything else — to:

- its `commit`, or
- its `ticket`, restricted to that ticket's commits **at or before `as_of`** in the
  first-parent walk.

The `as_of` bound exists because attribution is live: without it, a new commit landing
under an old ticket next month would silently inherit asserted coverage nobody earned —
the exact silent-drift failure otto designs against. Commit-keyed entries need no bound;
the sha already is one.

**Aging is free and by content.** A line later rewritten migrates, by attribution
supersession, to the newer commit — and drops out of the entry's line set automatically.
No cache, no snapshot, no invalidation surface. Whitespace-only edits do not
re-attribute (the walk's `-w -M`), so they do not shed asserted coverage — matching the
manual-validity contract for the same reason.

**Store interaction.** For each covered line already present in the coverage store, the
entry contributes hits to its section's tier. A line is *marked* asserted in tier T only
while its sole T hits are override-sourced; once a real recorded run covers it in T, the
mark disappears — the line is now proven, and the report says so.

Two entries (same or different tiers) may cover the same line; each contributes to its
own tier and each is independently listed in the line's provenance.

**Prune signal.** When an entry no longer contributes any asserted mark, report
generation logs an info statement naming the entry (tier, key, reason) and why it is
inert, so maintainers can prune the file. Two distinct causes, distinguished in the
message because the maintainer's action differs:

- *Fully covered:* every line the entry covers already has real recorded coverage in
  its tier — the testing was since proven, delete the entry.
- *Fully aged out:* the entry's line set is empty — every line was rewritten (or, for a
  ticket entry, superseded past `as_of`), so the assertion no longer applies to any
  current code.

## 4. Configuration and module layout

- `models/settings.py` — `CoverageOverridesSpec`: the optional `[coverage.overrides]`
  block with its single `file` key, validated at settings-parse time like its siblings.
- `coverage/overrides.py` (new) — loads and validates the override file against the
  resolved tier list and the SUT repo (sha resolution), returning an `OverrideConfig`:
  asserted entries grouped by tier, plus the reattribution map
  `dict[sha, list[str]]`. Follows the `report_config.py` / `tickets.py` pattern:
  pydantic validates the settings block at the boundary; this module re-reads raw data
  at report time. Raises `OverrideConfigError` (sibling of `TicketConfigError`).
- `coverage/attribution.py` — `attribute_tickets` accepts the reattribution map and
  applies it in `_extract_real_tickets`.
- Store build (reporter) — after attribution, resolves each asserted entry to its line
  set and adds hits + provenance to the store.

## 5. Data contract

`store.json` bumps **v5 → v6** (exact-match loud-fail, no migration shim — the
established policy):

- New top-level `overrides` table: entry id → `{tier, key ("ticket:PROJ-412" /
  "commit:<sha>"), as_of?, reason}`.
- `LineRecord` gains `asserted: dict[tier, list[entry-id]]`, omit-when-empty. Refs point
  into the `overrides` table so the reason is stored once, not per line.
- SPA chunks follow the established chunking discipline: the `overrides` table rides
  `cov_data/index.js` (it is small — one row per entry); per-line asserted refs ride the
  existing per-file chunks. Every chunk carries the report stamp; the stamp-mismatch
  guard applies unchanged.

## 6. UI

- **File page / line expander:** asserted lines render a visually distinct tier marker
  (hollow/dashed variant of the tier chip) in place of the solid proven marker; the
  expander shows the entry's `reason` and key.
- **Report-level indicator:** when overrides are active, the app bar shows a small badge
  ("n overrides"); clicking it lists the entries (tier, key, reason) — the at-a-glance
  view of what was bypassed and why, mirrored from the file.
- **"Hide asserted" toggle** in the app bar's overflow menu (`⋮`), alongside the other
  view toggles — the natural home for menu toggles rather than a standalone control:
  recomputes every percentage, bar, and tier
  column with override-sourced hits excluded, so a reader can flip to proven-only
  numbers. The toggle state is stamped/namespaced like the existing focus machinery. The
  narrowing is never silent: while active, the stats-card scope line says so.
- **Tickets page:** no new columns; asserted hits simply participate in the per-tier
  percentages, and the toggle applies here too.
- Reattributed commits need no special rendering — after the remap they are ordinary
  attribution facts.

## 7. Export

`tickets.json` gains an additive per-tier `asserted` count alongside the existing
`per_tier` numbers, and a top-level `overrides_active: true` flag when the file is
present. Per the documented compatibility policy ("the schema changes on its own
schedule, and only when the exported shape itself changes"), adding fields is a shape
change: `format` bumps 1 → 2, recorded in the guide's compatibility section as an
additive change. Determinism rules unchanged.

## 8. Testing strategy

RepoTimeline pins (extending the parent spec's harness and cases):

- A commit under the same ticket **after** `as_of` stays uncovered.
- A line rewritten after assertion drops out; a whitespace-only edit does not.
- A reattributed commit's lines move ticket **everywhere**: tickets page rollups, file
  gutter, ticket context, and `tickets.json` all agree (asserted-entry resolution keyed
  on the corrected ticket included).
- `tickets = []` reattribution lands the lines in `(no ticket)`.
- A real recorded run covering an asserted line clears the asserted mark in that tier
  and only that tier.
- The prune-signal info log fires for a fully-covered entry and for a fully-aged-out
  entry, with the two causes distinguished in the message; an entry still contributing
  at least one mark logs nothing.
- Every validation rule in §2 loud-fails with a named error, each pinned: unknown table
  name, non-manual tier, reserved-name tier, both/neither key, missing `reason`, missing
  `as_of`, unresolvable sha, never-seen ticket, unknown entry key.
- Overrides never add lines absent from the store (denominator pinned unchanged).
- Absent file and absent settings key → report byte-identical to today (pinned).

Frontend (browser lane runs the full matrix, `nox -s dashboard`, not bare pytest):
asserted marker renders distinct from proven; reason visible on expand; badge lists
entries; "hide asserted" recomputes numbers and announces itself; toggle composes with
run focus and ticket context.

Export: additive fields round-trip; byte-equality determinism regenerated twice.

## 9. Rollout and open items

- Store v6 regenerate, loud-fail, no shim. `make web` rebuilds the covapp bundle —
  no new artifact.
- Docs: coverage guide gains a "manual-testing overrides" section (the file format, the
  `as_of` ruling and its rationale, the aging-by-content behavior, the honesty model);
  `tickets.json` schema addendum; settings reference for `[coverage.overrides]`.
  Screenshots regenerated where the new marker/badge appears.
- Open (recorded, not scoped): helper CLI verbs (`otto cov override --check`, or an
  add-entry helper that resolves `as_of` for you) if hand-editing proves error-prone;
  surfacing asserted counts as their own column on the tickets page if program
  management asks for it.

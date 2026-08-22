(coverage-tickets)=
# Per-ticket coverage

Otto can answer, for every ticket named in the repo's commit history, how
much of the code it wrote is covered and exactly which lines still aren't —
the report's tickets page, a per-line gutter chip on the file page, and the
`tickets.json` export ({ref}`coverage-tickets-json`) all read from the same
attribution. The feature is entirely **opt-in**: with no `[coverage.tickets]`
block, none of it runs — no git log walk, no tickets page, no gutter column,
no ticket data anywhere in the store or an export — and the coverage
numbers themselves are exactly what they'd be without this section.

Attribution walks `git log --first-parent`, so a line is credited to the
**merge** that brought it to the mainline rather than the topic-branch commit
that first wrote it — on a merge-heavy history that is where the ticket id
actually lives. See
{doc}`../../../architecture/subsystems/coverage/attribution` for why the
engine is a bounded log walk instead of per-file `git blame`, and for the
measured cost behind that ruling.

## Configuration

```toml
[coverage.tickets]
pattern = "#(?P<num>[0-9]+)"
url = "https://github.com/org/repo/issues/{num}"
```

| Field | Meaning |
|-------|---------|
| `pattern` | Required. A Python regex `finditer` over each commit's subject + body. |
| `url` | Optional. A `str.format` template rendering a tracker link for a ticket id. |

**The display id is the whole match**, not a named group — a commit that
writes `Fixes #1204` shows `#1204` in the gutter and the tickets page,
matching what the commit actually wrote. **`url` formats over `pattern`'s
named groups**, plus the positional `{0}` for the whole match, so a
template can consume only part of the id: GitHub's example above links
`#1204` to `.../issues/1204` via the named group `num`, while a Jira-style
`pattern = "(?P<key>[A-Z]{2,10}-\\d+)"` would use `{key}` — identical to the
whole match there, since Jira ids carry no leading punctuation to strip.
A commit naming several ids (`Fixes #101, relates to #205`) attributes its
lines to **all** of them — see "Overlapping tickets" below.

Both fields are validated **loudly, at settings load**, never at render
time: `pattern` must compile as a regular expression, and every field name
that `url` references must exist as a named group in `pattern` (or be `0`)
— a template naming a group the pattern doesn't define is a config error
raised before any report is built, not a blank link discovered later.

Two synthetic rows keep every owned line represented on the tickets page
and in `tickets.json`, consistent with "every attributed line belongs
somewhere": **`(uncommitted)`** for working-tree lines that haven't been
committed yet, and **`(no ticket)`** for lines whose owning commit matched
`pattern` nowhere.


(coverage-tickets-overlap)=
## Overlapping tickets

**A ticket's owned lines are not a partition of the repo.** Because a
commit naming several ticket ids attributes its lines to all of them, two
tickets can — and in practice regularly do — both claim the same line. The
tickets page states this explicitly under its table (rows overlap and do
not sum to the stats card above them, which counts each attributed line
once regardless of how many tickets claim it) rather than leaving it as a
surprise when the numbers don't add up. The same rule applies to
`tickets.json`: summing every ticket's `lines.owned` across the file can
legitimately exceed the top-level `totals.owned`.

(coverage-overrides)=
## Manual-testing overrides

Two related, opt-in capabilities live in one hand-edited file:
**asserted manual coverage** ("the work of this ticket/commit was tested by
hand, before otto could record it — count it") and **ticket reattribution**
("this commit's message named the wrong ticket; fix it everywhere"). Both
build on {ref}`coverage-tickets-json`'s attribution walk, so both require
`[coverage.tickets]` to be configured. With no override file (and no
`[coverage.overrides]` key), neither feature runs — no store change, no
report change, byte-identical to a build without this section.

The file is TOML, not JSON or a CLI verb, on purpose: it is a deliberate,
commented, PR-reviewed record, meant to be read by humans as much as by
otto. Default location is `.otto/coverage-overrides.toml`, next to
`settings.toml`, so it's versioned alongside the SUT repo whose history it
makes claims about. Point at a different path with:

```toml
# settings.toml
[coverage.overrides]
file = "somewhere-else/coverage-overrides.toml"   # relative to the repo root
```

An explicitly configured path that doesn't exist is a load error, not a
silent no-op — only the *absent* default is silent.

### File format

Top-level table names are manual-tier names (any tier declared
`kind = "manual"` under `[coverage.tiers]`), plus the reserved
`[[reattribute]]` table:

```toml
# Legacy manual-test record for the 3.x line. See the release sign-off docs.

[[bench]]                          # section name = the manual tier it maps to
ticket = "PROJ-412"
as_of = "a1b2c3d4e5"               # required for ticket entries
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

Every rule below fails the load with a named error instead of silently
producing a partial or misleading report:

- Every top-level table name is either `reattribute` or a tier declared
  under `[coverage.tiers]` **with `kind = "manual"`**. A typo'd tier, a
  non-manual tier, or an undeclared tier is a config error — and a manual
  tier literally named `reattribute` is a config error too (the name is
  reserved).
- Each asserted entry has **exactly one** of `ticket` / `commit`, and a
  non-empty `reason`.
- `ticket` entries require `as_of`; `commit` entries must not carry one.
- Every sha (`commit`, `as_of`) must resolve in the SUT repo. Abbreviated
  shas are accepted if unambiguous.
- A ticket entry's id must appear in at least one commit at or before its
  `as_of`. Owning zero *current* lines is legal — that's full aging (below),
  not a typo; never having appeared at all is a typo and fails loud.
- `[[reattribute]]` entries carry `commit`, `tickets` (a list; **empty is
  legal** and lands the commit's lines in `(no ticket)` — "this should
  never have named a ticket" is a real mistake), and a non-empty `reason`.
- Unknown keys in any entry are errors, matching the settings model's
  posture elsewhere.
- **An override file requires `[coverage.tickets]` to be configured.**
  Both halves operate on the attribution walk — reattribution rewrites its
  ticket extraction, asserted entries resolve against its line→commit map
  — and the walk only runs when the tickets feature is on. A present
  override file without that block is a config error, not a silent no-op.

### Semantics

**Reattribution applies first**, replacing a commit's message-parsed
ticket ids with the entry's `tickets` list before anything downstream
runs — one hook, so every consumer (tickets page, file-page gutter, ticket
context, `tickets.json`, and asserted entries keyed on the corrected
ticket) sees the fix consistently.

**An asserted entry covers the lines currently attributed** — the same
`-w -M --first-parent` rules as everything else in
{ref}`coverage-tickets-json` — to its `commit`, or to its `ticket`
restricted to that ticket's commits **at or before `as_of`** in the
first-parent walk.

The `as_of` bound exists because attribution is *live*: without it, a new
commit landing under an old ticket next month would silently inherit
asserted coverage nobody earned for it — exactly the silent-drift failure
otto otherwise designs against. A commit-keyed entry needs no bound; the
sha it names already is one.

**Aging is free and by content, not authorship.** A line that's later
rewritten migrates, by ordinary attribution supersession, to the newer
commit — and drops out of the entry's line set automatically. There's no
cache, snapshot, or invalidation surface to keep in sync. Whitespace-only
edits do not re-attribute (the same `-w -M` as the rest of the walk), so
they don't shed asserted coverage either.

### The honesty model

Asserted coverage is never indistinguishable from a recorded run:

- A line covered only by an override renders with a visually distinct,
  hollow/dashed tier marker rather than the solid "proven" marker, and the
  file page's expander shows the entry's `reason` and key on request.
- The app bar shows a small badge ("*n* overrides") whenever any override
  is active; opening the `⋮` overflow menu lists every entry (tier, key,
  reason) — the at-a-glance view of what was bypassed and why.
- The `⋮` menu's **"Hide asserted coverage"** toggle recomputes every
  percentage, bar, and tier column with override-sourced hits excluded, so
  a reader can flip to proven-only numbers. It's never silent: while
  active, the stats-card scope line says so (`· asserted hidden`). The
  tickets page's own aggregate/row stats decline to a single dash (`—`)
  under the toggle rather than subtracting — there's no deduped
  "asserted-only" total to subtract from a per-ticket row honestly, so the
  page says "no data" instead of guessing.
- Once a real recorded run covers a line in a tier, the mark disappears —
  the line is now proven, and the report says so. Two overrides (same or
  different tiers) can cover the same line; each is independently listed
  in the line's provenance.

### Prune signal

When an entry no longer contributes any asserted mark, report generation
logs an info-level line naming the entry so a maintainer can clean up the
file. The two causes are distinguished in the message because the correct
maintainer action differs:

```text
override %s (tier %r) is fully aged out — no current line is attributed to
it; prune it from %s (reason: %s)

override %s (tier %r) is fully covered by recorded runs — every line is
proven; prune it from %s (reason: %s)
```

*Fully aged out* means the entry's line set is empty — every line was
rewritten, or (for a ticket entry) superseded past `as_of` — so the
assertion no longer applies to any current code. *Fully covered* means
every line the entry covers already has real recorded coverage in its
tier — the testing was since proven, so the override adds nothing further.
An entry still contributing at least one mark logs nothing.

(coverage-tickets-json)=
## The `tickets.json` Export

`otto cov report --tickets-json PATH` (mirrored on `otto test` as
`--cov-tickets-json PATH`) writes a machine-readable per-ticket coverage
summary — otto's **first public export format**. Every other JSON otto
writes (`store.json`) is an internal, renderer-shaped artifact free to
reshape on any `otto` release; `tickets.json` has consumers otto does not
control (CI dashboards, ticket-coverage bots, ad-hoc scripts), so it is
specified and versioned as a stable contract instead:

```json
{
  "format": 2,
  "generated": "2026-07-26T21:00:00Z",
  "otto_version": "0.8.0",
  "project": "myproduct",
  "traversal": "first-parent",
  "overrides_active": true,
  "thresholds": {"high": 80, "medium": 70},
  "tiers": ["unit", "system", "manual"],
  "totals": {"owned": 17284, "covered": 16240, "uncovered": 1044},
  "tickets": [
    {
      "id": "PROJ-388",
      "url": "https://jira.example.com/browse/PROJ-388",
      "commits": ["a1b2c3d4e5f6..."],
      "lines": {"owned": 97, "covered": 61, "uncovered": 36},
      "per_tier": {"unit": 61, "system": 0, "manual": 0},
      "asserted": {"unit": 0, "system": 0, "manual": 4},
      "files": [
        {
          "path": "src/net/arp.c",
          "owned": 64,
          "covered": 41,
          "missing": [[142, 158], [204, 204], [219, 221]]
        }
      ]
    }
  ]
}
```

Each ticket's `asserted` map counts, per tier, how many of that ticket's
lines are covered in that tier *only* via a {ref}`coverage-overrides`
entry (`tier in line.asserted`) — the same distinction the report UI's
dashed marker draws, exported as numbers. `asserted` counts are additive
to, not subtracted from, `per_tier` — a line counted in `asserted[tier]`
is also counted in `per_tier[tier]`, since it is covered there. The
top-level `overrides_active` flag is `true` whenever an override file is
configured (the spec's wording) — that is, whenever `.otto/coverage-overrides.toml`
(or the path named by `[coverage.overrides]`) was found and loaded for this
report — regardless of whether it declares any asserted entries at all (a
reattribute-only file is still "active" with an empty `overrides` list) or
whether a declared entry still contributes a mark (see
{ref}`coverage-overrides`'s prune signal).

### Compatibility policy

- **`format` is its own integer, versioned independently of
  `store.json`'s `STORE_FORMAT_VERSION`.** The internal store may be
  reshaped freely for the renderer's benefit; this export's schema changes
  on its own schedule, and only when the exported shape itself changes.
  **`format` bumped 1 → 2** for the manual-overrides feature: each ticket
  object gained the additive `asserted` map and the payload gained the
  additive `overrides_active` flag — but v2 is not a *pure* addition:
  each ticket's existing `commits` array also changed **content**
  (not shape). v1 (shipped in v0.8.1) populated it from only the commits
  that currently own an attributed line for that ticket; v2 populates it
  **walk-complete** — every commit the first-parent walk visited that
  named the ticket, including a commit whose lines have since been
  rewritten or superseded. This matters for the manual-overrides feature:
  an asserted ticket entry's `as_of` bound (and the "fully aged out" prune
  signal) needs to see a ticket's full commit history, not just its
  current line owners, to tell a legitimately-aged entry from a typo'd
  id. A ticket still only appears in `tickets` at all when it owns at
  least one coverable line — that inclusion rule is unchanged from v1 —
  so this is a same-shape, same-membership, different-content change to
  one existing field, disclosed here rather than folded silently into
  "additive."
- **Output is deterministic apart from the `generated` timestamp**:
  tickets sorted by `id`, each ticket's `files` sorted by `path`, and
  `missing` ranges ascending. Two reports built from identical coverage
  data at the same `generated` stamp produce byte-identical files — this
  is what a CI diff actually compares (the timestamp aside), and is itself
  pinned by test (generating twice with a fixed `generated` and asserting
  byte equality, not just field-by-field) — so a diff between two real
  regenerations is exactly the coverage delta, never noise from key
  ordering or incidental formatting.
- **Every `path` is repo-relative POSIX**, never the internal store's
  absolute, machine-specific path — two CI runners with different
  checkout locations emit identical bytes for identical coverage, and an
  external consumer can map a path onto its own checkout without knowing
  anything about the machine that produced the file.
- **`missing` ranges are inclusive `[start, end]` pairs** — `[142, 142]`
  for a single line — using the exact same grouping the tickets page
  renders (`group_ranges`, shared code, not two independent
  implementations that could drift apart).
- **`(uncommitted)` and `(no ticket)` appear as ordinary ticket entries**,
  so the export's `totals` sum the same way the tickets page's stats card
  does.
- **Loud-fails without `[coverage.tickets]` configured, or with a
  configuration that attributed nothing.** Requesting `--tickets-json`
  asks for ticket data; writing an empty file instead of erroring would
  read as "this project has no uncovered ticket work" — exit `1` with a
  clear cause instead. `otto test --cov-tickets-json` fails this same way
  *before the suite runs* when `[coverage.tickets]` isn't configured at
  all (a known misconfiguration, worth failing fast on); a git walk that
  ran but matched nothing is only knowable after the run, so that case is
  a warning on the otherwise-successful test run instead, matching every
  other `--cov-*` post-run tail's never-fail-a-green-run policy.
- **Omitted flag → no file written**, ever — there is no implicit default
  path, so nothing appears that wasn't explicitly asked for.

This is also the natural substrate for a future per-ticket
`--cov-fail-under` variant, and the shape other planned report formats
(Cobertura, Coveralls, Codecov) are expected to follow: independent
format version, deterministic ordering, loud failure on missing inputs.
Neither is built yet.

# Host ID & Naming Rules — Design

**Status:** designed 2026-07-07, approved, awaiting implementation.
**Lands as:** a **follow-on to sub-project #1** (now merged to `main`), built on
branch `worktree-link-foundation` on top of the merged #1. It touches code that
feeds the frozen link-id / sentinel contracts, but is **contract-safe**: valid
`[a-z0-9]` elements slug to themselves, so no already-shipped id changes (§10).
**Precedes:** sub-project #2a (`otto link` observe/reap). #2a resumes on top of
this.

Related: `docs/superpowers/specs/2026-07-06-link-foundation-design.md` (#1),
`todo/link.md` (the living link stack scratchpad).

---

## 1. Motivation & scope

A host's `id` is otto's primary key: it keys `Lab.hosts`, backs reservations
(indirectly), drives selector / `host_preferences` matching, is hashed into
`make_link_id`, is encoded into live-tunnel sentinels, labels monitor
time-series, and names coverage staging directories. Yet today:

- **The id can silently collide.** `make_host_id` renders `element.lower()` +
  a *bare* `element_id` (no separator) + optional `_board+slot`. A single-host
  element with no `element_id` yields a bare `element` id (e.g. `server`), and
  two such hosts — common **within** a lab and **across** labs — collide. The
  only uniqueness guard (`Lab.add_host`'s `KeyError`) fires only within one
  `Lab` build; the multi-lab merge path (`Lab.__add__`'s `dict.update`), the
  cross-lab addressing map, and docker's direct `lab.hosts[id] = host` writes
  all **silently overwrite**.
- **Numbers meant to be globally unique clutter display.** Element numbers are
  typically globally unique (so they *disambiguate*), but that makes them large
  (`server103`), and large numbers clutter CLI output and GUI labels.
- **Identity vs. display is not cleanly separated.** The monitor selects
  *parsers* by `host.id` but attributes *series / DB rows* by `host.name` —
  which is user-overridable and never uniqueness-checked, so two hosts can
  collide in the monitor DB even with distinct ids.

This spec defines a **proper, foundational rule set for host identity and
naming** that holds in **all** id usages, not just the link stack, and fixes the
silent-collision and identity/display-coherence gaps. It is contract-safe (§10):
valid existing ids are unchanged, so it lands cleanly on the merged #1.

**In scope:** id derivation by slugging `element` (no separate id field), an
input charset, fail-loud global uniqueness, a lab-scoped logical index,
display-name derivation, CLI input resolution, and moving monitor attribution to
the id.

**Out of scope:** the `otto link` CLI and live tunnels (#2a/#2b); any change to
the `make_host_id` *format for valid inputs* (kept identical, so existing link
ids and fixtures are undisturbed); a single-lab membership model (membership
stays a list — a host may belong to many labs).

---

## 2. Guiding principle

> **Names are for display. IDs are for correlation.**

- **Name** — what a human reads in CLI output and web-frontend labels. Exposed
  directly as the host's `name` property: a space-joined, **original-case**
  concatenation `element [logical ID] [board] [slot]` (§4.2). The number is the
  small **logical index** (§4), shown only when the element repeats — so labels
  stay readable (`"Server 2 Blade 3"`), never `server103`.
- **id** — the internal correlation key, **derived automatically** by slugging
  `element` (§3): `"Lab X Server"` → `lab-x-server`. Stable, absolute, globally
  unique. Everything that *stores, hashes, compares, or references* a host uses
  the id: `Lab.hosts` keys, `make_link_id`, sentinels, selector /
  `host_preferences` matching, monitor attribution, coverage staging paths, and
  the `hop` / `power_control.controller` / declared-link-endpoint references
  inside `lab.json`.
- **The one exception is CLI input** (§5). Where a human *types* a host handle,
  otto accepts **both** the canonical id (the slug) **and** the logical form,
  and tab-completion offers both.

There is **no separate custom-`id` field** — the id is always derived, and a
custom readable handle is expressed by writing a richer `element`. Everything
below follows from this split.

---

## 3. Canonical id

### 3.1 Derivation — slug the element, keep the shape

`make_host_id(element, element_id, board, slot)` keeps its structure but slugs
its string inputs:

```
slug(element) + element_id? + ("_" + slug(board) + slot?)?
```

- `slug(s)` = lower-case → replace every maximal run of characters outside
  `[a-z0-9]` with a single `-` → strip leading/trailing `-`. So
  `"Lab X Server"` → `lab-x-server`, `"server"` → `server`, `"Big Board"` →
  `big-board`. A string that slugs to empty is a load error.
- The **only** structural delimiter is `_`, between the element-slug and the
  board-slug. Hyphens appear **only inside** a slug (word breaks); underscores
  appear **only** as the board separator. The two delimiter systems are disjoint
  — you can tell a plain host id from a board host id by its delimiter, and
  cross-scheme collisions nearly vanish.
- **Existing simple elements are undisturbed.** For an `element` already in
  `[a-z0-9]`, `slug` is the identity, so its id is byte-for-byte what
  `make_host_id` produces today. Every existing link id, sentinel, and
  format-lock test stays green; only multi-word / mixed-case / punctuated
  elements — which could not have produced a clean id before — begin slugging.

### 3.2 `element` is the human name — no separate id or custom-id field

The id is *always* derived; there is no hand-typed `id` field. A custom,
readable handle is expressed simply by writing a richer `element`:

```json
{ "element": "Lab X Server" }        // id: lab-x-server,  display: "Lab X Server"
{ "element": "dut", "element_id": 2 } // id: dut2,          display: "dut 2" (if repeated)
```

- `element` carries the human-readable string (display, §4.2) **and** is the
  slug source for the id (§3.1). One field, two derived outputs.
- `element` still means the *type* for grouping and product-provider matching.
  Two distinct single hosts are disambiguated by giving them distinct `element`
  strings (`"Lab X Server"` vs `"Lab Y Server"`) or an `element_id` — not by a
  separate id field.
- This intentionally **couples** the id to the display string: the id is always
  `slug(element)`, so a short id with an unrelated long display is not
  expressible. That is the deliberate cost of dropping the custom-id interface.

### 3.3 Input charset

`element` and `board` are free human strings; `slug` (§3.1) normalizes them, so
the *derived id* is delimiter-safe by construction (`slug` output ⊂ `[a-z0-9-]`,
and the only `_` is the structural board separator). Constraints:

- Raw `element` / `board`: printable text that slugs to a non-empty
  `[a-z0-9-]` token. A value that slugs to empty (all punctuation/whitespace) is
  a fail-loud load error. Because `slug` folds `_`, `.`, `:`, `|`, `/`,
  whitespace, and regex metacharacters into `-`, none of those ever reach the
  id.
- `element_id`, `slot` ∈ integers ≥ 0.

Validation is fail-loud at load, naming the offending host and field. This is a
hard-cutover rule (consistent with #1's `lab.json` cutover).

Rationale: ids flow into regex selectors (`re.fullmatch` / `re.search` over the
id), the `|`-delimited `make_link_id` canonical string, `:`-joined sentinels
(percent-encoded, but cleanliness still matters), `/`-delimited monitor series
keys (`f"{host}/{label}"`), and coverage staging directory names. Slugging the
inputs keeps the id safe across all of them.

### 3.4 Global uniqueness — fail-loud

Canonical ids **must be unique across all loaded labs**. A duplicate is a
configuration error, reported loud at load time, naming **both** offending hosts
and the remedies (differentiate the `element` string, assign/uniquify
`element_id`, or set `board`/`slot`). No auto-generated suffix — that would be
context-dependent and would break id stability (and therefore link-id stability).

`element_id` stays **optional** — required only to break an *actual* collision.
A lone `server` with no `element_id` keeps working (id `server`); it only errors
when a second colliding `server` is loaded. Note two *different* raw `element`
strings can slug to the same token (`"Lab X Server"` and `"lab-x-server"` both →
`lab-x-server`); the validator catches that as the collision it is.

**Silent-overwrite sites this validator must close** (from the codebase survey):

| Site | Today | Fix |
| --- | --- | --- |
| `configmodule/lab.py` `Lab.__add__` | `self.hosts.update(other.hosts)` — last-wins on the multi-lab merge path (highest risk) | route through the uniqueness check; raise on collision |
| `storage/json_repository.py` cross-lab addressing map | `addressing[host_id] = ...` last-wins | detect + **warn** on differing duplicate (keep first); this all-files map stays resilient — the hard failure is at `add_host`/`__add__` for loaded labs |
| `docker/compose.py` container registration | `lab.hosts[id] = host` / `[placeholder.id]` — bypasses `add_host` | route through the guarded path |
| `configmodule/lab.py` `add_host` | `KeyError` only within one Lab build | keep, but it is no longer the *only* guard |

The uniqueness check is one shared helper so all paths enforce the same rule.

---

## 4. Logical index & display names

### 4.1 Logical index

A **lab-scoped positional number** among a host's same-`element` siblings:

- Computed in a **lab-assembly pass** over `Lab.hosts` (grouping by `element`),
  because a host needs its siblings to know its position — it cannot be computed
  at host construction. The pass runs after all hosts are registered and re-runs
  after `Lab.__add__` (the sibling set is the merged union).
- **Ordered by `element_id` ascending** (hosts without an `element_id` sort
  after those with one, stably by canonical id). Position is 1-based.
- **Active-lab-scoped.** `OttoContext.lab` is a single active lab per invocation
  (assembled by merging). The logical index — and therefore the display name —
  is relative to that active composition. A host in multiple labs can present a
  different logical index in differently-merged views. This is acceptable
  **because the index/name is display-only**; the id is absolute.
- **Never stored, hashed, or used as a correlation key.** It is stamped onto the
  runtime host object (e.g. `logical_index: int`) purely to drive the display
  name (§4.2) and CLI input resolution (§5).

### 4.2 Display name

The display name is a **space-joined, original-case** concatenation of the
host's identity fields — built for human reading (CLI output and the overhauled
web frontend's labels), never for correlation:

```text
<element> [<logical element ID>] [<board>] [<slot>]
```

- Components are joined by single spaces, each **omitted when absent**, each
  **preserving the original case** of the provided value: `element="Lab X Server"`
  → `"Lab X Server"` (not lower-cased). This is the opposite of the id, which
  lower-cases and slugs. So a board host reads `"Node 2 Blade 3"` — note this
  adds spaces the current `_generate_name` omits (`"node2 blade3"` today).
- The number is the **logical element ID** — the small positional index (§4.1),
  never the raw `element_id`. It is included only when the element repeats in the
  lab: a lone `"Server"` shows no number; three servers show `"Server 1"`,
  `"Server 2"`, `"Server 3"`.
- The existing optional `name` field is a **display-only override** at
  construction (unrelated to the id, which `element` owns); when set it is
  returned verbatim.

**Accessor.** The host object exposes the display name **directly** as a
property/field (`Host.name`), so the CLI and the web frontend read it straight
off the host. Because the logical index needs lab siblings, the value is fed by
the lab-assembly pass (§4.1) that stamps `logical_index`; a host with no lab
context simply omits the number. Today `_generate_name` (`remote_host.py:258`)
builds `"{element}{element_id} {board}{slot}"` at `__post_init__` — no space
before the number, raw `element_id`. It becomes: space-joined, original case,
logical index, override-respecting, lab-assembly-fed.

**Display names are lab-context-relative and need not be globally unique** —
they are labels, not identity. Correlation always uses the id. The display string
(`"Lab X Server"`) and the typed CLI handle (the slug `lab-x-server`, §5) differ
by the predictable slug transform; tab-completion bridges the gap.

---

## 5. CLI input resolution — the one exception

Where a human types a host handle (the `otto host <id>` positional, `--hop`,
docker `--on`, and any other host-id CLI argument), otto accepts **both** the
canonical id and the logical form.

### 5.1 Resolution rule — canonical wins, positional falls back

Given a typed handle `H`:

1. If `H` exactly matches a canonical id (a slug) in the active lab → that host.
2. Else split `H` into a trailing digit-run `N` and a prefix (longest trailing
   `[0-9]+` is `N`, the remainder is the prefix); if some element group's slug
   equals the prefix and has an `N`-th host by logical index → that host.
3. Else → fail loud, listing available handles.

Handles are always **slugs** (`server`, `server2`, `lab-x-server`): the typed
form is the id, not the pretty display string. Because step 1 (exact canonical
match) always takes precedence, a host whose real id happens to end in digits
(e.g. `element="h2"` → id `h2`) is still reached by its id; the positional split
in step 2 is only a fallback for handles that match no canonical id.

This is purely a **resolution-layer** feature. Selectors / `host_preferences`,
link ids, sentinels, storage keys, and `lab.json` references continue to use
canonical ids **only**.

Worked examples:

- Element_ids `{47, 103, 288}` (the typical large-unique convention): no
  canonical `server1/2/3`, so `server1/2/3` resolve positionally to 47/103/288 —
  matching the display labels exactly. **Type what you see works.**
- Dense element_ids `{1, 2, 3}`: canonical == logical, no ambiguity.

### 5.2 Load-time shadow warning

Display numbering is *logical*; input resolution prefers *canonical*. They
disagree only when a small `element_id` numerically shadows a **different**
host's logical position (e.g. ids `{2, 5}`: the id-5 host labels as `server2`,
but typing `server2` hits the id-2 host, since canonical wins).

Rather than change syntax or reverse the rule, otto emits a **fail-soft warning
at load** whenever a canonical id `<element><N>` resolves to a host that is *not*
the element group's `N`-th by logical index. This surfaces the rare mixed-set
footgun instead of leaving it silent. In the typical large-element_id convention
the condition never arises, so no warning fires.

### 5.3 Tab-completion

Completion offers **both** canonical ids and logical handles for the active lab,
de-duplicated where they coincide (dense element_ids). Logical handles may carry
a completion hint pointing at their canonical target (e.g. `server2 → server103`)
so the user understands the mapping. Completion enumeration otherwise continues
to source canonical ids (`collect_host_ids`, `hosts_by_lab`) as today.

---

## 6. Coherence sweep — id usage across the codebase

The "all cases" audit (full survey retained in the plan). Each id-consuming site
is classified and made to honor the split.

### 6.1 Correlation sites — canonical id only

- **Link layer:** `make_link_id` (hashes `host` verbatim), `encode_sentinel` /
  `parse_sentinel` (host ids in the wire marker), `LinkEndpoint.host`. Untouched
  because derivation is unchanged.
- **Selector / `host_preferences`:** `re.fullmatch` / `re.search` over the id
  (`host/capability.py`, `context.py` `all_hosts`, `settings.py` selector
  validation, `cli/cov.py`'s id→regex). Charset (§3.3) keeps ids regex-safe.
- **Storage / registry keys:** `Lab.hosts`, `context.get_host`,
  `do_for_all_hosts` result maps, the cross-lab addressing map, `hop` and
  `power_control.controller` references, docker parent/child id scans,
  coverage staging directory names.
- **Name-as-id conflation to fix:** several call sites pass the *display name*
  where a correlation id is expected — `host_id=self.name` into `SessionManager`
  (`unix_host.py:343,371,512,549`). These become `host_id=self.id`. Audit for
  any other `…=self.name` feeding a correlation/attribution parameter.
- **Completion enumeration:** canonical id lists in the completion cache
  (`hosts_by_lab`, `collect_host_ids`) stay canonical; logical handles are added
  *for display in completion only* (§5.3).

### 6.2 Display sites — name

CLI output and web-frontend labels render the host's `name` property —
space-joined, original-case `element [logical ID] [board] [slot]` (§4.2).

### 6.3 Monitor attribution — moved from name to id

The monitor collector / DB / store / history currently key time-series and log
rows on `host.name` (`collector.py` `host_name = target.host.name`;
`f"{host_name}/{label}"`; `db.py` `metrics.host` / `log_events.host` columns;
`store.py` `key.split("/")[0]`; `history.py`). These move to the canonical
**`host.id`** (now uniqueness-enforced), so monitor attribution is coherent with
parser selection (already id-based) and immune to name collisions / overrides.
The GUI resolves id → display name at render time. The `/`-delimited series-key
format is preserved (ids exclude `/` by §3.3).

### 6.4 Docker dotted ids

Docker synthesizes a second id form, `parent.project.service` (dotted), built
separately from `make_host_id` (its `.` is inherent to that form, not a slug
delimiter). It is covered by the same uniqueness check (§3.4) — it just stops
being able to silently overwrite (§3.4 table) — and its `project` / `service`
segments are slugged the same way (§3.1) so the dotted id stays `[a-z0-9.-]`.
No format change.

---

## 7. Non-goals

- No single-lab membership model — `labs` stays a list.
- No change to the id *structure* — still `element[element_id][_board[slot]]`
  with the same delimiters; the only new step is slugging the string inputs
  (an identity no-op for simple `[a-z0-9]` elements, so existing ids are stable).
- No separate custom-`id` field — a readable handle is a richer `element` (§3.2).
- No lab name embedded in the id (ill-defined under multi-lab membership; would
  break id purity and the frozen link contracts).
- No auto-generated disambiguation suffix (context-dependent, unstable).

---

## 8. Testing strategy

Almost all of this is pure-function / unit-testable:

- **Slug & charset:** `slug` cases (multi-word → hyphen, mixed case → lower,
  punctuation/underscore/whitespace runs → single hyphen, strip ends, empty →
  error); a simple `[a-z0-9]` element slugs to an identical id (contract-drift
  guard); `element_id`/`slot` reject non-integers.
- **Uniqueness:** duplicate ids across `add_host`, `__add__`, the addressing
  map, and docker registration all fail loud with both hosts named; the four
  silent-overwrite sites are each regression-tested.
- **Logical index:** ordering by element_id ascending; element-only vs
  large-sparse vs dense sets; re-computation after `__add__`; hosts without
  element_id sort last.
- **Display name (`name` property):** space-joined, original-case
  `element [logical ID] [board] [slot]`; case preserved (`"Lab X Server"` not
  lower-cased); logical index shown only when the element repeats and never the
  raw `element_id`; board/slot space-separated (`"Node 2 Blade 3"`); explicit
  `name` override returned verbatim; correct value with and without lab context.
- **CLI resolution:** canonical-wins then positional fallback across the worked
  examples; unknown handle fails loud; the shadow-warning fires exactly on the
  mixed-set condition and not otherwise.
- **Completion:** canonical + logical offered, deduped on coincidence.
- **Monitor attribution:** series / log rows keyed by id; a name override no
  longer changes attribution; two same-name distinct-id hosts stay separate.
- **Format-lock guards:** `test_capability_hosts.py` and the link-id / sentinel
  tests stay green unchanged (derivation untouched), proving no contract drift.

Live-bed work is unaffected (no network behavior changes here).

## 9. Landing & sequencing

1. Implement on `worktree-link-foundation`, on top of the merged #1.
2. Full gate green (`nox -s lint typecheck`, `make coverage`, `make docs -W`).
3. Whole-branch review on **fable** before finishing.
4. Then resume the #2a (`otto link` observe/reap) spec → plan → implement on top.

## 10. Long-term consequences ledger

- **The id is a frozen contract — #1 has shipped.** Simple `[a-z0-9]` elements
  slug to themselves, so no link id or sentinel already shipped in #1 changes.
  The new rules only *reject* invalid inputs or *normalize* strings that could
  not have produced a clean id before; they never silently re-map a valid
  existing id. This safety holds independently of #1's merge status.
- **`slug` is itself a stability contract.** Once ids ship, the slug algorithm
  is frozen exactly like `make_link_id` and the sentinel format — a slug tweak
  would re-map ids and invalidate live tunnel markers. Pin it with a
  STABILITY-CONTRACT docstring and round-trip tests.
- **The logical index is deliberately NOT a contract.** It is display/CLI-input
  sugar, re-derived per active-lab composition. Nothing may store it, hash it,
  or reference it across process boundaries — doing so would reintroduce exactly
  the staleness the live-discovery design (#1) rejected.
- **Monitor DB rows written before the attribution move used names.** A hard
  cutover (consistent with the project's no-migration stance) is acceptable;
  note it so a monitor DB predating this change is understood to key on names.
- **id and display are coupled through `element`.** The id is always
  `slug(element)`, so renaming a host's `element` for readability also changes
  its id — and therefore any declared-link route id and any live tunnel over it.
  Renaming an `element` is an identity change, not a cosmetic one; documented in
  the lab-config guide.

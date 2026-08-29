# Getting Started overhaul — a worked example built to survive interface change

**Date:** 2026-08-27
**Status:** Designed 2026-08-27; amended 2026-08-29 (inventory and reservations
pages, probe sequencing, library-doc findability); awaiting implementation plan
**Depends on:** nothing outstanding. `2026-08-27-lab-definition-v2-design.md`,
`2026-08-28-host-inventory-layer-design.md` and
`2026-08-28-three-level-reservations-design.md` have all landed on main, so
every page is written against the shipped shapes and no page has to wait.

## 1. Goal

Rework the Getting Started section into a worked example that walks a new user
through defining otto's own unix and embedded labs from scratch, showing real
output from the real OSes — and structure the docs testing so that when otto's
first users force interface changes (likely), the rework cost is paid in data
files and captured artifacts, not in prose.

Two hard requirements from the todo: (1) setup procedures are **tested** so users
are never led astray; (2) no new duplication — the example is a concrete
instance, not a second reference.

### Out of scope — follow-ups to this work

Everything here is a **follow-up**, confirmed with Chris on 2026-08-29: out of
scope now, tracked in `todo/documentation-getting-started-improvements.md`,
and designed later. No page in this plan waits on any of them (§9): where a
capture would show a gap a follow-up will close, the page says so honestly,
and the follow-up's landing is a capture refresh, not a rewrite (§7).

- `otto host probe` enhancements. A separate workstream, and the three items
  the todo lists are three different kinds of change:
  - *Port recon / protocol guessing* changes probe's **output**. Tier 3
    captures exist precisely to absorb that: re-run the capture runner, prose
    is untouched. Not a prerequisite for any page.
  - *The per-profile metric-defaults review* changes what probe reports for
    BusyBox **and which monitor stats actually work there**. This is the one
    follow-up a page can *see*: `customizations.md` promises a monitor capture
    proving the stats land, and today's BusyBox socket metrics are known to
    mismatch sometimes. The page does not wait. It captures what the bed does
    now and, where a stat is missing, says so with the same honest "not yet"
    note §8 already prescribes for Zephyr SNMP — a pinned gap with a sentence
    beside it is not a contract, a pinned gap with no sentence would be. When
    the review lands, refreshing the capture removes the note.
  - *`userland_options` booleans versus tri-state* needs no code. Every field is
    already `Literal[...] | None`: the values that look like `True`/`False` are
    `"present"`/`"absent"`, and `None` is the third state, "unknown — probe it
    at connect". A boolean cannot express that without a sentinel, so three
    values are required for as long as probe-at-connect exists. The question
    is closed here; `defining-hosts/index.md` explains the tri-state (§8).
  The Zephyr page documents the probe + debug-log workflow *as it exists*.
- Monitor stat down-selection (`--monitor-stats` on `otto test`, and the
  matching narrowing on `otto monitor`). Follow-up; `customizations.md` shows
  the full default set.
- Reworking the per-field reference (`guide/configuration/lab-config.md`)
  beyond keeping it complete. Follow-up; in this plan it stays the canonical
  field reference, the completeness guard (§6) keeps it complete, and the
  worked example links into it rather than restating it.
- Writing backend plugins as a subject. `library/reservation-backends.md` and
  `library/inventory-backends.md` already cover the contract, registration and
  conformance testing, and remain the reference. The reservations page shows
  one minimal plugin and links to them (§8); §8a fixes the fact that nothing
  user-facing links to them today.
## 2. Page tree

`getting-started.md` slims to a hub: installation pointer (existing), a short
`otto init` paragraph with a pointer to the CLI page (removing the duplication
noted in the todo), completion setup, and links into the worked example.

```text
getting-started/
  index.md                 hub (install pointer, init pointer, completion, map)
  defining-hosts/
    index.md               workflow: names, IPs, creds, hops, OS → probe → pin
    inventory.md           the two-layer rule; the hosts move from inline
                           fields to a referenced record; lookup / list /
                           doctor; the JSON → NetBox growth path (pointer)
    unix.md                unix-specific settings (slim); is probe needed here?
    busybox.md             busybox profile specifics; the userland tri-state
    zephyr.md              default host class + debug log → CommandFrame;
                           defining the oldest Zephyr host (customization)
  customizations.md        login proxy, post-connect actions (custom prompt),
                           adding perf metrics, modifying metric commands/parsers
  boards-of-interest.md    [project] host_patterns / lab_patterns regex;
                           effect on all_hosts() / do_for_all_hosts()
  reservations.md          json backend setup; resources at the three levels;
                           check refused → held → OK; a minimal custom backend
                           and its conformance test; pointers to the library docs
```

Every page follows the same spine: *what you are deciding → the fragment you
write (included from the example project) → the command you run → its captured
output → where the full reference lives.*

## 3. The example project — one source of truth

The worked example is a **real, checked-in project**: `docs/examples/getting-started/`
holding the `settings.toml`, `lab_data/lab.json` (v2 shape; otto's real bed —
`test1..3`, the Zephyr hosts — with credentials as they appear in the existing
fixture data), and any customization module the pages show.

Two pages need more than that file, and each addition is a file the page
*includes*, never prose that restates it:

- **The inventory page** moves the unix hosts out of the lab file and into a
  referenced record. That is two states of the same lab, so the project holds
  both: the inline `lab.json` that `defining-hosts` builds, and beside it the
  referenced form with its JSON inventory and `creds_file`. Duplication is what
  §1 forbids, so the two are not allowed to merely *look* alike: a tier-1 guard
  loads both and asserts they produce identical hosts once the inventory is
  joined. The `tech1-inventory` fixture already proves exactly this equivalence
  for the real bed (`tests/unit/labs/test_inventory_equivalence.py`), and
  `tests/unit/docs/test_inventory_worked_example.py` already doc-tests the
  reference page's worked example the same way; the example project reuses
  that proof's shape rather than the fixture, and the plan's verify-first
  phase (§9) starts from those two tests rather than writing a third from
  scratch.
- **The reservations page** defines a backend. The project holds that module
  (registered through `[init]` in its `settings.toml`, the way the library docs
  show) and the JSON reservations file the shipped backend reads.

Prose never hand-copies JSON, TOML, or Python. Pages `literalinclude` fragments
with Sphinx `:start-after:` / `:end-before:` anchors so one file serves many
pages. Anchors are ordinary comments in TOML and Python; JSON has no comments,
so lab.json anchors are the sanctioned `_`-prefixed comment keys
(`"_doc": "start zephyr-host"`) — stripped by the loader, allowed by the
generated schema, and visibly marking "this fragment is documented" in the
file itself. A schema change touches the file once; every page follows.

## 4. Three tiers of docs testing

Chosen so each tier fails only for reasons *inside* the change it guards —
bed-dependent checks fail for reasons outside any docs edit, so they are
monitoring, never the push gate (and CI has no lab access regardless).

| Tier | What | Runs where | Fails when |
| ---- | ---- | ---------- | ---------- |
| 1. Validated includes | The example project's lab.json / settings.toml are validated against the live models by a unit test; a second test asserts every included marker still resolves | push gate | the schema or settings model changes, or a marker is orphaned |
| 2. Labless executables | `{doctest}` fences for what runs without the bed (model validation, `create_host_from_dict`, `LocalHost`); **labless captures** — `otto init` output, `--help`, `otto schema export` — refreshed by the capture runner and diffed in the gate | push gate (`nox -s docs`) | otto's own behavior or CLI text changes |
| 3. Bed captures | Real command output from real OSes (`otto host <id> probe`, connect-with-debug-log for Zephyr, monitor runs), captured into committed text artifacts that pages include | nightly diff; local refresh on demand | the bed's behavior drifts or otto's output changes |

Tier 3 is the direct answer to "real output from the different OSes": the pages
show genuine output, the output is pinned in git, and drift is detected without
ever making a docs edit depend on the lab being up.

## 5. The capture runner

A standalone script (`scripts/refresh_docs_captures.py`) exposed as a nox
session (`nox -s docs_captures -- --check --labless` joins the docs gate; the
Makefile gets a `docs-captures` convenience target for the full refresh) —
**not** a pytest module: anything collected under `tests/integration/` triggers
the autouse lab-reaping fixture, and the runner must be safe to invoke while
unrelated work is in flight.

- A capture manifest (TOML, next to the artifacts) lists each capture: id,
  command, target host/lab, `labless: bool`, and any redaction rules
  (timestamps, durations, transient ids → stable placeholders, so diffs are
  about content).
- `--labless` refreshes only tier-2 captures; the default refreshes everything
  and requires the bed.
- `--check` re-captures and diffs against the committed artifacts, exit 1 on
  drift, printing a unified diff per capture. The gate runs `--check --labless`;
  the nightly runs `--check`.
- Artifacts live at `docs/examples/getting-started/captures/<id>.txt` and are
  included by pages verbatim.

Redaction rules are the part most likely to need iteration; they are data in
the manifest, not code, so tuning them is not a code change.

## 6. Field-reference completeness guard

The todo's "fields not covered at all" finding gets a permanent fix in the
lab-v2 spec (§13 there): a unit test asserts every field of every registered
host spec, plus the element and `labs`-entry specs, appears in
`guide/configuration/lab-config.md`, so adding a spec field without documenting
it fails the gate. This spec relies on that guard rather than restating it: the
worked example links to the reference, so the example never needs to be
exhaustive, and the reference cannot fall behind the model.

## 7. Why this minimizes rework on the next interface change

A schema change forced by user feedback costs:

1. Edit the example project files (tier 1 goes red at the file, then green).
2. Run the capture runner (tier 2 in the gate, tier 3 locally against the bed).
3. Fix whatever prose the diffs and the completeness guard point at.

Nothing else. Prose that merely *includes* fragments and outputs is untouched;
prose that *explains semantics* is found by the diffs. Contrast with today's
`getting-started.md`, where lab.json fragments and command output are inline and
must be hunted by hand.

## 8. Page-content notes

- **defining-hosts/index.md** — the workflow in the order the todo recorded it:
  names, IPs, creds, hops, OS type first; then `otto host <id> probe`; then paste
  the pin. Explain the tri-state (`unset` = probe) once, here — and say plainly
  why the values are not booleans, since that is the question a reader who has
  just seen `"present"`/`"absent"` will ask (the answer is in Out of scope).
  Open with one short paragraph on **where hosts and reservations come from**:
  host facts are typed inline or referenced from an inventory (JSON, NetBox,
  or a backend you write — `library/inventory-backends.md`), and the lab may
  be gated by a reservation scheduler (the JSON file, or a backend you write —
  `library/reservation-backends.md`). Two links, no explanation: the reader
  learns on page one that both seams are pluggable and where the contract
  lives, before either the inventory page or the reservations page asks them
  to care. This is the earliest of the three §8a pointer sites.
- **defining-hosts/inventory.md** — once the inline lab loads: the two-layer
  rule in one paragraph (data lives in exactly one layer; keys may be asserted
  in both and must agree); the example hosts move from inline fields to
  `"inventory": "<key>"` plus a JSON inventory and `creds_file`, shown as
  fragments of the second lab form (§3); `otto inventory lookup`, `list` and
  the doctor as labless captures; the JSON → NetBox growth path as a
  **pointer** to `guide/configuration/inventory.md`'s Adoption path, which
  already walks all three stages and must not be restated — and, as that
  path's last step, "neither JSON nor NetBox" → `library/inventory-backends.md`,
  so the inventory plugin is reachable from the page that made the reader
  want one. Sits right after
  `index.md` because the todo is explicit that setting up the inventory is
  now part of defining hosts — but after, not instead: a reader with three
  machines and no inventory tool needs the inline form first, and the
  reference form is the growth step.
- **unix.md** — slim by design. Make the recommendation the todo asked for:
  probe stays worthwhile for older / non-GNU unix hosts; on modern GNU userlands
  every default already matches and probe is confirmation, not discovery.
- **busybox.md** — the `busybox` profile, applet declarations, and where the
  per-profile metric defaults live (pointer; the defaults review itself is a
  follow-up — see Out of scope).
- **zephyr.md** — the crucial page: connect with the default host class at
  debug log level, read the frame in the log, define the `CommandFrame`; then
  the oldest Zephyr host as the customization example. Captures for both.
- **customizations.md** — each override shown as an included fragment plus its
  monitor capture proving the stats land. Zephyr SNMP monitoring gets a capture
  if it works against the bed and an honest "not yet" note if it does not; a
  BusyBox stat the current defaults miss gets the same treatment (Out of
  scope). The note is prose beside a pinned capture, so the follow-up that
  closes the gap removes it by refreshing the capture.
- **boards-of-interest.md** — this is existing functionality
  (`[project] host_patterns` / `lab_patterns`, `repo_targets`); the page
  documents it with the busybox-hosts regex the todo asked for and shows the
  `all_hosts()` / `do_for_all_hosts()` effect via a labless doctest.
- **reservations.md** — last, because it is the team gate: the current hub
  already frames reservations as "setting otto up for a *team*", after the lab
  works for one person. Three parts.
  - *Setup.* `[reservations] backend = "json"` in `settings.toml`; `resources`
    declared at the lab, element and host levels in the example `lab.json`
    (the `rig` shape from `guide/cli/reservation/index.md`, on the real bed);
    and `otto reservation check` captured three times — refused, then after
    the identity holds the resource, OK. Every capture here is **labless**: the
    json backend reads a file, and loading a lab connects to nothing.
  - *Plugin definition* — the hard part, and the reason the page exists.
    A minimal custom backend checked into the example project (§3): what to
    implement, where the module lives, how `[init]` registers it, how
    `backend = "<name>"` selects it. Then `assert_reservation_backend_conforms`
    run against it as a **tier-2 doctest**, so the plugin the docs show is
    proven against the contract on every docs build, and a contract change
    reddens the page before it can mislead anyone.
  - *Pointers.* The contract rules, identity resolution, `-R`, fail-closed
    behaviour and the library API stay where they are:
    `guide/cli/reservation/index.md` and `library/reservation-backends.md`.

### 8a. Library-doc findability

`library/reservation-backends.md` and `library/inventory-backends.md` are
complete — selection in settings, the contract, `assert_*_conforms` — but a
user setting up either feature from the guide cannot find them: the
reservation one is reached only by a deep anchor from
`guide/cli/reservation/windows.md`, and the inventory one by no user-facing
page at all. Pointers at three depths fix that, from earliest to most specific:
`getting-started/defining-hosts/index.md` names both seams in its opening
paragraph (§8); `defining-hosts/inventory.md` and `reservations.md` each link
their own library page at the point the reader outgrows the shipped backend;
and `guide/cli/reservation/index.md` and `guide/configuration/inventory.md`
each gain a short "writing your own backend" section, so the reference pages
find the library docs too, not just the worked example.

Link, never move. The library section is where a backend *author* looks, and
moving it under Getting Started would make the worked example the second
reference §1 forbids.

## 9. Phasing

One implementation plan, three phases.

**Verify first.** The todo's smaller findings — fields missing from the
`otto init` README, an incomplete JSON schema, no host snippets, only one of
the two completion lines in `otto init`'s output — predate lab v2, which shipped
host snippets and the completeness guard (§6). The plan's first task checks
each one against main and turns only the ones still open into work. Nothing
that already landed is redone, and nothing is assumed closed because a spec
said it would be.

**Infrastructure** (example project, the inventory-equivalence guard of §3,
anchor-resolution and include-validation guards, the capture runner with its
manifest and the labless gate wiring, the hub page move to
`getting-started/index.md` with toctree and `{doc}` refs updated).

**Pages**, in the order defining-hosts → inventory → zephyr → busybox/unix →
customizations → boards-of-interest → reservations, each page landing with its
captures, plus the two §8a pointers with the page they serve. The order is the
reader's journey: one person defines hosts and grows into an inventory,
customizes them, scopes a repo to its boards, and only then sets up the team
gate. No page waits on a follow-up (Out of scope): a page whose capture shows
a gap a follow-up will close says so beside the capture and lands anyway.

## 10. Testing strategy (the guards must fail first)

- Tier 1 include-validation test: red by introducing an unknown key in the
  example lab.json; marker test red by renaming a marker.
- Capture `--check`: red by hand-editing one committed artifact; labless mode
  red by changing a `--help` string.
- Completeness guard: red by removing one field's row from `lab-config.md`.
- Inventory-equivalence guard (§3): red by changing one field's value in the
  referenced lab form so the joined hosts no longer match the inline ones.
- Reservations conformance doctest: red by breaking one contract rule in the
  example backend (returning a list where the contract says set, say); the
  `check` captures red by hand-editing the committed artifact.
- Existing doctest-fence linter and `sphinx -W` continue to run unchanged.

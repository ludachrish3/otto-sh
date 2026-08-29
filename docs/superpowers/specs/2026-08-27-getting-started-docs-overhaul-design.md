# Getting Started overhaul — a worked example built to survive interface change

**Date:** 2026-08-27
**Status:** Designed (this session); awaiting implementation plan
**Depends on:** `2026-08-27-lab-definition-v2-design.md` — host-entry pages are
written against the v2 shape and hold until it lands. Pages that never show
host-entry syntax (installation, init, completion, probe workflow framing) may
start earlier.

## 1. Goal

Rework the Getting Started section into a worked example that walks a new user
through defining otto's own unix and embedded labs from scratch, showing real
output from the real OSes — and structure the docs testing so that when otto's
first users force interface changes (likely), the rework cost is paid in data
files and captured artifacts, not in prose.

Two hard requirements from the todo: (1) setup procedures are **tested** so users
are never led astray; (2) no new duplication — the example is a concrete
instance, not a second reference.

### Out of scope

- `otto host probe` enhancements (recon, protocol guessing). The Zephyr page
  documents the probe + debug-log workflow *as it exists*.
- Monitor stat down-selection (`--monitor-stats`).
- Reworking the per-field reference (`guide/configuration/lab-config.md`) beyond
  keeping it complete — it stays the canonical field reference; the worked
  example links into it rather than restating it.

## 2. Page tree

`getting-started.md` slims to a hub: installation pointer (existing), a short
`otto init` paragraph with a pointer to the CLI page (removing the duplication
noted in the todo), completion setup, and links into the worked example.

```text
getting-started/
  index.md                 hub (install pointer, init pointer, completion, map)
  defining-hosts/
    index.md               workflow: names, IPs, creds, hops, OS → probe → pin
    unix.md                unix-specific settings (slim); is probe needed here?
    busybox.md             busybox profile specifics; the userland tri-state
    zephyr.md              default host class + debug log → CommandFrame;
                           defining the oldest Zephyr host (customization)
  customizations.md        login proxy, post-connect actions (custom prompt),
                           adding perf metrics, modifying metric commands/parsers
  boards-of-interest.md    [project] host_patterns / lab_patterns regex;
                           effect on all_hosts() / do_for_all_hosts()
```

Every page follows the same spine: *what you are deciding → the fragment you
write (included from the example project) → the command you run → its captured
output → where the full reference lives.*

## 3. The example project — one source of truth

The worked example is a **real, checked-in project**: `docs/examples/getting-started/`
holding the `settings.toml`, `lab_data/lab.json` (v2 shape; otto's real bed —
`test1..3`, the Zephyr hosts — with credentials as they appear in the existing
fixture data), and any customization module the pages show.

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
  the pin. Explain the tri-state (`unset` = probe) once, here.
- **unix.md** — slim by design. Make the recommendation the todo asked for:
  probe stays worthwhile for older / non-GNU unix hosts; on modern GNU userlands
  every default already matches and probe is confirmation, not discovery.
- **busybox.md** — the `busybox` profile, applet declarations, and where the
  per-profile metric defaults live (pointer; the defaults review itself is the
  probe workstream).
- **zephyr.md** — the crucial page: connect with the default host class at
  debug log level, read the frame in the log, define the `CommandFrame`; then
  the oldest Zephyr host as the customization example. Captures for both.
- **customizations.md** — each override shown as an included fragment plus its
  monitor capture proving the stats land. Zephyr SNMP monitoring gets a capture
  if it works against the bed and an honest "not yet" note if it does not.
- **boards-of-interest.md** — this is existing functionality
  (`[project] host_patterns` / `lab_patterns`, `repo_targets`); the page
  documents it with the busybox-hosts regex the todo asked for and shows the
  `all_hosts()` / `do_for_all_hosts()` effect via a labless doctest.

## 9. Phasing

One implementation plan, two phases: **infrastructure first** (example project,
anchor-resolution and include-validation guards, the capture runner with its
manifest and the labless gate wiring, the hub page move to
`getting-started/index.md` with toctree and `{doc}` refs updated), then
**pages** in the order defining-hosts → zephyr → busybox/unix → customizations
→ boards-of-interest, each page landing with its captures.

## 10. Testing strategy (the guards must fail first)

- Tier 1 include-validation test: red by introducing an unknown key in the
  example lab.json; marker test red by renaming a marker.
- Capture `--check`: red by hand-editing one committed artifact; labless mode
  red by changing a `--help` string.
- Completeness guard: red by removing one field's row from `lab-config.md`.
- Existing doctest-fence linter and `sphinx -W` continue to run unchanged.

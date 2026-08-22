# Test-strategy upgrades and the unix-lab rename

**Date:** 2026-08-22
**Status:** approved design, awaiting implementation plans

## Motivation and provenance

This design came out of a comparison with the Linux kernel's testing
method, chosen because the kernel shares otto's central problem: a support
claim spanning many OS versions, ISAs, and (for otto) Python versions,
where the combinatorial space is far larger than any enumerated test
matrix. Much of the kernel's approach otto already practices — feature
probing over version checks, quirk tables with measured evidence (the
BusyBox gap registry), seed-reproducible randomness (the chaos lane),
sanitizers armed in default gates (leak detectors, BedHygiene). Three
kernel ideas are genuinely missing, and they are what this spec adds:

1. **`randconfig` sampling** — the kernel's answer to a config space it
   cannot enumerate is to sample it forever with a recorded seed. otto's
   analogue is a seeded sampler over the backend/host axis space, running
   a shared conformance suite against whichever cell it draws.
2. **A machine-checked support matrix** — the kernel generates
   `Documentation/features/` from the tree. otto's analogue generalizes
   the BusyBox gap registry to every host profile, generated from what
   the suite actually measured, never hand-asserted.
3. **Systematic error-path injection** — the kernel's fault-injection
   framework treats error paths as first-class test targets. otto's
   `sweep_cancellation()` already walks every await point; it needs a
   richer fault vocabulary, not a new harness.

Two kernel practices were considered and deliberately rejected:
merge-first-find-breakage (otto gates before every squash instead), and
tolerance for silent skips on unavailable hardware (otto fails loudly,
naming the host).

Alongside the strategy work, the produce-themed lab naming
(veggies/fruits, carrot/tomato/pepper, basil, sprout\*) is retired. The
rename rides this workstream deliberately: the new suites will reference
hosts by name, so the rename lands first and the new work is born
neutral. Labs remain named, first-class entities (`--lab`, `OTTO_LAB`,
the `labs` field); nothing here replaces lab names with roles.

## 1. Naming decision record

### Labs and hosts

| Lab (old → new) | Host (old element → new element) | Old id → new id | IP |
| --- | --- | --- | --- |
| `veggies` → `unix` | `carrot` → `test1` | `carrot_seed` → `test1` | 10.10.200.11 |
| | `tomato` → `test2` | `tomato_seed` → `test2` | 10.10.200.12 |
| | `pepper` → `test3` | `pepper_seed` → `test3` | 10.10.200.13 |
| `fruits` → `unix_alt` | `orange` → `alt1` | `orange_seed` → `alt1` | 10.10.200.11 |
| | `apple` → `alt2` | `apple_seed` → `alt2` | 10.10.200.12 |
| | `grape` → `alt3` | `grape_seed` → `alt3` | 10.10.200.13 |
| `embedded` (kept) | `basil` → `test4` | `basil_seed` → `test4` | 10.10.200.14 |
| | `sprout` → `zephyr37_fat` | `sprout` → `zephyr37-fat` | 192.0.2.1 |
| | `sprout_lfs` → `zephyr37_lfs` | `sprout-lfs` → `zephyr37-lfs` | 192.0.2.5 |
| | `sprout_no_fs` → `zephyr37_nofs` | `sprout-no-fs` → `zephyr37-nofs` | 192.0.2.37 |
| | `sprout27` → `zephyr27_fat` | `sprout27` → `zephyr27-fat` | 192.0.2.13 |
| | `sprout44_lfs` → `zephyr44_lfs` | `sprout44-lfs` → `zephyr44-lfs` | 192.0.2.29 |
| | `sprout_cov` → `zephyr37_cov` | `sprout-cov` → `zephyr37-cov` | 192.0.2.33 |
| | `sprout_cov44` → `zephyr44_cov` | `sprout-cov44` → `zephyr44-cov` | 192.0.2.34 |
| `busybox` (kept) | unchanged (`bb1161`…`bb1350`, board `qemu`) | unchanged | unchanged |

Notes:

- **`board: seed` is dropped** from test1–test4 and alt1–alt3, so their
  ids are exactly their element names — matching the Vagrant VM names
  1:1 for test1–3 and removing the standing VM-name → element → id
  translation layer. Board-in-id composition stays exercised on the bed
  by the busybox lab (`bb1161_qemu` …) and in unit fixtures.
- **Zephyr guest names encode facts**: `zephyr<version>_<fs>` for the
  behaviour guests (`fat` covers the FAT-on-RAM-disk build; the RAM-disk
  detail stays a build note). The two coverage bases carry their role
  (`_cov`) instead of an fs type — coverage is their distinguishing
  fact and their fs is a build detail.
- **`slug()` is untouched** (stability contract in `remote_host.py`):
  `_` in an element still becomes `-` in the id, exactly as today.
- **`unix_alt` exists solely as the per-project scoping discriminator**:
  same three IPs as `unix`, different lab and element names, proving
  per-project lab scoping (repo1 → unix, repo2 → unix_alt) and
  multi-source lab loading. It is not a second bed.
- **Vagrant VM names are unchanged** (`test1`–`test3` already match;
  the `zephyr` VM keeps its name — `test4` is its element in lab data,
  and the VM is also the SDK/QEMU box).
- `project_env` sets `OTTO_LAB=unix`.

### What is not rewritten

Historical records keep their vocabulary: `docs/superpowers/specs/*`,
`docs/superpowers/plans/*`, `CHANGELOG.md`, git history. Everything live
is swept: `src/`, `tests/` (including the `repo*` fixture repos),
`docs/` guide and architecture pages, `noxfile.py`, `Makefile`,
`Vagrantfile` (comments and provisioning tables), `.vscode/launch.json`,
`todo/`, `project_env`.

## 2. Seeding architecture: one root seed

`pytest-randomly` (already an active direct dependency) provides the
root seed for all sanctioned randomness. The rules:

- **The root seed is pytest-randomly's session seed** — printed in the
  pytest header, pinned with `--randomly-seed=N`. One number reproduces
  test order plus every derived draw.
- **Test infrastructure never draws from the global `random` module.**
  pytest-randomly reseeds it per test, but any other consumer mid-test
  would shift the stream; every lane uses a dedicated `random.Random`
  seeded from the root.
- **The conformance sampler** (§4) derives its cell draws from the root
  seed via `request.config.getoption("randomly_seed")`. No new env var.
- **The chaos lane** keeps `OTTO_CHAOS_SEED` as an explicit pin
  override (documented, proven, convenient for bed scripting), but its
  *default* seed derives from the root seed instead of `os.urandom(4)`.
  The printed `chaos seed: N (reproduce with OTTO_CHAOS_SEED=N)` line
  stays — it remains the failure-capture reproduce handle.
- `os.urandom`-as-seed disappears from test infrastructure. A lint or
  ast-grep guard banning fresh entropy sources under `tests/` (outside
  the seed module) is a plan-level option, not a requirement.

## 3. Host profiles: the machine-readable axis space

Both new features consume the same description of what a host cell *is*.
A **profile** is a dataclass:

```
Profile(os_type, userland,        # "gnu" | "busybox-<ver>" | "zephyr-<ver>"
        terms, transfers,          # the valid_* menus
        hop_depth, docker_capable)
```

It is **derived, not declared**: `lab.json` already carries
`valid_terms`, `valid_transfers`, `hop`, `docker_capable`, `os_type`;
userland flavor/version comes from the existing userland layer's
knowledge of each bed host. One resolver module in `tests/_fixtures/`
(`profiles.py`) maps a host id → `Profile` and enumerates a lab's axis
space (every `(host, term, transfer)` combination the menus permit).
Nothing lands in `src/otto` for this; if a product feature later needs
profiles, promotion is a separate decision. No otto behavior changes.

## 4. The conformance suite and the randconfig sampler

### One suite, two venues

A new top-level `tests/conformance/` directory (precedent:
`tests/busybox/` already sits outside the unit/integration/e2e level
tree and is invoked by its own make target). The suite asserts the
shared host contracts against a **resolved cell** — a concrete host
reached through a concrete `(term, transfer)` pair:

- **exec contract**: command runs; exit code, output frame, and
  timeout behavior match the documented semantics.
- **transfer contract**: put/get roundtrip preserves content and mode.
- **timeout contract**: a command that exceeds its budget fails the
  documented way.

The initial contract set is deliberately small; contracts grow as
surfaces earn matrix rows (§5).

The venue switch follows the `chaos_target` / `OTTO_CHAOS_DOCKER`
precedent:

- **Hermetic venue (default)** — no lab required. Cells resolve to:
  loopback `sshd` (GNU over ssh, reusing `make_loopback_target`),
  BusyBox artifacts as local subprocess hosts (reusing the artifact
  tier's fetch-and-verify layer), and the runner's own userland via the
  local host. Cells the venue cannot build (real telnet consoles,
  hops > 0, Zephyr) are excluded from its sample space, not skipped.
- **Bed venue (`OTTO_CONFORMANCE_BED=1`)** — cells resolve to real bed
  hosts: test1–3 (GNU), the five bb guests (BusyBox over telnet,
  hopped), the Zephyr guests. Dev-VM only, run via `make conformance`.

A host that should be reachable in the selected venue and is not fails
loudly, naming the host — never a skip.

### Sampling

Each run samples `N` cells (default 8; `OTTO_CONFORMANCE_CELLS=N|all`)
from the venue's resolvable space, drawn from a `Random` seeded off the
root seed (§2). The reproduce handle is `--randomly-seed=N` from the
pytest header. Every run logs which cells it drew and how many the
venue's space held, so a shrinking space is visible, not silent.

### Selection and guards

`tests/conformance/` gets its own auto-stamped `conformance` marker
(conftest hook, mirroring the level-stamp pattern) and stays out of
every default gate the same way `tests/busybox/` does. The marker-drift
guards in `test_tier_marker_invariants.py` grow to cover the new lane:
the stamp fires (G1-style), and no catch-all or resource-slice selection
can co-select it (G4/G7-style).

### CI

`nightly.yml` gains a `conformance-hermetic` job: prime the artifact
cache, run the suite at its default sample size on the hostless runner.
It fails on two conditions:

- a **contract violation** in any drawn cell, and
- **matrix drift** — a drawn cell that the committed
  `schemas/support_matrix.json` (§5) records as `measured-ok` but that
  fails fresh measurement (or vice versa). CI thereby re-confirms the
  committed artifact for hermetic-resolvable cells continuously, not
  just the code.

The job uploads its observation records (§5) as a workflow artifact so
they can be collated locally; it never writes the matrix itself.

Sampling stays a nightly concern, not a per-PR gate: selection is
random per run, so a per-PR gate could fail an unrelated PR on
pre-existing breakage in a never-before-drawn cell — the same reason
the kernel keeps randconfig in -next and 0-day rather than per-patch
CI.

## 5. The machine-checked support matrix

Generalizes the BusyBox gap-registry pattern to
`{surface} × {profile}`, where surfaces start as the conformance
contracts (§4) and profiles come from §3. Each cell holds one of:

- `measured-ok` — evidence: test nodeid, venue, `as_of` date.
- `measured-broken` — same evidence fields plus the failure summary,
  in the spirit of the existing gap registry's verdicts.
- `untested` — the default. Matching the BusyBox stance: untested, not
  unsupported.

Bed-only cells (telnet consoles, hops, Zephyr, the live bb guests) can
only ever be measured by local bed runs — GitHub runners have no lab —
so their currency is bounded by how often `make conformance` runs on
the dev VM, and the rendered `as_of` dates keep that staleness visible
rather than silent. Hermetic-resolvable cells are re-confirmed nightly
by the drift gate (§4).

Mechanics:

- A conformance run **emits an observation record per cell** it
  exercised (JSON, into the run's xdir output).
- `make conformance` ends with a **collate step** that folds
  observations into a committed `schemas/support_matrix.json`. The
  collate step also accepts observation artifacts downloaded from the
  nightly `conformance-hermetic` job, recording `venue: ci-hermetic` in
  the evidence — so hermetic rows can carry CI-measured dates without a
  dev-VM run. Chris commits every update; CI never commits, keeping a
  person in front of every verdict that enters the matrix.
- A docs page (`docs/architecture/` alongside the busybox-support page)
  renders the committed JSON at docs-build time, following the
  busybox-support render pattern.
- Guards riding the unit gate: the committed file validates against its
  schema; every `measured-*` cell carries complete evidence fields; the
  renderer fails on a surface or profile the current tree no longer
  declares. Cells can only become `measured-*` through collation, so a
  hand-edit that fabricates a verdict without evidence fails the guard.

## 6. Fault-injection vocabulary

`sweep_cancellation()` keeps its harness — per-await-point injection,
integer-counter determinism, oracle-asserted invariants — and grows its
fault table. Phase 1 adds **exception-shaped faults only**:

- timeout (otto's timeout exception type),
- connection-reset (distinct from the existing `ConnectionDropped`),
- command-failure (the exception shape a failed command surfaces as in
  the layers that raise).

Each fault entry carries an applicability predicate (not every fault is
meaningful at every await point; the fake declares what can happen
there). Existing call sites pick up the grown default table — if a new
fault reveals a bug in a swept chain, that is the feature working.
Sweep cost scales as points × faults over no-I/O fakes; going from two
faults to five stays cheap, and stays in tier 1 (default gate).

**Deferred, explicitly:** data-shaped faults — truncated reads,
interleaved output, partial writes. They mutate returned bytes rather
than raising, need oracle-aware handling, and are future work this spec
records so the deferral is visible.

## 7. Rename mechanics

The rename is **item 1** of the sequence (§8) so every later item is
born neutral.

- **Lab data**: element renames and `board` drops in
  `tests/_fixtures/lab_data/tech1/lab.json` and `tech2/lab.json`; link
  endpoint ids updated to match (link ids derive from host ids).
- **The sweep is a per-file disposition map, not a blind sed.** Every
  match of the old vocabulary gets a disposition (update / historical,
  keep / false positive, keep). Known false positives the map must
  exclude: `applet` (BusyBox terminology), CSS/color uses of `orange`,
  platform strings in `uv.lock` (never hand-edited anyway), and `seed`
  as RNG terminology (chaos seed, randomly seed) — only the *board*
  `seed` disappears, and it disappears by field drop, not word sweep.
- **Exit criterion** — this grep, word-bounded and case-insensitive,
  returns hits only in the historical allowlist
  (`docs/superpowers/specs/`, `docs/superpowers/plans/`,
  `CHANGELOG.md`):

  ```
  grep -rniE '\b(fruits?|veggies?|carrot|tomato|pepper|basil|sprout[a-z0-9_-]*|orange|apple|grape)\b' \
    --exclude-dir=.git --exclude-dir=node_modules --exclude=uv.lock .
  ```

  Both element and id spellings (`_` and `-`) of every sprout name are
  covered by the `sprout[a-z0-9_-]*` arm.
- **Doctests count as code**: the `veggies`/`carrot_seed` examples in
  `src/otto` docstrings run in the default gate and are updated with
  everything else.
- **Bed verification before the squash**: `make coverage-unix` and
  `make coverage-embedded` on the dev VM, plus the standard full gates.
  The chaos lane is not run for this item unless Chris asks.
- Stale completion caches on developer machines regenerate on next use;
  no migration needed.

## 8. Sequencing and landing plan

One item = one implementation plan = one fully-gated squash onto main,
in this order:

1. **Rename** (§1, §7)
2. **Profile foundation** (§3)
3. **Conformance suite, hermetic venue + nightly job** (§4)
4. **Bed venue + `make conformance`** (§4)
5. **Support matrix, collation, render, guards** (§5)
6. **Fault vocabulary phase 1** (§6)

Items 2–6 each update `docs/architecture/testing.md` as they land their
piece (the conformance lane, the matrix, the fault table), so the
strategy page stays the single narrative of the taxonomy.

## Out of scope

- Renaming Vagrant VM definitions (`zephyr` stays `zephyr`).
- Data-shaped fault injection (§6, deferred).
- Promoting the profile resolver into `src/otto`.
- An arm64 CI leg for the Python matrix (raised during design, cut by
  decision).
- Any change to lab semantics: labs stay named, `--lab`/`OTTO_LAB`
  unchanged, `slug()` untouched.

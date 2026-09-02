# Cache management and the home I/O budget — design

**Date:** 2026-09-02
**Status:** draft for review
**Follows:** `2026-09-01-startup-io-budget-design.md` (the cache this manages; the instrument this extends)

## Problem

Two leftovers from the startup I/O work, plus one discoverability defect:

1. **The otto home grows without bound.** Every distinct `OTTO_SUT_DIRS` set gets a
   workspace dir under `otto_home()` (`~/.otto` or `$OTTO_HOME`) holding its completion
   cache — and nothing ever removes one. A dev VM accumulated 8,843 stale dirs. On an
   NFS home with quotas and backups, accumulation is the dominant operator complaint;
   per-invocation latency is not (measured: a warm `--help` touches the home exactly
   3 times — 1 open + 2 stats. That is the empty-home floor: a present
   `settings.toml` adds one open, and a cached inventory backend adds a
   per-invocation snapshot fingerprint read — the largest home-side read there is).
2. **Cache management hides in a root eager flag.** `--clear-autocomplete-cache` is
   invisible next to `otto <noun>` command groups, covers only "clear", and can never
   grow prune/inspect semantics as a flag.
3. **Home I/O is invisible to the release gate.** The import-budget harness pins
   `OTTO_HOME` to a temp dir and gates only fixture-root opens (`open_fixture`), so the
   3-touch property that makes NFS homes cheap has no guard.

## Goals

- `otto cache` command group: **info**, **clear**, **prune** — cache management that is
  discoverable, scriptable, and safe by construction.
- Bounded home growth via `prune`, without ever touching durable state.
- `open_home` joins the gated I/O counters: the home footprint becomes a budget.
- One docs section: operating otto when `$HOME` is on NFS, including the
  `OTTO_HOME=<local disk>` relocation experiment that will decide whether a dedicated
  cache-dir option is worth building.

## Non-goals

- A dedicated cache-dir setting (`OTTO_CACHE_DIR` / XDG split). Explicitly deferred:
  the `OTTO_HOME` relocation experiment on the real NFS deployment decides whether it
  earns a place. This spec must not make that future split harder.
- Pruning `inventory-cache/` (home-level inventory snapshots). Different lifecycle,
  different owner (host-inventory spec §9.5); noted as future work.
- Reducing warm home I/O below its floor. The floor is 1 open (the cache file) +
  2 stats (cache existence, user `settings.toml` probe — when the file exists, the
  probe's stat is followed by an open to read it); going lower means dropping
  user-settings support. There is no room; the counter's job is to hold the line.

## What lives where (measured facts the design rests on)

```
otto_home()                        ~/.otto or $OTTO_HOME   (never enumerated by otto at runtime)
├── settings.toml                  user settings — DURABLE
├── inventory-cache/               inventory snapshots — out of scope
└── <workspace key>/               [0-9a-f]{8}-<slug≤40>, slug may be "no-repos"
    ├── completion_cache.json      rebuildable CACHE — prune target
    ├── remote_completion_cache.json  rebuildable CACHE (remote-path sidecar) — prune target
    └── env/                       a real virtualenv from `otto env create` — DURABLE
```

- Reads are pure: `otto_home()` / `workspace_home()` never create directories.
- Writes are rare (rebuild-gated) and atomic (tempfile + `os.replace`); the
  read-validate-serve path takes no locks and involves no lock daemon (NFS lockd
  cannot wedge otto). One exception outside that path: tab-time test collection
  guards itself with a short-lived `O_EXCL` lockfile (plain file creation with
  staleness-steal — still no lockd).
- Section digests are STAT-based for key files — `hash_file()` hashes
  `(path, mtime_ns, size)`, never file bytes — and mtime/size are NFS *server*
  state, identical across clients, so a home SHARED by several machines still
  validates consistently: a cache written on machine A validates identically on B
  (provided both mount the project at the same path — a path mismatch is a clean
  miss, not wrongness); racing writers are last-writer-wins with both versions
  valid. (The one non-stat component: a cached inventory backend folds the
  snapshot's content hash into the digest — which agrees cross-machine at least
  as well.) Stale attributes on the *cache file* cost one redundant rebuild;
  stale attributes on a *source* file can leave another machine's edit unseen
  until the attribute cache expires (bounded by the cache's 24h TTL) — the same
  exposure any stat-validated cache has.

## Component 1: the `otto cache` group

New `src/otto/cli/cache.py`, `lab_free=True`, registered through the same lazy builtin
dispatch as `host`/`docker` — root help lists it without importing the module, so the
gated surfaces gain no import cost.

### `otto cache info`

Read-only. One Rich rounded table (house style for dense CLI output):

- the resolved home path and whether it came from `$OTTO_HOME`;
- per workspace dir: key, cache size, cache age, whether an `env/` is present;
- totals: workspace count, cache bytes, count-over-prune-threshold.

Sorted oldest-cache-first so the prune preview reads top-down. `info` is the measuring
cup for the NFS relocation experiment.

### `otto cache clear [--all]`

- Bare: today's flag semantics exactly — unlink the CURRENT workspace's
  `completion_cache.json` AND its remote-path sidecar `remote_completion_cache.json`
  (the existing `clear_cache()` + `clear_remote_cache()` seams — the flag always
  cleared both, and "a user reaching for the escape hatch wants completion state gone,
  not one half of it" carries over). Report which files were removed.
- `--all`: unlink both cache filenames in every matching workspace dir, then remove
  any dir left empty. Age-blind (clear means clear).

### `otto cache prune [--age DAYS] [--dry-run]`

The GC. Defaults: `--age 60`.

- **Candidate set:** direct children of `otto_home()` whose names match the workspace
  key pattern `^[0-9a-f]{8}-` (the `workspace_key()` format). `settings.toml`,
  `inventory-cache/`, and anything unrecognized are structurally out of reach —
  the matcher is the safety boundary, not a skip-list.
- **Action:** within a candidate, unlink each of the two cache filenames
  (`completion_cache.json`, `remote_completion_cache.json`) iff that file's mtime is
  older than the cutoff. Then `rmdir` the candidate iff it is now EMPTY — `rmdir`, never a
  recursive delete, so a dir holding `env/` (a real virtualenv) or anything else
  survives by construction. Symlinked candidates are skipped, not followed.
- `--dry-run`: print the victim list and byte total, delete nothing.
- Report: files removed, dirs removed, bytes freed, dirs retained (and why: young /
  non-empty).

Concurrency note: pruning a cache another invocation is mid-read of is safe — the
reader holds an open fd (POSIX unlink semantics) or misses and falls back to a full
load (cache-or-load). No locking is introduced.

### Root flag removal (BREAKING → 0.10.0)

`--clear-autocomplete-cache` (cli/main.py:659) is deleted outright, on the 0.10.0
breaking train. Changelog line; docs references move to `otto cache clear`. No hidden
alias — it was an escape hatch, not settled API.

## Component 2: `open_home` joins the gated counters

`scripts/import_budget.py`:

- The child preamble attributes opens under the surface's (temp) `OTTO_HOME` by
  realpath'd prefix — the same mechanism, hardening, and dedup as `open_fixture`.
- `open_home` is added to `GATED_IO_COUNTERS`; every `<key>.io.<minor>.txt` golden
  regenerates on all five interpreters (the established nox route). Expected values are
  MEASURED, not asserted here — warm surfaces should show the single cache-file open;
  cold repo-bearing surfaces additionally show the write path (tempfile open).
- The audit layer has no stat event (established fact), so the two warm stats ride
  invisibly. The golden gates the open; the docs state the full 3-touch figure.
- Guard updates: the io-payload-keys test admits the new key; the harness pin test
  extends to whatever invariant `open_home` depends on (the temp-home pin already
  asserted). Red-proofs: one injected home-side open → named failure; counter
  dead-mutation (`+= 0` / `return 0`) → named failure. Both observed, both restored.

## Component 3: docs

- `docs/guide/startup-performance.md` gains **"When `$HOME` is on NFS"**: the 3-touch
  measured footprint; shared-home safety (stat-based digests, atomic rename,
  lock-free serve path); accumulation and `otto cache prune`; and the relocation
  experiment —
  `export OTTO_HOME=/local/disk/otto`, with its two caveats (user `settings.toml`
  moves with it and must be copied; env activation state moves too; each machine pays
  one cold start per workspace). Outcome of that experiment decides the future
  cache-dir option.
- The cache-economics section links `otto cache info/clear/prune` as the management
  story; the CLI docs tree gains the group's page per the existing per-command pattern.
- Everywhere else that mentioned the removed flag links to `otto cache clear`
  (one home per topic; link, never restate).

## Testing

- **Prune semantics (unit):** age boundary honored (59d survives, 61d goes at
  `--age 60`); `env/`-bearing dir loses its cache file but the dir survives; a
  non-matching dir name is untouched even with a `completion_cache.json` inside;
  `--dry-run` removes nothing while listing correctly; empty-dir rmdir happens;
  symlinked candidate skipped; `settings.toml`/`inventory-cache` unreachable.
  Every guard proven by mutating the implementation, never the test.
- **clear/--all (unit):** current-workspace scope matches `clear_cache()` today;
  `--all` + rmdir-empty.
- **info (unit):** table rows against a constructed home; env-presence column.
- **CLI (e2e, one test):** `otto cache prune --dry-run` through the real subprocess
  harness (which now isolates `OTTO_HOME` — the fixture home is constructible).
- **Budget:** module snapshots pick up `otto.cli.cache` only where it genuinely loads;
  caps only move down or by the measured delta of the deliberate new group.

## Versioning and rollout

Breaking (flag removal) → ships on the 0.10.0 train with the docker breaking work.
The `otto cache` group itself is additive and could land earlier, but the flag removal
and group should ship together so the changelog tells one story: "cache management
moved to `otto cache`".

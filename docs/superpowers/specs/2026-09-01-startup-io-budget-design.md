# Startup I/O budget — cutting otto's filesystem cost

**Date:** 2026-09-01
**Status:** Design — revision 2, approved for planning

**Goal, stated precisely:** reduce **absolute filesystem operations**, independent of any
deployment's round-trip time. Latency is what made the cost visible; the cost is the I/O itself,
and removing it helps every deployment.

> **Revision 2 (2026-09-01)** — adversarial review falsified four claims in revision 1. Corrected
> here: the shim's location (r1 put it where it saves nothing); the `pathlib.Path.rglob` gate (otto
> uses `os.walk`, so the gate could never fail); a reference to an untagged-build banner that does
> not exist; and the cache split, which r1 specified without the consumer that would read it or the
> storage-layout change that makes it possible. The problem analysis below survived review intact.

## Problem

`otto --version` takes ~3 s cold and ~1 s warm on an air-gapped deployment where both otto's venv
and the user's sut dirs live on NFS. The same command takes 0.21–0.22 s on a local-disk dev VM.

Measured on the deployment: **RTT ≈ 1.2 ms**, `__pycache__` **is** writable (stale-bytecode
recompilation ruled out). Corpus size ≈ **500 files**.

## Cost model

```
wall_time ≈ fs_RTT × path_syscalls_not_absorbed_by_client_cache
```

Dev VM, local disk, warm, otto 0.9.0, CPython 3.10.20:

| Measurement | Value |
|---|---|
| bare interpreter floor | 200 path syscalls |
| `otto --version`, no repos | 2,427 |
| `otto --version`, 205-file repo | 3,162 (+735); 233 opens under the test tree |
| `otto --version`, 500-file repo / 25 dirs | 3,862 (+1,435); 553 opens under the test tree |
| `import otto.version` + `get_version()` — the true shim path | **582** |
| `import otto.cli` alone | 440 modules |
| `import otto.config` alone | pulls 46 `otto.host.*` modules |

2,427 × 1.2 ms = **2.9 s** against an observed 3 s cold. The warm ~1 s is ≈ 830 effective
round-trips: the client absorbs most attribute lookups but not file reads or writes.

Three independent terms, each removed by a different fix:

| Term | Scales with | Removed by |
|---|---|---|
| Framework imports (2,427, paid at module import) | fixed | Fix A |
| Cache rebuild — reads + `ast.parse` + a write | corpus size | Fix B |
| Fingerprint validation walk — `os.walk` + per-file `stat` | corpus size | Fix C |

## Verified findings

1. **The cache is rewritten on every invocation.** `otto/cli/main.py:865-895` calls
   `write_cache(..., tests=collect_test_names(result.repos))` unconditionally after bootstrap.
   Proven by nanosecond mtime across consecutive runs — second-granularity mtime hides it.
2. **`otto --version` reads the whole corpus** — 553 opens under a 500-file tree, to print a
   version string.
3. **`entry()` runs full `bootstrap()` before argv parsing** (`main.py:808-847`).
4. **`otto/cli/__init__.py` is `from .main import app`**, so `import otto.cli` alone loads **440
   modules**. Any shim under `otto.cli` triggers that before its first line. `otto/__init__.py` is
   already PEP-562 lazy (import-budget cap 19), so the shim must live at **`otto/_shim.py`**.
5. **The eager import chain is not where importtime points.** `otto/config/__init__.py` eagerly
   re-exports from `.fleet` *and* `.lab`; `config/lab.py:339-340` imports `otto.labs.*` at module
   level and `labs/json_repository.py:11` pulls `otto.host.factory`. `import otto.config.lab` alone
   loads 46 `otto.host.*` modules. `importtime` charges a shared subtree to whichever module reaches
   it **first**, which is why `fleet.py:14`'s `DEFAULT_COMMAND_TIMEOUT` looked decisive. Moving that
   constant saves ~0 and does not remove the tach edge (`tach.toml:143-147` records three causes,
   and tach counts function-scope imports as edges).
6. **`iter_test_files` is top-level and non-recursive** (`config/repo.py:903`), so registration
   reaches the registry only from init modules and top-level test files.
7. **The corpus walk uses `os.walk`, not `rglob`.** `_match_py_files`
   (`completion_cache.py:416-443`) says so in its own docstring, and both `collect_test_names` and
   `compute_fingerprint`'s enumeration go through it. **A `pathlib.Path.rglob` gate would be a guard
   that cannot fail.** `os.walk` fires one `os.scandir` audit event per directory, so **`os.scandir`
   is the corpus-walk signal.**
8. **The cache's top-level key *is* the full-corpus fingerprint** (`write_cache`:894,
   `existing[fingerprint] = ...`). A names-only reader therefore cannot even *locate* its entry
   without performing the walk it exists to skip. Any section split must change the storage layout.

## Design

### Fix A — Console-script shim at `otto/_shim.py`

The 2,427-syscall framework term is paid at **module import**, before `entry()` runs. Skipping
bootstrap inside `entry()` cannot remove it, because the entry module *is* the cost.

`[project.scripts]` moves from `otto.cli.main:entry` to `otto._shim:main`. The shim imports nothing
from otto at module scope; for exact `sys.argv[1:] == ["--version"]` it imports `otto.version` and
prints, otherwise it defers to `otto.cli.main.entry`.

**Exact match, never membership** — `otto put --version` is a real subcommand invocation needing the
registry.

Measured ceiling: the shim path costs **582** syscalls against a 200-syscall bare-interpreter floor,
so ~1,845 are removed. `importlib.metadata.version()` is a meaningful share of the 382 above floor;
if that matters later, it is a separate, self-contained optimisation.

This subsumes revision 1's separate `--version` dispatch case inside `entry()`. One dispatch site,
not two: once the shim intercepts, `entry()`'s copy is unreachable through any supported invocation.

**Packaging change — requires reinstall.** Note in release notes.

### Fix B — Stop rebuilding the cache on every command

Gate the `collect_test_names` + `write_cache` block (`main.py:876-895`) on cache validity.
`read_cache` already returns `None` for every miss reason, so a non-`None` return means current.

Two details review surfaced:

- **Do not compute the fingerprint twice.** On a miss, `read_cache` computes it and `write_cache`
  computes it again. Thread the computed digest through.
- **When no cache can be written at all** (`_cache_path()` is `None`), skip collection entirely
  rather than collecting and discarding at `write_cache`.

### Fix C — Section registry, with its consumer

Revision 1 specified two hand-rolled sections. That is the wrong growth shape — each new cached item
means another digest function, another `section=` branch, another keyword on an already
`noqa: PLR0913` writer, another schema bump — and it did not address finding 8.

A section is a registration:

```
(name, key_paths(repos) -> list[Path], collect(result) -> payload)
```

with one shared digest (`_hash_file`) over the section's key paths, one generic reader that
validates only the named section, and one generic writer that updates only its section. Taint is a
per-section field. The 3rd through 10th cached item is a three-line registration.

**Storage layout (finding 8).** One file — one open per read is the network-filesystem optimum and
must be preserved. The entry becomes:

```
{"schema": N, "sections": {"<name>": {"fingerprint", "generated_at", "tainted", "payload"}, ...}}
```

The full-corpus fingerprint stops being the top-level key, so a section can be located and validated
without walking the corpus.

Two sections at introduction:

| Section | Key paths | Serves |
|---|---|---|
| `names` | init trees ∪ `.otto/settings.toml` ∪ pytest configs ∪ **top-level** test files | commands, instructions, suites, hosts |
| `tests` | the full corpus walk | `--tests` completion |

Sound because of finding 6: everything that can register lives in the `names` key set, and that set
is small. Keying by *file kind* would not be — an instruction may register from an init module or a
top-level test file, both under the same `registering_repo` seam (`bootstrap.py:272-289`).

**The consumer ships in the same release.** Root help and completion resolve names via
`read_cache(section="names")`, falling back to full bootstrap on a miss. Without this the section
work removes no I/O — revision 1's central omission. **Cold-cache help stays correct always**; a
degraded "third-party commands not loaded" help was considered and rejected.

`names` covers **root help and completion only**. Sub-app help (`otto run --help`) needs
per-instruction help, and the cached option payload degrades to `options: []` on serialization
failure (`completion_cache.py:957-969`) — built for completion, never certified for help rendering.
Deferred.

**Taint.** `entry()` writes the cache even when `result.errors` is non-empty. A tainted entry must
never serve `names`, or help goes silently and permanently partial: the broken file's hash is stable
until edited, so the fingerprint never moves.

### Fix D — Lazify the eager config chain

Move the `.fleet` / `.lab` re-exports in `otto/config/__init__.py` into the existing `_LAZY_EXPORTS`
PEP-562 table (`config/__init__.py:64-78`), and defer `lab.py:339-340`'s `otto.labs.*` imports into
their call sites.

Guard the **help surface**, not merely `import otto.config`: `cli/callbacks.py:5`, `cli/host.py:13-14`,
`cli/docker.py:37-38`, and `cli/monitor.py:30` all pull config names at module level and would
re-trigger the chain.

Drop revision 1's `DEFAULT_COMMAND_TIMEOUT` move and its tach claim (finding 5).

### Fix E — Extend the import-budget system

`scripts/import_budget.py` measures non-stdlib module count with `OTTO_*` stripped, so zero repos are
discovered and the per-repo term is invisible. Module count also cannot see filesystem work at all.

**I/O counters — audit events only.** Verified on 3.10.20 and 3.14.3:

| Signal | Event | Notes |
|---|---|---|
| file read | `open` | `Path.read_text` fires it |
| directory walked | `os.scandir` | **42 on both 3.10 and 3.14** for the same 500-file/21-dir tree; `os.walk` fires one per directory — **this is the corpus-walk signal** |
| — | `pathlib.Path.rglob` | otto does not use it on this path (finding 7). **Not a gate.** |
| — | `pathlib.Path.glob` | fires on 3.10 when called directly, and during `rglob` only on 3.14. **Never gate its count.** |

**No `os.stat` wrapper.** On 3.10 `pathlib` binds its accessor at import, so patching `os.stat`
afterwards is invisible to `Path.stat()` — measured 0 against 500 real stats. There is no `os.stat`
audit event. A stat-only walk therefore has **no per-file signal**; it is observable at *directory*
granularity via `os.scandir`, which is why the fixture must scale directory count independently of
file count.

**Surfaces that run the real `entry()`.** The existing `_CHILD_CLI` deliberately bypasses `entry()`
and help rendering to exclude rich-markdown/pygments as measurement artifacts. A surface family that
does not run `entry()` cannot observe any fix in this spec. The new repo-bearing surfaces run the
real entry path and carry their own snapshots.

**Repo-bearing surfaces** use a *generated* fixture repo, and must inject a temp `OTTO_HOME`:
`workspace_home()` (`config/home.py:105`) resolves under `otto_home()` (`home.py:43-53`) = `$OTTO_HOME`
else `~/.otto`, and the sanitizer strips `OTTO_*`, so they would otherwise read and write the
developer's real home.

**Gate deltas and identity, not absolute I/O counts.** pathlib and glob internals changed across
3.10→3.14; an absolute cap either drifts red for reasons outside the change — monitoring, not
gating — or is padded until it cannot fail. Gate:

- **Scaling deltas** across two fixture sizes, on **both** `open` (per-file reads) and `os.scandir`
  (per-directory walks). Scale files *and* directories, or the fingerprint term stays invisible.
- **Absence assertions:** no `otto.host` / `otto.models` after `--version`; no `_otto_suite_*`
  modules (`repo.py:967`) on name-only surfaces.
- **`len(sys.path)`** on repo-bearing surfaces — `add_libs_to_pythonpath` (`bootstrap.py:273`)
  prepends lib dirs before every later import probe.

Absolute counts stay as per-version snapshots or move to nightly.
**Fix at the source, never raise the cap.**

### Fix F — Operator guidance

One home for startup-latency guidance, linked from anywhere else that mentions it: venv on local
disk; `__pycache__` writability and `PYTHONPYCACHEPREFIX`; short `sys.path` (~114 extra path
syscalls per added entry, mostly successful `FileFinder` revalidation stats, not ENOENTs);
NFS `actimeo`/`nocto` tradeoffs stated honestly; `PYTHONDONTWRITEBYTECODE` is the **wrong**
lever; and the two diagnostic commands verbatim.

## Third-party caching packages — evaluated, rejected

Judged on file-I/O reduction, not ergonomics. otto already reads its cache in **one open**, which is
the floor on a network filesystem.

| Candidate | Verdict |
|---|---|
| `diskcache` / sqlite (incl. reusing `aiosqlite`) | **No.** db + `-wal` + `-shm` opens and POSIX-lock hazards over NFS — one read becomes several. |
| `joblib.Memory` | **No.** Two files per key; multiplies opens. |
| `cachetools` | **No.** In-process; the problem is cross-invocation. |
| `msgspec` / `orjson` | **No.** Cheaper CPU, identical syscalls. |
| `platformdirs` | **No.** Path selection, zero I/O; `otto_home()` exists. |
| `filelock` | **No.** Adds lock create/unlink per write; `_atomic_write_json` already gives lock-free atomicity. |

Every candidate fragments the single-open read or adds lock traffic. The fix is *when* the file is
read, written, and validated — not *what* stores it.

## Non-goals / deferred

- **Lazy per-repo registration** ("import only the file that owns the name"). Better ceiling — it
  would fix `otto run <suite>` too — but it needs a static decorator scan, a heuristic that silently
  loses a user's suite when wrong. Deferred until these fixes are measured on the real box.
- **Cached sub-app help** (Fix C).
- **Directory-mtime fingerprints.** Rejected: POSIX directory mtime does not move on an in-place
  content edit.

## Verification

| Metric | Baseline | Target |
|---|---|---|
| `--version` syscalls, no repos | 2,427 | ≈ 582 (measured shim path; floor is 200) |
| `--version` syscalls, 500-file repo | 3,862 | ≈ 582 — **repo-independent** |
| `--version` opens under the test tree | 553 | 0 |
| cache writes per `--version` | 1 | 0 |
| `otto.host` / `otto.models` after `--version` | present | **absent** |
| `--help` `open` delta across fixture sizes | O(corpus) | ≈ 0 |
| `--help` `os.scandir` delta across fixture sizes | O(dirs) | ≈ 0 |
| deployment `--version`, warm | ~1 s | ≤ 0.3 s |
| deployment `--version`, cold | ~3 s | ≤ 1 s |

Every new guard must be **proven to go red** — mutate, observe, restore. A guard not seen red has
not been tested; revision 1 shipped a `rglob` gate that could never fail.

## Open questions

- Does the deployment use a non-file lab backend? If so `_fingerprint_is_ephemeral` may suppress
  cache writes entirely, shrinking Fix B's win to the read-and-parse half.

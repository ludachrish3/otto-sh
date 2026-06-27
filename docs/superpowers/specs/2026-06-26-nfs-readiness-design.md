# NFS-readiness pass — design

> Captured 2026-06-26. Addresses the **NFS scaling hazards** flagged in the
> Fable architecture review ([todo/fable_review_outcome.md](../../../todo/fable_review_outcome.md)
> "NFS scaling hazards (concrete)") and the original review question
> ([todo/fable_review.md](../../../todo/fable_review.md)): *"Does `otto` scale
> well on an NFS-mounted file system?"* No code has changed yet.

---

## 1. Context & motivation

All otto testing to date has run on a single host driving local VMs. In a real
team deployment the log/artifact root (`OTTO_XDIR`), the monitor database, and
lab data may live on a shared mount (NFS, CIFS/SMB, sshfs, …). Two of otto's
filesystem behaviours misbehave there:

1. **The monitor SQLite database uses WAL journaling.** SQLite explicitly does
   not support WAL over a network filesystem — WAL needs a coherent shared-memory
   `-shm` mmap on one host, which network filesystems cannot provide — so the DB
   can fail to open or behave incorrectly. The DB's `flock`-based "another
   instance is writing" guard also weakens on NFS, where `flock` semantics vary
   and may not detect a writer on a *different machine*.
   ([src/otto/monitor/collector.py](../../../src/otto/monitor/collector.py),
   `init_db`.)
2. **Log rotation does an unbounded `listdir` + `stat` walk on every otto
   invocation** (when retention is configured). On a large shared tree this
   becomes a per-invocation stat storm.
   ([src/otto/logger/logger.py](../../../src/otto/logger/logger.py),
   `remove_old_logs`.)

This spec is a focused **hardening pass**: one shared filesystem-detection
primitive, two targeted code fixes, and a docs section — backed by a full audit
so the paths we *don't* touch are deliberately and explicitly left alone.

## 2. Goals / non-goals

**Goals**

- Monitor DB works correctly on a network filesystem out of the box, by
  auto-adapting the journal mode. **Behaviour on local disk is byte-for-byte
  unchanged.**
- Log rotation can never turn into a multi-minute stat storm — bound it by wall
  clock.
- A single, reusable, well-tested `is_network_fs()` primitive.
- Honest documentation of what is and isn't safe on shared storage.

**Non-goals (explicit)**

- No coordinator/reservation **service** — otto stays server-less (review
  decision #6).
- No rewrite of the reservations, lab-data, or coverage storage layers.
- No blanket "you're on NFS" warnings on every artifact write.
- No new `OTTO_*` setting / pydantic settings-model change (the rotation budget
  is a code constant, see §4.3).
- No behaviour change of any kind on local disk.

## 3. The audit (record of every filesystem-scaling path)

Complete enumeration of where otto touches the filesystem in a way that scales
with I/O, with the verdict for each. Only the **Fix** rows get code changes.

| # | Path / operation | Location | Verdict |
|---|---|---|---|
| 1 | Monitor DB: `PRAGMA journal_mode=WAL`, `busy_timeout`, per-commit writes | `monitor/collector.py` `init_db` (~195–227) | **Fix (P1)** — adapt journal mode on network FS |
| 2 | Monitor DB: `flock(LOCK_EX\|LOCK_NB)` on `<db>.lock` | `monitor/collector.py` `init_db` (~207–216) | **Fix (P1)** — keep lock; add debug breadcrumb (cross-host caveat) |
| 3 | Monitor DB reader `from_sqlite()` | `monitor/collector.py` (~677–728) | Fine — read-only, no WAL set |
| 4 | Log rotation `listdir`+`stat` walk of whole `xdir` tree, every invocation | `logger/logger.py` `remove_old_logs` (~158–207) | **Fix (P2)** — time-box to 5s |
| 5 | Per-run artifact dir `mkdir(parents=True)` + `otto.log` create | `logger/logger.py` `create_output_dir` / `_add_log_handlers` | Fine — bounded, one tree per run |
| 6 | Reservations JSON re-read on every query | `reservations/json_backend.py` `_load` | Fine — read-only; review judged read paths fine |
| 7 | Lab discovery: `hosts.json` `exists()`/`is_file()`/parse | `storage/json_repository.py` `_find_hosts_files`/`_load_json_hosts` | Fine — read-once, direct path checks (no walk) |
| 8 | Coverage `rglob('*.gcno')` / `iterdir` host dirs | `coverage/correlator/merger.py`, `coverage/reporter.py` | Document only — bounded by a coverage run, not per-invocation |
| 9 | Docker build-context `rglob('*')` hashing | `docker/_context_hash.py` | Document only — bounded by a docker run |
| 10 | Completion-cache atomic write (`NamedTemporaryFile` + `os.replace`) | `configmodule/completion_cache.py` | Fine — atomic-rename is the correct NFS-safe pattern |
| 11 | Docker staging fixed at `/tmp/otto-docker` | `docker/staging.py` | Document only — `/tmp` assumed local |
| 12 | `/proc` iteration for child signalling | `host/session.py` `_signal_children` | N/A — procfs is always local |

**User-configurable bases that could point at a shared mount:** `OTTO_XDIR`
(logs + artifacts + default DB location), `[[labs]]` paths, `[reservations.json]
path`, `--db`, `--cov-dir`.

## 4. Design

```
src/otto/filesystem.py        NEW — is_network_fs() / network_fs_type()   (stdlib only, zero otto imports)
   ├─ monitor/collector.py    P1 — journal-mode adapt + flock breadcrumb
   └─ logger/logger.py        (optional debug breadcrumb when xdir is on a network mount)
logger/logger.py              P2 — time-boxed remove_old_logs()
docs/guide/monitor.md         P3 — "Running otto on shared/NFS storage"
```

### 4.1 Shared primitive — `src/otto/filesystem.py`

A new loose top-level module (matches the `console.py` / `context.py` /
`utils.py` convention). **Stdlib only, imports nothing from `otto`**, so it can
never participate in an import cycle. Linux-targeted (otto targets Linux).

Public API:

- `network_fs_type(path: str | Path) -> str | None`
  - Resolve `path`; if it does not exist yet (the DB file is created *after* this
    check), walk up to the nearest existing parent directory.
  - Find the **longest mount-point prefix** of that path in
    `/proc/self/mountinfo` and return its filesystem type string (e.g. `"nfs4"`,
    `"cifs"`, `"fuse.sshfs"`).
  - Return `None` if local, or if detection is not possible.
- `is_network_fs(path: str | Path) -> bool`
  - `True` iff `network_fs_type(path)` is in `_NETWORK_FSTYPES`.

`_NETWORK_FSTYPES` (explicit set, conservative): `nfs`, `nfs4`, `cifs`, `smb3`,
`smbfs`, `fuse.sshfs`, `glusterfs`, `fuse.glusterfs`, `lustre`, `ceph`,
`fuse.ceph`, `afs`, `9p`, `beegfs`, `ocfs2`, `gpfs`. We deliberately do **not**
blanket-flag all `fuse.*` — local FUSE mounts are common.

**`/proc/self/mountinfo` parsing.** Each line is:
`<id> <parent> <maj:min> <root> <mountpoint> <opts>… - <fstype> <source> <super-opts>`.
The fstype is the first field *after* the ` - ` separator; the mount point is
field index 4 (space-escaped as `\040` etc. — unescape it). To classify a path:
unescape and collect `(mountpoint, fstype)` pairs, pick the pair whose
mountpoint is the **longest** prefix of the resolved path. The parser is a
**pure function over mountinfo text + a path** so it is unit-testable with
synthetic input (no real mounts, no privileges).

**Graceful fallback.** Any failure (mountinfo unreadable, unparsable, path not
found) → `None` / `False` (assume local). This is safe by construction: a
false-negative just preserves today's behaviour; a false-positive only costs the
(harmless for otto's workload) DELETE journal mode.

### 4.2 P1 — Monitor DB adapt (`collector.py` `init_db`)

Immediately before the `PRAGMA journal_mode=WAL` line:

- `fstype = network_fs_type(self._db_path)`.
- If on a network FS:
  - Set `journal_mode=DELETE` instead of `WAL`.
  - Emit **one** `logger.debug(...)` line naming the DB path and detected fstype
    and that WAL was disabled. (One line per process that opens the DB; no
    console output, no per-invocation banner.)
- The `flock` guard is **unchanged** — it still protects the common same-host
  double-run case. On a detected network FS, emit a second `logger.debug(...)`
  noting cross-host exclusivity is not guaranteed there.
- `busy_timeout`, schema creation, the `end_ts` migration, and `from_sqlite()`
  are all unchanged.

**Why DELETE is lossless for otto.** The monitor writes one row + `commit()` per
metric poll (seconds apart). DELETE's per-commit rollback-journal fsync cost is
irrelevant at that cadence; WAL's batching advantage only matters under
high-frequency commits otto never produces.

### 4.3 P2 — Time-boxed log rotation (`remove_old_logs`)

`remove_old_logs` currently walks the entire `xdir` tree
(`listdir(xdir)` → `listdir(cmd_dir)` → `stat()` per log dir) with no bound, on
every invocation that has retention configured.

Change: bound the **wall-clock** time of the scan.

- New module constant `LOG_ROTATE_BUDGET_SECONDS = 5.0`.
- `remove_old_logs(self, seconds, *, time_budget: float = LOG_ROTATE_BUDGET_SECONDS)`.
- Capture `start = time.monotonic()` at entry. At the **top of each log-dir
  iteration** (before the `stat`), if `time.monotonic() - start > time_budget`,
  stop the whole scan (break out of both loops) and `self.debug(...)` that
  rotation hit its time budget and will resume on the next run.
- The current-iteration `stat`/`rmtree` is allowed to finish; the check just
  prevents *starting* new work past the budget. Elapsed time naturally includes
  prior `rmtree` cost, so the bound is on the whole pass.

**Not a behaviour change in the common case.** A normally-sized tree finishes far
under 5s. A pathological backlog is processed oldest-reachable-first and **drains
across subsequent runs** — dirs are deferred, never permanently missed. The
name-pattern guard (`_LOG_DIR_NAME_RE`) and all existing safety checks stay.

`time_budget` is a keyword arg (not an `OTTO_*` setting) so tests inject a
controlled value and the pydantic settings model is untouched. Promoting it to a
real setting later is a trivial, isolated follow-up if a need arises.

Optionally (small, low-risk): in `create_output_dir`, if `is_network_fs(self.xdir)`,
emit one `debug(...)` breadcrumb that the log root is on a network mount. Nice
for diagnosing slow runs; can be dropped if it feels like noise.

> **Implementation note (2026-06-26):** this optional breadcrumb was **dropped**
> during implementation. Its only test was fragile under `pytest -n auto`:
> `otto.logger.logger` gets duplicate-imported (`--doctest-modules` + the
> `get_otto_logger()` module-singleton), so a `monkeypatch` of
> `logger_mod.network_fs_type` patched a different module copy than the one the
> singleton logger's `create_output_dir` actually executes. The production code
> was correct (one module in real runs), but the breadcrumb is a nice-to-have and
> not worth shipping with an unreliable test or a project-wide import-mode change.
> The duplicate-import hazard is recorded in
> `todo/doctest-modules-duplicate-import-hazard.md`. The two core fixes (§4.2 DB
> journal adapt, §4.3 time-boxed rotation) are unaffected.

### 4.4 P3 — Documentation

- New subsection in [docs/guide/monitor.md](../../../docs/guide/monitor.md) near
  the `--db` docs: **"Running otto on shared/NFS storage."** The monitor DB
  auto-adapts to DELETE journaling on a network filesystem; for a multi-machine
  setup where several hosts target the **same** DB file, put the DB on local disk
  (the `flock` guard is same-host only there).
- A short "what's safe on shared storage" note distilled from the §3 audit: lab
  JSON / settings are read-once (fine); logs and artifacts are fine and rotation
  is time-budgeted; the DB adapts automatically; `/tmp` docker staging is assumed
  local.

## 5. Behaviour summary

| Scenario | Before | After |
|---|---|---|
| DB on local disk | WAL + flock | **Identical** (WAL + flock) |
| DB on NFS/CIFS/… | WAL — may fail/misbehave | DELETE journal + flock + 1 debug line |
| Two collectors, same host, same DB | flock blocks the 2nd (RuntimeError) | Identical |
| Two collectors, different hosts, same NFS DB | flock may not block (silent) | Same risk + debug breadcrumb + documented "use local disk" |
| Log rotation, normal tree | full walk | **Identical** (finishes < 5s) |
| Log rotation, huge shared tree | unbounded stat storm | bounded ≤ ~5s/run, drains across runs |

## 6. Testing strategy

All unit-tier, deterministic, no VMs, no real network mounts, nothing touching
the dev repo (everything in `tmp_path`).

- **`filesystem.py`** — feed the mountinfo parser synthetic text: an `nfs4`
  mount, a `cifs` mount, a `fuse.sshfs` mount, an all-local tree, a path under
  the longest of several nested mounts, a not-yet-existing DB path (parent walk),
  space-escaped mount points (`\040`), and an unreadable/garbage mountinfo
  (asserts graceful `None`/`False`).
- **`collector.py`** — monkeypatch `is_network_fs` → `True`: assert the resulting
  `PRAGMA journal_mode` is `delete` and a debug record is emitted; → `False`:
  assert `wal` (unchanged). Use a `tmp_path` DB.
- **`remove_old_logs`** — build a synthetic timestamped tree in `tmp_path`;
  monkeypatch `time.monotonic` with a controllable counter so the budget trips
  deterministically after a known number of entries; assert the scan stops, a
  debug line is logged, and a second run (clock reset) drains the remainder.

## 7. Risks & edge cases

- **mountinfo format drift / containers.** Mitigated by the pure-function parser
  + graceful fallback; misdetection only changes journal mode, never correctness
  on local disk.
- **Symlinked `--db` path across filesystems.** `network_fs_type` resolves the
  path (`realpath`) before matching, so the real backing mount is what's
  classified.
- **DB file doesn't exist yet at `init_db` time.** Handled by walking up to the
  nearest existing parent directory.
- **`time.monotonic` monkeypatch in tests** must target the name as imported in
  `logger.py` (`import time` + `time.monotonic()`), documented in the test.

## 8. Out of scope / future

- Promoting the rotation budget to an `OTTO_*` setting (only if a real need
  appears).
- Caching reservations/lab JSON reads (review judged read paths fine).
- A configurable docker staging root off `/tmp`.
- Any cross-host coordination primitive for the DB (server-less by decision #6).

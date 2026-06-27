# NFS-readiness pass Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make otto behave correctly and bounded on network/shared storage — the monitor SQLite DB auto-adapts off WAL on a network filesystem, and log rotation is time-boxed — with a single reusable detection primitive.

**Architecture:** A new stdlib-only `otto.filesystem` module classifies a path's backing filesystem via `/proc/self/mountinfo`. The monitor collector uses it to pick `DELETE` vs `WAL` journaling; the logger uses it for a diagnostic breadcrumb and gains a wall-clock budget on its rotation scan. Behaviour on local disk is byte-for-byte unchanged.

**Tech Stack:** Python 3.10+, stdlib (`os`, `pathlib`), `aiosqlite`, `pytest` + `pytest-asyncio` (strict mode).

**Spec:** [docs/superpowers/specs/2026-06-26-nfs-readiness-design.md](../specs/2026-06-26-nfs-readiness-design.md)

## Global Constraints

- **Python 3.10+.** Real annotations only — **never** add `from __future__ import annotations` (it trips otto's Sphinx nitpicky `-W` docs gate). `X | None` union syntax at runtime is fine on 3.10+.
- **No self-commit.** Chris commits in otto-sh (the `prepare-commit-msg` hook needs a TTY; agent commits mis-attribute). Every task's final step **stages only** (`git add`) and records the intended commit message. Do **not** run `git commit`.
- **`otto.filesystem` imports nothing from `otto`** — stdlib only, so it can never create an import cycle.
- **Tests are unit-tier and self-contained:** deterministic, no VMs, no network mounts, everything under `tmp_path`/`tmpdir`. Never touch paths inside the dev repo. No heavy/parallel xdist loops.
- **Linux-targeted** detection (otto targets Linux); detection failure ⇒ treat as local (safe default).
- **Per-task gate:** run the scoped `pytest` shown in each task. **Full gate once at the end** (Task 5): `make coverage`, `make typecheck`, `make docs`. `make nox` (5 Pythons, live VM) is Chris's to run.

---

### Task 1: `otto.filesystem` network-FS detection primitive

**Files:**
- Create: `src/otto/filesystem.py`
- Test: `tests/unit/test_filesystem.py`

**Interfaces:**
- Consumes: nothing (stdlib only).
- Produces:
  - `network_fs_type(path: str | Path) -> str | None` — fstype string (e.g. `"nfs4"`) when `path` is on a network FS, else `None`.
  - `is_network_fs(path: str | Path) -> bool` — `True` iff `network_fs_type(path) is not None`.
  - Internal, unit-tested: `_parse_mountinfo(text: str) -> list[tuple[str, str]]`, `_fstype_for_path(path_str: str, pairs: list[tuple[str, str]]) -> str | None`, `_unescape_mountinfo(field: str) -> str`, `_read_mountinfo() -> str | None`, `_resolve_existing(path) -> str`.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_filesystem.py`:

```python
import otto.filesystem as fs
from otto.filesystem import (
    _fstype_for_path,
    _parse_mountinfo,
    _unescape_mountinfo,
    is_network_fs,
    network_fs_type,
)

# A representative /proc/self/mountinfo: root ext4, an nfs4 mount, a nested
# local mount under it, a cifs mount, and a space-escaped mount point.
_MOUNTINFO = (
    "23 28 0:21 / / rw,relatime shared:1 - ext4 /dev/sda1 rw\n"
    "40 23 0:35 / /mnt/nfs rw,relatime shared:2 - nfs4 server:/export rw\n"
    "41 40 0:36 / /mnt/nfs/local rw,relatime shared:3 - ext4 /dev/sdb1 rw\n"
    "42 23 0:37 / /mnt/share rw,relatime shared:4 - cifs //srv/share rw\n"
    "43 23 0:38 / /mnt/my\\040share rw,relatime shared:5 - nfs //srv/x rw\n"
)


def test_unescape_mountinfo_decodes_octal_space():
    assert _unescape_mountinfo("/mnt/my\\040share") == "/mnt/my share"
    assert _unescape_mountinfo("/plain/path") == "/plain/path"


def test_parse_mountinfo_extracts_mountpoint_and_fstype():
    pairs = _parse_mountinfo(_MOUNTINFO)
    assert ("/", "ext4") in pairs
    assert ("/mnt/nfs", "nfs4") in pairs
    assert ("/mnt/share", "cifs") in pairs
    assert ("/mnt/my share", "nfs") in pairs  # unescaped


def test_fstype_for_path_picks_longest_prefix():
    pairs = _parse_mountinfo(_MOUNTINFO)
    assert _fstype_for_path("/home/user/x", pairs) == "ext4"          # root
    assert _fstype_for_path("/mnt/nfs/run/m.db", pairs) == "nfs4"     # nfs mount
    assert _fstype_for_path("/mnt/nfs/local/m.db", pairs) == "ext4"   # nested local wins
    assert _fstype_for_path("/mnt/share/m.db", pairs) == "cifs"


def test_network_fs_type_classifies(monkeypatch):
    monkeypatch.setattr(fs, "_read_mountinfo", lambda: _MOUNTINFO)
    monkeypatch.setattr(fs, "_resolve_existing", lambda p: "/mnt/nfs/run/m.db")
    assert network_fs_type("anything") == "nfs4"
    assert is_network_fs("anything") is True


def test_local_path_is_not_network(monkeypatch):
    monkeypatch.setattr(fs, "_read_mountinfo", lambda: _MOUNTINFO)
    monkeypatch.setattr(fs, "_resolve_existing", lambda p: "/home/user/m.db")
    assert network_fs_type("anything") is None
    assert is_network_fs("anything") is False


def test_unreadable_mountinfo_falls_back_to_local(monkeypatch):
    monkeypatch.setattr(fs, "_read_mountinfo", lambda: None)
    assert network_fs_type("/mnt/nfs/m.db") is None
    assert is_network_fs("/mnt/nfs/m.db") is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_filesystem.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'otto.filesystem'`.

- [ ] **Step 3: Write the implementation**

Create `src/otto/filesystem.py`:

```python
"""Network-filesystem detection.

A small, stdlib-only helper used to decide whether a path lives on a network
filesystem (NFS, CIFS/SMB, sshfs, …). otto uses this to adapt behaviour that is
unsafe or slow on shared storage — notably the monitor SQLite database, whose
WAL journal mode is unsupported over a network filesystem.

Linux-only (otto targets Linux): detection reads ``/proc/self/mountinfo``. Any
failure to read or parse it is treated as "local" — a safe default, since the
only consequence of misdetection is a (harmless-for-otto's workload)
journal-mode change.

This module imports nothing from ``otto`` so it can never create an import cycle.
"""

from pathlib import Path

_MOUNTINFO_PATH = '/proc/self/mountinfo'

# Filesystem types treated as "network/shared". Deliberately an explicit set —
# we do NOT blanket-flag all ``fuse.*`` because local FUSE mounts are common.
_NETWORK_FSTYPES = frozenset({
    'nfs', 'nfs4',
    'cifs', 'smb3', 'smbfs',
    'fuse.sshfs',
    'glusterfs', 'fuse.glusterfs',
    'lustre',
    'ceph', 'fuse.ceph',
    'afs',
    '9p',
    'beegfs',
    'ocfs2',
    'gpfs',
})


def _unescape_mountinfo(field: str) -> str:
    """Decode the octal escapes (``\\040`` space, ``\\011`` tab, …) mountinfo uses."""
    if '\\' not in field:
        return field
    out: list[str] = []
    i = 0
    n = len(field)
    while i < n:
        if field[i] == '\\' and i + 4 <= n and all(c in '01234567' for c in field[i + 1:i + 4]):
            out.append(chr(int(field[i + 1:i + 4], 8)))
            i += 4
        else:
            out.append(field[i])
            i += 1
    return ''.join(out)


def _parse_mountinfo(text: str) -> list[tuple[str, str]]:
    """Return ``(mountpoint, fstype)`` pairs parsed from mountinfo ``text``.

    mountinfo line layout::

        ID PARENT MAJ:MIN ROOT MOUNTPOINT OPTIONS... - FSTYPE SOURCE SUPEROPTS

    The mount point is field index 4; the fstype is the first field after the
    `` - `` separator (there are zero or more optional fields before it).
    """
    pairs: list[tuple[str, str]] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        parts = line.split(' - ', 1)
        if len(parts) != 2:
            continue
        left_fields = parts[0].split()
        right_fields = parts[1].split()
        if len(left_fields) < 5 or not right_fields:
            continue
        pairs.append((_unescape_mountinfo(left_fields[4]), right_fields[0]))
    return pairs


def _fstype_for_path(path_str: str, pairs: list[tuple[str, str]]) -> str | None:
    """fstype of the longest mount-point prefix of ``path_str`` (or ``None``)."""
    best_len = -1
    best_fstype: str | None = None
    for mountpoint, fstype in pairs:
        if mountpoint == '/':
            is_under = True
            mp_len = 1
        else:
            mp = mountpoint.rstrip('/')
            is_under = path_str == mp or path_str.startswith(mp + '/')
            mp_len = len(mp)
        if is_under and mp_len > best_len:
            best_len = mp_len
            best_fstype = fstype
    return best_fstype


def _read_mountinfo() -> str | None:
    try:
        with open(_MOUNTINFO_PATH, encoding='utf-8') as f:
            return f.read()
    except OSError:
        return None


def _resolve_existing(path: 'str | Path') -> str:
    """Resolve ``path`` absolutely, walking up to the nearest existing parent.

    The target (e.g. the DB file) may not exist yet at detection time; we still
    want the mount point of the directory it will live in.
    """
    p = Path(path).resolve()
    while not p.exists() and p != p.parent:
        p = p.parent
    return str(p)


def network_fs_type(path: 'str | Path') -> str | None:
    """Return the network filesystem type backing ``path``, or ``None``.

    Returns the mountinfo fstype string (e.g. ``"nfs4"``, ``"cifs"``,
    ``"fuse.sshfs"``) when ``path`` is on a network filesystem; otherwise
    ``None``. Detection failures are treated as local (return ``None``).
    """
    text = _read_mountinfo()
    if text is None:
        return None
    fstype = _fstype_for_path(_resolve_existing(path), _parse_mountinfo(text))
    return fstype if fstype in _NETWORK_FSTYPES else None


def is_network_fs(path: 'str | Path') -> bool:
    """``True`` when ``path`` lives on a network/shared filesystem."""
    return network_fs_type(path) is not None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_filesystem.py -v`
Expected: PASS (all 6 tests).

- [ ] **Step 5: Stage (do not commit)**

```bash
git add src/otto/filesystem.py tests/unit/test_filesystem.py
# Intended commit message (Chris commits):
# feat(filesystem): add network-FS detection via /proc/self/mountinfo
```

---

### Task 2: Monitor DB adapts WAL→DELETE on a network filesystem

**Files:**
- Modify: `src/otto/monitor/collector.py` (import near line 28; `init_db` body ~218–220)
- Test: `tests/unit/monitor/test_collector_nfs.py` (create)

**Interfaces:**
- Consumes: `otto.filesystem.network_fs_type` (Task 1).
- Produces: no new public API; `MetricCollector.init_db()` now selects journal mode based on the DB path's filesystem. `collector.py` exposes `network_fs_type` at module scope (imported), so tests monkeypatch `otto.monitor.collector.network_fs_type`.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/monitor/test_collector_nfs.py`:

```python
import pytest

import otto.monitor.collector as collector_mod
from otto.monitor.collector import MetricCollector


async def _journal_mode(collector: MetricCollector) -> str:
    cur = await collector._db_conn.execute('PRAGMA journal_mode')
    row = await cur.fetchone()
    assert row is not None
    return str(row[0]).lower()


@pytest.mark.asyncio
async def test_init_db_uses_delete_journal_on_network_fs(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(collector_mod, 'network_fs_type', lambda p: 'nfs4')
    collector = MetricCollector(db_path=str(tmp_path / 'm.db'))
    with caplog.at_level('DEBUG', logger='otto'):
        await collector.init_db()
    try:
        assert await _journal_mode(collector) == 'delete'
        assert any('network filesystem' in r.message for r in caplog.records)
    finally:
        await collector.close_db()


@pytest.mark.asyncio
async def test_init_db_uses_wal_on_local_disk(tmp_path, monkeypatch):
    monkeypatch.setattr(collector_mod, 'network_fs_type', lambda p: None)
    collector = MetricCollector(db_path=str(tmp_path / 'm.db'))
    await collector.init_db()
    try:
        assert await _journal_mode(collector) == 'wal'
    finally:
        await collector.close_db()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/monitor/test_collector_nfs.py -v`
Expected: FAIL — `AttributeError: <module 'otto.monitor.collector'> has no attribute 'network_fs_type'` (monkeypatch target missing) and/or the network-FS case still reports `wal`.

- [ ] **Step 3: Add the import**

In `src/otto/monitor/collector.py`, add to the otto imports (after line 28 `from ..host.host import RunResult`):

```python
from ..filesystem import network_fs_type
```

- [ ] **Step 4: Adapt the journal mode in `init_db`**

In `src/otto/monitor/collector.py`, replace these three lines (currently ~218–220):

```python
        conn = await aiosqlite.connect(self._db_path)
        await conn.execute('PRAGMA journal_mode=WAL')
        await conn.execute('PRAGMA busy_timeout=5000')
```

with:

```python
        net_fstype = network_fs_type(self._db_path)
        journal_mode = 'DELETE' if net_fstype else 'WAL'
        if net_fstype:
            logger.debug(
                "Monitor DB '%s' is on a network filesystem (%s); using "
                "journal_mode=DELETE instead of WAL (WAL is unsupported over "
                "network filesystems).",
                self._db_path, net_fstype,
            )
            logger.debug(
                "Monitor DB lock guard on '%s' is same-host only on network "
                "filesystems; for multi-machine setups sharing one DB, place it "
                "on local disk.",
                self._db_path,
            )

        conn = await aiosqlite.connect(self._db_path)
        await conn.execute(f'PRAGMA journal_mode={journal_mode}')
        await conn.execute('PRAGMA busy_timeout=5000')
```

(`logger = logging.getLogger('otto')` already exists at module scope, line 54.)

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/unit/monitor/test_collector_nfs.py -v`
Expected: PASS (both tests).

- [ ] **Step 6: Run the existing collector tests for regression**

Run: `uv run pytest tests/unit/monitor -q`
Expected: PASS (no regressions — local-disk behaviour is unchanged).

- [ ] **Step 7: Stage (do not commit)**

```bash
git add src/otto/monitor/collector.py tests/unit/monitor/test_collector_nfs.py
# Intended commit message (Chris commits):
# fix(monitor): use DELETE journal for the metrics DB on network filesystems
```

---

### Task 3: Time-box log rotation + network-FS breadcrumb

**Files:**
- Modify: `src/otto/logger/logger.py` (imports; new constant near `_LOG_DIR_NAME_RE` ~line 36; `create_output_dir` ~120–136; `remove_old_logs` ~158–207)
- Test: `tests/unit/logger/test_logger.py` (add tests)

**Interfaces:**
- Consumes: `otto.filesystem.network_fs_type` (Task 1).
- Produces:
  - Module constant `LOG_ROTATE_BUDGET_SECONDS = 5.0`.
  - `OttoLogger.remove_old_logs(self, seconds: float, *, time_budget: float = LOG_ROTATE_BUDGET_SECONDS)` — same behaviour as today but stops scanning once `time_budget` wall-clock seconds elapse, resuming next call.
  - `network_fs_type` available at `otto.logger.logger` module scope (imported) for monkeypatching.

- [ ] **Step 1: Write the failing tests**

Add to `tests/unit/logger/test_logger.py` (the file already imports `os`, `time`, `Path`, `pytest`, and defines `logger` + `_backdate`):

```python
import otto.logger.logger as logger_mod


def test_remove_old_logs_respects_time_budget(monkeypatch, caplog):
    """The scan stops once the time budget is exceeded and resumes next run."""
    cmd_dir = logger.xdir / 'pytest'
    olds = []
    for i in range(6):
        d = cmd_dir / f'20200101_0000{i:02d}_000'
        d.mkdir()
        _backdate(d, seconds=3600)
        olds.append(d)

    # Fake monotonic clock advancing 1.0s per call: start=0.0, then the inner
    # checks see 1.0, 2.0, 3.0, ... so a 2.5s budget trips on the 3rd check.
    ticks = iter([float(n) for n in range(0, 1000)])
    monkeypatch.setattr(logger_mod.time, 'monotonic', lambda: next(ticks))

    with caplog.at_level('DEBUG', logger='otto'):
        logger.remove_old_logs(seconds=60, time_budget=2.5)

    assert [d for d in olds if d.exists()], 'budget should stop before removing all dirs'
    assert any('time budget' in r.message for r in caplog.records)

    # A second pass with a non-advancing clock (elapsed always 0) drains the rest.
    monkeypatch.setattr(logger_mod.time, 'monotonic', lambda: 0.0)
    logger.remove_old_logs(seconds=60, time_budget=2.5)
    assert not [d for d in olds if d.exists()], 'remaining old dirs should drain on the next run'


def test_remove_old_logs_no_budget_message_on_normal_run(caplog):
    """A small tree finishes well under budget — no truncation message."""
    cmd_dir = logger.xdir / 'pytest'
    d = cmd_dir / '20200101_000000_000'
    d.mkdir()
    _backdate(d, seconds=3600)

    with caplog.at_level('DEBUG', logger='otto'):
        logger.remove_old_logs(seconds=60)  # default 5.0s budget, real clock

    assert not d.exists()
    assert not any('time budget' in r.message for r in caplog.records)


def test_create_output_dir_logs_network_fs_breadcrumb(monkeypatch, caplog):
    """A debug breadcrumb is emitted when the log root is on a network FS."""
    monkeypatch.setattr(logger_mod, 'network_fs_type', lambda p: 'nfs4')
    with caplog.at_level('DEBUG', logger='otto'):
        logger.create_output_dir(command='pytest', subcommand='nfs_probe')
    assert any('network filesystem' in r.message for r in caplog.records)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/logger/test_logger.py -k "time_budget or budget_message or network_fs_breadcrumb" -v`
Expected: FAIL — `remove_old_logs()` rejects the `time_budget` keyword (`TypeError`); `network_fs_type` not importable from the logger module.

- [ ] **Step 3: Add imports and the budget constant**

In `src/otto/logger/logger.py`:

Add `import time` to the top imports (after `import re`, line 4):

```python
import re
import time
```

Add `DEBUG` to the `from logging import (...)` block (after `setLoggerClass,`):

```python
from logging import (
    DEBUG,
    FileHandler,
    Logger,
    LogRecord,
    getLogger,
    getLoggerClass,
    setLoggerClass,
)
```

Add the import for the detection primitive (after `from ..console import CONSOLE`, line 24):

```python
from ..console import CONSOLE
from ..filesystem import network_fs_type
```

Add the budget constant right after the `_LOG_DIR_NAME_RE = ...` definition (line 36):

```python
# Maximum wall-clock seconds ``remove_old_logs`` may spend scanning per call.
# A safety valve against unbounded stat storms on large/slow (e.g. NFS) log
# trees; a backlog drains across subsequent runs. Far above any normal tree.
LOG_ROTATE_BUDGET_SECONDS = 5.0
```

- [ ] **Step 4: Add the network-FS breadcrumb to `create_output_dir`**

In `src/otto/logger/logger.py` `create_output_dir`, immediately after `self._output_dir.mkdir(parents=True)` (~line 124), add:

```python
        # Diagnostic only: note when the log root is on a network mount (log
        # I/O and rotation may be slower there). Gated on DEBUG so the
        # mountinfo read is skipped entirely on normal runs.
        if self.isEnabledFor(DEBUG):
            xdir_fstype = network_fs_type(self.xdir)
            if xdir_fstype:
                self.debug(
                    "Log root '%s' is on a network filesystem (%s); "
                    "log I/O and rotation may be slower.",
                    self.xdir, xdir_fstype,
                )
```

- [ ] **Step 5: Time-box `remove_old_logs`**

In `src/otto/logger/logger.py`, change the signature (currently `def remove_old_logs(self,\n        seconds: float,\n    ):`) to:

```python
    def remove_old_logs(self,
        seconds: float,
        *,
        time_budget: float = LOG_ROTATE_BUDGET_SECONDS,
    ):
        """
        Remove all logs older than `seconds` seconds old.

        This method deals with seconds, enabling quick unit testing.

        Args:
            seconds: Number of seconds to retain old logs.
            time_budget: Maximum wall-clock seconds to spend scanning per call.
                When exceeded the scan stops early and resumes on the next call,
                bounding the per-run cost on large/slow (e.g. NFS) trees.
        """
```

Then, inside the method, add the clock just before the `for cmd_dir_name in listdir(xdir):` loop (after `loggedDeletion = False`):

```python
        loggedDeletion = False
        start = time.monotonic()
        budget_hit = False
```

Guard the outer loop so it stops once the budget is hit:

```python
        for cmd_dir_name in listdir(xdir):
            if budget_hit:
                break
            cmd_dir = xdir / cmd_dir_name
```

Add the budget check at the **top** of the inner loop, before any `stat`:

```python
            for log_dir_name in listdir(cmd_dir):
                if time.monotonic() - start > time_budget:
                    budget_hit = True
                    break
                output_dir = cmd_dir / log_dir_name
```

Finally, after the loops complete (de-indented to method level, after the existing loop body), add:

```python
        if budget_hit:
            self.debug(
                "Log rotation hit its %gs time budget; remaining old "
                "directories will be removed on the next run.",
                time_budget,
            )
```

- [ ] **Step 6: Run the new tests to verify they pass**

Run: `uv run pytest tests/unit/logger/test_logger.py -k "time_budget or budget_message or network_fs_breadcrumb" -v`
Expected: PASS (3 tests).

- [ ] **Step 7: Run the full logger test file for regression**

Run: `uv run pytest tests/unit/logger/test_logger.py -q`
Expected: PASS (existing rotation/formatting tests still green — default budget never trips on their tiny trees).

- [ ] **Step 8: Stage (do not commit)**

```bash
git add src/otto/logger/logger.py tests/unit/logger/test_logger.py
# Intended commit message (Chris commits):
# perf(logger): time-box log rotation; debug breadcrumb for network log roots
```

---

### Task 4: Documentation — "Running otto on shared/NFS storage"

**Files:**
- Modify: `docs/guide/monitor.md` (after the `### Persisting data` subsection)

**Interfaces:**
- Consumes: behaviour from Tasks 1–3. Produces: user-facing docs only (no code).

- [ ] **Step 1: Add the docs subsection**

In `docs/guide/monitor.md`, immediately after the `### Persisting data` block (the one ending with the `otto --lab my_lab monitor --db metrics.db` fenced example), insert:

```markdown
### Running otto on shared/NFS storage

otto is safe to run with its log/artifact root (`OTTO_XDIR`) on a shared mount
(NFS, CIFS/SMB, sshfs, …):

- **Monitor database.** SQLite's WAL journaling is not supported over a network
  filesystem, so when the `--db` path is on one otto automatically uses the
  `DELETE` journal mode instead (logged at debug level). This is transparent and
  lossless for monitoring's write pattern.
- **Multi-machine, one shared database.** The "another instance is already
  writing" guard relies on `flock`, whose semantics on network filesystems are
  same-host only. If several machines may write to the *same* database file,
  put that database on **local disk** (or give each machine its own `--db`
  path).
- **Logs and artifacts.** Per-run log directories are fine on shared storage.
  Old-log rotation is wall-clock budgeted, so even a very large log tree cannot
  stall a run — any backlog is pruned across subsequent runs.
- **Lab data and settings** (`hosts.json`, `.otto/settings.toml`) are read once
  per run and are unaffected.

If otto cannot determine the filesystem type, it assumes local disk and keeps
its default behaviour.
```

- [ ] **Step 2: Build the docs to verify no warnings**

Run: `make docs`
Expected: PASS — Sphinx build clean (no new nitpicky `-W` warnings), markdown doctest lint green.

- [ ] **Step 3: Stage (do not commit)**

```bash
git add docs/guide/monitor.md
# Intended commit message (Chris commits):
# docs(monitor): document otto behaviour on shared/NFS storage
```

---

### Task 5: Full-gate verification

**Files:** none (verification only).

**Interfaces:** Consumes Tasks 1–4.

- [ ] **Step 1: Coverage gate (pinned Python, enforces threshold)**

Run: `make coverage`
Expected: PASS — suite green and coverage at/above the configured threshold (≈91%). The three new test modules/cases are included.

- [ ] **Step 2: Type check**

Run: `make typecheck`
Expected: PASS — `ty check` clean (new annotations resolve; no `unresolved-import` for `otto.filesystem`).

- [ ] **Step 3: Docs gate**

Run: `make docs`
Expected: PASS — HTML build + Sphinx + doctests clean.

- [ ] **Step 4: Confirm everything is staged for Chris**

Run: `git status --short`
Expected: staged (`A`/`M`) entries for: `src/otto/filesystem.py`, `tests/unit/test_filesystem.py`, `src/otto/monitor/collector.py`, `tests/unit/monitor/test_collector_nfs.py`, `src/otto/logger/logger.py`, `tests/unit/logger/test_logger.py`, `docs/guide/monitor.md`, and the spec/plan docs. Nothing committed.

- [ ] **Step 5: Hand off to Chris**

Report the green gate output and the per-task commit messages. Chris runs `make nox` (5 Pythons, live VM) and performs the commits. Optionally suggest folding the four task commits into one, e.g.:

```
feat(nfs): NFS-readiness pass — DB journal adapt, time-boxed log rotation

- otto.filesystem: detect network FS via /proc/self/mountinfo
- monitor DB: DELETE journal on network FS (WAL unsupported); flock breadcrumb
- logger: 5s wall-clock budget on remove_old_logs; network-root debug breadcrumb
- docs: "Running otto on shared/NFS storage"
```

---

## Self-Review

**1. Spec coverage:**
- §4.1 detection primitive → Task 1. ✓
- §4.2 monitor DB WAL→DELETE + flock breadcrumb → Task 2. ✓
- §4.3 time-boxed rotation (5.0s, injectable, drains across runs) → Task 3 (Steps 3, 5). ✓
- §4.3 optional xdir breadcrumb (DEBUG-gated) → Task 3 (Step 4). ✓
- §4.4 docs section → Task 4. ✓
- §6 testing (synthetic mountinfo; monkeypatched journal/clock; tmp_path) → Tasks 1–3 tests. ✓
- §3 audit "leave alone" paths → no tasks, by design (non-goals). ✓
- Full gate (§ implicit; project convention) → Task 5. ✓

**2. Placeholder scan:** No TBD/TODO/"handle edge cases"/"similar to". Every code step shows complete code; every run step shows the command and expected result. ✓

**3. Type consistency:** `network_fs_type(path) -> str | None` and `is_network_fs(path) -> bool` are used identically in Tasks 2 and 3. The `time_budget` keyword name matches between the signature (Task 3 Step 5) and the tests (Task 3 Step 1). `LOG_ROTATE_BUDGET_SECONDS` is defined once and referenced as the default. Monkeypatch targets (`otto.monitor.collector.network_fs_type`, `otto.logger.logger.network_fs_type`, `otto.logger.logger.time.monotonic`) match where each name is imported/used. ✓

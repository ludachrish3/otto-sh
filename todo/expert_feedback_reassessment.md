# Expert Feedback Re-evaluation: otto Current State

Reassessment date: 2026-03-28

The project underwent a professional design review. Significant work has been done since. This assessment evaluates what's been addressed, what remains, and any new concerns.

---

## Architectural Concerns — Resolved

### 1. RemoteHost God Object — FIXED

Decomposed into focused components:

- `ConnectionManager` (`src/otto/host/connections.py`) — transport lifecycle, lazy-connect, per-host pooling
- `SessionManager` (`src/otto/host/session.py`) — shell sessions with sentinel-wrapped execution
- `FileTransfer` (`src/otto/host/transfer.py`) — SCP/SFTP/FTP/netcat dispatch
- `RepeatRunner` (`src/otto/host/repeat.py`) — background periodic tasks
- `SshHopTransport` (`src/otto/host/transport.py`) — multi-hop tunnel chains

RemoteHost itself is now an orchestrator that delegates to these components.

### 2. Skeleton Fields (hops) — FIXED

`hop` is fully implemented with multi-hop SSH tunnel chains, cycle detection, and integration tests. Phase 2 completed in commit `d09f7c8`.

### 3. Dependency Injection — FIXED

`_connection_factory` parameter on RemoteHost (`remoteHost.py:147-149`) allows injecting test doubles.

### 4. Shell State Leakage — FIXED

- Timeout recovery sends Ctrl+C + recovery sentinel (`session.py:306-330`)
- Dead sessions auto-replaced on next use via `_ensure_session()`
- Per-session UUID markers prevent cross-contamination
- Sentinel-wrapped commands isolate output per execution

### 5. SQLite Concurrency — FIXED

- WAL mode enabled (`collector.py:176`)
- 5-second busy timeout (`collector.py:177`)
- Exclusive file lock prevents multi-instance DB conflicts (`collector.py:164-173`)

### 6. Multi-Instance Safety — MOSTLY FIXED

- DB file locking with clear error messages
- Monitor server binds to port 0 by default (OS-assigned ephemeral port)

### 7. Feature Gaps Filled

- **Parametrized tests**: Full `@pytest.mark.parametrize` support with sanitized test dir names
- **Dry-run mode**: `--dry-run` / `-n` CLI flag, `Status.Skipped` for dry-run results
- **Bastion/hop support**: Multi-hop SSH chains (Phase 2 complete)
- **Test tagging/filtering**: Full pytest `-m` marker expression support
- **Rollback/cleanup**: Yield-based autouse fixtures for teardown

---

## Remaining Concerns

### HIGH PRIORITY

#### A. No Async Context Manager on RemoteHost

`RemoteHost` has `close()` and a fragile `__del__` fallback but no `__aenter__`/`__aexit__`. Users can forget to call `close()` and leak connections. Named sessions (`HostSession`) already support `async with` — RemoteHost should too.

**Fix**: Add `__aenter__`/`__aexit__` to RemoteHost. Low effort, high value.

#### B. Plain-Text Credentials

Credentials are still stored as plain text in JSON lab files and passed through the system as `dict[str, str]`. No encryption, vault integration, or environment variable credential sources exist. For a tool that SSHes into hosts, this is the most significant security gap.

**Scope**: This is a design decision more than a quick fix — options include environment variable lookups, encrypted credential files, or vault integration. Worth planning as a dedicated initiative.

#### C. No Global Connection Pool Cap

Each `RemoteHost` owns a `ConnectionManager` with lazy connections — only the protocol matching the host's `term` type (SSH *or* Telnet, not both) is opened, plus optional SFTP/FTP on demand. Per-host pooling is sound, but there's no fleet-level cap. A test suite with 100+ hosts could exhaust SSH daemon `MaxSessions`/`MaxStartups` limits on targets or the local system's file descriptor limit.

**Consideration**: This may not be urgent depending on typical lab sizes, but becomes critical at scale.

### MEDIUM PRIORITY

#### D. Unbounded SSE Subscriber Queues

`asyncio.Queue()` in `collector.py` subscriber system has no `maxsize`. A slow SSE client causes unbounded memory growth. Should use `asyncio.Queue(maxsize=N)` with a drop-oldest or skip policy.

#### E. Per-Point DB Commits

Each metric write does an individual `INSERT` + `commit()` (`collector.py:196-203`). Under high-frequency monitoring this creates unnecessary I/O pressure. Batch writes per tick would be more efficient.

#### F. Resource Locking — Still Unimplemented

The `resources` field exists on `RemoteHost` and `Lab` as metadata, but there's no lock manager to prevent concurrent test suites from using the same exclusive resource. This matters when multiple otto instances share a lab.

### LOW PRIORITY (Feature Gaps vs. Mature Tools)

These are legitimate gaps vs. tools like Ansible/Robot but may not be priorities for otto's niche:

| Gap | Current State |
| --- | --- |
| **Structured log output** | Rich markup / plain text only. No JSON/XML export from otto's own logger (JUnit XML comes from pytest) |
| **Cross-suite parallelism** | Suites run sequentially in `run_all()`. Only per-host asyncio concurrency within a suite |
| **Inventory grouping/inheritance** | Flat JSON arrays. No group_vars, host_vars, or hierarchy |
| **Idempotency/check-mode** | `Status.Skipped` exists for dry-run but no Ansible-style changed/ok distinction |
| **Config format fragmentation** | JSON (lab), TOML (repo settings), Python dataclasses (runtime). Three formats persist |

---

## New Observations (Not in Original Review)

1. **No collection timeout per host**: The monitoring `asyncio.gather()` loop waits for all hosts each tick. One unresponsive host blocks the entire collection cycle. Should use `asyncio.wait_for()` or `asyncio.timeout()` per host.

2. **`resources` field ambiguity**: It's declared as a `set[str]` on both RemoteHost and Lab, and Lab aggregates host resources, but nothing consumes or enforces it. Either implement locking or document it purely as user-facing metadata.

---

## Summary Scorecard

| Original Concern | Verdict |
| --- | --- |
| RemoteHost god object | **Resolved** |
| Skeleton hops field | **Resolved** |
| Skeleton resources field | Metadata only — no locking |
| Config fragmentation | **Unchanged** |
| No dependency injection | **Resolved** |
| No connection pool cap | **Unchanged** |
| runCmd() sequential | **Documented** — exec() and named sessions provide concurrency |
| SQLite concurrency | **Resolved** |
| No backpressure | **Partially resolved** — unbounded queues remain |
| Shell state leakage | **Resolved** |
| close() reliability | **Unchanged** — no async context manager |
| Secret handling | **Unchanged** |
| Multi-instance safety | **Mostly resolved** |
| Feature gaps (parametrize, dry-run, hops, tagging) | **Resolved** |
| Feature gaps (structured logs, parallelism, inventory, idempotency) | **Unchanged** |

# Monitor-DB test flakes under COUNT=10 (3.12, full-suite contention)

> **2026-07-02:** the monitor revamp Phase 1 plan extracts all DB handling
> into a new `src/otto/monitor/db.py` (`MetricDB`) — re-check both flakes
> against that module when Phase 1 lands and fold the suggested fixes there.

Discovered during the merge-readiness stability campaign (Stage 3, `--count=10`)
on `feature/embedded-host`, 2026-06-06. Two **test-context** flakes in the
branch-new SNMP monitor code. Both are rare (~1-in-6,870 test executions),
**Python 3.12 only**, and surface **only** under the full unit suite at
`-n auto --count=10` — they do **not** reproduce when the monitor tests run in
isolation (need ~1370 other tests churning concurrently to create the timing).

**Not production bugs.** The production monitor closes its DB connection
correctly: the OttoPlugin monitor fixture awaits `collector.close()` →
`close_db()` at teardown ([src/otto/suite/plugin.py:358](../src/otto/suite/plugin.py#L358)).
Both flakes are test-isolation/cleanup gaps exposed by extreme stress.

Per the campaign decision (2026-06-06), these are **documented, not blocking**
the merge — see the PR evidence appendix.

## Flake 1 — `database is locked`

- **Test:** `tests/unit/monitor/test_collector_db.py::TestSchemaInit::test_creates_metrics_table`
- **Signature:** `sqlite3.OperationalError: database is locked`
- **Mechanism (hypothesis):** the test verifies the schema with a *synchronous*
  `sqlite3.connect(db_path)` that has no `busy_timeout`, while the collector's
  `aiosqlite` connection (WAL + `busy_timeout=5000` + an `flock` on `<db>.lock`)
  is still active. Under load the sync verify-connect occasionally collides with
  a WAL write/checkpoint and fails immediately instead of waiting.
- **Suggested fix:** give the verify-connect a `busy_timeout` (e.g.
  `sqlite3.connect(db_path, timeout=5)`) or ensure the collector's connection is
  closed before the synchronous verification reads the file.

## Flake 2 — `Connection.__del__` ResourceWarning (leaked connection)

- **Test:** `tests/unit/suite/test_plugin.py::test_e2e_monitor_collects_metrics_under_class_loop_scope`
- **Signature:** `PytestUnraisableExceptionWarning: Exception ignored in:
  <function Connection.__del__ ...>` (an unclosed `sqlite3`/`aiosqlite`
  connection GC'd → `ResourceWarning`, escalated by `filterwarnings=["error"]`).
- **Mechanism (hypothesis):** the e2e test injects a real `MetricCollector`
  (patches `build_monitor_collector`) and replaces `run` with a fake; its
  `finally` only evicts the inner-session module. The collector's DB connection
  opened during the inner session isn't reliably closed — likely because the
  monitor fixture's `await collector.close()` runs on a different loop scope
  than the one the `aiosqlite` connection (and its worker thread) were opened on,
  so the underlying `sqlite3.Connection` lingers to GC.
- **Suggested fix:** close the injected collector in the test's `finally`
  (`asyncio.run(real_collector.close_db())` or equivalent), and/or make
  `aiosqlite` connection teardown loop-scope-robust.

## Reproduction notes

- Reproduces only via the full unit suite: `pytest tests/unit -m "not integration
  and not hops" --count=10` on Python 3.12 (intermittent — expect to loop it
  several times). Does NOT reproduce from `tests/unit/monitor/` +
  `tests/unit/suite/test_plugin.py` alone.
- The asyncio-leak detector (`OTTO_DETECT_ASYNCIO_LEAKS=1`) targets transports,
  not raw `sqlite3.Connection`s; add a connection-specific check (or
  `tracemalloc`) when picking this up.

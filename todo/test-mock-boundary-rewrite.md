# Test Mock Boundary Rewrite

Push CLI test mock boundaries from business logic methods down to the
I/O layer so that bugs in validation, parsing, and dispatch logic are
caught by unit tests.

**Background:** The `_get_literal_values` bug (using `case Literal()` in
a match statement, which crashes because `Literal` is a `_SpecialForm`)
slipped through because every CLI test that called `setTermType` patched
it out. This is a systemic pattern across the CLI test suite.

**Guiding principle:** Mock at I/O, not at business logic. If the
function you are patching had a bug, your test should catch it.

Phases 1 and 2 (real tmp_path config in `test_main.py`/`test_run.py`;
session-layer `FakeSession` in `test_host.py`; real `MetricCollector` cycles
in `test_monitor.py`) shipped long ago — detail pruned 2026-07-25. Phase 3
below remains.

---

## Phase 3 -- Larger effort

### `test_host.py` `put`/`get` commands: Mock at protocol level

**File:** `tests/unit/cli/test_host.py`

Current state: `host.putFiles` / `host.getFiles` are `AsyncMock`s.
Protocol selection, concurrent batching, progress reporting, and error
handling are all untested.

**What to do:**
1. Mock at `ConnectionManager.scp()`, `.sftp()`, `.ftp()` -- each
   returns a fake protocol client object.
2. Let `FileTransfer` dispatch, batch, and error-handle for real.
3. Stub protocol clients:
   - **SCP:** fake `SSHClientConnection` with `put_file()` / `get_file()`
   - **SFTP:** fake `SFTPClient` with `put()` / `get()`
   - **FTP:** fake `aioftp.Client` with `store()` / `retrieve()`
4. Defer **netcat** transfers to integration tests (SSH exec channels +
   TCP socket handling is too complex to stub meaningfully).

**What this unlocks:**
- Transfer protocol selection logic
- Concurrent file batching via `asyncio.gather()`
- Error handling for missing files, permission errors
- Progress handler invocation

**Effort:** Large -- three protocol APIs with different callback
signatures. Netcat is the most complex and should stay in integration.

---

### `test_test.py`: Run real pytest with bounded suite

**File:** `tests/unit/cli/test_test.py`

Current state: `pytest.main()` is mocked. Argument construction is
tested but actual collection and execution are not.

**What to do:**
1. Create a small test suite in `tmp_path` with 2-3 test cases (one
   pass, one fail, one skip).
2. Call real `pytest.main()` with the generated suite.
3. Verify:
   - JUnit XML output is written and contains expected results
   - Exit code reflects pass/fail
   - Result aggregation works for stability runs (multiple iterations)
4. Follow the `test_listing.py` pattern for creating real temporary
   repos with `.otto/settings.toml`.

**Main challenge:** Injecting mocked host fixtures into pytest's plugin
system for suite tests that require `RemoteHost` objects. May need a
conftest.py generated alongside the test file.

**Effort:** Small-Medium -- the pattern exists in `test_listing.py`.

---

## Summary

| Phase | Area | Effort | What it catches |
|-------|------|--------|-----------------|
| 3 | `test_host.py` `put`/`get` at protocol level | Large | Transfer batching, protocol selection |
| 3 | `test_test.py` real pytest execution | Small-Med | Collection + result aggregation |

Phase 3 is the largest effort and can be deferred until
those areas see bugs in practice.

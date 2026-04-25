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

---

## Phase 1 -- Low-hanging fruit  ✅ DONE

### `test_main.py`: Replace mock fixtures with real tmp_path config files

**File:** `tests/unit/cli/test_main.py`

**What was done:**

- Added `real_main_mocks` fixture in `conftest.py` that creates real
  `settings.toml` and `hosts.json` in `tmp_path`.
- `initOttoLogger` now runs for real (mocking only `removeOldLogs` and
  `RichHandler` at the I/O boundary). `getLab` reads real hosts.json.
  `setConfigModule`/`getConfigModule` run for real.
- `getRepos` is still patched (module-level singleton), but returns a
  real `Repo` object built from `tmp_path`.
- `TestLoggerArguments` (13 tests) asserts on observable logger state
  (level, xdir, rich_logging) and I/O mock call args instead of
  mocking `initOttoLogger` entirely.
- `TestLabLoading` (7 tests) asserts on real `Lab`/`RemoteHost` objects
  instead of mock call args. Includes `test_host_objects_have_correct_ip`
  for deeper validation.

---

### `test_run.py`: Test real instruction execution

**File:** `tests/unit/cli/test_run.py`

**What was done:**

- Added `TestInstructionExecution` class with 3 new tests:
  1. `test_instruction_body_executes` -- proves the async body runs
  2. `test_instruction_receives_typer_arguments` -- Typer arg parsing
     works through the `@instruction` decorator
  3. `test_instruction_calls_host_method` -- instruction calls
     `host.runCmds()` on `AsyncMock(spec=RemoteHost)`, asserts awaited
- Mock boundary is at the host method level (acceptable for thin wrappers).

**Coverage impact:** `configmodule/lab.py` 33%→87%,
`storage/json_repository.py` 16%→94%, `storage/factory.py` 19%→100%,
`logger/logger.py` 40%→97%, `cli/callbacks.py` 85%→100%.
Test count: 41→45 CLI tests, 619 total passing.

---

## Phase 2 -- Medium effort, high value  ✅ DONE

### `test_host.py` `run` command: Mock at session layer, not `runCmds`

**File:** `tests/unit/cli/test_host.py`

**What was done:**

- Created `FakeSession` class (a `ShellSession` subclass) with pre-loaded
  `(output, retcode)` responses.  When the base class writes a
  sentinel-wrapped command, the fake immediately enqueues the begin
  marker, output lines, and end sentinel into an `asyncio.Queue`.
- Injected via `SessionManager`'s `session_factory` parameter (cleaner
  than `_connection_factory` — avoids stubbing `SSHClientConnection` /
  `create_process()` / `stdin` / `stdout`).
- Added `_make_host_with_session(responses)` helper that builds a real
  `RemoteHost` and replaces `host._session_mgr` with a `SessionManager`
  using the fake.
- Updated 7 tests across `TestHostCallback`, `TestHostRun`, and
  `TestHostTermAndTransfer` to use the helper instead of patching
  `host.runCmds`.  Kept `test_run_closes_host_on_exception` as-is
  (tests CLI error handling, not execution).
- Logging callbacks suppressed in the helper to avoid interfering with
  `CliRunner`'s stdout capture.

**What this unlocks:**
- Command wrapping with sentinels
- Output parsing and stripping
- Exit code extraction
- Status propagation through `BaseHost.runCmds` -> `RemoteHost.runCmd`
  -> `SessionManager.run_cmd` -> `ShellSession.runCmd`

**Bugs found:** None -- sentinel parsing, exit code extraction, and
status aggregation all work correctly through the full chain.

---

### `test_monitor.py` live mode: Let `MetricCollector` run for real

**File:** `tests/unit/cli/test_monitor.py`

**What was done:**

- Added `_make_monitor_host(name)` helper returning a
  `MagicMock(spec=RemoteHost)` whose `runCmds` returns canned
  `CommandStatus` tuples with synthetic output for `free -b`,
  `cat /proc/loadavg`, and `grep -c ^processor /proc/cpuinfo`.
- Added `TestCollectorLiveRun` class with 4 async tests:
  1. `test_single_cycle_parses_metrics` -- MemParser + LoadParser,
     verifies `get_series()` contains correct values (62.5% mem,
     0.52 load).
  2. `test_collection_stores_to_sqlite` -- verifies SQLite file is
     created and contains expected rows after a collection cycle.
  3. `test_multiple_hosts_collected` -- two hosts, verifies both
     appear in `get_series()`.
  4. `test_failed_command_does_not_crash_collector` -- host returns
     failures with empty output, verifies collector completes with
     empty series.
- Used `duration=timedelta(seconds=0)` to run only the initial
  collection cycle (no loop iterations).
- Existing `live_mode_mocks` tests left unchanged (they test CLI
  argument parsing/routing).

**Coverage impact:** `collector.py` 12%→84%, `parsers.py` 43%→93%,
`cli/monitor.py` 32%→81%.
Test count: 23→27 monitor tests, 623 total passing.

**Bugs found:** None -- parser selection, output parsing, series
storage, and DB writes all work correctly.

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
| 1 | `test_main.py` real config loading | Small | TOML/lab parsing bugs |
| 1 | `test_run.py` instruction execution | Small | Instruction body bugs |
| 2 | `test_host.py` `runCmds` at ConnectionManager | Medium | Sentinel parsing, exit codes, timeouts |
| 2 | `test_monitor.py` live collection cycle | Medium | Parser + metric insertion bugs |
| 3 | `test_host.py` `put`/`get` at protocol level | Large | Transfer batching, protocol selection |
| 3 | `test_test.py` real pytest execution | Small-Med | Collection + result aggregation |

Phase 1 can be done in a single session. Phase 2 is roughly a day of
focused work. Phase 3 is the largest effort and can be deferred until
those areas see bugs in practice.

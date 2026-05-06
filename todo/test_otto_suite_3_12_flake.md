# Flaky `test_test_dir_created_per_test` on Python 3.12

`tests/unit/suite/test_otto_suite.py::TestOttoTestDir::test_test_dir_created_per_test`
intermittently fails when the full unit suite runs under Python 3.12. The
test passes in isolation and passed on every retry of the full matrix
(observed once across three full-suite runs on 3.12; never on 3.10, 3.11,
3.13, 3.14).

Observed during the matrix sweep that landed `filterwarnings = ["error"]`
(2026-05-06):

```
=== Python 3.12 ===
=========================== short test summary info ============================
FAILED tests/unit/suite/test_otto_suite.py::TestOttoTestDir::test_test_dir_created_per_test
======================= 1 failed, 1049 passed in 10.81s ========================
```

Two subsequent full-suite runs on 3.12 passed cleanly (1050 passed, 0
warnings). The failure tail did not include a warning summary, so this is
not warnings-as-errors collateral — it's a separate, pre-existing flake.

## Suspected cause

The test spawns an inner `pytest.main()` session via
[tests/unit/suite/test_otto_suite.py:_run_inner_pytest](../tests/unit/suite/test_otto_suite.py#L52-L65)
that writes/reads a capture file under `tmp_path` while the outer session
is running with `-n auto --dist loadgroup`. Likely culprits, in order of
likelihood:

1. **xdist worker contention** — the inner pytest invocation runs serially
   inside a worker process; if two workers happen to schedule
   inner-pytest-spawning tests concurrently, one may briefly trip on the
   other's pytest plugin state (registry/logger patches).
2. **Logger patch leakage** — `_run_inner_pytest` does
   `with patch.object(suite_module, "logger", mock_logger)` at outer-session
   scope; if a different test in the same worker imports `suite_module`
   while the patch is active, behavior diverges.
3. **3.12-specific timing** — the failure has not been observed on adjacent
   versions, suggesting a Python 3.12 GC/asyncio scheduling quirk amplifies
   whatever race exists.

## Investigation plan

1. Reproduce reliably: loop the 3.12 full-suite invocation
   (`for i in {1..50}; do uv run --python 3.12 --group dev pytest tests/unit
   -m "not integration and not hops" --no-cov || break; done`) and capture
   the failure detail (assertion message, capture file contents, inner
   pytest stdout) when it next fires.
2. Once a failure is captured, confirm whether `capture_file.read_text()`
   produced fewer than 2 lines, contained extra lines, or was empty —
   that distinguishes a teardown ordering bug from a worker-isolation bug.
3. If worker contention is implicated, mark the inner-pytest tests with
   `@pytest.mark.xdist_group("inner_pytest")` so they serialize on a
   single worker.
4. If logger-patch leakage is implicated, scope the patch tighter (only
   around the `pytest.main()` call, not around the helper's full body) or
   inject the logger via a fixture instead.

## Related

- The same helper is reused across `TestOttoTestDir`,
  `TestSuiteOptionsFixture`, `TestTeardownMethod`, and the parametrize
  test in the same file — if the root cause is in `_run_inner_pytest`,
  any of those could flake next.
- Test was not previously known to flake; the warnings-as-errors change
  did not touch this file.

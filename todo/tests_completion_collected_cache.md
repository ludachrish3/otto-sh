# `--tests` completion: cache of *collected* test names — IMPLEMENTED

Status: **done** on `worktree-architecture-docs-restructure` (this branch).
Chose **Option A (collect at tab time) + Option B (warm on real runs)** with
the static `ast` scan kept as the always-available floor.

## What shipped

`otto test --tests` completion now layers two sources:

1. **Static floor** — `collect_test_names` (`ast` scan, unchanged). Instant,
   never runs user code, covers every statically written `def test_*` /
   `Test*` method. Guarantees completion is never empty.
2. **Collected set** — real pytest collection, so *dynamically generated*
   tests (`pytest_generate_tests`, conftest fixtures) are included and the
   result matches the repo's actual pytest config. Base names only —
   `--tests` selects by base name (a bare `test_x` runs every `test_x[...]`),
   and per-parametrization ids are rejected by `_resolve_selection`, so
   offering them would be a bug.

### Where the collection cost is paid (the key decision)

Chris's call: Option A is a superset of B — with the real-run hook feeding the
same cache, the only time anyone pays a slow TAB is the very first `--tests`
TAB on a cold cache with no prior collection. So both are wired to **one**
cache field:

- **Option A (tab time):** `_tests_completer` → `read_collected_tests`; on a
  cold/stale miss → `maybe_warm_collected_tests`, which spawns a **disposable,
  timeout-bounded subprocess** (`_OTTO_DUMP_TEST_NAMES=1 otto`, handled as an
  early exit in `cli/main.py:entry`) that does a full `collect_tests()` and
  prints a framed name list. The completer parses it, caches it, and returns
  it — so the triggering TAB is already enriched.
- **Option B (real runs):** `otto test --list-tests` with **no** marker/suite
  narrowing calls `record_collected_tests_from_items` — the full collection it
  already ran warms the cache for free. (Filtered collections are never cached:
  they'd store an incomplete set.)

### Why a subprocess, not in-process (load-bearing)

`repo.collect_tests()` runs `pytest.main()` in-process with no timeout. Running
it inside the completer would risk a **wedged shell** on a slow/hanging import
and could corrupt the completion stdout stream. The subprocess gives a hard
`COLLECT_TIMEOUT_SECONDS` cap + kill, full stdout isolation (framed payload),
and crash containment — which is exactly what makes Chris's "single slow TAB"
*bounded* rather than "possibly-wedged TAB".

### Robustness guards

- **Cooldown** (`COLLECT_COOLDOWN_SECONDS`): a failed/timed-out attempt is
  recorded (`names=None`); subsequent TABs skip re-collecting during the
  window. This enforces "single slow TAB" even when a repo can't collect
  within the timeout (otherwise every TAB would be slow).
- **Lock** (`COLLECT_LOCK_FILENAME`, atomic `O_EXCL`, stolen when stale):
  concurrent TABs don't each spawn a collection.
- **Fingerprint keying:** the collected set lives under the reserved
  `__collected_tests__` key, keyed by the *same* fingerprint as the main cache
  (test-file mtime/size), so a test edit invalidates it automatically.
- **Separate namespace:** writing collected names never touches the main
  fingerprint entries, so the slow-path writer (which must NOT collect) and
  this warmer touch disjoint data — no clobber in either direction.
- `maybe_warm_collected_tests` never raises into the shell.

## Touchpoints (as built)

- `src/otto/configmodule/completion_cache.py` — `__collected_tests__`
  namespace + `COLLECTED_SCHEMA_VERSION`; `read_collected_tests`,
  `_record_collected_tests`, `record_collected_tests_from_items`,
  `maybe_warm_collected_tests` / `_warm_collected_tests`,
  `dump_collected_test_names`, `_run_collect_subprocess`,
  `_acquire_collect_lock`, `_parse_dumped_names`, `_test_names_from_items`;
  `_atomic_write_json` factored out of `write_cache`.
- `src/otto/cli/main.py` — `entry()` early exit on `DUMP_TESTS_ENV_VAR`.
- `src/otto/cli/test.py` — `_tests_completer` (floor ∪ collected, warm on
  cold); `--list-tests` unfiltered branch warms via Option B.
- Tests: `tests/unit/configmodule/test_collected_tests_cache.py`,
  `tests/unit/cli/test_lab_tests_completers.py`.
- Docs: architecture `lifecycles/test.md` + `subsystems/registries.md`,
  `guide/test.md` + `guide/cli-reference.md`, CHANGELOG `[Unreleased]`.

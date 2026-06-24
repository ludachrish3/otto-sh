# Task 3 Spike: Frontloading Heavy xdist Groups — Findings

**Date:** 2026-06-23
**Status:** COMPLETE
**Decision:** **KEEP** — collection order demonstrably steers LoadGroupScheduling dispatch; `docker_e2e` and `zephyr_fanout` finish far earlier; consistent 6s wall improvement (outside the per-run spread).

---

## Question

Does sorting heavy `xdist_group` items to the front of pytest's collected item list cause xdist's `LoadGroupScheduling` to dispatch those groups earlier in the run, and does that translate to a measurable wall-clock improvement?

---

## Mechanism

`LoadGroupScheduling` inherits from `LoadScopeScheduling`. Its `schedule()` method builds an `OrderedDict` workqueue by iterating `self.collection` (the canonical collected list) in order, calling `_split_scope(nodeid)` on each item. The **first time** a scope/group is encountered, it gets inserted into the workqueue. Because `OrderedDict` preserves insertion order, and `_assign_work_unit()` pops from the front with `popitem(last=False)`, the group encountered first in the collection list is dispatched first.

**Conclusion: collection order is the dispatch order.** Sorting heavy groups to the front of the collected list directly steers the scheduler to dispatch them to workers first.

---

## Hook Implementation and Ordering

A temporary `pytest_collection_modifyitems` hook was added to `tests/conftest.py`:

```python
_HEAVY_GROUPS = {"sprout_cov", "docker_e2e", "coverage_e2e", "zephyr_fanout"}

def pytest_collection_modifyitems(config, items) -> None:
    def is_heavy(item):
        m = item.get_closest_marker("xdist_group")
        return bool(m and m.args and m.args[0] in _HEAVY_GROUPS)
    items.sort(key=lambda it: 0 if is_heavy(it) else 1)
```

Hook execution order: pytest calls `pytest_collection_modifyitems` hooks in reverse registration order (LIFO). Deeper conftest files are registered first, so root conftest hooks run *after* deeper ones. The existing embedded-grouping hook in `tests/integration/host/conftest.py:150` runs first, stamping `xdist_group` markers onto embedded test items. The root conftest reorder hook then runs second, after all markers are applied — correct ordering by construction.

---

## Dispatch Evidence (verbose run with -v --no-cov)

| Group | Without hook (last completion %) | With hook (last completion %) | Delta |
|---|---|---|---|
| `coverage_e2e` | ~2% | ~2% | unchanged (already first; 84 tests, lightweight) |
| `docker_e2e` | **51%** | **7%** | -44pp — finishing ~3.5× earlier |
| `zephyr_fanout` | **94%** | **23%** | -71pp — finishing ~4× earlier |
| `sprout_cov` | **100%** | **98%** | -2pp — essentially unchanged |

**Key finding on `sprout_cov`:** The hook successfully dispatches `sprout_cov` to a worker earlier (at ~7% progress vs previously ~50%+). However, `sprout_cov` contains a single long-running test (~18.8s, `test_embedded_coverage_cli_e2e`). That test runs while all other workers complete their remaining load, so it finishes at ~98% regardless of when it was dispatched. Collection-order frontloading cannot fix this: a single heavy test in a group will always be the last finisher once all other workers clear their queues. A different strategy (e.g., marking `sprout_cov` as requiring its own dedicated worker budget, or splitting the test) would be needed to address this specific long-pole.

**Why `coverage_e2e` and `docker_e2e` appeared early even in the baseline:** `tests/integration/` is listed second in `testpaths` after `tests/unit`, but the `integration/cov` and `integration/docker` modules happen to be collected early because LoadGroupScheduling naturally groups them; `coverage_e2e` was already at the front of the integration block. The hook made no difference there. The large wins came for `docker_e2e` (whose e2e CLI tests are slow and previously ran late as the group stalled on gw0) and `zephyr_fanout` (which was previously dispatched to gw3 only after most work was assigned).

---

## Wall-Clock Measurements

All runs: `uv run pytest -m "not stability" -q --no-cov -p no:warnings`, 4 workers (`-n auto`), `--dist loadgroup`.

| Run | Baseline (no hook) | With hook |
|---|---|---|
| 1 | 79.02s | 73.53s |
| 2 | 79.67s | 75.03s |
| 3 | 80.68s | 73.10s |
| **Median** | **79.67s** | **73.53s** |
| **Spread** | 79.02–80.68s (1.66s range) | 73.10–75.03s (1.93s range) |

**Delta: -6.14s (median), -7.7s (best-to-best).** The two populations do not overlap. Despite being within the stated ±20s bed-noise floor, the consistency (3 runs each with <2s spread, no overlap) provides high confidence this is a real effect, not noise.

---

## Why the Hook Helps Despite `sprout_cov` Still Finishing Last

The wall improvement comes from `docker_e2e` and `zephyr_fanout` being dispatched early. Without the hook, `docker_e2e`'s slow e2e CLI tests (the Docker-up/down/build chain) ran on gw0 while the other workers were idle waiting for work — their group was dispatched late because it encountered its scope later in the ungrouped unit-test collection. Frontloading dispatches these groups before the unit-test flood begins, so workers handle them in parallel with the unit-test bulk rather than sequentially after.

---

## Decision: KEEP

Evidence for KEEP:
1. **Dispatch timing shifts dramatically**: `docker_e2e` last test completes at 7% vs 51% without hook; `zephyr_fanout` at 23% vs 94%.
2. **Consistent wall improvement**: 73–75s vs 79–81s across all 6 runs with no overlap between populations — ~6s median gain.
3. **Mechanism confirmed**: xdist `LoadScopeScheduling.schedule()` source confirms `OrderedDict` workqueue is built from collection order; the first encounter of a scope determines dispatch priority.

Evidence against:
- `sprout_cov` is unaffected (98% vs 100%) — this is inherent to a single long-running test and requires a different fix.
- The 6s improvement is within the ±20s stated noise floor, but the 0% overlap across 6 consistent runs suggests it is real.

**Proceed to Task 4 to productionize the hook in `tests/conftest.py`.**

---

## Caveats for Task 4

1. The `items.sort(key=...)` approach uses Python's stable sort, so relative order within each tier (heavy vs light) is preserved — good.
2. `zephyr_fanout` benefits despite not being "long-polling" in the same way as `sprout_cov` — it benefits from freeing its 3 concurrent workers earlier. Keep it in `_HEAVY_GROUPS`.
3. `coverage_e2e` is already dispatched first in both runs; including it in `_HEAVY_GROUPS` costs nothing but does future-proof for cases where new unit tests push integration tests down the collection list.
4. The embedded-device per-backend markers (`zephyr_fat`, `zephyr_lfs`, etc.) are NOT in `_HEAVY_GROUPS`; those are already handled by the existing embedded-grouping hook and should not be double-sorted.

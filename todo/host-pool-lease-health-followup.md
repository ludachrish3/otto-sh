# Follow-up: make `lease_unix_host` health-aware + bounded

**Source:** final whole-branch review of the test-suite speedup (2026-06-24), finding I-1.
Non-blocking — recorded for a future, properly-scoped change.

## Gap

`tests/_fixtures/_host_pool.py::lease_unix_host` is an unbounded busy-wait
(`while True` + 50 ms poll) with **no timeout** and **no consultation of the
reactive bed-wedge gate** (`tests/integration/host/conftest.py`). The speedup
spec (§2, §4a) called for the lease to be *health-aware* — skip a wedged host
and **fail loudly with the host names** on a fully-sick pool, "never silently
wait."

## Why it was left as-is (not fixed in the speedup)

- A **down** host does not break the lease (the lock is a local file, not the
  host); the lease still hands the host to a test, which then fails **loudly,
  host-named**, on its connection error. So the project's "never skip on
  host-down" policy is already satisfied in practice.
- The only un-handled edge is *all pool locks held forever* (a leaked lease),
  which the release-on-exit `finally`/fd-close path makes practically
  impossible — and which would otherwise surface as a (loud, test-named)
  pytest-timeout, not a silent skip.
- Adding a naive timeout risks **false failures under legitimate heavy
  contention** (many concurrent lease-wanters, few hosts), so it was not worth
  the risk inside the speedup.

## If/when implemented

- Add a wall-clock deadline to the poll loop; on expiry
  `raise`/`pytest.fail(f"no free Unix host in {list(candidates)} after Ns — "
  "pool may be wedged")` (loud, host-named), with the deadline generous enough
  not to trip under normal contention.
- Optionally skip candidates already marked in the per-worker `_BED_HEALTH`
  signal so a known-wedged host is never leased.

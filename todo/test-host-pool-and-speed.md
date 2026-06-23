# Test host-pool & speed strategies (follow-up brainstorm)

> Deferred from the 2026-06-22 test-restructure brainstorm (which scoped itself
> to restructure + dedup). This captures the **speed half** of that request so a
> follow-up brainstorm → spec → plan can start warm. Gate this AFTER the
> restructure lands (the restructure moves `_console_lock.py` into
> `tests/_fixtures/`, which the pool lease would build on).

## The problem

Integration/e2e tests hardcode a "favorite VM" instead of pulling an available
host from a pool, which both concentrates load and forces a test to wait on a
specific host even when an equivalent one is free.

- `host1`, `transfer_host`, and the hop fixtures all funnel onto **`carrot`**;
  `host2`→`tomato`, `host3`→`pepper`. **`basil`** sits nearly idle (only the
  embedded SSH hop). (See `tests/conftest.py`.)
- The suite keeps growing; parallelization is uneven.

## Ideas to explore

- **Dynamic Unix-host lease from a pool** `{carrot, tomato, pepper, basil}`:
  a test that just needs "a Unix host" leases whichever is free, spreading load
  ~4×. Cross-worker lease can mirror the writer-fair file lock in
  `tests/_fixtures/_console_lock.py`.
- **Lab data already has the scaffolding:** per-host `resources` and `labs`
  tags, plus `remote_name(worker_id, …)` for per-worker path namespacing.
- **Health-aware routing:** lease around the reactive bed-wedge gate
  (`integration/host/conftest.py`) so a sick host is skipped, not waited on.

## Hard constraints to carry forward

- **Coverage equivalent; scenarios preserved.** Same as the restructure.
- **Embedded backends are NOT poolable** — each `sprout*` is a distinct
  coverage scenario (fs × os_version × command_frame). Keep them
  scenario-parametrized.
- **Preserve resource-contention groups** — `--dist loadgroup`, the single
  -client console lock, the docker-host serialization.
- **otto stays server-less** (fable review #6): the pool is a *test-harness*
  lease, not a coordinator service.

## Open questions for the brainstorm

- Is the pool worth it given SSH/telnet servers already accept many concurrent
  clients (so the real bottleneck is per-VM CPU/disk under soak, not connection
  count)? Quantify the actual win before building.
- Provision more interchangeable Unix VMs, or just better-utilize the existing
  four?
- Does the lease integrate with otto's own JSON/DB reservation backend, or stay
  a pytest-only concern?

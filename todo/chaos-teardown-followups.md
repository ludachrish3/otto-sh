# Chaos hardening Plan 2 — deliberate scoping leftovers

Recorded during the final-review fix wave for
`docs/superpowers/plans/2026-07-31-chaos-plan2-teardown-robustness.md`. Both
items below are known gaps, deliberately left out of Plan 2's scope rather
than missed — flagging them here so a later chaos plan (or a standalone fix)
picks them up instead of them being rediscovered from scratch.

## 1. nc GET-path listener has no cancel-time reap

Task 9 (this plan) wrapped the **put** path's cancel-time listener reap in
`compensate()`: `NcFileTransfer._put_files_nc`'s `except asyncio.CancelledError`
handler in `_attempt` cancels+joins `listen_task` and reaps the remote `nc -l`
via `_cancel_and_reap` (`src/otto/host/transfer/nc.py`, `_attempt`, ~line 976).

The **GET** path (`_get_files_nc` / `_get_files_nc_tunneled` and their inner
`_get_one`, `src/otto/host/transfer/nc.py` ~592-710) has no equivalent
handler. A cancellation landing mid-GET tears the local coroutine but never
cancels or reaps the remote listener task it spawned — that `nc -l` lingers
on the remote host until its own `-w` timeout fires.

`NcOptions.listener_timeout` defaults to `30.0` seconds
(`src/otto/host/options.py:451`), which is **longer** than
`DEFAULT_TEARDOWN_DEADLINE = 10.0` (`src/otto/lifecycle.py`). So on this path
the chaos spec's success-criterion #1 ("every teardown chain either
completes or is force-abandoned within the deadline — nothing outlives it
unbounded") is not met: the remote listener can outlive otto's own teardown
deadline by up to 20 extra seconds, unbounded from otto's side once the
local process has exited.

**Why deferred:** deliberate Plan 2 scoping, not an oversight — the governing
spec's call-site list for `compensate()` names only the put-path reap
(alongside tunnel/link rollback and the `as_user` undo chain); the GET path
was never in Plan 2's four call sites.

**Where the fix goes:** a future chaos plan (3-5) or a standalone fix:
extract a `_cancel_and_reap`-shaped helper for the GET path's listener task
(mirroring Task 9's shape) and run it under `compensate()` from
`_get_one`'s/`_get_files_nc_tunneled`'s own `except asyncio.CancelledError`
handling. Consider also whether `listener_timeout`'s default should drop
below `DEFAULT_TEARDOWN_DEADLINE` independent of this fix.

## 2. `clean_remote_gcda`'s tail rebuild can discard live same-loop connections

`src/otto/coverage/collect.py`'s `clean_remote_gcda` (~lines 94-96) ends with:

```python
for host in all_hosts():
    if isinstance(host, UnixHost):
        host.rebuild_connections()
```

This runs **unconditionally**, for every configured Unix host, regardless of
whether that host's current connections are actually dead (opened on a
now-closed loop — the case `HostScope.rebuild_connections()`, added in this
plan's Task 11, exists to handle) or perfectly live on the **current** loop.
If `clean_remote_gcda` is ever invoked after a host already has working
same-loop connections open (e.g. a caller that connects, does other work,
then runs the `--cov-clean` pre-run step later in the same `run_command`),
this rebuild tears down and discards those live connections for no reason —
the same defect class Task 11 fixed one function over (`suite/run.py`'s
post-run sweep used to attempt cross-loop closes; here the direction is
reversed — a *live* connection gets thrown away pre-emptively instead of a
*dead* one being closed late).

**Why deferred:** out of this plan's scope — Plan 2's Task 11 was scoped to
`suite/run.py`'s post-run sweep specifically (the call site the governing
spec named); `clean_remote_gcda` is a pre-run cleanup path with different
callers and wasn't part of that task's file list.

**Where the fix goes:** a standalone fix (or folded into a later chaos plan
touching `otto/coverage/`): give `HostScope`/`UnixHost` a way to distinguish
"connections opened on a dead loop" from "connections opened on the current,
live loop" so `rebuild_connections()` (or a new loop-aware variant) only
drops the former, and have `clean_remote_gcda` call that instead of the
unconditional rebuild.

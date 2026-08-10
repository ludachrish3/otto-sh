# nc remote listeners leak forever — `-w` does not bound `nc -l` (FIXED)

Found 2026-08-10 while chasing what looked like a ~6% flake in
`test_session_stability_integration.py::test_real_concurrent_transfers[nc-telnet]`.
It is not a flake. It is a real product leak in `src/otto/host/transfer/nc.py`,
and the test was correctly reporting it.

## The false premise

`NcOptions.listener_timeout` (`src/otto/host/options.py`) documents:

> "Seconds a remote `nc -l` listener may wait for a client before it
> self-terminates (passed as `nc -w`) … Bounds the orphaned-listener hang: if a
> concurrent process wins a port-collision race, our sender's bytes land in
> *its* listener and ours never gets a client — without this it would block
> forever."

OpenBSD netcat's man page (the bed runs `OpenBSD netcat, Debian patchlevel
1.226-1ubuntu2`) says the opposite, verbatim:

> "The **-w** flag has **no effect on the -l option**, i.e. nc will listen
> forever for a connection, with or without the -w flag."

So `nc -l -w 30` never self-terminates while waiting to accept. Confirmed three
ways: the man page; six leaked listener pairs alive 1–3.5 days on tomato, all
spawned with `-w 30`; and a direct control (`nc -l -w 20`, still running at 28s).

The belief is load-bearing in at least four comments in `nc.py`, and both
listener spawn sites pass `timeout=float("inf")` to `_exec_cmd` *because* of it
("netcat self-bounds via `-w <listener_timeout>`; an otto timeout here would be
redundant"). One wrong premise removed both bounds.

## What actually leaks

`_reap_nc_listener` / `_cancel_and_reap` already exist and do the right thing —
a throwaway connect-and-close makes a lingering listener exit. They are wired to
**only** the two `asyncio.CancelledError` handlers (external Ctrl+C / chaos
teardown, from `todo/chaos-teardown-followups.md` §1).

Every *ordinary* error path does a bare `listen_task.cancel()` +
`gather(...)`, which cancels otto's local await and leaves the remote `nc`
listening forever. As of this writing:

    line  924  BARE CANCEL      line 1020  BARE CANCEL
    line  934  BARE CANCEL      line 1126  reaps (inside _cancel_and_reap)
    line  951  BARE CANCEL      line 1181  BARE CANCEL
    line  974  BARE CANCEL      line 1210  BARE CANCEL
    line 1001  BARE CANCEL      line 1221  BARE CANCEL
                                line 1239  BARE CANCEL

Ten leaking branches, two reaping. Includes the branch that *reports* the
problem — the orphaned-listener timeout returns
`"nc listener on port N did not exit … (orphaned listener …)"` and then does not
reap the orphan it just named.

## Bed evidence (reaped 2026-08-10; forensics in the session scratchpad)

Twelve processes, six pairs, **all on tomato**; carrot, pepper and basil clean.

    put  nc -l  -w 30  9000/9003/9004   > /tmp/nc_hop_upload.txt   Aug 6, 7, 9
    get  nc -Nl -w 30  9001/9002/9005   < /etc/hostname            Aug 7, 7, 9

Two things fall out of that table:

1. **The ports climb monotonically with each leak** (9000→9005 in strict date
   order). Each leaked listener permanently holds a port, so `_find_free_port`
   is pushed one higher every time. This compounds; it does not plateau.
2. **Only the hop path leaks.** Both hop nc tests hard-code `tomato`
   (`test_hop_integration.py::test_nc_put_through_hop` / `test_nc_get_through_hop`);
   ordinary nc transfers lease from the pool and left carrot/pepper spotless
   across days. The hop path is the one that reaches these error branches often
   enough to matter — plausibly because with a tunnel the local asyncssh
   listener "accepts immediately regardless, hiding the not-yet-listening
   remote" (nc.py's own comment), but that link is NOT yet established.

## Why it presented as a 6% flake

`transfer_host` leases a host from `UNIX_POOL` (carrot tried first, then tomato,
pepper), so it only lands on tomato under concurrency. The leak assertion uses
`_NC_LISTENER_PROBE` = `pgrep -af "[n]c -l"`, which is **host-global**: whenever
the lease landed on tomato it correctly reported the permanent foreign
leftovers. 20/20 passed in isolation because the lease got carrot every time,
which is also why a serial re-run always "fixes" it.

## Second defect: the probe has a 50% blind spot

`[n]c -l` cannot match `nc -Nl`. The entire **get** direction is invisible to
the guard that exists to catch exactly this class — half the leaks above were
found only with a broader sweep. `_NC_LISTENER_PROBE` is described in
`tests/_fixtures/bed_hygiene.py` as the bed-hygiene authority, so the authority
under-reports by half.

## Fix — landed

All of 1-3 below shipped, plus 4 (the `timeout` wrapper, approved by Chris). What
actually shipped differs from this plan in one important way, recorded under
"What the plan got wrong" at the end. 5 is still open.

1. Route the ten bare-cancel branches through the existing `_cancel_and_reap`.
   Minimal, idiomatic, and no behaviour change for a healthy transfer.
2. Widen `_NC_LISTENER_PROBE` to match `nc -Nl` as well as `nc -l`. Expect this
   to surface get-direction leaks that have always been there.
3. Correct the `-w` comments and the `listener_timeout` docstring — they are the
   reason the reap was never added to these branches.
4. Decide separately: a remote-side bound (`timeout N nc -l …`; coreutils
   `timeout` is present on all four lab hosts) would also cover otto dying
   outright, which no `finally` can. **Trade-off:** `timeout` caps the whole
   listener lifetime, including an established transfer, so a large or slow
   transfer would be cut at `listener_timeout`. That is a real behaviour change
   and wants a deliberate decision, not a drive-by.
5. Consider whether the hop tests should lease from `UNIX_POOL` like everything
   else instead of hard-coding tomato. Not the cause of the leak, but it is why
   the damage concentrated on one box and collided with leaseholders.

## Test to write first

The leak is observable without the bed: assert that each error branch reaps.
A unit test over `_get_files_nc` / `_put_files_nc` with a stubbed `_exec_cmd`
can assert `_reap_nc_listener` was awaited for the branch under test — one case
per branch, each of which fails against today's code.


## What the plan got wrong

The plan said "route the ten bare-cancel branches through `_cancel_and_reap`",
and the test section said a unit test could "assert `_reap_nc_listener` was
awaited for the branch under test — one case per branch". Both are enumerations,
and the enumeration was incomplete.

Review found an **eleventh** path with no `listen_task.cancel()` to grep for:
PUT's bare `await self._wait_for_remote_listener(port)` raises `ConnectionError`
past every handler except `CancelledError`, stranding the remote listener, the
local task, and `_put_one`'s retry. The AST guard written alongside the first
cut was built from the same `listen_task.cancel()` query that produced the list
of ten — so it inherited the same blind spot and reported clean.

**A guard built from the query that found the bug can only re-find that bug.**
The shipped fix is therefore one `finally` per attempt, before `_release_port`,
which covers every branch including ones not yet written, and the guard asserts
that structure rather than a list of sites.

Two further things the plan did not anticipate, both found by the tests
reacting to the change rather than by reading:

- `_reap_nc_listener`'s `forward_port` had no timeout, so reaping on the
  forward-setup-timeout branch re-entered the very forward that had just
  stalled — a bounded failure became a hang. Now bounded.
- The `if listen_task.done(): return` short-circuit is timing-dependent across
  interpreters: 3.10 and 3.11 skip the reap on the GET empty-transfer branch,
  3.12 through 3.14 perform it. Only `make nox`'s multi-Python leg caught it,
  after two single-version runs had "confirmed" a stub fixture in opposite
  directions.

Still open, from the review: `forward_port` is not cached, so each tunneled
reap leaks a local asyncssh forward (pre-existing, now exercised more often);
on the collision branch a blind connect-and-close could truncate a *racing*
process's file; the hard cap could reasonably be an `NcOptions` field; and a
GET that exceeds the one-hour cap would still truncate silently.

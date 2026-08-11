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

Still open, from the review: on the collision branch a blind connect-and-close
could truncate a *racing* process's file; the hard cap could reasonably be an
`NcOptions` field; and a GET that exceeds the one-hour cap would still truncate
silently.

## Follow-up, landed: the uncached forward (the local half of the leak)

The review's first open item — `forward_port` is not cached — was its own leak,
local rather than remote, and is now fixed. Measured before the fix on a
`tomato`-via-`carrot` host: five sequential hop puts took the process from 10 to
18 open descriptors, and a single put of six files from 8 to 20 — two per
*file*, because `forward_local_port("", ...)` bound both address families. All
of it was reclaimed at `close()`, which is exactly why nobody noticed: the
damage is bounded by one host session, and a bulk put of N files strands 2N
descriptors while it is running.

Two mechanisms, because one is not enough — and the first cut shipped only the
first one, which review caught.

**Caching.** `SshHopTransport._port_forwards` is a dict keyed by
`(dest_host, dest_port)`. Keying on the destination is sound because a forward
is a route, not a session: asyncssh resolves `dest_host:dest_port` and opens the
channel when the local listener *accepts*, so a listener built for one remote
`nc` reaches the next one, and a rebuilt telnet client is carried by the forward
its dead predecessor used. That last case is a second, distinct leak the cache
fixes on its own: `ConnectionManager.telnet` rebuilds when the cached client
reports `alive` false and runs the same `_forward_port` call, so before this
every telnet reconnect stranded a listener for port 23.

**Releasing.** Caching alone does almost nothing for the case that motivated
the fix. `_put_files_nc` dispatches every file through an unbounded
`asyncio.gather`, and `_find_free_port` reserves a *distinct* remote port per
in-flight caller, so a bulk put opens N forwards at once — N different keys,
no reuse available. (The fan-out is bounded as of 2026-08-11 — see
`nc-listener-readiness-under-concurrency-2026-08-11.md`, where the unbounded
gather turned out to be a second defect: it overran the remote sshd's
`MaxSessions`. The reasoning here is unaffected, since the bounded fan-out still
opens several distinct-key forwards at once.) Measured on an 8-file put through a hop: **6 descriptors
stranded with caching alone, 0 once each attempt releases its own.** The same
gap appears sequentially on any target whose port strategy resolves to `python`
or `custom`, which return a fresh ephemeral port every call instead of
rescanning from the base — there the cache never hits at all. So
`unforward_port` releases the forward in the per-attempt `finally` the previous
fix established, after the reap (which needs the forward to reach the listener
it is killing) and before `_release_port`. It is synchronous on purpose: that
`finally` can run under cancellation, where an `await` can raise and skip the
rest.

Also in `close()`: each `listener.close()` now runs under `teardown_step`. One
raising listener used to skip every later listener, the tunnel teardown, and the
parent cascade — the cleanup path of a leak fix being the largest leak in the
file.

The bind narrowed from `""` to `"localhost"` in the same change. Every caller
already connected to `localhost`, so all-interfaces only ever bought off-box
reachability nobody asked for. Note what this is *not*: forwards have always
outlived their transfer, so this removes a standing exposure rather than one the
caching introduced. On this VM it also halves the sockets per forward, but that
is incidental — `getaddrinfo("localhost")` returns one address here only because
Debian names `::1` `ip6-localhost`; on a distro that maps `::1 localhost` it is
still two, bound to the same port by asyncssh's own retry of the assigned port.

### What the fd watermark bracket could not see

A `hops`-scoped `fd_watermark_bracket` went into
`tests/integration/host/conftest.py` alongside this. It is worth having, but it
did **not** catch this leak and could not have: `ConnectionManager.close()`
releases every forward, each hop test closes its host in a `finally`, and the
bracket's verdict is taken after that. The whole `-m hops` module reported 17
passed while leaking. Same structural blindness as the collectable-transport
case in `todo/`-adjacent notes — when the cleanup that runs before the verdict is
what erases the evidence, a teardown bracket is the wrong instrument.

Two tests measure from inside the host's lifetime instead, one per shape, with
descriptors counted before the close that would hide them:

- `test_hop_transfers_do_not_accumulate_port_forwards` — eight sequential
  single-file puts. Eight rather than two or three because the leak is one
  descriptor per transfer, so the repeat count *is* the margin against the
  tolerance of 2; widening the tolerance would blunt the test, adding repeats
  sharpens it. Each repeat writes a distinct payload and reads it back, so a
  forward that is reused but no longer routes anywhere fails on content rather
  than passing on a flat count.
- `test_a_bulk_hop_put_does_not_strand_a_forward_per_file` — one put of eight
  files. This is the shape the cache cannot help and the one that fails if the
  release is removed. Without it the suite would have certified a stronger
  property than the code delivers, which is exactly what the first cut did.

Cost of the bracket, since leak instruments have been expensive before: 18 tests,
three serial runs each way, 10.93s mean with and 10.97s without — below noise, so
`gc_policy="always"` it is, matching the other bed lanes. `on-suspicion` was the
first choice, justified by the 16.1s -> 54.0s figure from `tests/unit/host`; that
figure is 1426 tests times two collects and does not transfer to 18. With nothing
to buy, the policy that cannot be fooled by the previous test's garbage inflating
the baseline is the right one, and this lane has the noisiest heap in the repo.
Tolerance stays at the authority's 4 rather than `tests/unit/host`'s 0: that lane
earned 0 by measuring a flat floor, nobody has measured this one, and it has live
SSH sessions moving under the test. The cost of that is stated plainly in the
fixture — the bracket cannot see a retained leak of four descriptors or fewer,
which is a further reason the in-test counters exist alongside it rather than
instead of it.

### Follow-up, landed: the teardown race

`close()` took neither lock, so an in-flight `forward_port` could store its
listener into an already-swept dict, and one awaiting `get_tunnel()` could open a
fresh SSH connection to the hop *after* close — the zombie-transport
`[unraisable]` class. Four windows, not the two guessed here: the factory await,
the bind await, and waiting on either lock while another caller holds it. On a
multi-hop chain the factory ALSO assigns `_parent` and connects it as a side
effect (`RemoteHost._build_hop_transport`), so `close()` read `_parent is None`,
skipped its cascade, and a fix that tore down only the connection moved the
zombie one hop up.

Fixed with a GENERATION counter, not the `_closed` flag suggested above. That
flag was implemented first and was wrong: `close()` in otto means "release
resources now", not "this object is dead". Measured against the pre-fix tree, a
post-close `get_tunnel` calls the factory a second time, `ConnectionManager`
never clears `_hop`, and `tests/e2e/tunnel_stability/test_monitor_loop.py`
closes a host deliberately so the next scan dials through a wedged sshd. Making
the transport terminal also broke hopped embedded coverage collection, where
`suite.py`'s class-scoped release closes every lab host and `EmbeddedHost` has
no `rebuild_connections`. The docstring claim that the factory is "called at
most once" was already false before any of this.

So `close()` ends a generation rather than the object: it bumps before it can
yield, and each creation path records the generation it entered in and releases
what it built if that generation moved. Reuse still works; only a resource whose
owner is gone is refused, as `HopTransportTornDownError`.

Two review passes shaped it. Both capture points must precede their lock — a
caller queued on a contended lock otherwise resumes and reads the *new*
generation, which is the same leak by another route and the one window the
`_closed` entry guard had covered by accident. Eleven mutants are pinned,
including both capture points, the bump's position (invisible to any test whose
`wait_closed` is an `AsyncMock`, because those never yield), and the parent
cascade on the abandon path.

### Still open

- Two concurrent `close()` calls: the second returns before the first has
  finished tearing down. Pre-existing shape, shared with
  `ConnectionManager.close`, and not reachable through `_build_hop_transport`
  today since each host builds its own chain.
- `close()` no longer guarantees "nothing is held on return" — a caller that
  starts *after* the bump can adopt a connection during close's own awaits and
  keep it. That is the price of reuse and is documented on the method.
- A factory that raises *after* assigning `_parent` and opening the parent's
  tunnel, when a `close()` has already passed, leaves the parent unowned. Narrow
  and pre-existing; the generation check is on the success path only.
- `HopTransportTornDownError` is a `RuntimeError`, and
  `ConnectionManager.forward_port` documents `RuntimeError` for "no tunnel
  configured", so an `except RuntimeError` now conflates the two.

# A bulk nc put loses files: the fan-out vs the remote sshd's channel budget

Found 2026-08-11 while running `make nox-full` to verify the hop-transport
teardown-race fix (commit "fix(host): end a generation of hop resources"). The
3.14 leg failed; 3.10 through 3.13 passed. **Not caused by that fix** — see the
differential below.

**FIXED 2026-08-11, same day.** Root cause, fix and verification are recorded
here; the residuals are at the bottom. Kept rather than deleted because the
"why 3.14" question is answered by argument, not by measurement, and because
one deliberate non-change is worth not re-deciding.

## The symptom

`tests/integration/host/test_hop_integration.py::test_a_bulk_hop_put_does_not_strand_a_forward_per_file`
puts 8 files in one call. Exactly one of the 8 fails; the other 7 succeed. Two
manifestations of the same window:

    Remote nc listener on port 9000 not ready within 5.0s     (the readiness probe gave up)
    ... : open failed                                          (probe passed, sshd's channel open to the port still refused)

## Root cause: sshd `MaxSessions`, which REFUSES rather than queues

Not a readiness-timing problem at all, which is why the first instinct — raise
the 5.0s budget — would have hidden it.

An nc transfer holds an SSH **exec channel** for the whole life of its remote
`nc -l`, and its readiness poll opens **another** while that one is held. A
default OpenSSH server allows `MaxSessions 10` channels per CONNECTION and
refuses the excess outright — it does not queue it. Measured on the bed
(tomato via carrot, one connection), concurrent `host.exec` calls:

    N=8   refused=0      N=20  refused=10
    N=10  refused=0      N=24  refused=14
    N=12  refused=2      N=32  refused=22

Exactly `refused = N - 10`. Both symptom messages are that one ceiling seen
from two sides: `open failed` is the listener's channel being refused, and
"listener not ready" is the READINESS POLL's channel being refused, so a
perfectly healthy listener cannot be confirmed.

The traceback confirmed the layer: `ChannelOpenError('open failed')` arrives via
`BaseHost.exec()` (`src/otto/host/host.py:765`) — a session/exec channel, not
the port forward.

Every nc direction then dispatched its files through a bare `asyncio.gather`,
so the channel demand was whatever the caller's file count happened to be.

## The fix

`NcFileTransfer._gather_per_file` — one dispatcher shared by all three
directions, holding an `asyncio.Semaphore` bound. Three properties are
load-bearing, each with its own guard in
`tests/unit/host/test_transfer_nc_fanout.py`:

- **Permits span whole TRANSFERS, not channels.** Bounding channels deadlocks:
  an in-flight listener holds its channel while its own readiness poll asks for
  a second, so enough listeners take every permit and block the very polls that
  must finish to release them.
- **The semaphore is per INSTANCE, not per call**, because `MaxSessions` is per
  connection. A per-call semaphore bounds one bulk transfer while handing every
  other concurrent transfer on the same host its own full budget — and
  `test_real_nc_high_fanout_put` is exactly that shape (20 separate one-file
  puts gathered against one host).
- **The default is derived, not picked**: `(10 - 2 headroom) // 2 per transfer`,
  from the OpenSSH default. `NcOptions.max_concurrent_transfers` overrides it,
  because otto cannot read the server's actual setting and a host with a
  *lowered* `MaxSessions` needs to go narrower.

Sharing one dispatcher was the point: the three directions each held an
identical copy of the gather-and-zip, so a fix applied where the failing test
pointed would have left the two get paths exposed.

## Verification

Same amplifier, same 3.14 interpreter, same session, bound toggled by making the
limit 1000 (which never blocks, i.e. the pre-fix behaviour with every other line
of the fixed tree intact):

| | N=8 | N=16 | N=28 |
| --- | --- | --- | --- |
| bound OFF, 3 runs | 0, 0, 0 | 0, 1, 1 | **4, 3, 5** |
| bound ON, 3 runs | 0, 0, 0 | 0, 0, 0 | **0, 0, 0** |

Seven mutants killed on the new guards: each of the three paths reverted to a
bare gather; permits collapsed to 1; the budget rescoped per call; the
configured limit ignored; the out-of-range check dropped; and the derived limit
hand-set above the channel budget.

## Why the fix was NOT "the flake predates the teardown-race commit"

That claim was made in this file's first version and it holds — three full 3.14
legs on each side of `b4da4f9c` failed 2/3 with it and 1/3 without, which is not
a distinguishable rate at n=3, and the pre-change runs carried unrelated
`coverage_e2e` failures from a worktree that had `uv sync` but no `npm ci`. The
root cause above is independent of that commit, which settles it properly: the
fan-out has been unbounded far longer than the teardown fix has existed.

## Residuals

- **Why 3.14 specifically is still not measured.** The mechanism is
  version-independent, so the honest reading is that load decides how much the
  transfers overlap, and the 3.14 leg runs last on the warmest bed. Consistent
  with everything observed, but argued rather than proven. It no longer matters
  for this defect; it would matter again if a similar single-cell failure shows
  up.
- **The 5.0s readiness budget is untouched, on purpose.** It was never the
  problem, and raising a bound to hide a queueing problem is the wrong fix.
- **`_warmup_for_transfer(len(src_files))` still pre-opens one telnet session
  per FILE**, which is now more than can be in flight. Deliberately not changed:
  pool sessions are reused, and capping it at the fan-out limit exactly would
  leave a concurrent control op to open a cold session mid-transfer — the very
  handshake the warmup exists to avoid. Sizing that needs a measurement nobody
  has taken, and over-warming is harmless.
- **Telnet pays the bound without needing it** (there is no `MaxSessions` on a
  telnet pool). The cost is small because telnet's control plane is already
  serialized by `_control_lock`, so the concurrency it loses is data-phase
  overlap only — but it is a real, unmeasured cost, and the knob is the way out
  for a lab that cares.
- The earlier 3.14 failure of this SAME test (`expected 33 bytes, got 20`, fixed
  2026-08-10 by putting the nc tests in the `nc-serial` xdist group) was a
  cross-WORKER port collision. That fix closed the cross-worker window and left
  this one open. `nc-serial` serializes tests, never the transfers inside one.

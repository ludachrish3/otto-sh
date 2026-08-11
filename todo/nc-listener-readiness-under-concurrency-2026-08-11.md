# A bulk nc put loses one file on 3.14: the listener-readiness budget vs 8-way concurrency

Found 2026-08-11 while running `make nox-full` to verify the hop-transport
teardown-race fix (commit "fix(host): end a generation of hop resources"). The
3.14 leg failed; 3.10 through 3.13 passed. **Not caused by that fix** — see the
differential below.

## The symptom

`tests/integration/host/test_hop_integration.py::test_a_bulk_hop_put_does_not_strand_a_forward_per_file`
puts 8 files in one call. Exactly one of the 8 fails; the other 7 succeed. Two
manifestations of the same window:

    Remote nc listener on port 9000 not ready within 5.0s     (the readiness probe gave up)
    ... : open failed                                          (probe passed, sshd's channel open to the port still refused)

`open failed` is sshd's channel-open failure reason for a `direct-tcpip`
channel it could not connect — so the local `nc` reached its forward, the hop
tried to connect onward to `tomato:PORT`, and nothing was listening yet.

## Why it is not the teardown-race fix

Measured, not argued. Three full `tests_all-3.14` legs on each side:

| tree | hop test outcome |
| --- | --- |
| `b4da4f9c` (with the fix) | FAIL, PASS, FAIL — 2/3 |
| `cdf75b80` (its parent) | PASS, PASS, FAIL — 1/3 |

Present on both sides, so the flake predates the fix. The rates are NOT
distinguishable at n=3 and should not be read as "the fix made it worse": the
pre-change runs were in a fresh worktree that had `uv sync` but not `npm ci`, so
each carried 10-13 unrelated `coverage_e2e` failures, which changes the load the
race is sensitive to. (Worktree lesson, again: `uv sync` alone is not an
equivalent environment.)

The code supports the same conclusion. Absent a `close()`, the fix's diff is
inert on this path: every functional change is inside `close()`, inside a
`generation != self._generation` branch that a close is required to reach, or a
bare read of the counter. A bulk put never closes the hop — the per-attempt
`finally` calls `unforward_port`.

## Where to look

- `_put_files_nc` dispatches every file through an **unbounded**
  `asyncio.gather`, so 8 files means 8 concurrent remote `nc -l` spawns, 8
  readiness probes and 8 forwards at once. Nothing bounds the fan-out.
- The readiness budget is 5.0s. On 3.14 that is evidently not always enough for
  the 8th spawn — worth measuring where the time actually goes before touching
  the number, because raising a bound to hide a queueing problem is the wrong
  fix and this repo has a rule about it.
- Why 3.14 specifically is unexplained. Do not assume it is a Python
  regression; the 3.14 leg also runs last and may simply inherit a warmer/busier
  bed. Establish the mechanism before believing the version is the cause
  ("single-matrix-cell failures name half the cause").
- Related but distinct: the earlier 3.14 failure of this SAME test
  (`expected 33 bytes, got 20`, fixed 2026-08-10 by putting the nc tests in the
  `nc-serial` xdist group) was a cross-WORKER port collision. That fix closed
  the cross-worker window and left this one open, so the class was larger than
  the fix. Do not treat `nc-serial` as evidence this area is now serialized —
  it serializes tests, not the 8 transfers inside one test.

## Suggested first step

Instrument rather than tune: log per-file spawn → ready → connect timings for
one bulk put on 3.14 and on 3.12, and see whether the 8th listener is slow to
bind or the readiness probe is starved. That answers whether the fix is a
concurrency bound on the gather, a longer budget, or a readiness probe that
does not contend with its own siblings.

Not reproducible in isolation: 6/6 green running the test alone on the 3.14
interpreter. It needs full-suite load.

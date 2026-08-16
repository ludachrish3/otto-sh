# `HostSession` never folds the host's standing log mode

Found 2026-08-15 while adding `HostSession`'s dry-run arms (the commit that adds
this file). **Pre-existing, on the REAL path as well as the dry-run one, and not
introduced or worsened by that change.** Filed because the fix needs design, and
because the reason it was left alone is itself a decision worth keeping.

## The gap

`BaseHost` folds the host's standing mode into every command it logs, at the emit
seam, via `_effective_log` (`host.py`, `effective_mode(self.log, log)`), and
`HostFilter`'s docstring records that this is where the fold lives:

> The per-host standing mode is now folded into each record's `log_mode` via
> `BaseHost._effective_log` at the emit seam, so the filter decides purely on
> `record.log_mode` plus the global command-output flag.

`HostSession` does not do it. `HostSession.run`'s per-command runner logs at the
per-command mode only:

```python
mode = sc.log if sc.log is not None else LogMode.NORMAL
if mode is not LogMode.NEVER:
    self._log_command(sc.cmd, mode)
```

and `HostSession.send` does the same. The sink it calls IS `BaseHost._log_command`
(the `SessionManager` is built with `log_command=self._log_command`), but the sink
does not fold — the fold is the caller's job, and this caller skips it.

**Consequence:** a host declared `log = false` in lab data (coerced to `QUIET` by
`HostSpec._coerce_log_bool`), or a monitor host pinned to `NEVER` by
`monitor.factory`, still prints commands run through a NAMED session at `NORMAL`.
`host.run("x")` obeys the standing mode; `(await host.open_session("s")).run("x")`
does not. Same host, same operator instruction, two answers.

## Why the dry-run work matched it rather than fixing it

The dry-run decline (`HostSession._decline_command`) announces at the same
unfolded per-command mode the real path uses. That was deliberate, and the
principle it follows is endorsed: **a dry run puts on the console exactly what a
real run puts there** — no more (it would leak; that is the `write_file`
base64-body defect the contract already fixed) and no less (an empty dry run is a
bug). Folding only in the dry-run arm would have made `-n` QUIETER than the real
thing, which is a different lie in the same family.

So the gap is real and it is one gap, not two. Fixing it fixes both paths at once.

## Why it is not a one-liner

`HostSession` holds **no reference to its host**. Its constructor takes
`log_command`/`log_output` callables, `creds`, `host_id` and `history_prefix` —
deliberately, so a session is drivable by anything that can supply those seams
(`SessionManager` is constructed with a bare `session_factory` in tests and by
`LocalHost`/`DockerContainerHost` with no `ConnectionManager` at all). The
standing mode lives on the host (`self.log`, mutable at runtime — `SuppressCommandOutput`
swaps it in and out per context). So the fold needs one of:

1. **Fold at the sink.** Move `_effective_log` inside `BaseHost._log_command`
   instead of asking callers to apply it. One place, cannot be forgotten, and
   `HostSession` gets it for free. But the fold is idempotent (`max`), so the
   existing folded call sites stay correct — check that claim against every caller
   before believing it, including `_dry_run_result`, which folds today and would
   then fold twice.
2. **Pass a mode-resolver callable** into `SessionManager`/`HostSession` beside
   `log_command`, so the session can ask the host for its current standing mode at
   emit time (it must be read late, not captured, because `SuppressCommandOutput`
   mutates it).
3. **Give `HostSession` a host reference.** Simplest to write, worst for the
   layering — `session.py` already imports `host.py` at module scope, so it is not
   a cycle, but it widens a deliberately narrow constructor.

(1) looks right and is the smallest, but it changes the contract of a sink every
host verb already calls, so it wants its own gate run rather than a drive-by.

## Test shape when someone takes it

Two-sided, at the same seam, in one test: a host built with `log=LogMode.QUIET`,
a named session opened on it, one command run through the session and one through
`host.run` — assert BOTH are absent from a `HostFilter`-guarded console sink and
BOTH present on the unfiltered verbose sink. Today the session's line is on the
console and the host's is not, so the test is red before the fix and the positive
control is built in. The existing `_two_sided_sinks` helper in
`tests/unit/host/test_dry_run.py` is the topology to reuse.

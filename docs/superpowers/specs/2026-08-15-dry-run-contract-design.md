# otto `--dry-run` contract

**Status:** approved by Chris 2026-08-15 (brainstorm in session; decisions recorded here).
**Builds on:** the shipped dry-run work (`8b9a0be4` — host LogMode/announcements, link
and tunnel previews, both backstops), which conforms to this design and is not redone.

## The contract, one sentence

A dry run never runs a command on any device; by default it validates and stops at
the CLI seam, commands may opt in to a deeper configuration-only preview, and an
optional flag may permit *connections* (never commands) for reachability.

## Why (recorded so the rationale outlives the transcript)

The pre-`8b9a0be4` dry run answered every device command with a synthetic success
(`Status.Skipped`, `retcode=0`, `is_ok=True`, a banner string in `.value`) — a poison
pill shaped exactly like data. Every status branch passed it and every parser chewed
it: userland probes settled fabricated capabilities, `write_file -n` printed the
file's body, `link list -n` reported every link clean, `tunnel add -n` accused a host
of missing socat, and the mgmt/hop-transit self-lockout refusals silently vanished.
When logic branches on a device fact there are exactly three available behaviours —
fabricate it, decline loudly, or never run the logic — and "continue gracefully" is a
synonym for the first.

The governing concern (Chris): first- and third-party command authors will not keep
dry runs in mind when writing run-parse-branch logic, and **a consistently broken
complex feature is worse than a reliable, simple feature**. So the default must
require zero author effort and must be safe when the author never thought about dry
runs at all. Depth is bought deliberately, per command, by whoever wants it.

## 1. Default: validate and stop at the CLI seam

Under `--dry-run`, the CLI dispatch layer — not the command — does the following and
exits 0 **before the command body runs**:

- arguments parse and coerce (a typo'd `--mode 789` fails here, as today)
- the lab loads; every named host/link/tunnel reference resolves against it
- the command's module imports; for `otto test`, the suite imports and steps bind
- it prints what would run: command, target, echoed arguments

No body executes. A command author who does nothing gets this for free and cannot
get it wrong. `otto test -n` is the same rule, not a special case: the step list is
printed and no step body runs.

This is a REGISTRATION-LAYER concern because first- and third-party commands
register through the same mechanism (`@cli_exposed`); there is no way to treat
otto's own parsers specially, and there should not be — third-party authors get the
same capabilities, not a lesser tier.

## 2. Opt-in previews: `dry_run_preview=True` at registration

A command that wants `link impair -n`'s quality declares
`dry_run_preview=True` at registration (default `False`). A boolean, not a
string enum: there are exactly two behaviours — stop at the seam, or run the
body for a deeper preview — and a boolean cannot grow a third spelling or a
typo'd variant (`"preview"` vs `"Preview"`) that fails only at dispatch time.

With `dry_run_preview=True`, the body runs and short-circuits at the device
boundary with an **inline `is_dry_run()` branch** — the shape `link`/`tunnel`
already ship. The inline branch
was chosen over a separate preview callable because the preview IS the real path's
own configuration-only prefix (`find_link`, `_directions`, placement resolution);
separating it means duplicating that prefix, which is the mirrored-default drift
this codebase has been bitten by before. One authority, shared by construction.

A preview is three things, in the shipped vocabulary (`would:` / `not checked:` /
`not measured`):

1. **The plan** — concrete actions from configuration alone (host, netdev, the
   exact command line where config can produce it).
2. **The pure refusals** — checks needing no device still fire (bash-less
   `--expire`, local-link, mode parsing).
3. **The honest gaps** — what could not be checked without contact, named.

SUPPRESS THE PAYLOAD, NEVER THE ANNOUNCEMENT: a preview names actions and targets,
never carries file bodies or secrets, and a dry run whose output is empty is a bug
(a dry run with no product is useless; one with an invented product is dangerous).

`link` and `tunnel` conform by adding the registration flag; their bodies do not
change. `write_file`'s announcement is the same pattern at the library layer.

## 3. Reachability: `--dry-run --probe`

Opt-in flag on the dry run (not a separate verb — Chris's call). With `--probe`,
otto may OPEN TRANSPORTS to the hosts the command names and reports
reachable/latency per host. It still runs no command: **the flag permits a
connection, never a command.** A connection attempt produces no command result and
therefore feeds no logic — the fabrication hazard is untouched. Without the flag, a
dry run touches nothing whatsoever.

## 4. The primitive: the synthetic result stops being usable as data

`host.run` / `exec` / `put` / `get` under dry run still return a synthetic result
(gentler for library callers than raising at the call site), but it hardens:

- **Status is a new `Status.NotRun` member with `is_ok=False`.** `Status.Skipped`
  cannot flip: `Skipped.is_ok=True` is load-bearing for genuine skips (test steps,
  transfer folds) everywhere. `NotRun` is scoped to "a dry run declined this".
- **Reading `.value` raises** a dedicated error naming the command and the host, at
  the exact line that mistook a non-measurement for a measurement. The PARSE class —
  the always-wrong class — breaks loudly; fire-and-forget callers keep working.

`is_ok=False` is itself a deliberate break, not collateral: it is what stops
`BaseHost.reboot`'s `rebuild_connections()` firing on a reboot that never happened,
and what makes `exec_or_raise`-style callers fail loudly instead of proceeding on a
fiction. Anything that *believed* the fake success stops believing it.

The `[DRY RUN]` announcement machinery (LogMode fold at the emit seam, `write_file`'s
announcement) survives unchanged; only the returned object hardens. The link/tunnel
backstops (`LinkNotMeasuredError`, `TunnelNotMeasuredError`, the two `_device_*`
funnels) remain — belt and braces above the primitive.

## 5. Consequences and scope

**Falls out nearly for free:** the queued `BaseHost.reboot` holes
(`todo/dry-run-followups-2026-08-15.md` §1) — `is_ok=False` stops the transport
teardown; the hard-reboot power-cycle and the wait-phase dialing still need their
explicit guards (a PowerController spy test with both halves asserted).

**Known breaks, to state in the implementation plan:**

- Library callers under `dry_run` that branched on the old `is_ok=True` synthetic.
- Any test asserting the synthetic result's old shape (`Status.Skipped`,
  `retcode=0`, banner `.value`) — `tests/unit/host/test_dry_run.py` has several.
- CLI rendering of a default command's dry run moves from the command to the
  dispatch layer.

**Explicitly out of scope:** making suite step bodies previewable (they never run —
that is the feature); any per-subsystem retrofit beyond link/tunnel (the default
covers them); reachability beyond transport-open (health is not the question).

## Testing requirements

The discipline this workstream established applies:

- Every "no output"/"no contact" assertion carries a positive control in the same
  test against the same seam.
- Every new guard is shown red against a real defect; a mutation is scored red only
  on `rc == 1` plus named failing tests.
- The hostile condition is injected (`active_context(dry_run=True)` around real
  objects), not inherited.
- The `--probe` guard must assert both halves: transports opened with the flag,
  nothing opened without it.
- The `Status.NotRun` change needs a sweep of existing `Skipped`-as-ok consumers
  with each hit adjudicated (genuine skip vs dry-run synthetic), not a rename.

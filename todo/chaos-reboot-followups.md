# Chaos hardening Plan 6 — reboot pipeline follow-ups

Recorded during the final-review fix wave for Plan 6 (reboot hardening,
`src/otto/host/host.py`'s `BaseHost.reboot` + `UnixHost._confirm_recovered`).
Known gaps, deliberately out of this wave's scope rather than missed.

## 1. ✅ CLI-exposed reboot floats bypass `_validate_timeout`

Done: `reboot`'s bounds resolve through the `reboot_options` table (#979).

## 2. EmbeddedHost / console-server topologies need a topology-aware down probe

The down phase watches whatever `is_reachable` probes — for a
console-server-attached embedded target that's the console server, not the
target OS, so it may never observe "down" even though the target really
rebooted. Once Plans 4/5 bring real embedded reboot scenarios, either give
these hosts a topology-aware down probe or document `down_timeout=0`
guidance for them (the same skip Finding 3 added for `LocalHost`).

## 3. `_confirm_recovered`'s time budget is whatever's left over

`_confirm_recovered` only ever sees `deadline - (time spent in the down and
up waits)` — it has no floor of its own, so a slow down/up phase can leave
the recovery gate almost no time to retry a stalled shell. Worth deciding
whether it deserves a guaranteed minimum slice of `timeout` once real-host
(not scripted-probe) reboot scenarios exist to validate against (Plan 4).
The guest's shutdown-hold unit now makes this reproducible
(`tests/e2e/chaos/_bed.py::set_shutdown_hold`).

## 4. Prompt-then-freeze: one clean round-trip is not proof of a settled boot

Raised by Chris (2026-07-31, from field experience — not necessarily otto's
testbed hosts): some hosts get far enough to serve a shell prompt, answer a
command, and then freeze a few seconds later as the rest of the boot
sequence lands. That defeats `_confirm_recovered`'s current success
criterion — a single clean `exec("true")` round-trip — the same way
accept-then-stall defeated TCP-accept. No known-reliable detection
strategy yet. Candidate directions when this gets picked up: treat the
first success as a *candidate*, not a confirmation, and require N
consecutive successful probes spread over a settle window (costs latency on
every healthy reboot); or a family-tunable settle delay before the
confirming probe. Interacts with #3 (any settle window eats the leftover
budget). Real-host scenarios (Plan 4) are the right place to learn what
actually discriminates.

The guest's shutdown-hold unit also makes this reproducible: hold the shutdown,
then watch what a prompt-then-freeze boot does to the confirming probe.

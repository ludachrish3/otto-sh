# Chaos hardening Plan 6 — reboot pipeline follow-ups

Recorded during the final-review fix wave for Plan 6 (reboot hardening,
`src/otto/host/host.py`'s `BaseHost.reboot` + `UnixHost._confirm_recovered`).
Known gaps, deliberately out of this wave's scope rather than missed.

## 1. CLI-exposed reboot floats bypass `_validate_timeout`

`reboot`'s `timeout`, `down_timeout`, and `poll_interval` are all
`@cli_exposed` floats but none are run through `_validate_timeout`
(`src/otto/host/host.py:78`) the way `run`/`exec` args are. A negative
`poll_interval` busy-spins the wait loops; a NaN `timeout` makes the
`min(down_timeout, timeout)` clamp order-dependent (`min(x, nan)` vs
`min(nan, x)` differ). `timeout` itself already had this gap pre-Plan-6;
`down_timeout`/`poll_interval` are new with this plan. Any future validation
must respect the `down_timeout<=0` skip semantics from Finding 3 — a
blanket `>= 0` check would break the documented opt-out.

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

# NC Monitor Session Retirement

## Status
Deferred optimization. Picked up only if further nc-transfer startup shaving is needed after the warm-up and strategy-resolution work in `plans/` lands.

## Motivation

`FileTransfer._nc_monitor` (see [src/otto/host/transfer.py](../src/otto/host/transfer.py) — introduced in commit 072d843) is a dedicated shell session used to serialize nc control-plane ops (port scans, listener probes) so that concurrent callers share one handshake. It solves two real problems:

1. Concurrent control callers would otherwise each open a fresh exec-pool session (1–2 s telnet handshake each).
2. Control calls need a session that isn't tied up by a long-lived `nc -l` listener.

The cost: one extra telnet session handshake per host, on the critical path of the very first transfer (~1.5–2 s on telnet).

## Proposal

Replace the dedicated monitor session with a lock around `_exec_cmd` on the telnet control-path. Net structure:

- `FileTransfer._control_lock: asyncio.Lock` — serializes concurrent control callers so they share one pool session.
- `_control_run(cmd)` → `async with _control_lock: await _exec_cmd(cmd)` (telnet only; SSH keeps the direct path).
- Pool behavior already guarantees serial callers reuse one warm session; the lock just forces control callers to act serially.

Correctness guarantees preserved:
- Port reservation race already covered by `_port_lock` (same commit).
- Listener-check serialization covered by the new control lock.

Handshake accounting:
- Today: `1 monitor + N listener-pool` sessions per host.
- After: `max(control, listener-pool)` — control either reuses an idle pool session (free) or opens a new one that subsequently serves listeners too (no net extra handshake).

## Interaction with warm-up (prerequisite plan)

The warm-up plan opens the monitor and `N` pool sessions concurrently, which already collapses monitor cost into the `max()` of parallel handshakes. If measurements show warm-up got us under the ~2 s target, this refactor is strictly optional. Pick it up if:
- Lab sizes grow such that per-host warm-up overhead matters more.
- A future change needs the control-plane to share a session with a nc listener (e.g. for inline ready-signaling via stdout).

## Files likely touched

- [src/otto/host/transfer.py](../src/otto/host/transfer.py) — replace `_nc_monitor` / `_nc_monitor_lock` with `_control_lock`; remove `_open_session('_nc_monitor')` call in `_control_run`.
- [tests/unit/host/test_transfer_nc_put.py](../tests/unit/host/test_transfer_nc_put.py) — retire any tests that specifically assert the monitor session's existence; add coverage for the lock-around-pool path.

## Verification

- `tests/unit` must stay green; the ghosting-bursting and sequential-transfer regression tests are the correctness bar.
- Benchmark first-transfer startup before/after on a representative telnet host: expect -1.5 s on the critical path when warm-up is already in place and monitor is then removed.

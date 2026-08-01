# Chaos hardening Plan 3 — real-signal integration follow-ups

Recorded during the final-review fix wave for Plan 3 (real-signal
integration, `tests/integration/chaos/`). Known gaps, deliberately out of
this wave's scope rather than missed.

## 1. Monitor startup-failure × signal race can lose the interrupt exit code

`MonitorServer.serve()` (`src/otto/monitor/server.py`) polls `server.started`
in a `while` loop with `await asyncio.sleep(0.05)` between checks, and only
calls `task.result()` — which re-raises `run_uvicorn()`'s translated
`RuntimeError` — on the iteration where it notices `task.done()`. A signal
arriving inside that ~50ms window, on an already-failing startup (uvicorn
died binding the port, a bad TLS cert, etc.), races otto's lifecycle signal
handling against the startup-failure path: depending on which one the event
loop schedules first, the process can exit either on the interrupt's policy
(banner + 130/143) or on the startup `RuntimeError`, and it isn't obviously
guaranteed which exit code wins when both fire in the same window. Nothing
in this plan exercises "signal arrives during a failing startup" — every
chaos scenario signals a server that already reported `started`. Worth a
dedicated test once someone wants to pin the tie-break behavior explicitly
(likely by injecting a startup failure, e.g. a pre-bound port, then racing a
signal against it).

## 2. `test_monitor_e2e.py`'s (0, -2, 130) SIGINT tolerance predates the ownership fix

`test_monitor_collects_and_persists`'s exit-code assertion (~line 285:
`assert result.returncode in (0, _sigint_negative, _sigint_shell)`) accepts
three outcomes because, pre-Plan-3, uvicorn's own `capture_signals`
could race the exit code the way `test_signal_monitor.py`'s new docstring
describes ("a racy exit code from the post-drain signal re-raise"). Now
that Task 2's ownership fix makes otto's lifecycle own SIGINT/SIGTERM for
the whole serve window, SIGINT during `otto monitor --live` should exit
130 deterministically — the same tri-state tolerance is no longer
necessary and is strictly weaker than it needs to be (a test that accepts
three outcomes catches fewer regressions than one that pins the single
correct one). Left alone in this wave because tightening it needs a real
bed run (this is a `monitor_host` e2e test, not a hermetic loopback one) to
confirm 130 is now the *only* outcome across enough repetitions to trust
removing the other two branches.

## 3. Loopback sshd orphan if the pytest worker is SIGKILLed

`LoopbackSshd` (`tests/integration/chaos/_sshd.py`), started once per
session by `chaos_target` in `conftest.py`, is torn down by a `finally:
sshd.stop()` around the fixture's `yield` — normal teardown, including a
test failure, always reaches it. But if the pytest *worker process itself*
is killed with SIGKILL (a CI job hard-timeout, an OOM-kill, `pytest-xdist`
worker crash), that `finally` never runs. `spawn_otto`'s
`start_new_session=True` only groups the *otto subprocess's* descendants
for `assert_no_process_group()` — the sshd is a direct child of the pytest
worker, not of otto, so a worker-level SIGKILL orphans it. Low severity in
practice: it's a throwaway, user-owned, unprivileged daemon bound to an
ephemeral port under `tmp_path_factory`'s tmp dir, so it costs a stray
process and an open port, not a security or correctness problem — but
worth a documented cleanup story (e.g. reap stale sshd pidfiles left in
`/tmp` on the next run) before this fixture pattern gets copied into more
suites.

## 4. Bed mode (`OTTO_CHAOS_BED_HOST`) is an untested prototype

`chaos_target`'s bed-host branch (`bed_host_override()` /
`make_bed_target()` in `tests/integration/chaos/_target.py`) exists so the
whole tier-2 suite can, in principle, run against a real leased lab host
instead of the hermetic loopback sshd — but no run in Plan 3 ever actually
exercised it end to end, and it does no reservation lease at all: it just
connects to whatever `OTTO_CHAOS_BED_HOST` names. The spec's "leased bed
host" language implies otto's own reservation system should hold the host
for the run's duration; today nothing stops a concurrent lab user (or a
second chaos run) from colliding with it. Treat `OTTO_CHAOS_BED_HOST` as an
unverified escape hatch, not a supported mode: before relying on it, (a)
actually run the suite against a real bed host at least once, and (b) wire
an acquire/release around the session using otto's existing reservation
backend.

## 5. Graceful-teardown remote reaping is incidental PTY-HUP, not a sweep

`test_signal_run.py`'s remote-hygiene assertions (`_wait_remote_reaped`)
pass today because the SSH session's PTY hangs up when otto tears down its
session, and the remote shell's job-control kills the foreground child on
that HUP — that's PTY semantics, not otto reaping anything itself. A
`nohup`'d or explicitly backgrounded remote command (`cmd & disown`,
`setsid cmd`) would survive a graceful teardown completely untouched, since
nothing sends it a signal. That's fine for what Plan 3 set out to prove
(the *local* process's signal contract, end to end) but it means "remote
command survives teardown" is not a guarantee otto currently makes or
tests — real remote-process lifecycle material for Plan 4, not a side
effect to lean on here.

## 6. Third-signal window remains untested by design

Already documented in `tests/integration/chaos/_driver.py`'s module
docstring: once `_main` removes its signal handlers, a third signal
delivered during `asyncio.run`'s finalization surfaces as a bare
`KeyboardInterrupt` (SIGINT) or SIG_DFL kill (SIGTERM) — a timing window no
phase marker can reliably gate a test on, so nothing in this suite attempts
it. Recorded here for completeness, not as a miss: it's a known, deliberate
non-goal.

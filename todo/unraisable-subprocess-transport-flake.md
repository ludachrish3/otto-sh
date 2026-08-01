# Flake: unraisable subprocess-transport ExceptionGroup misattributed to a MockSession test

Seen once, 2026-07-31, first `make coverage` on merged main `798c6b1f`
(chaos Plan 6). Not reproduced on rerun; scoped class run green; identical
tree fully green in a worktree the same hour. Load-sensitive.

## Symptom

`tests/unit/host/test_session.py::TestSessionDeath::test_eof_during_run_cmd_returns_error`
ERRORs **on setup** with
`exceptiongroup.ExceptionGroup: multiple unraisable exception warnings (3 sub-exceptions)`:

1. `ResourceWarning: unclosed transport <_UnixReadPipeTransport fd=16 open>`
2. `ResourceWarning: unclosed file <_io.FileIO name=16 mode='rb'>`
3. `ResourceWarning: unclosed transport <_UnixSubprocessTransport pid=... returncode=-15 stdout=<_UnixReadPipeTransport fd=16 open>>`

All three are one leak: an asyncio subprocess that died by SIGTERM
(`returncode=-15`) whose transport was garbage-collected without
`transport.close()`, plus its stdout pipe and the pipe's FileIO.

## Why the attributed test is innocent

The named test uses a `MockSession` and spawns nothing. pytest's
unraisable hook collects warnings at the *next* test's setup on that xdist
worker, so attribution drifts (same class as the 2026-07-21 `make release`
unraisable leak — see that fix for the pattern). The real leaker is an
earlier test on the same worker that spawns a **real** subprocess and lets
it be SIGTERM'd without closing/awaiting the transport.

## Hunt strategy (when it recurs or someone picks it up)

- Candidates: tests that launch real subprocesses — `launch_command` /
  systemd-run/socat paths, suite `run` e2e, CLI subprocess e2e, daemon
  toolkit tests. `returncode=-15` means someone `terminate()`d it.
- Reproduce single-process (`-p xdist off` / no `-n auto`) with
  `-W error::ResourceWarning` over candidate modules — the `-n auto`
  scatter is what makes attribution useless (same lesson as nightly
  isolation #108: cross-module leak guards must be single-process).
- `PYTHONTRACEMALLOC=25` turns the "Enable tracemalloc" hint into a real
  allocation traceback naming the spawner.
- Fix at the source: close/await the transport (or use the process
  context manager) in the leaking test/fixture — never broaden warning
  filters.

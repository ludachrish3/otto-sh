# Chaos hardening: interrupt handling, graceful teardown, and the chaos test harness

**Date:** 2026-07-30
**Status:** Approved design, awaiting implementation plans

## Problem

Early termination of an otto process — Ctrl+C, `kill`, a crashed terminal, a CI
timeout — can strand resources on remote hosts: live SSH sessions, half-built
tunnels, orphaned `nc` listeners, docker compose stacks, staging directories,
netem qdiscs, and (after `otto host login`) a local terminal stuck in raw mode.
A codebase sweep confirmed the exposure is structural, not incidental:

1. **No SIGTERM handling anywhere.** `entry()` runs `app()` bare
   (`src/otto/cli/main.py:693`). `kill <pid>` performs zero teardown.
2. **Ctrl+C teardown is single-shot and unshielded.** `HostScope.__aexit__`
   (`src/otto/context.py:70-78`) is a plain `gather`; a second Ctrl+C during
   cleanup aborts the cleanup. Nothing in the product uses `asyncio.shield`.
3. **Sync command paths bypass the host scope.** Only `async_typer_command`
   enters `ctx.scope`. `cli/cov.py`, `cli/monitor.py`, `suite/run.py`, and
   `config/repo.py` call `asyncio.run` directly, so hosts they open are never
   swept.
4. **Close chains are unguarded sequences.** In `ConnectionManager.close`
   (`src/otto/host/connections.py:466-520`) one raising step (e.g. `ftp.quit()`
   on a dead socket) skips every later step, including the SSH-hop teardown.
   `UnixHost.close` has the same shape.
5. **`HostScope` closes hosts concurrently with no ordering**, contradicting
   `DockerContainerHost.close`'s documented requirement to close before its
   parent (`src/otto/host/docker_host.py:622-630`).
6. **Compensating actions are interruptible.** The `add_tunnel` rollback
   (`src/otto/tunnel/manage.py:476-478`), `impair_link` rollback, `as_user`
   undo commands (`src/otto/host/privilege.py:154-166`), and nc listener
   reaping are all awaits that a Ctrl+C can tear mid-flight.
7. **`reset_context` is never called from the CLI** — the token from
   `set_context` is discarded (`src/otto/cli/invoke.py:386`).
8. **No test sends SIGINT/SIGTERM to a real otto process.** The only
   cancellation coverage is two live-bed integration tests.

Existing machinery to build on: the asyncio transport leak detector
(`tests/_fixtures/_transport_leaks.py`), the orphaned-loop reaper
(`tests/_fixtures/_loop_reaper.py`), FD-count brackets
(`tests/e2e/tunnel_stability/conftest.py`), and remote-side leftover checks in
the tunnel and link e2e suites.

## Decisions

- **Sequencing: harden first, then chaos.** The known structural gaps are
  fixed with targeted regression tests before the chaos harness hunts
  unknowns. Chaos tests written against known-broken code would only
  rediscover this list.
- **Docker: opt-in chaos lane with real docker.** The standing rule (docker
  only in 1-2 old-OS e2e tests; monitor e2e docker-free) continues to apply to
  the default lanes. The new chaos lane is excluded from default gates (like
  `tests/e2e/tunnel_stability/`) and may use docker freely. In GitHub CI the
  docker daemon is the runner's own, reached over loopback sshd (see CI
  wiring); real remote docker hosts exist only on the lab bed.
- **Ctrl+C policy: shield teardown, second signal forces.** First
  SIGINT/SIGTERM cancels the work and runs shielded cleanup with a status
  line; a second abandons graceful teardown, hard-closes transports, restores
  the terminal, and exits. Teardown is bounded by a deadline either way;
  deadline expiry behaves like the second signal.
- **Chaos scope: all seven surfaces.** CLI interrupt handling, Host/session
  teardown, docker containers, tunnel/link rollback, transfer paths,
  interactive login/terminal state, privilege/elevation unwinding.
- **Harness architecture: layered determinism.** Deterministic cancellation
  sweeps at unit tier; phase-marker-triggered real signals at integration
  tier; seeded randomized injection only in the opt-in chaos lane. Randomized
  timing never appears outside tier 3, and every tier-3 failure reproduces
  from its printed seed.

Python floor is 3.10: no `asyncio.Runner` or `asyncio.timeout`. The design
uses `asyncio.run`, `loop.add_signal_handler`, and `asyncio.wait_for`.

## Phase 1 — Hardening (product changes)

### Canonical lifecycle entry: `run_command`

A new `run_command(coro)` becomes the only blessed way a CLI command runs its
async body, replacing every bare `asyncio.run` in command paths (`cli/cov.py`,
`cli/monitor.py`, `suite/run.py`, `config/repo.py`) and used internally by
`async_typer_command`. Responsibilities:

1. **Scope entry.** When an `OttoContext` is active, the command body runs
   inside `async with ctx.scope`. This fixes the sync-command bypass: hosts
   opened by cov/monitor/suite paths get swept.
2. **Signal handlers.** `loop.add_signal_handler` for SIGINT and SIGTERM,
   installed inside the running loop. Each command owns its own loop, so
   per-loop installation is the natural fit; no process-global state. Both
   signals route to the same cancellation path. Exit codes: 130 (SIGINT),
   143 (SIGTERM).
3. **Two-stage policy.** First signal: cancel the main task, print
   `cleaning up remote sessions… (Ctrl+C again to abandon)`, run teardown
   under `asyncio.shield` with a deadline (default 10 s, settings-
   overridable). Second signal or deadline expiry: abandon the shielded wait,
   hard-close transports, restore terminal state, exit.
4. **Context hygiene.** `reset_context` runs in the wrapper's `finally`.

Behavior is identical across TTY and non-TTY except status-line printing.

### Teardown chain robustness

- `ConnectionManager.close`: per-step guards (log-and-continue) so no single
  raising step can skip the steps behind it. Ordering is preserved
  (sftp → ssh → ftp → telnet → hop).
- `UnixHost.close`: try/finally so `_connections.close()` runs even when
  `close_all()` raises.
- `HostScope.__aexit__`: close in dependency rank order — hosts with a parent
  (e.g. `DockerContainerHost`) first, then parents — `gather` with
  `return_exceptions=True` within each rank. This honors the documented
  close-before-parent requirement.

### Shielded compensating actions

A helper `await compensate(coro, deadline)` wraps every rollback/undo path so
an interrupt mid-compensation cannot tear it. Call sites: `add_tunnel`
rollback, `impair_link` rollback, `as_user` undo commands, nc listener
reaping. Deadlines take an injectable clock/timeout so unit tests trigger
expiry deterministically — count work, not time.

### Terminal restore on the force path

The force-exit path runs the same termios restore + SIGWINCH uninstall that
`interact.py`'s `finally` performs, so an abandoned teardown never strands a
raw-mode terminal. While in raw mode during `login`, the terminal does not
generate SIGINT (^C forwards to the remote as bytes, as today); SIGTERM is the
interrupt that matters there.

### Non-goals

- Tunnel/link daemon persistence is by design (`systemd-run`/`setsid`
  survivors are the tunnel record) and is not changed.
- Kernel modules stay loaded across otto exit, by design.
- Reservations: otto is a consumer-only client; nothing to release.
- No `atexit` safety net: per-loop handlers cover everything short of
  SIGKILL, and SIGKILL behavior is characterized by the chaos lane, not
  survived by the product.

## Phase 2 — Chaos harness (three tiers)

### Tier 1: cancellation sweeps (unit, default gate)

A helper in `tests/_fixtures/chaos.py`:

```python
await sweep_cancellation(scenario_factory, oracle)
```

Runs the scenario once against instrumented fakes to count await points (N),
then N more times, injecting `CancelledError` (and, as a variant, a
connection-dropped exception) at point 1, 2, … N. After every run the oracle
asserts invariants: every must-run step that follows the injection point
still executed (or the chain failed loudly rather than silently skipping);
the compensating action ran exactly once. The fake layer extends the stub-host
family from `tests/unit/test_context.py` with programmable failure points:
raise-on-nth-call, and hang-as-never-resolving-future for the deadline path
(expiry driven by the injectable clock — no wall-clock waits).

Swept chains: `ConnectionManager.close`, `UnixHost.close`,
`HostScope.__aexit__` ordering, `run_command`'s two-stage state machine, and
all four `compensate()` call sites.

### Tier 2: real-signal integration tests (sequential)

New `tests/integration/chaos/`. Each test spawns a real `otto` subprocess
(via the existing PTY driver, `tests/e2e/host/_pty_driver.py`, where terminal
state matters), pointed at one SSH-reachable host, `--output-dir` into
`tmp_path`. The host comes from a **loopback-or-bed fixture**: on the lab, a
leased bed host; in GitHub CI, the runner itself behind loopback sshd (start
`sshd`, inject a key, connect to `127.0.0.1` as an ordinary `UnixHost`).
Real asyncssh either way — the harness does not care which.
Determinism comes from **phase markers, not timing**: the parent tails the
file log sink until a known line appears ("session opened", "transfer
started", "teardown started"), then delivers SIGINT or SIGTERM at exactly that
phase. Assertions: exit code (130/143), termios restored, no orphaned local
children, local FD bracket, and remote hygiene via an independent second SSH
connection. Signal-during-teardown and double-signal (force path) cases live
here. Sequential, one host — no bed hammering.

### Tier 3: chaos lane (opt-in, `nox -s chaos`, real docker)

New `tests/e2e/chaos/`, excluded from default gates exactly like
`tunnel_stability`. Scenarios split by venue:

- **Bed scenarios** (link impairment of SSH, tunnel/link rollback,
  multi-host) run only on the lab — GitHub CI has no route to the bed.
- **Docker scenarios** run wherever an SSH-reachable docker daemon exists.
  `DockerContainerHost` requires an SSH-based `UnixHost` parent
  (`src/otto/host/docker_host.py:152`), so the local daemon cannot be driven
  through `LocalHost`; the loopback-sshd fixture satisfies the requirement on
  a GitHub runner (`ubuntu-latest` ships both dockerd and openssh-server,
  with passwordless sudo for the daemon-restart scenario). Full fidelity —
  real asyncssh, real docker exec channel — with the daemon local to the
  runner.

Two ingredients:

- **`BedHygiene` oracle.** Consolidates today's piecemeal checks (tunnel
  leftovers, impair timers, FD brackets, shell history) into one fixture:
  snapshot per-host state before a scenario, diff after, fail naming the
  leftovers. Remote probe over a fresh connection: otto-tagged processes
  (`nc`, `socat`, `otto-impair` timers), `tc qdisc` listing,
  `docker ps` / `compose ls`, `/tmp/otto-*` staging dirs, sshd session count,
  shell history delta. Local: transport leak detector, loop reaper, child
  process table, termios. Pre-existing dirt is snapshotted out, as the tunnel
  e2e already does.
- **Seeded randomized injection.** Within a phase window (between two
  markers), the injection offset is chosen by a printed seed; nightly runs
  several seeds; a failure reproduces with the same seed. SIGKILL scenarios
  live only here: the product cannot survive SIGKILL, so the tests
  characterize what leaks and assert that the recovery commands
  (`otto tunnel remove --all`, `repair-link`) clean the bed.

### Scenario catalog (tier 3, per surface)

- **CLI:** SIGINT/SIGTERM at every phase marker of representative commands
  (`run`, `put`/`get`, `tunnel add`/`remove`, `link impair`/`repair`, docker
  flows, `host login` via PTY); double-signal force path; signal delivered
  during teardown; SIGKILL characterization + recovery assertions.
- **Host/session:** connection drop mid-command, injected with otto's own
  port-scoped link impairment (blackhole the SSH port); remote command that
  ignores SIGINT (session wedge → deadline path); slow-closing remote
  (teardown deadline expiry).
- **Docker:** `docker kill` mid-exec; `docker pause` (wedged exec channel →
  deadline); daemon restart under an open session; pile-up test — N
  interrupted `composed()` flows in a row, assert zero stack/container
  accumulation and no staging-dir growth; exec-that-never-returns inside a
  container + interrupt.
- **Tunnel/link:** interrupt between daemon launch and success return;
  interrupt during the rollback itself (proves the `compensate()` shield
  live); leftover-timer/qdisc assertions via BedHygiene.
- **Transfer:** interrupt mid-stream in both directions — no orphaned `nc`
  listeners, no half-written destination file (or a documented partial-file
  policy), SFTP channel accounting.
- **Privilege:** interrupt inside `as_user` — the next command on the same
  session sees the original user.
- **Login/terminal:** SIGTERM during `login`; termios restored, SIGWINCH
  handlers gone, stdin reader thread joined.

## CI wiring

GitHub CI is entirely `ubuntu-latest` and hostless today; the bed lanes
(`stability-unix`, `stability-tunnel`, docker e2e) are lab-run Makefile legs.
The chaos lanes follow the same split:

- **Default gate** (`make coverage`, ci.yml): tier 1 rides along
  automatically — plain unit tests, no bed, no docker.
- **GitHub nightly** (nightly.yml): tier 2 and the tier-3 docker scenarios,
  both against the loopback-sshd runner host. This gives interrupt and
  docker-chaos coverage on every nightly without any bed.
- **Lab chaos leg:** `make chaos` → `nox -s chaos` runs the full tier-3
  catalog including bed scenarios, mirroring the `stability-unix` /
  `stability-tunnel` pattern — run from the dev VM, never from GitHub.
  Bed-hostile discipline applies: chaos scenarios that impair SSH must never
  co-locate with other bed users.

## Decomposition: five ordered plans

1. **Lifecycle core** — `run_command`, two-stage signal policy, scope entry
   for sync commands, `reset_context` fix; tier-1 sweeps for its state
   machine. Proven-red first: the sweep demonstrating today's unshielded
   second-Ctrl+C.
2. **Teardown robustness** — guarded close chains, ranked `HostScope`,
   `compensate()` + its four call sites; tier-1 sweeps per chain.
3. **Real-signal integration** — phase-marker harness, the loopback-or-bed
   host fixture, + `tests/integration/chaos/` for the CLI/terminal
   scenarios; GitHub nightly job for the loopback slice.
4. **Chaos lane foundations** — `nox -s chaos` + `make chaos`, BedHygiene
   oracle, seeded injection, host/session + tunnel/link + transfer
   scenarios (lab leg).
5. **Docker + extended surfaces** — docker scenario set (loopback-sshd in
   GitHub nightly + bed hosts on the lab leg), privilege, login/terminal
   chaos.

Plans 3–5 assert behavior that only exists after plans 1–2 land; the order is
load-bearing.

## Amendment (2026-07-31): reboot — the eighth surface

Approved with chaos plan 6 (`docs/superpowers/plans/2026-07-31-chaos-plan6-reboot-hardening.md`).
Remote reboot is the remote-side SIGKILL: daemons (tunnels), qdiscs and
transient timers (link impairment), and cached transports do not survive it,
so scenarios characterize what is lost and assert the recovery commands
reconcile cleanly — the spec's existing SIGKILL pattern, applied to the
remote end.

- **Hardening first (plan 6):** `reboot(wait=True)` becomes truthful —
  stale connection state dropped at issue time (probes previously read
  `ConnectionManager`'s cached connection and vacuously succeeded), a
  two-phase down-then-up wait (`DEFAULT_REBOOT_DOWN_TIMEOUT = 60.0`; a host
  that never goes down means the reboot didn't take), and liveness-gated
  recovery (`_confirm_recovered`: early-boot sshd can accept a TCP
  connection then stall, so UnixHost recovery means a clean `exec("true")`
  round-trip, not a completed connect). Deterministic tier-1 tests cover
  the probe-through-cache, up-before-down, and accept-then-stall pitfalls.
- **Scenario catalog additions:** bed scenarios (plan 4's set) — happy-path
  `reboot(wait=True)` on the leased host; reboot at phase markers
  mid-command and mid-transfer; reboot × tunnel (half-chain discovery +
  `tunnel remove --all` reaps survivors on the peers); reboot × link
  (rebooted endpoint clean, peer's qdiscs remain, BedHygiene names them,
  `repair-link` idempotent against half-clean state). Docker analog
  (plan 5's set): `docker restart` of a container host mid-exec/mid-session
  — the CI-viable reboot stand-in; the runner itself cannot reboot.
- **Venue rule:** real reboots are bed-only, leased bed hosts exclusively;
  the docker-restart analog is the only reboot-shaped scenario GitHub CI
  runs.

## Success criteria

- A first Ctrl+C or SIGTERM at any phase of any command leaves zero otto
  resources on remote hosts (tunnel/link daemons excepted, by design) and a
  restored terminal, within the teardown deadline.
- A second signal exits promptly (< 1s beyond transport aborts) and still
  restores the terminal.
- The tier-1 sweep suite fails if any future edit reintroduces an unguarded
  step into a swept teardown chain.
- The chaos lane runs green nightly, and BedHygiene reports name specific
  leftovers on failure rather than generic "leak detected".

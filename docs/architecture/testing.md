# Test strategy

otto's job is to drive real, sometimes destructible, infrastructure — Vagrant
hosts over SSH/Telnet, an embedded target behind a QEMU serial console, real
docker daemons, a live terminal in raw mode. Its own regression suite
inherits that shape: some behavior is checkable with nothing but Python,
some behavior only exists once a real host answers, and a slice of behavior
*is* what happens when that infrastructure is killed, blackholed, or
rebooted mid-command. The taxonomy below exists to keep the first kind fast,
free, and automatic; make the second kind an explicit, provisioned
opt-in; and keep the third kind — tests that are deliberately destructive —
legible and impossible to run by accident.

This page is the *why* and the *decision procedure*: what each kind of test
is for, and how to pick one when you're adding code. For the mechanical
"how do I invoke this" — make targets, VM requirements, `nox` sessions — see
{doc}`../contributing`'s "Running tests" section, which this page links to
rather than repeats.

## Two axes: level and resource

Every test carries two independent classifications.

**Level** is *where the test file lives*, and it is cumulative:
`tests/unit/` ⊆ `tests/integration/` ⊆ `tests/e2e/`. Each directory is
stamped with its own marker automatically, by a `pytest_collection_modifyitems`
hook in that directory's `conftest.py` — no test writes the marker itself:

- `tests/integration/conftest.py` adds `integration` to every item under
  `tests/integration/` (idempotent, additive — an explicit `embedded` or
  `hops` on the same test survives untouched).
- `tests/e2e/conftest.py` mirrors this for `e2e`, and goes one step further:
  it *enforces* that every e2e test carries exactly one primary resource
  marker from `{hostless, integration, embedded}` (with `hops` allowed only
  as an additive refinement of `integration`). An e2e test with zero or two
  of those markers fails collection — nothing can slip into the wrong gate
  by omission.
- `tests/unit/` needs no stamp: level and "no resource" already coincide
  there, and a guard (below) keeps it that way.

Because the levels are cumulative, the gates that select by level compose
the same way: `make coverage-integration` runs `tests/unit` *and*
`tests/integration`; `make coverage` (no path filter) runs everything
pytest's `testpaths` covers.

**Resource** is *what infrastructure the test needs*, independent of which
directory it sits in, and it is selected by marker rather than path. The
full list, from `pyproject.toml`:

| Marker | Meaning |
| --- | --- |
| `integration` | Requires running Vagrant VMs |
| `embedded` | Requires the Zephyr QEMU instance under the zephyr Vagrant VM |
| `hops` | Multi-hop integration test requiring 3 Vagrant VMs; additive on top of `integration` |
| `hostless` | An e2e test that needs no test bed at all (no VMs, no docker daemon, no Zephyr) |
| `browser` | Needs a Playwright browser binary (`make browsers`) |
| `concurrency` | Fast, no-VM `SessionManager` concurrency/soak test; stays in the default coverage gate |
| `stability` | Heavy soak/churn test; bed-hostile, excluded from `make coverage` |
| `chaos` | Tier-3 chaos-lane scenario; bed-hostile like `stability`, excluded from every default gate |
| `soak` | The heavy browser-driven replay stress test; chromium-only, excluded even from `make dashboard`'s default selection |
| `busybox` | Needs a real BusyBox artifact (fetched, SHA-256-verified and cached; qemu-user-static on non-x86_64); excluded from every default lane, opt-in via `make busybox` |
| `conformance` | A host-contract conformance cell. In the default hermetic venue it stands up a throwaway loopback `sshd` and runs real BusyBox artifacts as subprocesses; under `OTTO_CONFORMANCE_BED=1` the same tests resolve against real bed hosts instead. Excluded from every default lane; opt-in via `make conformance` (hermetic) or `make conformance-bed` (real bed, dev VM only) — see below |
| `conformance_bed` | Carried *in addition* to `conformance` by the few tests in that tree which reach the real lab bed on **every** run, whichever venue is selected — today, the bed openers' witness. `make conformance` subtracts it, because that lane is hermetic and runs nightly in CI with no lab |
| `no_bundle_page` / `no_hygiene_bracket` | Narrow opt-outs for two specific autouse guards (a browser test that renders no bundle; a chaos test that manages its own hygiene bracket) |

Reading any test, then, is two lookups: which directory is it under (its
realism level), and which markers does it declare or inherit (what it
actually needs to run). Together those two answers are exactly what every
selection expression below is built from.

## Deciding what to write

Given a change, here is how to place the test that proves it:

| You're changing / proving | Put it in | Resource marker | Runs via |
| --- | --- | --- | --- |
| A pure function, parser, or model — no host, no I/O | `tests/unit/` | none | every gate, including `make coverage-hostless` |
| Behavior against a real Unix host over SSH/Telnet | `tests/integration/` | (auto-stamped `integration`) | `make coverage` / `make coverage-integration` / `nox -s tests_integration` |
| Specifically 3-hop routing | `tests/integration/` or `tests/e2e/` | `hops` (with `integration`) | `make coverage-unix` / `pytest -m "hops and not stability"` |
| Zephyr/embedded target behavior | wherever the level fits | `embedded` | `make coverage-embedded` / `nox -s tests_embedded` |
| Full CLI/API behavior, but exercised against fakes/harnesses — no real bed | `tests/e2e/` | `hostless` | rides `make coverage-hostless`, the no-testbed CI gate |
| Drives the real dashboard or covapp SPA in a browser | `tests/e2e/monitor/dashboard` or `tests/e2e/cov/report_browser` | `browser` | `make dashboard` (chromium; feeds `make coverage`) |
| A promise that must hold for EVERY host, across every terminal and transfer backend that reaches it | `tests/conformance/` | `conformance` | opt-in only — `make conformance` (hermetic, also the nightly CI job) / `make conformance-bed` (real bed, dev VM only) |
| A teardown/cancellation invariant, provable against fakes with no I/O | `tests/unit/` | none — use `sweep_cancellation()` | rides the default gate automatically (chaos tier 1) |
| A real SIGINT/SIGTERM reaching a real otto subprocess at an exact phase | `tests/integration/chaos/` | (auto-stamped `integration`) | rides `make coverage` / `nox -s tests_integration`; also runs standalone in GitHub nightly (chaos tier 2) |
| A destructive or randomized scenario against a live, leased bed (docker kill, reboot, SIGKILL, link blackhole) | `tests/e2e/chaos/` | `chaos` + `stability` | opt-in only — `make chaos` / `make chaos-embedded` (chaos tier 3) |
| A heavy soak/churn check that must own the bed exclusively | existing directory | `stability` | opt-in only — `make stability*` |

The last three rows are worth their own section, because "chaos" is not one
lane but three, sized to how much determinism each kind of proof can afford.

## Special lanes, and why they are opt-in

A few markers exist specifically to keep expensive or bed-hostile tests out
of the tests every contributor runs on every push:

- **`stability`** marks soak and churn tests that hammer real infrastructure
  for minutes at a time, and some of them are actively hostile to a
  co-located test: the SIGSTOP-wedge test in `stability-tunnel`, for
  instance, stops a peer host's `sshd` for tens of seconds, which would time
  out any other worker's concurrent SSH to that same host. `stability` tests
  therefore only ever run in the dedicated `make stability` family
  (`stability-unit`, `stability-unix`, `stability-tunnel`,
  `stability-embedded`), never mixed into a general run — with one
  deliberate exception: `make repeat`'s selection is `-m "not browser and
  not chaos"`, which excludes `browser` and `chaos` but says nothing about
  `stability`, so a `make repeat` soak co-selects `stability`-marked tests
  alongside everything else.
- **`chaos`** is the tier-3 opt-in lane discussed below: interrupts,
  SIGKILLs, and reboots delivered on purpose. Excluded from every default
  gate exactly like `stability`, for the same bed-exclusivity reason, and
  run only via `make chaos` / `make chaos-embedded`.
- **`browser`** tests default to Chromium only. `make dashboard` (which
  feeds `make coverage`) runs one engine; the full Chromium + Firefox +
  WebKit matrix only runs under `nox -s dashboard` (parametrized, one job
  per engine in CI) or `make dashboard-all`. The `soak` marker carves out
  one further exception inside `browser` itself: the SSE replay stress test
  is minutes long and Chromium-only (WebKit's main thread cannot answer a
  DOM read fast enough under that load), so it is excluded even from
  `make dashboard`'s default selection and only runs via
  `make dashboard-soak`.
- **`concurrency`** looks like `stability` — it is a soak test — but it
  needs no VM and is not bed-hostile, so unlike `stability` it stays inside
  the default `make coverage` gate; `make stability-unit` simply re-runs it
  under repetition and a leak detector.
- **`hops`** is a pure refinement: it never appears alone, only alongside
  `integration`, and it says "this specifically needs all three Unix VMs
  wired together," not "this needs a VM" (which `integration` already says).

## The chaos-testing architecture

Chaos testing is the deliberate half of the suite: tests whose whole point
is to interrupt, kill, or disconnect otto mid-operation and check that
nothing is left behind. It is built on one governing decision — **layered
determinism** — and split into three tiers by how much realism each layer
can afford while staying deterministic.

```{graphviz}
digraph chaos_tiers {
    rankdir=LR;
    node [shape=box];

    tier1 [label="Tier 1 — cancellation sweeps\ntests/unit\nfakes + sweep_cancellation()\nrides the default gate"];
    tier2 [label="Tier 2 — real signals\ntests/integration/chaos\nreal otto subprocess,\nphase-marker triggered"];
    tier3 [label="Tier 3 — chaos lane\ntests/e2e/chaos\nBedHygiene + seeded injection\nopt-in: make chaos"];
    nightly [label="GitHub nightly\nchaos-tier2 + chaos-docker jobs\n(loopback sshd, no bed)", shape=note, style=dashed];

    tier1 -> tier2 -> tier3;
    tier2 -> nightly [style=dashed];
    tier3 -> nightly [style=dashed, label=" docker slice only"];
}
```

**Tier 1 — cancellation sweeps (unit, default gate).** `tests/_fixtures/chaos.py`
provides `sweep_cancellation()`: it runs a scenario once against instrumented
fakes to count its await points (`ChaosPoints.point()` / `.sync_point()`),
then re-runs it once per applicable point/fault pair, arming exactly one
point each time. The table holds five exception-shaped faults: cancellation,
the transport dying in two shapes that are deliberately not interchangeable
(`ConnectionDropped`, a bare `Exception`, and `ConnectionResetError`, an
`OSError`), otto's own `WaitTimeoutError`, and `HostCommandError` for a
command that ran and failed. Not every fault is meaningful at every await, so
each entry declares the surfaces where it applies and each checkpoint
declares its surface (`Surface.NETWORK` for a transport teardown, and so on);
a socket close is never asked to survive a command failure. `sweep_cancellation()`
returns a `SweepReport` of what it injected and what it skipped, so a call
site that narrows its own sweep pins the narrowing rather than leaving a
reader to assume it asserted everything. After every run,
the caller's oracle asserts the chain's invariants held — every step that
must run after the injection point still ran, or the chain failed loudly
rather than silently skipping a step. There is no wall-clock wait and no
randomness anywhere in tier 1; the injection point is an integer counter, so
the same sweep is exactly reproducible every time it runs.
`tests/unit/test_context.py`, `tests/unit/host/test_connections_close.py`,
and `tests/unit/host/test_unix_host.py` are worked examples. Because these
are ordinary `tests/unit` tests, they need no opt-in at all — they ride
every `make coverage` and `make coverage-hostless` run for free. Data-shaped faults —
truncated reads, interleaved output, partial writes — are deferred: they
mutate returned bytes instead of raising, so they need oracle-aware handling
rather than another table entry.

**Tier 2 — real-signal integration tests (`tests/integration/chaos/`).** Each
test spawns a real `otto` subprocess (through the existing PTY driver where
terminal state matters) against one SSH-reachable host, and delivers a real
SIGINT or SIGTERM to it. Determinism here comes from **phase markers, not
timing**: the parent tails the subprocess's file log sink until a known
line appears (e.g. "session opened," "teardown started") and only then
sends the signal, so the test never guesses at a sleep duration to land the
interrupt at the right moment. The target host defaults to a hermetic
loopback `sshd` that the test session starts and tears down itself
(`tests/integration/chaos/conftest.py`'s `chaos_target` fixture, backed by
`_target.py`'s `make_loopback_target`) — no Vagrant lab required. Setting
`OTTO_CHAOS_BED_HOST` redirects the *target* at a real leased bed host
instead (`make_bed_target`); either way, the signal is always delivered to
the local otto process. Because this default needs no VM, and because these
tests carry no `chaos` or `stability` marker at all — only the auto-stamped
`integration` level marker — they already run as part of any ordinary
`make coverage` or `nox -s tests_integration` invocation, not only in a
dedicated lane. GitHub CI has no Vagrant lab, so `nightly.yml`'s
`chaos-tier2` job runs this same suite standalone against its loopback
default, giving every nightly interrupt-signal coverage with no bed at all.
Host-down in bed mode fails loudly, naming the host — never a silent skip,
matching the project's {doc}`fail-loud discipline <principles>`.

**Tier 3 — the chaos lane (`tests/e2e/chaos/`, opt-in).** This is the only
tier where randomized timing and genuinely destructive actions appear —
`docker kill`/`pause`/`restart`, soft reboots, SIGKILL, SSH blackholing via
otto's own link-impairment machinery — and it is excluded from every default
gate exactly like `stability`, via the same double marker stamp
(`chaos` + `stability` on every module, enforced by a guard below). It runs
only via `make chaos` (unix legs) and `make chaos-embedded` (the Zephyr
console leg, kept separate because a failure there can require a manual
board restart), against a session-scoped, exclusively-leased bed host from
the "unix" lab (`tests/e2e/chaos/conftest.py`'s `chaos_bed` fixture) —
never co-run with any other bed-using lane. Two ingredients make its
destructiveness safe to run at all:

- **The `BedHygiene` oracle** (`tests/_fixtures/bed_hygiene.py`) snapshots a
  host's remote state — tunnel daemons, impair timers, `nc` listeners, `tc`
  qdiscs, `/tmp/otto-*` staging entries, shell-history digest, docker
  containers and networks — over a *fresh* connection before a scenario
  runs, and diffs it against another fresh-connection snapshot after. The
  diff is **new-only**: whatever dirt was already on the bed going in is
  snapshotted out and never blamed on the scenario, and only items that
  newly appeared are reported, each one named individually (a qdisc that
  changed, a specific leftover container) rather than a generic
  "leak detected." A per-test autouse fixture wraps every tier-3 test in
  this bracket automatically; a test that manages its own bracket (because
  an in-flight reboot would make the after-probe race the boot itself) opts
  out with `@pytest.mark.no_hygiene_bracket` and asserts hygiene manually.
- **Seeded randomized injection** (`tests/e2e/chaos/_seed.py`) is the *only*
  sanctioned randomness anywhere in the suite. Within a phase window between
  two markers, a scenario draws its injection offset from a per-test
  `chaos_rng`, whose seed is resolved from `OTTO_CHAOS_SEED` if pinned, or
  drawn fresh otherwise — and printed on every run as
  `chaos seed: N (reproduce with OTTO_CHAOS_SEED=N)`. That printed line,
  which pytest's captured output always surfaces on a failure, *is* the
  reproduce handle: a nightly failure at a random offset is never a one-off
  you can't get back — rerunning with the same `OTTO_CHAOS_SEED` reproduces
  it exactly.

The docker slice (`tests/e2e/chaos/test_docker_chaos.py`) has a second life:
it is venue-selectable via `OTTO_CHAOS_DOCKER` (default/`test3` = the
docker-capable bed host; `loopback` = a hermetic `sshd` wrapping the
runner's own docker daemon), so the same module also runs standalone in
GitHub nightly's `chaos-docker` job with no lab at all — the one tier-3
slice that gets both a lab leg and a bedless nightly leg. The rest of tier
3 (tunnel/link rollback, transfer interrupts, privilege unwind, reboot as
the remote-side analog of SIGKILL, the embedded console-client-death
scenario) is lab-only, run from the dev VM, never from GitHub.

## The guards that keep the taxonomy honest

`tests/unit/test_tier_marker_invariants.py` is a set of drift guards, and
they run in the ordinary no-VM unit gate — so a change that breaks the
taxonomy fails on every PR, not months later at 2 a.m. in the nightly run.
The seven below (G1–G7) cover the level/resource taxonomy this page opened
with; the G8/G9 families carry the same rules for the BusyBox artifact tier
and the G10/G11 families for the host-contract conformance lane described
further down, each stating its own reasoning on the test itself:

- **G1 / G3** prove the `tests/integration/` and `tests/e2e/` auto-stamp
  hooks actually fire, by calling `pytest_collection_modifyitems` against a
  fake collected item and checking the marker landed.
- **G2** proves no test file under `tests/unit/` references a VM-only marker
  (`integration`, `embedded`, `hops`) at all — such a marker there would be
  dead weight (the unit gate never has a VM to give it) and usually signals
  a test in the wrong directory.
- **G4** scans every negation-only ("catch-all") `-m` expression in
  `noxfile.py` and asserts it excludes `stability`. A bare-negation
  selection otherwise means "everything not explicitly excluded," which
  silently sweeps the bed-hostile soak into a parallel run — the exact
  2026-07-19 incident this guard now prevents, where a wedge test stopped
  one host's `sshd` out from under a concurrent worker's own SSH attempt.
- **G5** proves every module under `tests/e2e/chaos/` carries *both*
  `chaos` and `stability`: the lane's exclusion from default gates rides
  entirely on the `stability` stamp (every catch-all already says
  `not stability`), while `chaos` is the *positive* selector `make chaos`
  opts in with. A module missing either mark either joins a gate it must
  never join, or silently falls out of the opt-in lane.
- **G6** checks the positive stability Make legs (`stability-unix`,
  `stability-embedded`, and `repeat`, none of which are catch-alls G4 would
  cover) all say `not chaos` — a chaos module also carries `stability`, so
  without this a stability soak would co-select chaos scenarios that reboot
  or blackhole the very bed the soak is running against.
- **G7** extends the same check to the resource-slice legs
  (`M_UNIX`/`M_EMBEDDED` in the Makefile, and nox's `tests_unix`/
  `tests_embedded`): they share a resource marker (`integration`/`embedded`)
  with the chaos lane, are bare positive selectors like the ones G6 covers,
  and need the identical `not stability and not chaos` exclusion — nothing
  else protects them.

Practically, this means a new chaos-lane test module that forgets the
`stability` mark, or a new catch-all nox session that forgets `not stability`,
fails immediately in the default gate rather than surfacing as a wrecked bed
three weeks later.

## Other axes: TypeScript, doctests, cross-Python

Three more axes sit alongside level/resource, each narrower in scope:

- **TypeScript/web.** `make test-ts` is the fast, no-coverage vitest loop
  for local iteration. `make lint-ts` / `make typecheck-ts` are Biome+knip
  and `tsc`. `make coverage-ts-unit` runs vitest with v8 coverage against
  the reduced unit-tier floor — the TS analogue of `coverage-hostless`'s
  reduced CI gate. `make coverage-ts` (part of `make coverage`) merges that
  vitest run with the Playwright e2e leg's coverage into one Istanbul
  report gated at the full, merged floor.
- **Doctests.** `>>>` examples inside `src/otto`'s own docstrings run
  automatically on every default pytest invocation — `--doctest-modules` is
  baked into `pyproject.toml`'s `addopts`. Prose examples embedded in the
  docs themselves (`.md`/`.rst`) are a separate mechanism, run through
  Sphinx's doctest builder (`make doctest`); both are part of `make docs`.
- **Cross-Python matrix.** otto's floor is Python 3.10 and its ceiling is
  3.14. `make nox-unit` re-runs the no-VM unit tier across all five
  versions — the fastest safe cross-version check. `make nox` tiers this
  further: the full suite on the 3.10 floor and the 3.14 canary (where
  version-specific deprecation warnings show up first), and the hostless
  slice on the versions between. `make nox-full` is the complete matrix,
  all tiers on all five Pythons, at roughly 5× `make nox`'s wall clock.

## The host axis space: what a bed host *is*

Some tests need to say more than "this needs a VM" — they need to know
*which* host, reached over *which* terminal protocol, moving files over
*which* transfer backend. `tests/_fixtures/profiles.py` is the single
resolver for that: `axes_for(element, tech)` returns a `HostAxes`
(`os_type`, `userland`, `terms`, `transfers`, `hop_depth`,
`docker_capable`), and `axis_space(lab)` crosses each host's two menus into
the full list of `(host, term, transfer)` cells that lab permits.

The rule that gives the module its reason to exist: **the axes are read off
a host otto's own factory built, never re-derived from `lab.json`.**
Measured against the current bed, 10 of the 19 hosts do not declare
`valid_terms` at all — the seven Zephyr guests (which do declare
`valid_transfers: ["console"]`) and `alt1`/`alt2`/`alt3` (which declare no
`os_type` and neither menu). The factory supplies what they omit: `alt1`
constructs as `unix` with `['ssh', 'telnet']` and
`['scp', 'sftp', 'ftp', 'nc']`, `zephyr37_fat` resolves to `['telnet']`. A
resolver that read the raw JSON would therefore produce *wrong* axes for
more than half the bed while looking perfectly correct on the nine hosts
that do declare theirs. Two fields are deliberate exceptions, read from the
raw entry because the host cannot answer them: `hop`, since chain depth is a
property of the lab rather than of any one host, and `userland_options`,
which the host normalizes into an all-`None` defaults object that is still
truthy.

`tests/unit/test_profiles.py` guards that rule across the whole population
rather than a sample, for a specific reason: the bed splits into two halves
that fail in opposite directions. The ten that declare no term menu catch a
resolver that stopped asking the factory; the nine that declare one — and
whose declared list the factory returns unchanged — catch the opposite
mistake, a resolver that overrode a menu the lab data had already stated.
Sampling only the second half would certify nothing, because those hosts
stay green under a raw read. A companion test pins the premise itself, so
lab data that started declaring `valid_terms` everywhere would report that
the guard has stopped discriminating instead of passing quietly.

## The host-contract conformance lane

`tests/conformance/` asserts otto's *host contracts* — the promises that
hold for every host, whatever userland it runs and whatever terminal and
transfer backend reaches it. Six contracts today, across exec, transfer and
timeout: an exit code reaches the caller unchanged, output carries nothing
the shell added, a failing command in a sequence is not reported as success,
put/get round-trips bytes, `put` lands the documented mode on the host, and
a command over its budget comes back the documented way (a `CommandResult`
with `timed_out=True`, not an exception) while its session stays usable.

Each contract runs against a **resolved cell** — a `(host, term, transfer)`
triple the suite can actually stand up, the same cell vocabulary
`tests/_fixtures/profiles.py` uses above. Six contracts times the cells a
run draws is the lane's size, less whatever a contract's own applicable
domain excludes: 48 tests in the hermetic venue at its default, and 284 on
the bed with every cell selected.

**Not to be confused with `otto.testing.conformance`.** otto ships a second
thing called conformance and it is unrelated: `src/otto/testing/conformance.py`
(re-exported as `otto.testing.assert_lab_repository_conforms` and
`otto.testing.assert_reservation_backend_conforms`, and documented in
{doc}`../library/lab-source-backends`) checks that a *pluggable backend
interface* conforms — that someone's custom lab source or reservation store
answers otto's protocol correctly. That is an API-shape check a third party
runs against their own code. This lane is about HOSTS, and it runs real
commands over real transports. Same word, different subjects; a reader who
hits one and assumes the other will be wrong about what is being proven.

### The two venues

The suite runs in either of two venues, on the
`chaos_target`/`OTTO_CHAOS_DOCKER` precedent. `OTTO_CONFORMANCE_BED` picks
one, `current_venue()` reads it once, and `resolve_space()` builds that
venue's cells:

- **Hermetic (default).** No lab. Cells resolve to the runner's own
  userland via `LocalHost`, to a throwaway non-root `sshd` on 127.0.0.1 with
  a real `UnixHost` over it (the tier-2 chaos lane's own fixtures), and to
  the five pinned BusyBox artifacts run as local subprocesses with an applet
  directory prepended to the session's `PATH`. Be exact about that last one:
  the *applets* otto invokes are genuinely the pinned artifact's, but the
  session's shell stays the runner's `bash` (otto's `LocalSession` spawns
  `bash --norc --noprofile` by name), so shell-*dialect* behaviour on a
  BusyBox userland is not measured here — that belongs to the bed venue's
  real guests. Its space is 8 cells and the default budget is 8, so the lane
  is exhaustive by arithmetic: 48 tests. This is what `make conformance`
  runs and what the nightly CI job runs.
- **Bed (`OTTO_CONFORMANCE_BED=1`).** Real hardware, built from the bed's own
  lab data by `tests/conformance/_bed.py`: the Unix VMs across
  `{ssh, telnet} × {scp, sftp, ftp, nc}`, the five BusyBox guests over hopped
  telnet, and the seven Zephyr guests over their single-client consoles.
  49 cells over 16 elements — 32 `bed-unix`, 10 `bed-busybox`, 7
  `bed-zephyr`. `make conformance-bed` is its only lane; it is **dev VM
  only** (nothing in CI runs it, and `tests/unit/test_tier_marker_invariants.py`
  asserts that no other lane can set the knob), and it is **exhaustive by
  default** rather than sampled, because a budget of 8 against a space of 49
  measures one cell in six and the crossing is this venue's whole claim.
  `make conformance-bed CONFORMANCE_CELLS=N` samples off the session seed
  instead.

**Why the bed venue is not `tests/integration/host/test_host_contract.py`
again.** That suite asserts host contracts against these same hosts
parametrized by *backend id* — one transport per backend, in depth. This
lane's distinct claim is the **crossing**: the same contract over every
`(term, transfer)` pair a host's own menus permit. A defect that holds over
`shell` and breaks over `nc` on the same host is invisible to a per-backend
suite, and the venue's first runs against real hardware surfaced exactly such
a difference (below). The two are complements, and neither is a superset:
depth there, breadth here.

A cell the selected venue cannot build is **dropped from the space, not
skipped**. The hermetic venue has no console server and no embedded
filesystem, so `telnet` terms and `console` transfers are not in its space at
all. A skip inside a drawn cell would report success for a contract nobody
ran, which is the failure this suite exists to make impossible — so the
space itself shrinks instead, and the run says so out loud.

### What the bed venue does not cover

A venue's gaps are worth more written down than rediscovered. Every item
below is a real limitation of `make conformance-bed` as it ships, not a
to-do list.

**The console lock is cross-worker, not cross-session.** Zephyr's
`shell_telnet` backend serves exactly ONE client per guest, and
`tests/conformance/_console_safety.py` holds the repo's writer-fair console
lock EXCLUSIVELY around every item whose drawn cell opens one. That lock is
taken in `tmp_path_factory.getbasetemp().parent`, which resolves to
`/tmp/pytest-of-<user>` under `-n0` but to `/tmp/pytest-of-<user>/pytest-<N>`
under xdist, because a worker's basetemp is a child of the controller's. The
suite's addopts turn xdist on, so **the lock serializes the workers of one
session and nothing else**. Two pytest sessions on this dev VM — a
`make conformance-bed` beside a `make coverage-embedded`, or two agent
sessions — are not serialized against each other. Neither is a person on
`telnet`, a `scripts/` tool, or a stale forward. Nor is an item that carries
no `resolved_cell` param at all: the autouse fixture reads the cell off the
item's callspec, so a test parametrized some other way gets no hold (the bed
openers' witness is deliberately the only such test, and it deliberately
opens no Zephyr guest). The same gap is true of `tests/integration/host/`
today; closing it needs one lock directory adopted by both trees, and doing
it in one only would be worse than doing it in neither. **The consequence of
losing that race is not a failed test**: two clients on one console can make
the guest re-initialise its telnet backend, after which it refuses every
connection until `make qemu-restart` (issue #260). The re-init is terminal
and does not self-clear.

**The timeout contract does not apply to Zephyr cells, and is covered
nowhere else.** `test_timeout_contract.applicable_cell` narrows the domain to
cells whose vocabulary has a long-running command, which excludes exactly the
seven `bed-zephyr` cells and narrows the hermetic venue by nothing. The
reason is not a missing stimulus that someone could add: the Zephyr shell is
**synchronous on the shell thread**, so a command that blocked for the
budget's duration would block the shell whose survival the second half of
this very contract asserts — a stimulus that made the first assertion pass
would make the second unmeasurable. (Driving a single-client console to a
timeout and then asserting recovery is also the #260 wedge sequence.) What an
embedded backend does with a command that outlives its budget is therefore
**not asserted anywhere in this repo** — `tests/integration/host/` contains
no `timed_out` assertion at all, and the only bed-side one is
`tests/integration/busybox_bed/test_session_frame.py`, which is a BusyBox
guest. This is a stated hole, not a delegation.

That predicate is also the one place this suite's applicable-domain model
deliberately differs from `test_transfer_contract.py`'s, which is otherwise
its model. The transfer domain reads OTTO'S OWN ANSWER (`remote_scratch is
None` ⟺ the host's filesystem reports `supports_transfer` False). There is no
otto property meaning "this userland can be made to block", so the timeout
domain reads **this suite's** vocabulary instead. It is still lab-derived
rather than an element-name sniff — the vocabulary is chosen by the userland
axis `axes_for` resolves off the host otto built — but a reader should not
mistake it for otto's answer.

**Where the tester cannot choose the output, the framing assertion is
strictly weaker.** On a POSIX cell the contract runs a `printf` whose exact
bytes it chose, and asserts exact equality — which also catches truncation,
reordering, interleaving, and a leaked shell prompt (the `vagrant@otto:~$`
measured on a loopback-ssh cell is caught by nothing weaker). On a Zephyr
cell there is no such command, so the stimulus is `kernel uptime` and the
assertion becomes the *contract itself*: no `__OTTO_` frame sentinel, no
`retval` read-back line, no ANSI, no echoed command line, plus a shape (an
integer somewhere in the reply). The shape is not decoration — it is what keeps
an empty answer, a bare prompt, or the previous command's output failing. But a
prompt *appended to* a correct answer survives both halves: the framing check
cannot see a prompt unless it arrives as an echoed command line or carries ANSI
(a prompt is not distinguishable from output in the general case, and otto
exposes no prompt string to compare against), and the shape check is already
satisfied by the real answer in front of it. The run's own parametrization ids
say which cells are in which tier.

**On Zephyr the sequence contract has only one distinct failure code.** The
exec contracts deliberately use two *different* non-zero codes so a
cross-wired constant cannot make one contract pass on the other's evidence.
On a Zephyr shell no second stable code exists: the obvious candidate,
`kernel uptime extra arg`, answers `-22` on 3.7 and 4.4 but **`0` on 2.7**
(2.7 ignores the extra words), so using it would make the contract's result
depend on firmware version; and `kernel bogus_subcommand` answers `1`, which
is the exact value the "collapses every failure to 1" defect produces. So
both failing stimuli use code `-8` with two different unknown *commands*.
What survives as the discriminator is `Results.first_failure.command`; what
is lost is the aggregate `exit_code`'s ability to tell one contract's
constant from the other's.

**Nothing in the venue is declared a known failure any more, and ten items
were.** From 2026-08-21 to 2026-08-25 the five `bed-busybox` cells crossed
with `nc` failed both transfer contracts — twenty items once the positive
controls landed beside them — and
`tests/conformance/test_transfer_contract.py`'s `expected_failure` declared
them `xfail(strict=True)`: not a suppression but an *assertion* of the
failure, so the lane's green kept meaning something and the day the product
was fixed the strict marker reddened the lane until the declaration was
repaid. That is what happened. The declaration and its pins are gone, the
cells assert the contract outright, and the hook the conftest reads
(`_XFAIL_HOOK`) is still wired for the next one.

It was never an undiscovered otto bug. `src/otto/host/userland.py` carried a
`nc-transfer` gap record whose `GapPath` for `NcFileTransfer._put_files_nc`
was `PATH_OPEN` and said in so many words that the listener was spelled
`nc -l -w <secs> <port>` while the BusyBox applet wants `-l -p PORT`, and
predicted the exact failure ("a timeout rather than the refusal this record
describes"). What kept it open was that settling it looked like it needed a
probe asking a device to *bind*, which has a side effect on the host being
questioned.

**What closed it was measuring the applets instead of interrogating a live
device.** `nc -l -p PORT` turned out to be the one listener spelling every
*measured* netcat accepts — OpenBSD 1.226 and all five pinned BusyBox builds,
which is the whole measured set; traditional netcat and ncat *document* the
same form but are unmeasured here, neither being installed on any reachable
host. So otto stopped asking the question and now always emits the universal
form, in both directions, with no `-N` anywhere
(the GET arms read exactly the size their `stat` prefetch measured and close
to terminate a sender that cannot close itself). The per-build evidence is
Tier-1 and hermetic: `tests/busybox/test_applet_contracts.py` binds a
listener on each pinned build, sends a binary-hostile payload through it, and
asserts it exits at the peer's close — plus the row-by-row proof that none of
them parses the `-N` the backend once sent. The gap record, its refusal and
the refusal's integration test are all deleted
(`docs/superpowers/specs/2026-08-25-nc-universal-spelling-design.md`).

**What the bed venue owed this, it paid on 2026-08-26 UTC** (the `as_of`
stamp in `schemas/support_matrix.json`): the five `bed-busybox[*:telnet:nc]`
cells are where otto's `nc` backend meets a real BusyBox applet end to end,
and that run was the first with the universal spelling in it. It came back
565 passed, 0 failed, 0 xfailed; the ten BusyBox `nc` cells — the five
`bed-busybox[*:telnet:nc]` GET/PUT pairs — flipped `measured-broken` →
`measured-ok`; the directional gate reported 10 ALLOWED / 0 BLOCKING; and the
census moved 41 ok / 10 broken / 3 not-observable to 51 / 0 / 3. None of that
was predicted by the applet contracts above and none of it could be: the
artifact is a record of a measurement, not a prediction, and it changed when
the bed was re-run and not before.

### What the crossing found that nothing else could

Two findings came out of the first bed runs, and neither was reachable from a
per-backend suite:

- **The `nc` × BusyBox asymmetry above.** The contract held over `shell` and
  broke over `nc` on the *same host*, so a suite that asks each host one
  transport could not see the difference. What the crossing added was not the
  discovery — otto's gap registry had measured the spelling on 2026-08-13,
  ten days before this venue first drove it — but the observation that one
  registered gap refused loudly in one direction (`get` raised
  `UnsupportedOnUserlandError` before the wire) and timed out in the other
  (`put` returned `Status.Error` after 5s: `Remote nc listener on port 9000
  not ready`), from one host build, measured on `bb1161:telnet:nc`. That is
  the observation the 2026-08-25 fix was written against: both directions now
  emit the universal spelling and neither refuses, so the finding is spent —
  which is what a venue's findings are for.
- **A coverage hole in the embedded matrix.** `tests/conftest.py`'s
  `_ZEPHYR_BACKEND_NE` maps five backend ids to five guests —
  `zephyr37_fat`, `zephyr37_lfs`, `zephyr37_nofs`, `zephyr27_fat`,
  `zephyr44_lfs` — and `EMBEDDED_BACKENDS` is `list(...)` of its keys, so
  every parametrized embedded suite in `tests/integration/host/` derives
  from those five. **`zephyr37_llext` and `zephyr44_llext` appear in
  neither**, and had therefore never been asked a contract question by the
  integration suite at all. The conformance space
  derives from `lab.json`, which has all seven — which is how the divergence
  became visible. Neither side can see it alone: one list is hand-maintained,
  the other is derived. It bit immediately, too — `version` is not registered
  on those two guests (`command not found`, retcode -8) while it works on the
  other five, so a stimulus "proven across the matrix" was proven only across
  the five the matrix names.

### How a cell is drawn, and how to reproduce a draw

Each run draws `OTTO_CONFORMANCE_CELLS` cells (default 8; `all` for the
whole space) and parametrizes every contract over the draw. The draw is a
pure function of pytest-randomly's session seed: cells rank by
`blake2b(seed:label)` and the budget's worth of lowest ranks is taken. It
never creates a seed of its own, because the reproduce handle is the
`--randomly-seed=N` printed in the run's own pytest header, and that handle
is only true if the draw derives from that number.

Every run logs both numbers at session start:

```text
conformance: venue=hermetic space=8 cells drawn=8 seed=809258197
conformance: drew local[local:local:local], loopback-ssh[loopback:ssh:sftp], ...
```

`drawn=` alone would read identically whether the venue offered 17 cells or
had collapsed to 8, so `space=` is what makes a venue that quietly stopped
resolving its loopback `sshd` visible rather than silent. Measured on the
dev VM today, the hermetic space holds 8 cells and the default budget is 8 —
so at the hermetic default the draw *is* the whole space, and sampling only
starts to bite at a smaller budget. The bed venue is where the space is
genuinely bigger than a budget (49 against 8), which is why its lane sets
`OTTO_CONFORMANCE_CELLS=all` and draws every one.

### Why it is nightly, not per-push

The lane is excluded from every default gate by its own `conformance`
marker. Exactly two lanes positively select it — `make conformance`
(hermetic) and `make conformance-bed` (the real bed, dev VM only) — and
nightly's `conformance-hermetic` job is the only place CI runs either. That
is not a cost decision. Selection here is *random per run*, so a per-push gate
could fail an unrelated PR on pre-existing breakage in a cell nothing had
ever drawn before — the same reason the kernel keeps randconfig in `-next`
and 0-day rather than in per-patch CI. Nightly is where a sampled lane
belongs: it accumulates coverage over runs, and a red one names the cell it
drew in the test id.

The exclusion needs guarding more than the BusyBox tier's does, because
`M_HOSTLESS` is a pure negation: a conformance test carries no
`integration`, `embedded`, `stability`, `browser` or `busybox` marker, so it
satisfies every clause already there and is *selected* by the ordinary gates
unless `not conformance` is spelled out. The G10/G11 families in
`tests/unit/test_tier_marker_invariants.py` hold that line — the stamp
fires, every module declares the marker itself, both build-file surfaces
carry the clause, no other lane can select the tier, the tree is inside
`testpaths` (a path-less `pytest -m conformance` collects nothing without
it), and a real path-less collection under `M_HOSTLESS` both deselects every
conformance test and proves the tree was collected at all. G11c closes the
other end: a tier excluded everywhere and invoked nowhere is a deletion, so
it asserts that some workflow job actually runs the lane.

The bed venue needed its own four, because the `conformance` marker cannot
see the difference between the two venues — the venue is an environment
variable, not a marker, so nothing in the marker family constrains it.
G11i is G11c's shape for the knob (a Makefile lane must both export
`OTTO_CONFORMANCE_BED` and be able to select the `conformance_bed` tests,
or `tests/conformance/_bed.py` and its lab context are code no lane runs);
G11j is the exclusion half, scanning every Makefile recipe, `noxfile.py`,
`scripts/stability_campaign.py` and every workflow for a lane that sets the
knob without positively selecting `-m conformance`; G11k asserts that no CI
workflow invokes the bed lane, which G11c's union over "the lanes selecting
`conformance`" would otherwise be satisfied by; and G11l runs the bed lane's
own recipe — target, env and marker expression read out of the Makefile
rather than retyped — path-less under `--collect-only`, and asserts it really
resolved `venue=bed` and collected at least one cell of every kind
`bed_space()` reports. It pins no count, so the property survives resampling
and lab growth.

## Coverage: a floor, not a scorecard

`make coverage` enforces `--cov-fail-under=96` over the whole default run
(the `dashboard` browser lane's Python-side coverage folds in via
`--cov-append` first); the hostless CI gate (`make coverage-hostless`,
`nox -s tests_hostless`) enforces a lower 95, because it deliberately
exercises only the slice of otto that needs no VM at all and can't reach
the full-run number. `nox -s tests_all`'s browser-excluded matrix sits at 92
for the same reason — it never folds in the dashboard lane's coverage. The
TypeScript side mirrors this shape: `coverage-ts-unit`'s vitest-only floor
is the browserless CI analogue of `coverage-ts`'s full merged floor.

Every one of these numbers is a *floor*, meant to catch a regression —
code that used to be exercised and silently stopped being — not a score to
chase upward for its own sake. A test that pads the percentage without
exercising real behavior (an assertion-free smoke test, a mock that never
lets the real code run) defeats the actual point even while turning the
gate green; the taxonomy above exists so that the *right* test — the one
that actually proves the behavior — is easy to find a home for, which is a
better lever on the number than tightening the threshold ever is.

## Where to run these

This page answers *what to write and why*; {doc}`../contributing`'s
"Running tests" section is the mechanical reference — the full make-target
table, VM requirements, `nox` invocations, and the embedded coverage bed's
own conventions. Start there once you know, from the guide above, which
kind of test you're adding.

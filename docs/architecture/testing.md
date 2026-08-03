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
then re-runs it once per point, injecting either `CancelledError` or a
`ConnectionDropped` stand-in at exactly one point each time. After every run,
the caller's oracle asserts the chain's invariants held — every step that
must run after the injection point still ran, or the chain failed loudly
rather than silently skipping a step. There is no wall-clock wait and no
randomness anywhere in tier 1; the injection point is an integer counter, so
the same sweep is exactly reproducible every time it runs.
`tests/unit/test_context.py`, `tests/unit/host/test_connections_close.py`,
and `tests/unit/host/test_unix_host.py` are worked examples. Because these
are ordinary `tests/unit` tests, they need no opt-in at all — they ride
every `make coverage` and `make coverage-hostless` run for free.

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
the "veggies" lab (`tests/e2e/chaos/conftest.py`'s `chaos_bed` fixture) —
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
it is venue-selectable via `OTTO_CHAOS_DOCKER` (default/`pepper` = the
docker-capable bed host; `loopback` = a hermetic `sshd` wrapping the
runner's own docker daemon), so the same module also runs standalone in
GitHub nightly's `chaos-docker` job with no lab at all — the one tier-3
slice that gets both a lab leg and a bedless nightly leg. The rest of tier
3 (tunnel/link rollback, transfer interrupts, privilege unwind, reboot as
the remote-side analog of SIGKILL, the embedded console-client-death
scenario) is lab-only, run from the dev VM, never from GitHub.

## The guards that keep the taxonomy honest

`tests/unit/test_tier_marker_invariants.py` is a set of seven drift guards
(G1–G7), and they run in the ordinary no-VM unit gate — so a change that
breaks the taxonomy fails on every PR, not months later at 2 a.m. in the
nightly run:

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

## Coverage: a floor, not a scorecard

`make coverage` enforces `--cov-fail-under=95` over the whole default run
(the `dashboard` browser lane's Python-side coverage folds in via
`--cov-append` first); the hostless CI gate (`make coverage-hostless`,
`nox -s tests_hostless`) enforces a lower 90, because it deliberately
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

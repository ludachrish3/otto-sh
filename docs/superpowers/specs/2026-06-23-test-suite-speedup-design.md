# Test-suite speedup — design

> **Status:** design approved 2026-06-23. Follow-up to the test-suite restructure
> (`c9e76d8`); the speed half deferred from the 2026-06-22 restructure brainstorm.
> Empirical baseline + raw measurements live in
> [`todo/test-host-pool-and-speed.md`](../../../todo/test-host-pool-and-speed.md).

## 1. Goal

Reduce the wall-clock of the local bed gate (`make coverage`) and the
cross-Python matrix (`make nox`) **without losing a single integration/e2e
scenario and without regressing coverage**, by removing real serialization
bottlenecks rather than by cutting tests.

## 2. Hard constraints (gates, non-negotiable)

These are inherited from the restructure and the brainstorm; every task is bound
by them.

- **Scenarios are the deliverable, not coverage.** Integration/e2e tests exist to
  confirm specific behaviors (telnet bad-creds fail-fast, console contention/
  recovery, SIGWINCH propagation, put/get round-trip). **No scenario may be
  dropped, weakened, or merged away.** Coverage equivalence is the *detector*
  that proves we didn't silently lose one — it is a guardrail, not the purpose.
- **Coverage stays equivalent** — `make coverage` ≥ the current 90% gate and
  within noise of the current ~91.85 %.
- **`make nox` keeps its full 5× Python matrix for the bed.** The win there comes
  from making each run faster + de-duplicating the *unit* tier, never from
  running the bed on fewer interpreters.
- **otto stays server-less** (fable review #6). The pool is a *test-harness*
  lease (a file lock), not a coordinator service or a change to otto's runtime
  reservation backend.
- **Embedded backends are NOT poolable.** Each `sprout*` console is a distinct
  scenario (fs × os_version × command_frame); they stay scenario-parametrized.
- **Resource-contention groups are preserved.** The single-client console lock
  and `--dist loadgroup` semantics remain; docker serialization is preserved
  *per daemon* but spread across VMs once docker runs on all three (§4e).
- **Lab data reflects reality; hop coverage stays explicit.** `hosts.json` drops
  the **Unix-host hops** (pepper's `carrot_seed` in tech1, line 49; the tech2
  Unix-host hops `orange_seed`/`apple_seed` — audited per lab) because they were
  test scaffolding, not a network need: the Unix VMs are directly reachable. The
  **embedded-zephyr hops** (`sprout* → basil_seed`) stay — basil is the real,
  required hop to the single-client QEMU consoles. Hop *functionality* remains
  covered by the dedicated `tests/integration/host/test_hop_integration.py`
  (explicit `carrot → tomato` chains via the `hop_host` fixture,
  `tests/conftest.py:539`), which does **not** depend on any host's lab-data hop —
  so the simplification removes no scenario.
- **Every win is validated with repetition.** Bed wall-clock is noisy (~±20 s
  run-to-run); a single clock proves nothing. Measure median over N runs
  (`pytest-repeat` / repeated invocations) before claiming an improvement.

## 3. Empirical baseline (why these interventions)

Measured on a clean idle box, pinned Python, gate scope (`-m "not stability"`).
Full data in the todo doc; the load-bearing facts:

- **Embedded is already hidden, not a tail.** `T (full) ≈ B (non-embedded)`,
  never `A (embedded) + B`. Per-device `xdist_group` + the *shared* console lock
  already parallelize the 5 consoles. Interleaving embedded with Unix is a
  **non-lever** (tested and disproven).
- **The real tail, every run:** (1) Unix `TestFileTransfer` (scp/sftp/ftp/nc)
  all funneling onto `transfer_host` = **carrot**; (2) `sprout_cov` — one ~18.8 s
  e2e test that finishes **dead last** purely from late scheduling; (3) the e2e
  single-worker chains `docker_e2e` / `coverage_e2e` / `interact_e2e`.
- **Coverage is cheap** (~5 s) and **not a lever**.
- **Three veggies-lab VMs are transfer-equivalent** — **carrot, tomato, pepper**
  all expose identical `scp/sftp/ftp/nc` backends. Basil is embedded-lab (out).
  Pepper's two former caveats are being removed: the single docker daemon
  (pepper) becomes docker on all three VMs (§4e), and the carrot hop in its lab
  entry is dropped for direct leasing (the hop was a test scenario, not a network
  need).
- **The docker daemon is the docker chain's bottleneck.** `docker_e2e` is one
  serial group *because there is one daemon* (pepper). Spreading docker across
  the three VMs turns that chain into a pool consumer (§4e).

## 4. The five interventions

Each is independently landable, independently measurable, and bound by §2.
Interventions 4a–4d need no infrastructure change; **§4e is gated on a Vagrant
change** (docker on all three Unix VMs), which Chris owns.

### 4a. Unix host-pool lease — the #1 lever

**Problem.** Transfer/command tests hardcode `transfer_host` = carrot
(`tests/conftest.py:567-580`), serializing the largest tail component onto one
VM.

**Design.** Introduce a cross-worker lease over the Unix pool —
**`{carrot, tomato, pepper}`** (the three veggies-lab peers with identical
`scp/sftp/ftp/nc` backends). A test that needs "a Unix host" leases whichever is
free; the lease spreads the transfer tail ~3×. The lease is the **single
coordination point** for all Unix-host work — once docker runs on every VM
(§4e), docker tests become ordinary pool consumers (lease a host, use *its*
daemon), so transfers and docker serialize per-host through the same lock.

- **Lease mechanism:** a writer-fair file lock mirroring
  `tests/_fixtures/_console_lock.py` (cross-worker via the shared
  `tmp_path_factory` base). One lock-file per pool host; a test acquires the
  first free host, holds it for the test, releases on teardown — including under
  a pytest-timeout SIGTERM (mirror the console lock's force-release teardown).
- **Health-aware:** the lease consults the reactive bed-wedge gate
  (`tests/integration/host/conftest.py:102-327`) so a wedged host is skipped, not
  waited on. A fully-sick pool **fails loudly** with the host names (never
  silently skips — per project policy).
- **Fixture seam:** `transfer_host` (and any "just needs a Unix host" fixture)
  becomes pool-aware. Tests that assert *carrot-specific* behavior (audit
  required) keep an explicit pin via an opt-out (e.g. `@pytest.mark.host("carrot")`).
- **Pepper is leased directly.** Per the lab-data simplification (§2),
  `hosts.json` no longer defines a hop for pepper, so the pool reaches it directly
  at `10.10.200.13` — no work-around needed. Hop traversal stays covered by
  `test_hop_integration.py` (§2). The lab-data task verifies the existing pepper
  consumers (`docker_e2e` / `host3`) still pass once pepper resolves direct.
- **Docker coordination.** Once §4e lands (docker on all VMs), docker tests lease
  from this same pool, so transfers and docker never double-book a host. Interim
  (if 4a ships before 4e): `docker_e2e` holds pepper's exclusive lease for its
  duration so a pooled transfer can't collide with the single pepper daemon —
  that rule is removed when 4e makes docker a normal pool consumer.
- **Scope guard:** basil stays **out** — it is embedded-lab and serves as the
  embedded hop. Extending the pool to basil is out of scope for v1.

**Scenario preservation.** The *same* transfer scenarios run; only the host they
land on changes. The equivalence protocol (§5) proves the same test IDs collect
and pass.

### 4b. Front-load the long poles — scheduling reorder

**Problem.** `sprout_cov` (~18.8 s) and the e2e chains finish last only because
`--dist loadgroup` dispatches their groups late, so they trail instead of
overlapping the parallel pool.

**Design.** A `pytest_collection_modifyitems` hook (alongside the existing one at
`tests/integration/host/conftest.py:150-183`) reorders collected items so the
**heaviest known serial groups dispatch first**: `sprout_cov`, `docker_e2e`,
`coverage_e2e`, the embedded fan-out. Reorder only — groups, lock semantics, and
scenarios are untouched.

**Open mechanism risk (validate first).** Under `--dist loadgroup` it is *not
guaranteed* that front-of-collection ⇒ dispatched-first. The first task is a
spike: confirm whether collection order steers loadgroup dispatch, and if not,
implement the hint another way (e.g. a dedicated worker assignment or a
scheduling plugin). If neither works cheaply, this intervention is dropped — it
is the lowest-effort item, not load-bearing for the others.

**Scenario preservation.** Pure reorder; tests must already be order-independent
(verified by the equivalence protocol running both orders).

### 4c. Split `interact_e2e` ssh/telnet → two groups

**Problem.** `interact_e2e` is one `xdist_group` (`test_interact_e2e.py:21-23`),
so the ssh and telnet parametrizations serialize on one worker.

**Design.** Key the group by terminal type — `interact_e2e_ssh` /
`interact_e2e_telnet`. The two parametrized classes use independent PTYs and
subprocesses (`tests/e2e/host/_pty_driver.py`); ssh and telnet do not collide on
ports or controlling terminals. They run concurrently on two workers.

**Scenario preservation.** Identical tests, finer grouping. Modest (~6 s),
low risk.

### 4d. nox: run the unit×5 sessions concurrently

**Problem.** `make nox` runs five sequential full suites (one per Python);
~804 s. The bed must stay sequential (single-client resources), but the bed-free
unit tier is re-run inside each of the five.

**Design.** Restructure `noxfile.py` (`:51-99`) so the **bed-free `tests_unit`
sessions run concurrently across the five Pythons** while the bed sessions stay
sequential and keep the full 5× matrix. Mechanism options (decide in the plan):
nox's parallel session execution for the unit sessions, or splitting `tests_all`
into a sequential bed session + a parallelizable unit session per Python. Keep
the 85 % gate + per-session JUnit.

**Scenario preservation.** No test changes; only session orchestration. The bed
still runs in full on all five interpreters.

### 4e. Parallelize `docker_e2e` across per-VM daemons — gated on a Vagrant change

**Problem.** `docker_e2e` is one serial group purely because there is a single
docker daemon (pepper). Parallelizing against *one* daemon was already tested and
is *slower* — image/network state serializes inside dockerd
(`test_docker_e2e_cli.py:41-48`).

**Dependency (Chris-owned).** Add docker to all three Unix VMs via the
`Vagrantfile`, and ensure the otto test images are present on each (build/load in
provisioning). Chris handles the redeploy.

**Design.** With three daemons, the docker tests become **pool consumers**: each
leases a Unix host (§4a) and drives *that* host's docker daemon. Each test already
uses a unique `OTTO_COMPOSE_SUFFIX` (`fresh_suffix`), so project/container names
never collide; the per-host daemon removes the single-daemon serialization. The
`docker_e2e` single group is replaced by per-host leasing, spreading the chain
~3×. The docker target host is parametrized off the leased host rather than
hard-pinned to pepper.

**Scenario preservation.** Identical docker scenarios; only the daemon they run
against changes. Same test IDs collect and pass (§5).

**Fallback.** If the infra change is not made, `docker_e2e` stays one group,
front-loaded (4b) — no regression. 4e is additive, not load-bearing for 4a–4d.

## 5. Validation & equivalence protocol

For each intervention, before merge:

1. **Scenario equivalence:** `pytest --collect-only -q` before/after must yield
   the **same set of test IDs** (modulo group-id renames in 4c). Same passed
   count in a green run. This is the hard "no scenario lost" proof.
2. **Coverage equivalence:** `make coverage` stays ≥ 90 % gate and within noise
   of baseline (~91.85 %).
3. **Speed, measured with repetition:** run the affected scope N≥3× (or
   `pytest-repeat`), report **median** wall + spread, not a single clock. Attribute
   the win to the intervention (A/B each one separately).
4. **Full gate before done:** `make coverage`, `make nox` (5/5), `ty`, `make docs`,
   and the tier/marker drift guards (`tests/unit/test_tier_marker_invariants.py`).

A wedged bed or a flaky SNMP-embedded run (observed once in baseline) **fails
loudly** and is distinguished from a real regression by re-running — it never
becomes a silent skip.

## 6. Explicitly out of scope (data-rejected or deferred)

- **`docker_e2e` split against a *single* daemon** — proven slower
  (`test_docker_e2e_cli.py:41-48`); never do this. Splitting across *multiple*
  daemons is the separate, infra-gated §4e — that **is** in scope.
- **`coverage_e2e` parallelization** — needs per-worker remote
  `/var/coverage/product-<id>` dirs + per-test coverage runs to avoid an
  asyncssh deadlock (`test_coverage_e2e.py:25-29`). High flake risk; **skipped**
  (user decision). Stays grouped; only front-loaded.
- **Embedded console pooling / per-console lock split** — embedded already
  overlaps; not worth the single-client-lock risk.
- **nox "bed once, not 5×"** — rejected; the full matrix is a hard constraint.

## 7. Risks

| Risk | Mitigation |
|------|------------|
| A transfer test has a hidden carrot-specific assumption → breaks on tomato/pepper | Audit transfer tests for host-specific asserts; provide an explicit host-pin opt-out |
| Removing Unix-host hops from `hosts.json` drops hop coverage | Already carried by the dedicated `test_hop_integration.py` (explicit carrot→tomato); the lab-data task confirms it relies on no lab-data hop |
| Pepper consumers (`docker_e2e` / `host3`) break when pepper resolves direct | Lab-data task re-runs those with direct pepper; pepper is directly reachable on the private net |
| `docker_e2e` + pooled transfer collide on pepper (interim, pre-4e) | `docker_e2e` holds pepper's exclusive lease until 4e makes docker a pool consumer |
| §4e infra change (docker on all VMs) not landed | 4e is additive + gated; 4a–4d proceed; `docker_e2e` stays grouped + front-loaded, no regression |
| otto test images missing on a VM after redeploy | Provision/build images in Vagrant; the first docker-pool task verifies image presence on each host |
| Front-load doesn't steer loadgroup dispatch | Spike first; drop the item if no cheap mechanism (it's not load-bearing) |
| ±20 s bed noise masks or fakes a small win | Median-over-N with repetition; A/B per intervention |
| New flakes from the lease (double-acquire, leaked host) | Mirror the proven console-lock teardown (force-release on SIGTERM); per-worker-safe acquire |
| Lease interacts badly with the bed-wedge gate | Reuse the existing `_BED_HEALTH` signal; fail loud on a fully-sick pool |

## 8. Sequencing (independently landable)

Suggested order, cheapest-and-safest first, measuring after each:

1. **4b front-load spike + hook** (cheapest; informs whether it stays in).
2. **4c interact split** (small, low-risk, self-contained).
3. **4a host-pool lease** (the biggest win; the most build) — *begins* with the
   `hosts.json` Unix-hop simplification (§2), then the lease + pool-aware fixtures.
4. **4d nox unit-concurrency** (orthogonal; the `make nox` win).
5. **4e docker parallelization** — after Chris's Vagrant redeploy lands and once
   4a's pool exists (4e reuses the lease). Additive; slots in whenever the infra
   is ready.

Each lands with its own before/after measurement and the §5 gates.

## 9. Execution notes

- Implementation happens in a **fresh worktree off `main`** (the restructure is
  merged; this is a new workstream).
- Stage-only; Chris commits (the `prepare-commit-msg` hook needs `/dev/tty`).
- **§4e depends on a Chris-owned `Vagrantfile` change** (docker on all three Unix
  VMs + test images per host) and its redeploy; the test-side work for 4e lands
  only after that infra is in place.
- Subagent-driven-development with a per-task before/after measurement step folded
  into each task's deliverable.

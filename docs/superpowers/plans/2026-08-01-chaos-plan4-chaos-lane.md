# Chaos Plan 4: Chaos Lane Foundations Implementation Plan

> **Status: DRAFT (executed per the Plans 1/2/3 precedent: write → self-review → execute).** Judgment calls a reviewer should weigh, settled as drafted unless Chris says otherwise:
>
> 1. **Marker strategy: chaos tests carry BOTH the new `chaos` marker AND the existing `stability` marker.** Every catch-all `-m` expression in the repo already says `not stability` (enforced by G4 in `tests/unit/test_tier_marker_invariants.py`), so double-stamping excludes the lane from every default gate with zero edits to existing expressions. The two `stability-*` legs whose *positive* selectors would co-select chaos modules (`stability-unix`: `stability and integration and not embedded and not hops`; `stability-embedded`: `stability and embedded`) gain `and not chaos`. New guards pin both facts.
> 2. **`nox -s chaos` / `nox -s chaos_embedded` use positive selectors** (`chaos and not embedded` / `chaos and embedded`) — G4-exempt by construction. Neither joins `nox.options.sessions`.
> 3. **Embedded console scenarios live behind a separate `make chaos-embedded` leg**, not the default `make chaos`. The console-client-death scenario deliberately reproduces the 2026-08-01 fat-board wedge's trigger class; a failure realistically means a manual zephyr bed restart, so it must never ride along implicitly.
> 4. **"Blackhole the SSH port" = port-scoped `loss 100%` on the declared carrot↔tomato eth2 link, applied to a hop-routed session.** otto's placement guards (`ensure_not_mgmt`) rightly refuse to impair a management netdev, so the interrupted session must reach tomato's *data-plane* address (192.168.1.12) via a carrot hop; the impairment then lands on eth2, which routes that hop leg and nothing else. Every impairment carries `--expire` as a self-healing backstop. Fallback if hop routing proves unroutable at execution: SIGSTOP of tomato's sshd from the probe connection (the stability suite's wedge precedent, with auto-CONT arming) — controller decision, recorded in the ledger.
> 5. **Reboots are soft-only (`hard=False`; the bed has no PowerController), only ever on the leased host, never basil** (it is the zephyr hop — rebooting it kills the embedded bed) **and never the dev VM.** Real bed reboots were Chris-approved in the spec's 2026-07-31 amendment; the controller RE-CONFIRMS that authorization at execution start before any reboot scenario runs.
> 6. **The bed "lease" = the existing flock pool lease + fail-loud reachability probe.** otto's reservation layer is a consumer-only client by design (spec non-goals: "nothing to release"), so `todo/chaos-realsignal-followups.md` §4's "wire a reservation acquire/release" resolves to: exercise `make_bed_target` for real (closing the untested-prototype gap) under `lease_unix_host` exclusivity — no reservation backend wiring.
> 7. **Seeded injection has no repo precedent** (swept: zero `random.Random(seed)`, no `--seed`, no pytest-randomly). Design: each scenario draws its injection offset from a per-test `random.Random(seed)` where seed comes from `OTTO_CHAOS_SEED` if set, else `os.urandom`; the seed is always printed as `chaos seed: N (reproduce with OTTO_CHAOS_SEED=N)` so any failure reproduces.
> 8. **The nc GET-path cancel-time reap (product fix) rides this plan** — `todo/chaos-teardown-followups.md` §1: today a cancelled GET leaks the remote `nc -l` for `listener_timeout=30 s`, which outlives `DEFAULT_TEARDOWN_DEADLINE=10 s` and violates spec success-criterion #1. `listener_timeout`'s default itself stays 30.0 (changing a user-facing default is out of scope; the fix makes the reap happen, the scenario proves it).
> 9. **Interrupted-transfer partial-file behavior is characterized, not changed.** The scenario asserts the hard invariants (no orphaned listener beyond teardown, exit code contract) and records the observed destination-file state in the test's docstring as the documented partial-file policy.
> 10. **Deferred, with reasons, in non-goals:** monitor-e2e SIGINT-tolerance tightening (needs bed repetition evidence this lane can gather later); loopback-sshd orphan-on-worker-SIGKILL cleanup story (tier 3 uses bed hosts, not the loopback fixture, so the pattern is not being copied); `_confirm_recovered` budget floor and prompt-then-freeze settle-window criteria (this plan's reboot scenarios *gather the data*; changing recovery semantics is a follow-on).

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Tier 3 of the chaos harness — the opt-in `make chaos` / `nox -s chaos` bed lane, the BedHygiene snapshot/diff oracle, seed-reproducible injection offsets, and the scenario catalog (host/session, connection-drop, transfer, tunnel/link, reboot, embedded console) against leased live-bed hosts, plus the one product fix the catalog requires (nc GET-path cancel-time listener reap).

**Architecture:** A new `tests/e2e/chaos/` suite drives a real `otto` subprocess (reusing tier-2's `spawn_otto`/`OttoProc` phase-marker driver and `make_bed_target`) against one leased veggies host (`lease_unix_host` flock + fail-loud reachability probe). Every scenario is bracketed by BedHygiene (`tests/_fixtures/bed_hygiene.py`): snapshot otto-tagged processes, nc listeners, qdiscs, `/tmp/otto*` staging, and shell-history digest over a fresh probe connection before; diff after; fail naming host + leftovers, pre-existing dirt snapshotted out (the `tunnel_bed.py` pattern generalized). Injection timing is drawn from a printed, overridable seed. SIGKILL and reboot scenarios characterize what leaks and assert the recovery commands (`otto tunnel remove --all --yes`, `otto link repair --all`) reconcile. The lane is marker-excluded from every default gate exactly like `tunnel_stability` and runs only via `make chaos` (unix) / `make chaos-embedded` (zephyr console), from the dev VM, owning the bed.

**Tech Stack:** Python 3.10 asyncio, pytest (sync tests driving subprocesses; `xdist_group("chaos_lane")` under the repo-wide `--dist loadgroup`), asyncssh probe connections, `tests/integration/chaos/` tier-2 driver reuse, `tests/_fixtures/{_host_pool,tunnel_bed,labdata}.py`, otto's own link-impairment (port-scoped netem) as the injection tool, nox + Makefile lane plumbing.

## Global Constraints

- Python floor is 3.10. `X | None` annotations fine; `asyncio.Runner`, `asyncio.timeout`, `except*` are NOT.
- NEVER add `from __future__ import annotations` (Sphinx nitpicky `-W`; repo-wide ban).
- **Real signals go ONLY to spawned otto subprocesses** (`OttoProc.signal` / `os.kill` on our own children). Never install real signal handlers in-process (root-conftest guard enforces).
- **Bed venue rules (bind every task):** scenarios touch ONLY the leased veggies host (carrot/tomato/pepper via `lease_unix_host`) plus, where a scenario is inherently two-host (hop/tunnel/link), the specific named peer. NEVER touch basil except as the zephyr hop in Task 10, and NEVER reboot basil, the boards, or the dev VM. Reboots are soft (`hard=False`), only on the leased host. Every impairment passes `--expire`; every scenario reaps/repairs in teardown even on failure (`try/finally`). The lane assumes exclusive bed use — the controller serializes it against all other bed activity.
- **Waits are condition-polls with deadlines, never fixed sleeps** — except the seeded injection offset itself, which is the one deliberate `time.sleep(offset)` (that sleep IS the scenario). Poll interval ≤ 0.1 s. Live-bed rule: timeouts generous, never tight (`pytest.mark.timeout` per module ≥ 300; reboot module 900).
- Every module in `tests/e2e/chaos/` carries `pytestmark` with: `pytest.mark.chaos`, `pytest.mark.stability`, exactly one resource marker (`integration`, or `embedded` for Task 10; add `hops` on multi-host modules), `pytest.mark.xdist_group("chaos_lane")`, and a `pytest.mark.timeout(...)`.
- `filterwarnings = ["error"]` is live; asyncssh probe connections run inside `asyncio.run` via context managers; every `UnixHost` built by a test/fixture is closed in `finally`.
- pytest-asyncio strict mode: scenario tests are deliberately sync (subprocess-driving); helpers that need a loop use `asyncio.run`.
- Lint suppressions are a failure mode: prefer restructuring; any `# noqa` needs a written justification on the same line.
- Per-task gate: scoped pytest (RED then GREEN) → `uv run nox -s lint` → `make typecheck-python` → `make coverage` (there is no `make test`). Run gates FOREGROUND with `timeout: 600000` spelled into the Bash call. Scoped chaos-scenario runs additionally require the controller's confirmation that the bed is free, and always run `-p no:cacheprovider --no-cov`.
- Never `git push`. Commit in the worktree with a conventional prefix; end every commit message with the trailer: `Assisted-by: Claude (Fable 5)`
- Worktree quirks: EnterWorktree branches from **origin/main** — `git reset --hard main` immediately after entering; fresh worktrees need `uv sync` and `npm ci` in `web/` before `make coverage`.

## Non-goals (deferred with reasons)

- Tightening `test_monitor_e2e.py`'s `(0, -2, 130)` SIGINT tolerance (`todo/chaos-realsignal-followups.md` §2) — needs bed repetition evidence; this lane provides the venue to gather it later, the tightening itself is a 3-line follow-on.
- Loopback-sshd orphan on worker SIGKILL (§3) — tier 3 does not copy the loopback fixture; the documented-cleanup requirement triggers only when the pattern spreads.
- Changing `_confirm_recovered`'s budget or success criteria (`todo/chaos-reboot-followups.md` §3/§4) — the reboot scenarios here *observe and record* real down/up/recovery timings in their logs; semantic changes come after that data exists.
- Docker scenarios, daemon-restart, docker-analog reboot — Plan 5, per the spec's decomposition.
- `clean_remote_gcda`'s unconditional rebuild (`todo/chaos-teardown-followups.md` §2) — standalone fix, not chaos-lane material.

## File Structure

| File | Role in this plan |
| --- | --- |
| `pyproject.toml` | Task 1: register the `chaos` marker |
| `noxfile.py` | Task 1: `chaos` + `chaos_embedded` sessions (positive selectors) |
| `Makefile` | Task 1: `chaos` + `chaos-embedded` legs; `and not chaos` on `stability-unix`/`stability-embedded`; help text |
| `tests/unit/test_tier_marker_invariants.py` | Task 1: G5 (chaos modules double-stamped) + G6 (stability legs exclude chaos) guards |
| `docs/contributing.md` | Task 1: chaos row in the regression-test-categories table |
| `tests/e2e/chaos/__init__.py` (new) | Task 2: package marker |
| `tests/e2e/chaos/_bed.py` (new) | Task 2: leased bed target + probe-host helpers |
| `tests/e2e/chaos/conftest.py` (new) | Task 2: `chaos_bed` fixture; Task 3: BedHygiene bracket; Task 4: seed report header |
| `tests/e2e/chaos/test_harness.py` (new) | Task 2: lane certification (clean run on leased host, probe round-trip) |
| `tests/_fixtures/bed_hygiene.py` (new) | Task 3: snapshot/diff oracle |
| `tests/unit/test_bed_hygiene.py` (new) | Task 3: pure diff/parse contract on canned outputs |
| `tests/e2e/chaos/_seed.py` (new) | Task 4: seeded offset helper |
| `tests/unit/test_chaos_seed.py` (new) | Task 4: reproducibility contract |
| `tests/e2e/chaos/test_session_chaos.py` (new) | Task 5: seeded interrupt, SIGINT-immune remote, nohup characterization |
| `tests/e2e/chaos/test_connection_drop.py` (new) | Task 6: hop-routed session vs port-22 blackhole |
| `src/otto/host/transfer/nc.py` | Task 7: GET-path cancel-time listener reap under `compensate()` |
| `tests/unit/host/transfer/test_nc_cancel.py` | Task 7: tier-1 reap test (mirror of the put-path's) |
| `tests/e2e/chaos/test_transfer_chaos.py` (new) | Task 7: mid-stream interrupt both directions, SIGKILL characterization |
| `tests/_fixtures/tunnel_bed.py` | Task 8: `PORT_BLOCKS` += the chaos block (15200-15299) |
| `tests/e2e/chaos/test_tunnel_link_chaos.py` (new) | Task 8: launch-window interrupt, rollback-window interrupt, SIGKILL + recovery reconciliation |
| `tests/e2e/chaos/test_reboot_chaos.py` (new) | Task 9: happy-path reboot, reboot-under-command, reboot×tunnel, reboot×link |
| `tests/e2e/chaos/test_console_chaos.py` (new) | Task 10: console-client death mid-session (zephyr, opt-in leg) |

---

### Task 1: Lane plumbing — marker, nox sessions, Make legs, guards

The lane must be *provably* excluded from every default gate before a single scenario exists. Mechanism (judgment call 1): chaos modules carry `chaos` + `stability`; existing catch-alls already say `not stability`; the two positive `stability-*` selectors gain `and not chaos`; two new guards pin it all.

**Files:**
- Modify: `pyproject.toml` (markers list, ~line 221)
- Modify: `noxfile.py` (two new sessions after `tests_embedded`)
- Modify: `Makefile` (`stability-unix` ~line 639, `stability-embedded` ~line 658, new `chaos`/`chaos-embedded` targets, `.PHONY` line 12, help printf block ~line 894)
- Modify: `tests/unit/test_tier_marker_invariants.py` (two new guards)
- Modify: `docs/contributing.md` (regression-test-categories table, ~line 298)

**Interfaces:**
- Consumes: `_nox_marker_expressions()` AST scraper and G4's structure in `test_tier_marker_invariants.py`; `PRIMARY_PYTHON`, `_junitxml` in `noxfile.py`; `$(SAY)`, `$(call junitxml,…)`, `STABILITY_*` idioms in `Makefile`.
- Produces: the `chaos` marker name (exact string `chaos`), sessions `chaos` / `chaos_embedded`, make targets `chaos` / `chaos-embedded` — Tasks 2-10 rely on all four.

- [ ] **Step 1: Write the failing guards**

Add to `tests/unit/test_tier_marker_invariants.py`, next to `test_catchall_nox_sessions_exclude_stability` (reuse its helpers; read the module first — `_nox_marker_expressions()` resolves module-level Name references, so the new sessions' `-m` strings will be scraped automatically):

```python
def test_chaos_modules_carry_chaos_and_stability():
    """G5: every module under tests/e2e/chaos declares BOTH markers.

    The lane's exclusion from default gates rides entirely on the module-level
    ``stability`` stamp (every catch-all already says ``not stability``); the
    positive ``chaos`` stamp is what the opt-in lane selects. A module missing
    either silently joins gates it must never join, or silently drops out of
    the lane. AST-scan pytestmark like the e2e resource-marker rule does at
    runtime — this guard runs in the no-VM unit gate, so it fires on every PR.
    """
    chaos_dir = Path(__file__).parents[2] / "tests" / "e2e" / "chaos"
    if not chaos_dir.is_dir():
        pytest.skip("tests/e2e/chaos not created yet")
    offenders = []
    for mod in sorted(chaos_dir.glob("test_*.py")):
        tree = ast.parse(mod.read_text())
        marks = _module_pytestmark_names(tree)
        missing = {"chaos", "stability"} - marks
        if missing:
            offenders.append(f"{mod.name}: missing {sorted(missing)}")
    assert not offenders, "chaos modules missing required markers:\n  " + "\n  ".join(offenders)


def test_stability_make_legs_exclude_chaos():
    """G6: the positive stability selectors must not co-select the chaos lane.

    ``stability-unix`` (``stability and integration and not embedded and not
    hops``) and ``stability-embedded`` (``stability and embedded``) would both
    match a double-stamped chaos module; chaos scenarios reboot and blackhole
    the bed, so riding a stability soak would wreck it mid-run. G4 covers
    noxfile catch-alls; this covers the two Makefile legs.
    """
    makefile = (Path(__file__).parents[2] / "Makefile").read_text()
    for leg in ("stability-unix", "stability-embedded"):
        recipe = makefile.split(f"\n{leg}:", 1)[1].split("\n\n", 1)[0]
        m_exprs = re.findall(r'-m\s+"([^"]+)"', recipe)
        assert m_exprs, f"{leg}: no -m expression found (recipe reshaped? update G6)"
        offenders = [e for e in m_exprs if "not chaos" not in e]
        assert not offenders, f"{leg}: -m expressions missing 'not chaos': {offenders}"
```

`_module_pytestmark_names(tree)` is a small helper to add beside the guards: walk `ast.Assign` nodes for a target named `pytestmark`, collect every `pytest.mark.<name>` attribute (plain `pytest.mark.chaos`) and call (`pytest.mark.timeout(300)`, `pytest.mark.xdist_group("chaos_lane")`) form:

```python
def _module_pytestmark_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "pytestmark" for t in node.targets
        )):
            continue
        for mark in ast.walk(node):
            if isinstance(mark, ast.Attribute) and isinstance(mark.value, ast.Attribute):
                if isinstance(mark.value.value, ast.Name) and mark.value.value.id == "pytest":
                    if mark.value.attr == "mark":
                        names.add(mark.attr)
    return names
```

- [ ] **Step 2: Run the guards to verify they fail**

Run: `uv run pytest tests/unit/test_tier_marker_invariants.py -k "chaos" -v` (foreground, timeout 600000)
Expected: G5 SKIPs (directory absent — it goes red the moment Task 2 creates a module without markers, which is the protection we want); G6 FAILs (`stability-unix` has no `not chaos`).

- [ ] **Step 3: Register the marker, amend the legs, add sessions and targets**

`pyproject.toml`, in `markers = [...]` after the `stability` line:

```toml
    "chaos: tier-3 chaos-lane scenario (tests/e2e/chaos) — bed-hostile (reboots, SSH blackholes), excluded from every default gate like stability; opt-in via make chaos / make chaos-embedded",
```

`noxfile.py`, after `tests_embedded` (positive selectors — G4-exempt; single pinned interpreter like `dashboard`):

```python
@nox_uv.session(python=[PRIMARY_PYTHON], uv_groups=["dev"])
def chaos(session: nox.Session) -> None:
    """Tier-3 chaos lane, unix legs (opt-in; run via `make chaos`).

    Bed-hostile by design: scenarios interrupt/SIGKILL real otto subprocesses
    mid-flight on a leased veggies host, blackhole SSH over the data-plane
    link, and soft-reboot the leased host. Requires the lab VMs and EXCLUSIVE
    bed use — never co-run with any other bed lane. No coverage: these runs
    exist to hunt teardown leaks, not to measure lines.
    """
    session.run(
        "pytest",
        "tests/e2e/chaos",
        "-m",
        "chaos and not embedded",
        "--no-cov",
        "-p",
        "no:cacheprovider",
        _junitxml(session, "nox-chaos"),
        *session.posargs,
    )


@nox_uv.session(python=[PRIMARY_PYTHON], uv_groups=["dev"])
def chaos_embedded(session: nox.Session) -> None:
    """Tier-3 chaos lane, zephyr console leg (opt-in; run via `make chaos-embedded`).

    Separate from `chaos` on purpose: the console-client-death scenario
    reproduces the trigger class of the 2026-08-01 fat-board wedge, and a
    failure can require a manual zephyr bed restart. Run it deliberately,
    never as a rider.
    """
    session.run(
        "pytest",
        "tests/e2e/chaos",
        "-m",
        "chaos and embedded",
        "--no-cov",
        "-p",
        "no:cacheprovider",
        _junitxml(session, "nox-chaos-embedded"),
        *session.posargs,
    )
```

`Makefile`: amend the two positive stability selectors —

- `stability-unix` recipe: `-m "stability and integration and not embedded and not hops"` → `-m "stability and integration and not embedded and not hops and not chaos"`
- `stability-embedded` recipe: `-m "stability and embedded"` → `-m "stability and embedded and not chaos"`

then add the two lane targets after `stability-embedded` (before the `stability` aggregator), following the spec's `make chaos → nox -s chaos` wiring:

```make
chaos: ## Tier-3 chaos lane, unix legs: interrupt/SIGKILL/reboot scenarios on a leased bed host. Requires lab VMs and EXCLUSIVE bed use (never co-run with other bed lanes). JUnit XML in reports/junit/nox-chaos/.
	@$(SAY) "pytest chaos: tier-3 scenarios on the live bed (unix legs, leak detector on)"
	@OTTO_DETECT_ASYNCIO_LEAKS=1 uv run nox -s chaos

chaos-embedded: ## Tier-3 chaos lane, zephyr console leg (console-client-death). Can wedge a board — run deliberately; a failure may need a zephyr bed restart. JUnit XML in reports/junit/nox-chaos-embedded/.
	@$(SAY) "pytest chaos: zephyr console scenarios (leak detector on)"
	@OTTO_DETECT_ASYNCIO_LEAKS=1 uv run nox -s chaos_embedded
```

Add `chaos chaos-embedded` to the `.PHONY` line, and a help row in the Testing printf block next to `stability-*`:

```make
	@printf '  \033[36m%-30s\033[0m %s\n' 'chaos / chaos-embedded' 'tier-3 chaos lane (opt-in, bed-hostile; unix legs · zephyr console)'
```

Verify the `repeat` target's pytest invocation is path-restricted to `tests/unit` (read the recipe at ~line 696); if it is not, add `and not chaos`-carrying restriction is NOT the fix — instead extend G6's leg list with `repeat`. (Expected: it is `tests/unit`-scoped and needs nothing.)

`docs/contributing.md`: add a row to the regression-test-categories table (~line 298), matching the surrounding style:

```markdown
| Chaos lane (tier 3, opt-in) | interrupt/SIGKILL/reboot scenarios + BedHygiene | `make chaos` / `make chaos-embedded` | leased veggies host (· zephyr console) |
```

- [ ] **Step 4: Run the guards and the sessions' collection to verify**

Run (each foreground, timeout 600000):
- `uv run pytest tests/unit/test_tier_marker_invariants.py -v` — all guards PASS (G5 skips, G6 passes).
- `uv run pytest tests/unit tests/e2e -m "not integration and not embedded and not stability and not browser" --collect-only -q --no-cov -p no:cacheprovider | tail -3` — the hostless slice still collects (sanity: marker registration broke nothing).
- `uv run nox -s chaos -- --collect-only` — expected to exit non-zero with "no tests ran / collected 0 items" (the suite doesn't exist yet); that is fine and expected at this stage — record it, don't chase it.

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml noxfile.py Makefile tests/unit/test_tier_marker_invariants.py docs/contributing.md
git commit -m "feat(chaos): opt-in tier-3 lane plumbing — chaos marker, nox sessions, make legs, exclusion guards"
```

---

### Task 2: Bed harness — leased target, probe host, lane certification

Reuses tier-2's driver and target machinery against a leased live-bed host, closing `todo/chaos-realsignal-followups.md` §4 (the untested `make_bed_target` prototype) by making it the lane's spine. No scenario yet — this task proves the harness: a clean otto run on the leased host exits 0, phase markers appear, the probe round-trips.

**Files:**
- Create: `tests/e2e/chaos/__init__.py` (empty)
- Create: `tests/e2e/chaos/_bed.py`
- Create: `tests/e2e/chaos/conftest.py`
- Create: `tests/e2e/chaos/test_harness.py`

**Interfaces:**
- Consumes: `tests.integration.chaos._target.make_bed_target / ChaosTarget / probe`; `tests.integration.chaos._driver.spawn_otto / OttoProc / BANNER`; `tests._fixtures._host_pool.lease_unix_host`; `tests._fixtures.tunnel_bed.build_bed_host / assert_reachable`; `tests._fixtures.labdata.host_data`.
- Produces: fixture `chaos_bed` (session-scoped) yielding a `ChaosBed` dataclass with fields `element: str`, `target: ChaosTarget`, `ip: str`; helper `probe_host()` context manager yielding a fresh `UnixHost` for oracle exec. Tasks 3-9 consume both. The module-level `pytestmark` idiom below is the template every scenario module copies.

- [ ] **Step 1: Write `_bed.py`**

```python
"""Leased live-bed target for the tier-3 chaos lane.

The lane's spine: lease ONE free veggies host (flock, cross-worker), prove it
reachable (fail LOUD, host-named, never skip), and expose it both as a
``ChaosTarget`` (for the otto subprocess under test) and as fresh probe
``UnixHost``s (for oracle exec over an independent connection). The tier-2
driver (`tests/integration/chaos/_driver.py`) is reused unchanged — signals
only ever go to the LOCAL otto subprocess; the bed host just runs its remote
commands.
"""

import asyncio
import contextlib
import dataclasses
from collections.abc import Iterator
from pathlib import Path

from tests._fixtures._host_pool import lease_unix_host
from tests._fixtures.labdata import host_data
from tests._fixtures.tunnel_bed import assert_reachable, build_bed_host
from tests.integration.chaos._target import ChaosTarget, make_bed_target


@dataclasses.dataclass(frozen=True)
class ChaosBed:
    element: str  # lab element name, e.g. "tomato"
    ip: str  # management ip from tech1/lab.json
    target: ChaosTarget  # aim the otto subprocess here


@contextlib.contextmanager
def leased_bed(lock_dir: Path) -> Iterator[ChaosBed]:
    """Lease a free veggies host; probe reachability; yield the bed handle."""
    with lease_unix_host(lock_dir) as element:
        ip = host_data(element)["ip"]
        asyncio.run(assert_reachable(element, ip))
        yield ChaosBed(element=element, ip=ip, target=make_bed_target(element))


@contextlib.asynccontextmanager
async def probe_host(element: str):
    """Fresh, independent ``UnixHost`` for oracle exec; always closed."""
    host = build_bed_host(element)
    try:
        yield host
    finally:
        await host.close()


def run_probe(element: str, coro_factory):
    """Run ``await coro_factory(host)`` on a fresh probe host in a fresh loop.

    Sync bridge for subprocess-driving (sync) scenario tests — mirrors the
    tier-2 suite's ``probe()`` shape but with a full UnixHost, so oracles can
    use ``exec``/``run`` semantics (sudo, timeouts, QUIET logging) instead of
    raw asyncssh.
    """

    async def _go():
        async with probe_host(element) as host:
            return await coro_factory(host)

    return asyncio.run(_go())
```

Note for the implementer: `assert_reachable`'s exact signature/behavior is in `tests/_fixtures/tunnel_bed.py:54-67`; `contextlib.asynccontextmanager` exists on 3.10. If `tests/integration/chaos` turns out not to be importable as a package from `tests/e2e` (it is expected to be — `pythonpath = ["src", "."]` and both trees carry `__init__.py`), STOP and report; do not copy the driver.

- [ ] **Step 2: Write `conftest.py` (harness half — Tasks 3/4 extend it)**

```python
"""tests/e2e/chaos — tier-3 chaos lane fixtures.

Session-scoped bed lease: the whole lane runs against ONE leased veggies
host (plus named peers in the inherently two-host scenarios), serialized by
xdist_group("chaos_lane"). Fail-loud on host-down, never skip.
"""

import gc
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.e2e.chaos._bed import ChaosBed, leased_bed

_FD_TOLERANCE = 4


@pytest.fixture(scope="session")
def chaos_bed(tmp_path_factory: pytest.TempPathFactory) -> Iterator[ChaosBed]:
    lock_dir = tmp_path_factory.getbasetemp().parent
    with leased_bed(lock_dir) as bed:
        yield bed


@pytest.fixture(autouse=True)
def _fd_watermark() -> Iterator[None]:
    """Local FD bracket per test (same shape as tunnel_stability's)."""
    gc.collect()
    before = len(list(Path("/proc/self/fd").iterdir()))
    yield
    gc.collect()
    after = len(list(Path("/proc/self/fd").iterdir()))
    if after > before + _FD_TOLERANCE:
        gc.collect()
        after = len(list(Path("/proc/self/fd").iterdir()))
    assert after <= before + _FD_TOLERANCE, f"fd leak: {before} -> {after}"
```

- [ ] **Step 3: Write the certification tests**

`tests/e2e/chaos/test_harness.py`:

```python
"""Lane certification: the leased-bed harness works before any chaos does.

Proves the previously-untested bed-target path end to end
(todo/chaos-realsignal-followups.md §4): otto subprocess -> leased veggies
host over real SSH, phase marker in verbose.log, clean exit, probe oracle
round-trips on an independent connection.
"""

import re

import pytest

from otto.logger.mode import LogMode
from tests.e2e.chaos._bed import run_probe
from tests.integration.chaos._driver import spawn_otto

pytestmark = [
    pytest.mark.chaos,
    pytest.mark.stability,
    pytest.mark.integration,
    pytest.mark.xdist_group("chaos_lane"),
    pytest.mark.timeout(300),
]


def test_clean_run_on_leased_host(chaos_bed, tmp_path):
    cmd = "echo CHAOS-CERT"
    p = spawn_otto(
        ["host", chaos_bed.target.host_id, "run", cmd],
        xdir=tmp_path,
        target=chaos_bed.target,
    )
    p.wait_for_log(re.escape(f"| {cmd}"), timeout=120.0)  # phase: command running
    rc = p.wait(timeout=120.0)
    assert rc == 0, p.stderr_text()
    p.assert_no_process_group()


def test_probe_oracle_round_trips(chaos_bed):
    async def _ping(host):
        return (await host.exec("echo CHAOS-PROBE", timeout=30, log=LogMode.QUIET)).value

    assert "CHAOS-PROBE" in (run_probe(chaos_bed.element, _ping) or "")
```

If the clean run fails at the reservation gate (the `tests/repo_e2e` SUT declaring a `[reservations]` backend), the fix is to append `"-R"` to the argv in `spawn_otto` calls for bed targets — make that decision once, here, and record it in the module docstring; the loopback tier-2 suite needs no such flag (its SUT has no backend → silent no-op).

- [ ] **Step 4: Run the certification (bed required; controller confirms the bed is free first)**

Run: `uv run pytest tests/e2e/chaos/test_harness.py -m "chaos" -p no:cacheprovider --no-cov -v` (foreground, timeout 600000)
Expected: 2 PASSED. Also verify exclusion holds: `uv run pytest tests/e2e/chaos --collect-only -q -m "not stability" --no-cov -p no:cacheprovider | tail -2` → 0 selected.

- [ ] **Step 5: Run the full gate and commit**

Gates (each foreground, timeout 600000): `uv run pytest tests/unit/test_tier_marker_invariants.py -v` (G5 now live and green) → `uv run nox -s lint` → `make typecheck-python` → `make coverage`.

```bash
git add tests/e2e/chaos
git commit -m "feat(chaos): tier-3 bed harness — leased target, probe oracle, lane certification"
```

---

### Task 3: BedHygiene oracle

Consolidates the piecemeal checks (tunnel sentinels, impair sentinels + qdiscs, nc listeners, `/tmp/otto*` staging, shell-history digest) into one snapshot/diff fixture that brackets every scenario, pre-existing dirt snapshotted out, failure naming host + leftovers — the `tunnel_bed.py` pattern generalized. Pure parsing/diff logic is unit-tested with canned outputs (no bed).

**Files:**
- Create: `tests/_fixtures/bed_hygiene.py`
- Create: `tests/unit/test_bed_hygiene.py`
- Modify: `tests/e2e/chaos/conftest.py` (autouse per-test bracket)

**Interfaces:**
- Consumes: `otto.tunnel.discovery.DISCOVERY_PS_COMMAND / parse_process_discovery`; `otto.link.sentinel.IMPAIR_PS_COMMAND / parse_impair_ps`; `otto.logger.mode.LogMode`; `tests.e2e.chaos._bed.probe_host`.
- Produces: `HygieneSnapshot` (frozen dataclass), `async snapshot_host(host) -> HygieneSnapshot`, `diff_snapshots(before, after) -> list[str]`, `format_hygiene_report(element, leftovers) -> str`, fixture `_bed_hygiene_bracket`. Tasks 5-10 rely on the bracket firing autouse; Task 9 additionally calls `snapshot_host`/`diff_snapshots` directly around reboots.

- [ ] **Step 1: Write the failing unit test for diff/format semantics**

`tests/unit/test_bed_hygiene.py`:

```python
"""Contract tests for the BedHygiene diff — canned probe outputs, no bed.

The oracle's one hard rule: PRE-EXISTING DIRT IS SNAPSHOTTED OUT. A leftover
is a line present after that was absent before; something dirty going in must
never be blamed on the scenario (2026-07-21 misattribution class), and must
never mask a NEW leftover of the same kind either.
"""

from tests._fixtures.bed_hygiene import (
    HygieneSnapshot,
    diff_snapshots,
    format_hygiene_report,
)


def _snap(**over):
    base = dict(
        tunnel_procs=frozenset(),
        impair_timers=frozenset(),
        nc_listeners=frozenset(),
        qdiscs={"eth2": "qdisc noqueue 0: root refcnt 2"},
        staging=frozenset(),
        history_digest="abc  -",
    )
    base.update(over)
    return HygieneSnapshot(**base)


def test_identical_snapshots_diff_empty():
    assert diff_snapshots(_snap(), _snap()) == []


def test_new_listener_named_old_listener_ignored():
    before = _snap(nc_listeners=frozenset({"111 nc -l -w 30 9000"}))
    after = _snap(nc_listeners=frozenset({"111 nc -l -w 30 9000", "222 nc -l -w 30 9001"}))
    leftovers = diff_snapshots(before, after)
    assert len(leftovers) == 1
    assert "222" in leftovers[0] and "111" not in leftovers[0]


def test_qdisc_change_reported_with_device_and_both_states():
    before = _snap()
    after = _snap(qdiscs={"eth2": "qdisc prio 1: root refcnt 2 bands 11"})
    leftovers = diff_snapshots(before, after)
    assert len(leftovers) == 1
    assert "eth2" in leftovers[0] and "prio" in leftovers[0]


def test_history_digest_change_reported():
    leftovers = diff_snapshots(_snap(), _snap(history_digest="def  -"))
    assert len(leftovers) == 1 and "history" in leftovers[0]


def test_report_names_host_and_lists_leftovers():
    report = format_hygiene_report("tomato", ["eth2: qdisc changed", "new nc listener"])
    assert "tomato" in report
    assert "eth2: qdisc changed" in report and "new nc listener" in report
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/test_bed_hygiene.py -v` (foreground, timeout 600000)
Expected: FAIL — module `tests._fixtures.bed_hygiene` does not exist.

- [ ] **Step 3: Write the oracle**

`tests/_fixtures/bed_hygiene.py`:

```python
"""BedHygiene: per-host snapshot/diff oracle for the tier-3 chaos lane.

Consolidates the piecemeal leftover checks (chaos spec, Tier 3): otto-tagged
tunnel daemons, impair timers, nc listeners, tc qdisc state, /tmp/otto*
staging entries, and the shell-history digest — probed over a FRESH
connection, snapshot before / diff after, failure naming the host and each
leftover. Pre-existing dirt is snapshotted out (the tunnel_bed.py pattern):
a dirty bed going in is never blamed on the scenario, and never masks a NEW
leftover of the same kind.

Docker probes deliberately absent — they ride Plan 5 with the docker
scenarios. Local-side leaks (transports, loops, fds) stay with the existing
repo-wide detectors; this module is remote-state only.
"""

import dataclasses

from otto.link.sentinel import IMPAIR_PS_COMMAND, parse_impair_ps
from otto.logger.mode import LogMode
from otto.tunnel.discovery import DISCOVERY_PS_COMMAND, parse_process_discovery

# Both bed netdevs matter: eth2 carries the declared data-plane link the
# connection-drop scenario impairs; eth1 (mgmt) must stay impairment-free
# ALWAYS — a qdisc appearing there means a placement guard failed.
_QDISC_DEVS = ("eth1", "eth2")
_NC_LISTENER_PROBE = 'pgrep -af "nc -l" | grep -v pgrep | grep -v "$$" || true'
_STAGING_PROBE = "ls -d /tmp/otto-* /tmp/otto_* 2>/dev/null || true"
_HISTORY_PROBE = "cat ~/.bash_history 2>/dev/null | sha256sum || true"
_PROBE_TIMEOUT = 30


@dataclasses.dataclass(frozen=True)
class HygieneSnapshot:
    tunnel_procs: frozenset  # str lines: "pid tunnel-id" from sentinel parse
    impair_timers: frozenset  # str lines: "pid link-id netdev [selector]"
    nc_listeners: frozenset  # raw pgrep lines
    qdiscs: dict  # netdev -> raw `tc qdisc show dev X` text (stripped)
    staging: frozenset  # /tmp/otto* entries
    history_digest: str  # sha256 line of ~/.bash_history


async def snapshot_host(host) -> HygieneSnapshot:
    """Probe one host over its (fresh) connection; never mutates anything."""

    async def _out(cmd: str) -> str:
        return ((await host.exec(cmd, timeout=_PROBE_TIMEOUT, log=LogMode.QUIET)).value or "").strip()

    tunnel_raw = await _out(DISCOVERY_PS_COMMAND)
    impair_raw = await _out(IMPAIR_PS_COMMAND)
    qdiscs = {}
    for dev in _QDISC_DEVS:
        qdiscs[dev] = await _out(f"tc qdisc show dev {dev} 2>/dev/null || true")
    return HygieneSnapshot(
        tunnel_procs=frozenset(
            f"{o.pid} {o.parsed.tunnel_id}" for o in parse_process_discovery(tunnel_raw)
        ),
        impair_timers=frozenset(
            f"{t.pid} {t.link_id} {t.netdev} {t.selector or ''}".strip()
            for t in parse_impair_ps(impair_raw)
        ),
        nc_listeners=frozenset(line for line in (await _out(_NC_LISTENER_PROBE)).splitlines() if line),
        qdiscs=qdiscs,
        staging=frozenset(line for line in (await _out(_STAGING_PROBE)).splitlines() if line),
        history_digest=await _out(_HISTORY_PROBE),
    )


def diff_snapshots(before: HygieneSnapshot, after: HygieneSnapshot) -> list:
    """Human-readable leftover lines; empty means clean. New-only semantics."""
    leftovers = []
    for label, b, a in (
        ("otto-tunnel daemon", before.tunnel_procs, after.tunnel_procs),
        ("otto-impair timer", before.impair_timers, after.impair_timers),
        ("nc listener", before.nc_listeners, after.nc_listeners),
        ("staging entry", before.staging, after.staging),
    ):
        for item in sorted(a - b):
            leftovers.append(f"new {label}: {item}")
    for dev in sorted(after.qdiscs):
        if after.qdiscs.get(dev, "") != before.qdiscs.get(dev, ""):
            leftovers.append(
                f"{dev}: qdisc changed: before={before.qdiscs.get(dev, '')!r} "
                f"after={after.qdiscs.get(dev, '')!r}"
            )
    if after.history_digest != before.history_digest:
        leftovers.append(
            f"shell history digest changed ({before.history_digest.split()[0][:12]} -> "
            f"{after.history_digest.split()[0][:12]}) — suppression leak?"
        )
    return leftovers


def format_hygiene_report(element: str, leftovers: list) -> str:
    lines = "\n  ".join(leftovers)
    return (
        f"BedHygiene: scenario left {element} dirty ({len(leftovers)} leftover(s)):\n"
        f"  {lines}\n"
        f"Pre-existing state was snapshotted out — these appeared DURING the scenario."
    )
```

Implementer notes: verify `parse_process_discovery` returns objects with `.pid`/`.parsed.tunnel_id` and `parse_impair_ps` with `.pid/.link_id/.netdev/.selector` (`src/otto/tunnel/discovery.py:35-41`, `src/otto/link/sentinel.py:86-93`) — adjust attribute access to the real shapes, keeping the snapshot's string form stable. The history digest uses the *exec* channel deliberately (non-interactive bash keeps no history — the measurement cannot pollute the measurand, `test_shell_history_e2e.py:112-123`); bash flushes history at session exit, so the bracket naturally samples after otto's sessions are gone (the otto subprocess has exited before the after-snapshot).

- [ ] **Step 4: Run unit tests to verify green**

Run: `uv run pytest tests/unit/test_bed_hygiene.py -v` (foreground, timeout 600000)
Expected: 5 PASSED.

- [ ] **Step 5: Wire the autouse bracket into the chaos conftest**

Append to `tests/e2e/chaos/conftest.py`:

```python
@pytest.fixture(autouse=True)
def _bed_hygiene_bracket(request, chaos_bed):
    """Snapshot/diff the leased host around EVERY scenario (spec: BedHygiene).

    Sync fixture with its own asyncio.run per side, over a fresh probe host
    each time — the scenario's own connections are dead by design when the
    after-side runs. Scenarios that dirty a PEER (tunnel/link/reboot tasks)
    add their own peer-side brackets; this fixture owns the leased host only.
    Opt out (reboot module's mid-reboot cases where the after-probe would
    race the boot) with @pytest.mark.no_hygiene_bracket + a manual bracket.
    """
    import asyncio

    from tests._fixtures.bed_hygiene import (
        diff_snapshots,
        format_hygiene_report,
        snapshot_host,
    )
    from tests.e2e.chaos._bed import probe_host

    if request.node.get_closest_marker("no_hygiene_bracket"):
        yield
        return

    async def _snap():
        async with probe_host(chaos_bed.element) as host:
            return await snapshot_host(host)

    before = asyncio.run(_snap())
    yield
    after = asyncio.run(_snap())
    leftovers = diff_snapshots(before, after)
    assert not leftovers, format_hygiene_report(chaos_bed.element, leftovers)
```

Register the opt-out marker in `pyproject.toml` markers:

```toml
    "no_hygiene_bracket: chaos-lane test that manages its own BedHygiene bracket (reboot races the autouse after-probe)",
```

- [ ] **Step 6: Prove the bracket live on the bed (controller confirms bed free)**

Run: `uv run pytest tests/e2e/chaos/test_harness.py -p no:cacheprovider --no-cov -v` (foreground, timeout 600000)
Expected: 2 PASSED (bracket ran clean around both). Then prove the bracket can FAIL: temporarily start a fake listener via the probe (`run_probe(element, lambda h: h.exec("nohup nc -l -w 60 15299 >/dev/null 2>&1 & echo started", ...))`) inside a scratch copy of the harness test, observe the bracket fail naming the listener, then delete the scratch test and reap the listener (`pkill -f 'nc -l -w 60 15299'` via probe). This is the proven-red requirement for an oracle whose whole job is failing.

- [ ] **Step 7: Full gate and commit**

Gates foreground timeout 600000: `uv run nox -s lint` → `make typecheck-python` → `make coverage`.

```bash
git add tests/_fixtures/bed_hygiene.py tests/unit/test_bed_hygiene.py tests/e2e/chaos/conftest.py pyproject.toml
git commit -m "feat(chaos): BedHygiene snapshot/diff oracle bracketing every tier-3 scenario"
```

---

### Task 4: Seeded injection offsets

Every randomized timing in tier 3 must reproduce from a printed seed (spec: "Randomized timing never appears outside tier 3, and every tier-3 failure reproduces from its printed seed"). No repo precedent exists — this defines it.

**Files:**
- Create: `tests/e2e/chaos/_seed.py`
- Create: `tests/unit/test_chaos_seed.py`
- Modify: `tests/e2e/chaos/conftest.py` (fixture + report header)

**Interfaces:**
- Consumes: nothing project-specific.
- Produces: `resolve_seed() -> int`, `offset_in(rng, lo, hi) -> float`, fixture `chaos_rng` yielding a seeded `random.Random` (printing the seed). Tasks 5-9 draw every injection offset from `chaos_rng` — never from bare `random`.

- [ ] **Step 1: Write the failing unit test**

`tests/unit/test_chaos_seed.py`:

```python
"""Seed contract: reproducible when pinned, printed always, bounded offsets."""

import random

from tests.e2e.chaos._seed import offset_in, resolve_seed


def test_seed_env_pin_wins(monkeypatch):
    monkeypatch.setenv("OTTO_CHAOS_SEED", "12345")
    assert resolve_seed() == 12345


def test_unpinned_seeds_vary_and_are_ints(monkeypatch):
    monkeypatch.delenv("OTTO_CHAOS_SEED", raising=False)
    seeds = {resolve_seed() for _ in range(8)}
    assert all(isinstance(s, int) for s in seeds)
    assert len(seeds) > 1  # os.urandom-backed — collisions across 8 draws mean it's broken


def test_same_seed_same_offsets():
    a = [offset_in(random.Random(99), 0.0, 5.0) for _ in range(20)]
    b = [offset_in(random.Random(99), 0.0, 5.0) for _ in range(20)]
    assert a == b
    assert all(0.0 <= x <= 5.0 for x in a)
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/test_chaos_seed.py -v` (foreground, timeout 600000)
Expected: FAIL — module does not exist.

- [ ] **Step 3: Write the helper and fixture**

`tests/e2e/chaos/_seed.py`:

```python
"""Seed-reproducible injection offsets (chaos spec, Tier 3).

The ONLY sanctioned randomness in the chaos lane. Every scenario draws its
injection offset from the per-test ``chaos_rng`` fixture; the seed prints on
every run as ``chaos seed: N (reproduce with OTTO_CHAOS_SEED=N)`` — captured
output surfaces it on failure, which is the reproduce path.
"""

import os
import random

_ENV = "OTTO_CHAOS_SEED"


def resolve_seed() -> int:
    pinned = os.environ.get(_ENV)
    if pinned:
        return int(pinned)
    return int.from_bytes(os.urandom(4), "big")


def offset_in(rng: random.Random, lo: float, hi: float) -> float:
    """Uniform offset within a phase window [lo, hi] seconds."""
    return rng.uniform(lo, hi)
```

Append to `tests/e2e/chaos/conftest.py`:

```python
@pytest.fixture
def chaos_rng():
    """Per-test seeded RNG; the printed seed is the reproduce handle."""
    import random as _random

    from tests.e2e.chaos._seed import resolve_seed

    seed = resolve_seed()
    print(f"\nchaos seed: {seed} (reproduce with OTTO_CHAOS_SEED={seed})")
    return _random.Random(seed)
```

- [ ] **Step 4: Run to verify green, then commit**

Run: `uv run pytest tests/unit/test_chaos_seed.py -v` (foreground, timeout 600000) → 3 PASSED. Lint gate: `uv run nox -s lint`.

```bash
git add tests/e2e/chaos/_seed.py tests/unit/test_chaos_seed.py tests/e2e/chaos/conftest.py
git commit -m "feat(chaos): seed-reproducible injection offsets for the tier-3 lane"
```

---

### Task 5: Host/session scenarios — seeded interrupt, SIGINT-immune remote, nohup characterization

The first real scenarios. Reuses the tier-2 driver; adds seeded timing, BedHygiene bracketing (autouse), and the deadline path (a remote command that ignores SIGINT drives otto to its teardown-deadline force path). Also characterizes the nohup survivor (`todo/chaos-realsignal-followups.md` §5): a `nohup`'d remote command survives graceful teardown untouched — that is otto's real contract, asserted rather than assumed.

**Files:**
- Create: `tests/e2e/chaos/test_session_chaos.py`

**Interfaces:**
- Consumes: `spawn_otto`, `OttoProc`, `BANNER` (tier-2 driver); `chaos_bed`, `chaos_rng`, `run_probe` (Task 2/4); the autouse `_bed_hygiene_bracket` (Task 3).
- Produces: nothing downstream — leaf scenario module.

- [ ] **Step 1: Write the scenario module (three tests)**

```python
"""Host/session chaos: interrupt a live remote command at a seeded offset
inside the command-running phase window; force via a SIGINT-immune remote;
characterize the nohup survivor. BedHygiene (autouse) asserts the leased host
is left clean; here we add the remote-reaped / remote-survives assertions the
hygiene diff cannot express (a foreground child reaped by PTY-HUP is gone
either way; a nohup'd one is SUPPOSED to remain, so it is not a leftover).
"""

import re
import time

import pytest

from tests.e2e.chaos._bed import run_probe
from tests.integration.chaos._driver import BANNER, spawn_otto
from otto.logger.mode import LogMode

pytestmark = [
    pytest.mark.chaos,
    pytest.mark.stability,
    pytest.mark.integration,
    pytest.mark.xdist_group("chaos_lane"),
    pytest.mark.timeout(300),
]

# Unique per-test marker string so the probe never matches a sibling's child.
_SLEEP = "sleep 300 # otto-chaos-session"


def _remote_pids(element: str, needle: str) -> list:
    # bracket-trick first char so the probe's own shell never self-matches
    pat = f"[{needle[0]}]{needle[1:]}"

    async def _find(host):
        out = (await host.exec(f"pgrep -af '{pat}' || true", timeout=30, log=LogMode.QUIET)).value or ""
        return [ln for ln in out.splitlines() if ln.strip()]

    return run_probe(element, _find)


def test_seeded_sigint_mid_command_cleans_up(chaos_bed, chaos_rng, tmp_path):
    from tests.e2e.chaos._seed import offset_in

    p = spawn_otto(
        ["host", chaos_bed.target.host_id, "run", _SLEEP, "--timeout", "300"],
        xdir=tmp_path,
        target=chaos_bed.target,
    )
    p.wait_for_log(re.escape(f"| {_SLEEP}"), timeout=120.0)  # phase: command running
    time.sleep(offset_in(chaos_rng, 0.0, 2.0))  # the ONE deliberate sleep: seeded injection offset
    p.signal(2)  # SIGINT
    p.wait_for_stderr(BANNER, timeout=15.0)  # phase: teardown running
    rc = p.wait(timeout=60.0)
    assert rc == 130, p.stderr_text()
    p.assert_no_process_group()
    # foreground remote child reaped via PTY HUP
    assert not _remote_pids(chaos_bed.element, _SLEEP), "remote foreground command not reaped"


def test_sigint_immune_remote_hits_deadline_force(chaos_bed, tmp_path):
    """A remote command trapping SIGINT drives otto to its teardown deadline.

    OTTO_TEARDOWN_DEADLINE small-but-nonzero: graceful teardown starts, the
    remote won't die on the channel's HUP fast enough, deadline fires -> force
    path. Asserts prompt exit on the honest double-outcome contract and no
    LOCAL orphans; the remote trap self-exits on its own timeout.
    """
    trap = "trap '' INT; sleep 300 # otto-chaos-immune"
    p = spawn_otto(
        ["host", chaos_bed.target.host_id, "run", trap, "--timeout", "300"],
        xdir=tmp_path,
        target=chaos_bed.target,
        extra_env={"OTTO_TEARDOWN_DEADLINE": "3"},
    )
    p.wait_for_log(re.escape("| trap"), timeout=120.0)
    p.signal(2)
    p.wait_for_stderr(BANNER, timeout=15.0)
    rc = p.wait(timeout=60.0)
    assert rc in (130, -2), p.stderr_text()  # graceful-in-time OR forced (Plan 3 contract)
    p.assert_no_process_group()


def test_nohup_remote_survives_graceful_teardown(chaos_bed, tmp_path):
    """Characterization (todo/chaos-realsignal-followups.md §5): otto reaps by
    PTY HUP, not by signalling the remote. A nohup'd command has no controlling
    terminal to lose, so it SURVIVES a graceful teardown — documented contract,
    not a leak. Teardown must clean up otto's OWN session state regardless.
    """
    marker = "otto-chaos-nohup"
    survivor = f"nohup sleep 300 >/dev/null 2>&1 & echo LAUNCHED-{marker}"
    p = spawn_otto(
        ["host", chaos_bed.target.host_id, "run", survivor, "--timeout", "300"],
        xdir=tmp_path,
        target=chaos_bed.target,
    )
    p.wait_for_log(f"LAUNCHED-{marker}", timeout=120.0)
    p.signal(15)  # SIGTERM
    rc = p.wait(timeout=60.0)
    assert rc == 143, p.stderr_text()
    p.assert_no_process_group()
    try:
        # The survivor is EXPECTED to be alive — that is the characterized behavior.
        assert _remote_pids(chaos_bed.element, "sleep 300"), (
            "nohup survivor unexpectedly gone — contract changed; update the docstring"
        )
    finally:
        run_probe(chaos_bed.element, lambda h: h.exec("pkill -f 'sleep 300' || true", timeout=30, log=LogMode.QUIET))
```

Implementer note: `_bed_hygiene_bracket` runs autouse around all three. The nohup survivor is reaped in the test's own `finally` BEFORE the after-snapshot runs — otherwise it would (correctly) show as an nc/process leftover. Actually it is a `sleep`, not otto-tagged, so the hygiene diff won't see it; the explicit reap is still required so it does not accumulate across the module. If the deadline test proves flaky on the bed (real SSH channel-close latency vs a 3 s deadline), widen the deadline to 5 s and record it — do not tighten it (live-bed rule).

- [ ] **Step 2: Run on the bed (controller confirms free), then gate**

Run: `uv run pytest tests/e2e/chaos/test_session_chaos.py -p no:cacheprovider --no-cov -v` (foreground, timeout 600000)
Expected: 3 PASSED, each printing its `chaos seed:` line, BedHygiene clean.
Then full gate: `uv run nox -s lint` → `make typecheck-python` → `make coverage`.

- [ ] **Step 3: Commit**

```bash
git add tests/e2e/chaos/test_session_chaos.py
git commit -m "feat(chaos): host/session interrupt scenarios — seeded injection, deadline force, nohup characterization"
```

---

### Task 6: Connection-drop scenario — SSH-port blackhole via port-scoped impairment

Injects a connection drop with otto's own port-scoped link impairment (spec: "blackhole the SSH port"). Per judgment call 4, the drop lands on the carrot↔tomato eth2 data-plane link carrying a hop-routed session — not the management path, which the placement guards rightly refuse. Every impairment carries `--expire` as a self-healing backstop; teardown repairs unconditionally.

**Files:**
- Create: `tests/e2e/chaos/test_connection_drop.py`

**Interfaces:**
- Consumes: `spawn_otto`/`OttoProc`; `chaos_bed`/`chaos_rng`/`run_probe`; the veggies link topology (`carrot_seed:eth2 ↔ tomato_seed:eth2`, link id from `tech1/lab.json`'s `links` block); `otto link impair/repair` CLI.
- Produces: nothing downstream.

- [ ] **Step 1: Write the scenario**

This scenario is inherently two-host and hop-routed, so it does NOT use the leased `chaos_bed` element (which is arbitrary). It pins carrot (hop) + tomato (target) explicitly and marks `hops`. It builds its own tiny SUT via the repo_e2e lab (`make_bed_target("tomato")` already targets tomato by mgmt ip; the hop-routed variant needs tomato reached via carrot over eth2 — build that target from lab data with `hop="carrot_seed"` and the eth2 ip).

```python
"""Connection-drop chaos: blackhole the SSH port mid-command with otto's own
port-scoped netem, on the carrot->tomato eth2 data-plane hop (mgmt path is
guard-protected and must stay up). Assert otto surfaces the drop, tears down
cleanly, and leaves no qdisc/timer behind after repair. Self-healing --expire
backstop on every impairment; teardown repairs unconditionally.
"""

import re

import pytest

from otto.logger.mode import LogMode
from tests._fixtures.labdata import lab_data_path
from tests.e2e.chaos._bed import run_probe
from tests.integration.chaos._driver import BANNER, spawn_otto

pytestmark = [
    pytest.mark.chaos,
    pytest.mark.stability,
    pytest.mark.integration,
    pytest.mark.hops,
    pytest.mark.xdist_group("chaos_lane"),
    pytest.mark.timeout(300),
]


def _veggies_link_id() -> str:
    import json

    data = json.loads(lab_data_path().read_text())
    link = data["links"][0]  # carrot_seed:eth2 <-> tomato_seed:eth2
    return link["id"]


def _repair(link_id: str) -> None:
    # idempotent; safe to call even when clean (manage.py:691-692)
    run_probe("carrot", lambda h: h.exec("true", timeout=5, log=LogMode.QUIET))  # ensure reachable
    from tests.integration.chaos._driver import spawn_otto as _spawn  # local import; not used for signalling


@pytest.mark.no_hygiene_bracket  # eth2 is deliberately dirtied then repaired; we bracket manually
def test_ssh_blackhole_mid_command_is_survivable_and_repairs_clean(chaos_rng, tmp_path):
    """otto blackholes tomato's SSH (port 22/tcp) on eth2 mid-command via a
    hop-routed session; asserts the run fails/interrupts cleanly and `otto
    link repair` restores an impairment-free eth2 on both endpoints.
    """
    import asyncio

    from tests._fixtures.bed_hygiene import diff_snapshots, format_hygiene_report, snapshot_host
    from tests.e2e.chaos._bed import probe_host

    link_id = _veggies_link_id()

    async def _snap(elem):
        async with probe_host(elem) as h:
            return await snapshot_host(h)

    before = {e: asyncio.run(_snap(e)) for e in ("carrot", "tomato")}
    impaired = False
    try:
        # 1) Start a long command against tomato over the carrot hop on eth2.
        #    Build the hop target inline (make_bed_target gives mgmt-path; we
        #    need the eth2 data-plane ip + hop=carrot_seed). Implementer: derive
        #    it from lab_data_path() the same way make_bed_target does, setting
        #    ssh_host to tomato's eth2 ip (192.168.1.12) and adding hop routing
        #    to the generated lab.json. If eth2 hop-routing proves unroutable
        #    from the SUT, fall back to the SIGSTOP-of-sshd approach (judgment
        #    call 4) and record the switch in this docstring + the ledger.
        target = _make_hop_target(tmp_path)  # see implementer note below
        cmd = "sleep 120 # otto-chaos-drop"
        p = spawn_otto(
            ["host", target.host_id, "run", cmd, "--timeout", "120"],
            xdir=tmp_path,
            target=target,
        )
        p.wait_for_log(re.escape(f"| {cmd}"), timeout=120.0)  # session established over the hop
        # 2) Blackhole SSH on eth2 with --expire backstop (100% loss, tcp/22).
        drop = spawn_otto(
            ["link", "impair", link_id, "--port", "22", "--proto", "tcp", "--loss", "100", "--expire", "60"],
            xdir=tmp_path / "impair",
            target=target,
        )
        assert drop.wait(timeout=60.0) == 0, drop.stderr_text()
        impaired = True
        # 3) otto must notice the dead session and exit non-zero within a bound
        #    (channel keepalive/read timeout), never hang past the test timeout.
        rc = p.wait(timeout=120.0)
        assert rc != 0, "blackholed session should fail, not succeed"
        p.assert_no_process_group()
    finally:
        # 4) Repair unconditionally, both directions, and verify clean eth2.
        rep = spawn_otto(
            ["link", "repair", link_id],
            xdir=tmp_path / "repair",
            target=_make_hop_target(tmp_path),
        )
        rep.wait(timeout=60.0)
        after = {e: asyncio.run(_snap(e)) for e in ("carrot", "tomato")}
        leftovers = []
        for e in ("carrot", "tomato"):
            leftovers += [f"{e}: {x}" for x in diff_snapshots(before[e], after[e])]
        assert not leftovers, format_hygiene_report("carrot+tomato", leftovers)
```

Implementer note (`_make_hop_target`): factor a small helper (in the test module, or `_bed.py` if cleaner) that writes a lab.json like `make_bed_target` but for tomato reached over the carrot hop on the data plane — `ssh_host = tomato eth2 ip (192.168.1.12)`, `hop = carrot_seed`, `ssh_port = 22`, creds from lab data. Consult `tests/_fixtures/tunnel_bed.py:cli_sut_dir` (~line 297) which already writes a veggies lab.json including `eth2` — reuse its shape. If the hop route cannot carry the session (data-plane not routed for SSH in this bed), switch to the SIGSTOP approach: `run_probe("tomato", lambda h: h.exec("sudo kill -STOP $(pgrep -f sshd|head -1)", ...))` with a `kill -CONT` armed via `nohup ... sleep 30; kill -CONT` and record the switch. Either way the invariant is identical: otto surfaces the drop, no local orphan, bed repaired clean.

- [ ] **Step 2: Run on the bed, gate, commit**

Run: `uv run pytest tests/e2e/chaos/test_connection_drop.py -p no:cacheprovider --no-cov -v` (foreground, timeout 600000) → PASS, eth2 clean after.
Gate: `uv run nox -s lint` → `make typecheck-python` → `make coverage`.

```bash
git add tests/e2e/chaos/test_connection_drop.py
git commit -m "feat(chaos): connection-drop scenario — SSH-port blackhole on the data-plane hop, repairs clean"
```

---

### Task 7: Transfer chaos + nc GET-path cancel-time reap (product fix)

Interrupt a transfer mid-stream in both directions. This forces the product fix from `todo/chaos-teardown-followups.md` §1: the nc GET path has no cancel-time listener reap, so a cancelled GET leaks the remote `nc -l` for `listener_timeout=30 s` — longer than the 10 s teardown deadline, violating spec success-criterion #1. The tier-1 test (mirror of the put-path's) proves the reap; the bed scenario proves it end-to-end and characterizes partial-file state.

**Files:**
- Modify: `src/otto/host/transfer/nc.py` (GET path `except asyncio.CancelledError` → `compensate()`-wrapped reap)
- Create: `tests/unit/host/transfer/test_nc_cancel.py` (or extend the existing put-path cancel test module if one exists — implementer checks)
- Create: `tests/e2e/chaos/test_transfer_chaos.py`

**Interfaces:**
- Consumes: `NcFileTransfer._get_files_nc / _get_files_nc_tunneled / _get_one` (~lines 592-710); the put path's `_cancel_and_reap` helper (`_attempt`, ~line 976) as the shape to mirror; `otto.lifecycle.compensate`.
- Produces: a reaped GET listener on cancellation — no new public API.

- [ ] **Step 1: Write the failing tier-1 test (mirror the put-path's cancel test)**

First read the put-path cancel test (grep for `_cancel_and_reap` and the put-path `CancelledError` test) and mirror its arrangement exactly against the GET path. The test drives `_get_one` (or `_get_files_nc`) to the point where the remote listener task is spawned, cancels, and asserts the listener task was cancelled AND the remote reap command was issued exactly once — using the same fake/stub host the put-path test uses. Concrete shape (adapt to the real fakes):

```python
"""GET-path cancel-time reap: a cancelled nc GET must cancel+reap its remote
listener, not leak it for listener_timeout seconds (todo/chaos-teardown-
followups.md §1; spec success-criterion #1: nothing outlives the deadline).
Mirror of the put-path's _cancel_and_reap coverage.
"""
# (mirror the existing put-path cancel test's imports, fakes, and structure)

async def test_get_cancel_reaps_remote_listener():
    ...  # arrange a GET whose listener task is live, cancel mid-flight,
         # assert the listener task is cancelled and the reap command ran once
```

- [ ] **Step 2: Run to verify it fails (proves the leak exists pre-fix)**

Run: `uv run pytest tests/unit/host/transfer/test_nc_cancel.py -v` (foreground, timeout 600000)
Expected: FAIL — the GET path leaks the listener (no reap on cancel). This is the proven-red requirement; a green result here means the test doesn't actually reach the GET listener path — fix the test before touching product code.

- [ ] **Step 3: Add the reap to the GET path**

In `src/otto/host/transfer/nc.py`, add to `_get_one` (and `_get_files_nc_tunneled` if it spawns its own listener) an `except asyncio.CancelledError` handler mirroring the put path's `_attempt` handler (~line 976): cancel+join the listener task and reap the remote `nc -l` via the existing `_cancel_and_reap`-shaped helper, run under `compensate(...)` so the reap itself is shielded and deadline-bounded. Reuse the put path's helper if it is already parameterized enough; otherwise extract a shared `_cancel_and_reap(listen_task, host, reap_cmd, *, what)` both paths call. Do NOT change `listener_timeout`'s default (judgment call 8).

```python
        except asyncio.CancelledError:
            await compensate(
                self._cancel_and_reap(listen_task, host, reap_cmd),
                what=f"nc get listener reap ({dst})",
            )
            raise
```

- [ ] **Step 4: Run to verify green**

Run: `uv run pytest tests/unit/host/transfer/test_nc_cancel.py -v` (foreground, timeout 600000) → PASS.

- [ ] **Step 5: Write the bed scenario (both directions + SIGKILL characterization)**

`tests/e2e/chaos/test_transfer_chaos.py` — the leased host's active transfer backend depends on the element (carrot/pepper=scp, tomato=nc); force `nc` explicitly via `--transfer nc` on the host group so the GET-path reap is exercised regardless of which host was leased:

```python
"""Transfer chaos: interrupt mid-stream both directions; assert no orphaned nc
listener beyond the teardown deadline and characterize partial-file state.
Forces the nc backend (--transfer nc) so the GET-path reap (Task 7 product
fix) is exercised on whichever host was leased.
"""

import re
import time

import pytest

from otto.logger.mode import LogMode
from tests.e2e.chaos._bed import run_probe
from tests.integration.chaos._driver import BANNER, spawn_otto

pytestmark = [
    pytest.mark.chaos,
    pytest.mark.stability,
    pytest.mark.integration,
    pytest.mark.xdist_group("chaos_lane"),
    pytest.mark.timeout(300),
]


def _nc_listeners(element: str) -> list:
    async def _find(host):
        out = (await host.exec('pgrep -af "nc -l" | grep -v pgrep || true', timeout=30, log=LogMode.QUIET)).value or ""
        return [ln for ln in out.splitlines() if ln.strip()]

    return run_probe(element, _find)


def test_sigint_mid_put_no_orphan_listener(chaos_bed, chaos_rng, tmp_path):
    from tests.e2e.chaos._seed import offset_in

    big = tmp_path / "payload.bin"
    big.write_bytes(b"0" * (64 * 1024 * 1024))  # 64 MiB so the stream is interruptible
    before = _nc_listeners(chaos_bed.element)
    p = spawn_otto(
        ["host", chaos_bed.target.host_id, "--transfer", "nc", "put", str(big), "/tmp/otto-chaos-put"],
        xdir=tmp_path,
        target=chaos_bed.target,
    )
    p.wait_for_log(r"NC put", timeout=120.0)  # phase: transfer streaming (DEBUG line)
    time.sleep(offset_in(chaos_rng, 0.0, 1.0))
    p.signal(2)
    rc = p.wait(timeout=60.0)
    assert rc == 130, p.stderr_text()
    p.assert_no_process_group()
    # No NEW listener may outlive the deadline (poll briefly past 10s deadline).
    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        new = [ln for ln in _nc_listeners(chaos_bed.element) if ln not in before]
        if not new:
            break
        time.sleep(0.5)
    assert not new, f"orphaned nc listener beyond teardown deadline: {new}"
    run_probe(chaos_bed.element, lambda h: h.exec("rm -f /tmp/otto-chaos-put || true", timeout=30, log=LogMode.QUIET))


def test_sigint_mid_get_no_orphan_listener(chaos_bed, chaos_rng, tmp_path):
    """The GET-path counterpart — the case Task 7's product fix repaired."""
    # Seed a big remote file first, then GET it and interrupt mid-stream.
    run_probe(
        chaos_bed.element,
        lambda h: h.exec("head -c 67108864 /dev/zero > /tmp/otto-chaos-src", timeout=60, log=LogMode.QUIET),
    )
    before = _nc_listeners(chaos_bed.element)
    try:
        p = spawn_otto(
            ["host", chaos_bed.target.host_id, "--transfer", "nc", "get", "/tmp/otto-chaos-src", str(tmp_path)],
            xdir=tmp_path,
            target=chaos_bed.target,
        )
        p.wait_for_log(r"NC get", timeout=120.0)
        from tests.e2e.chaos._seed import offset_in

        time.sleep(offset_in(chaos_rng, 0.0, 1.0))
        p.signal(2)
        rc = p.wait(timeout=60.0)
        assert rc == 130, p.stderr_text()
        p.assert_no_process_group()
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            new = [ln for ln in _nc_listeners(chaos_bed.element) if ln not in before]
            if not new:
                break
            time.sleep(0.5)
        assert not new, f"GET orphaned nc listener beyond deadline: {new}"
        # Partial-file characterization: record what the local dest looks like.
        dest = tmp_path / "otto-chaos-src"
        got = dest.stat().st_size if dest.exists() else None
        print(f"partial-file policy (nc get, interrupted): local dest size = {got}")
    finally:
        run_probe(chaos_bed.element, lambda h: h.exec("rm -f /tmp/otto-chaos-src || true", timeout=30, log=LogMode.QUIET))
```

Implementer note: `--transfer` is a host-group callback option that sits BEFORE the verb: `otto host <ID> --transfer nc put …` (per `cli/host.py`). Confirm the exact position by reading how `test_host_transfer_e2e.py` drives a backend override via subprocess. The 64 MiB size is a starting point — tune so the stream reliably spans the seeded 0-1 s window on the bed without making the test slow; record the chosen size. Partial-file behavior is characterized (printed), not asserted (judgment call 9) — the hard invariant is the no-orphan-listener check.

- [ ] **Step 6: Run on the bed, gate, commit**

Run: `uv run pytest tests/e2e/chaos/test_transfer_chaos.py -p no:cacheprovider --no-cov -v` (foreground, timeout 600000) → PASS.
Gate: `uv run nox -s lint` → `make typecheck-python` → `make coverage`.

```bash
git add src/otto/host/transfer/nc.py tests/unit/host/transfer/test_nc_cancel.py tests/e2e/chaos/test_transfer_chaos.py
git commit -m "feat(chaos): transfer interrupt scenarios + nc GET-path cancel-time listener reap"
```

---

### Task 8: Tunnel/link rollback chaos + recovery reconciliation

Interrupt between tunnel daemon launch and success return; interrupt during the rollback itself (proving the `compensate()` shield live from the outside); and a SIGKILL characterization asserting the recovery commands (`otto tunnel remove --all --yes`, `otto link repair --all`) reap survivors and reconcile the bed. Adds the chaos port block to the cross-suite registry.

**Files:**
- Modify: `tests/_fixtures/tunnel_bed.py` (`PORT_BLOCKS` += chaos block)
- Create: `tests/e2e/chaos/test_tunnel_link_chaos.py`

**Interfaces:**
- Consumes: `otto tunnel add/remove --all --yes`, `otto link impair/repair --all` CLI; tunnel launch phase marker (the `@host | bash -c '…otto-tunnel:v1:…'` QUIET line in verbose.log); `PORT_BLOCKS`; BedHygiene.
- Produces: `PORT_BLOCKS` entry `(15200, 15299, "tests/e2e/chaos/ (make chaos)")`.

- [ ] **Step 1: Register the chaos port block**

In `tests/_fixtures/tunnel_bed.py`, extend `PORT_BLOCKS` (~line 70-77):

```python
    (15200, 15299, "tests/e2e/chaos/ (make chaos)"),
```

Confirm with `grep -rn 1520 tests/e2e` that nothing else claims the block first.

- [ ] **Step 2: Write the scenarios**

```python
"""Tunnel/link rollback chaos: interrupt at the launch->return window and
during rollback itself; SIGKILL characterization + recovery reconciliation.
The product cannot survive SIGKILL (spec); the test characterizes what leaks
and asserts `otto tunnel remove --all --yes` / `otto link repair --all` clean
the bed. This is inherently multi-host (a tunnel spans a path), so it pins the
veggies trio and marks hops.
"""

import re

import pytest

from tests.e2e.chaos._bed import run_probe
from tests.integration.chaos._driver import BANNER, spawn_otto
from tests._fixtures.tunnel_bed import cli_sut_dir  # writes a veggies lab.json
from otto.logger.mode import LogMode

pytestmark = [
    pytest.mark.chaos,
    pytest.mark.stability,
    pytest.mark.integration,
    pytest.mark.hops,
    pytest.mark.xdist_group("chaos_lane"),
    pytest.mark.timeout(360),
]

_SVC_PORT = 15200  # chaos block, disjoint from tunnel_stability's 15100-15199


def _bed_clean_of_tunnels() -> list:
    """Owner-agnostic: any otto-tunnel sentinel process on the trio."""
    from tests._fixtures.tunnel_bed import observe_tunnel_processes
    import asyncio

    return asyncio.run(observe_tunnel_processes())


@pytest.mark.no_hygiene_bracket  # multi-host; we reconcile + assert manually
def test_sigkill_mid_tunnel_recovers_via_remove_all(tmp_path):
    """SIGKILL an `otto tunnel add` mid-launch; assert `tunnel remove --all
    --yes` reaps whatever daemons survived and the trio ends clean.
    """
    sut = cli_sut_dir(tmp_path)
    target = _tunnel_target(sut)  # ChaosTarget over the veggies lab; see note
    p = spawn_otto(
        ["tunnel", "add", "--hosts", "carrot_seed,tomato_seed", "--port", str(_SVC_PORT)],
        xdir=tmp_path,
        target=target,
    )
    # phase: at least one daemon launched (sentinel in a QUIET exec line)
    p.wait_for_log(r"otto-tunnel:v1:", timeout=120.0)
    p.signal(9)  # SIGKILL — no teardown possible
    p.wait(timeout=30.0)
    try:
        # Recovery command reconciles the bed.
        rm = spawn_otto(["tunnel", "remove", "--all", "--yes"], xdir=tmp_path / "rm", target=target)
        assert rm.wait(timeout=120.0) == 0, rm.stderr_text()
        assert not _bed_clean_of_tunnels(), "tunnel remove --all left survivors"
    finally:
        spawn_otto(["tunnel", "remove", "--all", "--yes"], xdir=tmp_path / "rm2", target=target).wait(timeout=120.0)


def test_interrupt_during_rollback_still_reaps(tmp_path):
    """Interrupt the rollback path itself (compensate() shield): force a tunnel
    add to fail so it rolls back, SIGINT during the banner window, assert the
    shielded rollback still completed (no survivors after) — the outside proof
    of Plan 2's compensate() shield on the tunnel rollback site.
    """
    # Implementer: induce a rollback by making the LAST hop unreachable (e.g.
    # a --hosts path whose final host is down / a bogus --dest), so add_tunnel
    # launches early hops then fails and rolls back. SIGINT at the BANNER. Then
    # assert observe_tunnel_processes() is empty. If inducing a deterministic
    # mid-rollback interrupt from outside proves unreliable, DROP this test to
    # the ledger as "could not drive deterministically from the CLI boundary"
    # rather than asserting something timing-dependent — the tier-1 sweep
    # already proves the shield; this is the belt-and-suspenders bed proof.
    ...


@pytest.mark.no_hygiene_bracket
def test_sigkill_mid_impair_recovers_via_repair_all(tmp_path):
    """SIGKILL an `otto link impair`; assert `otto link repair --all` restores
    impairment-free qdiscs on the trio.
    """
    import json
    from tests._fixtures.labdata import lab_data_path

    link_id = json.loads(lab_data_path().read_text())["links"][0]["id"]
    sut = cli_sut_dir(tmp_path)
    target = _tunnel_target(sut)
    p = spawn_otto(
        ["link", "impair", link_id, "--loss", "50", "--expire", "60"],
        xdir=tmp_path,
        target=target,
    )
    p.wait_for_log(r"\| (sudo )?tc qdisc", timeout=120.0)  # phase: qdisc being written
    p.signal(9)
    p.wait(timeout=30.0)
    try:
        rep = spawn_otto(["link", "repair", "--all"], xdir=tmp_path / "rep", target=target)
        assert rep.wait(timeout=120.0) == 0, rep.stderr_text()
        # eth2 must be netem-free on both endpoints.
        for elem in ("carrot", "tomato"):
            out = run_probe(elem, lambda h: h.exec("tc qdisc show dev eth2", timeout=30, log=LogMode.QUIET))
            assert "netem" not in (out.value or ""), f"{elem}: netem survived repair --all: {out.value!r}"
    finally:
        spawn_otto(["link", "repair", "--all"], xdir=tmp_path / "rep2", target=target).wait(timeout=120.0)
```

Implementer note (`_tunnel_target`): the tunnel/link CLI is reservation-gated (`gate=True`); build a `ChaosTarget` whose `sut_dir` is the `cli_sut_dir(tmp_path)` veggies SUT and whose `lab="veggies"`, then either pass `-R` in argv or ensure the SUT has no `[reservations]` backend. Read `tests/_fixtures/tunnel_bed.py:run_tunnel_cli` (~line 341) for the exact `--lab veggies` invocation shape and mirror it through `spawn_otto`. `observe_tunnel_processes()` and `assert_no_leftover_tunnel_processes` already exist — reuse them for the reconciliation asserts. The `run_probe` closures capturing `elem` in a loop must bind it (default-arg trick) — implementer handles the late-binding gotcha.

- [ ] **Step 3: Run on the bed, gate, commit**

Run: `uv run pytest tests/e2e/chaos/test_tunnel_link_chaos.py -p no:cacheprovider --no-cov -v` (foreground, timeout 600000) → PASS (or the rollback test parked in the ledger with reason).
Gate: `uv run nox -s lint` → `make typecheck-python` → `make coverage`.

```bash
git add tests/_fixtures/tunnel_bed.py tests/e2e/chaos/test_tunnel_link_chaos.py
git commit -m "feat(chaos): tunnel/link rollback + SIGKILL recovery-reconciliation scenarios"
```

---

### Task 9: Reboot chaos scenarios

The spec's 2026-07-31 reboot amendment, bed leg (Plan 4's set). Soft reboots (`hard=False`) on the leased host only. RE-CONFIRM reboot authorization at execution start (controller gate — this task does not run until Chris re-affirms). Scenarios: happy-path `reboot(wait=True)`; reboot at a phase marker mid-command; reboot × tunnel (half-chain, `tunnel remove --all` reaps survivors); reboot × link (rebooted endpoint clean, peer's qdiscs remain, `repair --all` idempotent). Records real down/up/recovery timings for the deferred `_confirm_recovered` tuning (`todo/chaos-reboot-followups.md` §3/§4).

**Files:**
- Create: `tests/e2e/chaos/test_reboot_chaos.py`

**Interfaces:**
- Consumes: `otto host <ID> reboot --wait [--down-timeout …]` CLI; `spawn_otto`; `otto tunnel remove --all --yes` / `otto link repair --all`; BedHygiene `snapshot_host`/`diff_snapshots` (manual bracket — the autouse after-probe would race the boot).
- Produces: nothing downstream (last of the unix scenario set).

- [ ] **Step 1: Write the scenarios (all `@pytest.mark.no_hygiene_bracket` — reboot races the autouse after-probe)**

```python
"""Reboot chaos (spec 2026-07-31 amendment, bed leg). Soft reboots on the
LEASED host only — never basil, never the boards, never the dev VM. Real bed
reboots were Chris-approved for Plan 4; the controller re-confirms before this
module runs. Records down/up/recovery timings for the deferred _confirm_
recovered tuning (todo/chaos-reboot-followups.md §3/§4).
"""

import re
import time

import pytest

from otto.logger.mode import LogMode
from tests.e2e.chaos._bed import run_probe
from tests.integration.chaos._driver import spawn_otto

pytestmark = [
    pytest.mark.chaos,
    pytest.mark.stability,
    pytest.mark.integration,
    pytest.mark.no_hygiene_bracket,  # after-probe would race the boot; each test brackets manually
    pytest.mark.xdist_group("chaos_lane"),
    pytest.mark.timeout(900),  # a real reboot cycle is minutes; live-bed rule: generous
]


def test_happy_path_reboot_wait_recovers(chaos_bed, tmp_path):
    """`reboot --wait` on the leased host returns success only after a clean
    shell round-trip (not just TCP accept). Records the wall-clock.
    """
    started = time.monotonic()
    p = spawn_otto(
        ["host", chaos_bed.target.host_id, "reboot", "--wait"],
        xdir=tmp_path,
        target=chaos_bed.target,
    )
    rc = p.wait(timeout=600.0)
    elapsed = time.monotonic() - started
    print(f"reboot --wait recovered in {elapsed:.1f}s")
    assert rc == 0, p.stderr_text()
    # Independent confirmation the host is genuinely usable post-reboot.
    out = run_probe(chaos_bed.element, lambda h: h.exec("echo POST-REBOOT", timeout=60, log=LogMode.QUIET))
    assert "POST-REBOOT" in (out.value or "")


def test_reboot_x_tunnel_survivors_reaped(tmp_path):
    """A tunnel whose endpoint reboots: the daemon dies with the OS; assert
    `tunnel remove --all --yes` reconciles the (now half-)chain and the trio
    ends clean. Multi-host -> uses the veggies SUT, pins carrot/tomato.
    """
    # Implementer: build a tunnel carrot<->tomato (chaos port block), reboot
    # tomato via `otto host tomato_seed reboot --wait`, then run tunnel remove
    # --all --yes and assert observe_tunnel_processes() empty. Reboot ONLY
    # tomato (a leased/pinned veggies peer), never basil. Bracket tunnel state
    # manually before/after.
    ...


def test_reboot_x_link_repair_idempotent(tmp_path):
    """Impair the carrot<->tomato link, reboot tomato (its qdisc clears with
    the OS), carrot's qdisc remains; `link repair --all` is idempotent against
    the half-clean state and ends with both endpoints netem-free.
    """
    ...
```

Implementer note: fill the two `...` scenarios following the Task 8 patterns (veggies SUT, `_tunnel_target`, `observe_tunnel_processes`, per-endpoint `tc qdisc show`). The reboot target for the multi-host cases is `tomato_seed` specifically (a pinned veggies peer), issued via `otto host tomato_seed reboot --wait`. Manual BedHygiene bracket: snapshot BEFORE the reboot, and take the AFTER snapshot only once `reboot --wait` has returned (the host is confirmed back), so the probe never races the boot. Every test cleans up (tunnel remove --all --yes / link repair --all) in `finally`. If a reboot leaves the bed in a bad state, STOP and report — do not retry-loop reboots.

- [ ] **Step 2: Controller gate — re-confirm reboot authorization, then run on the bed**

Before running: the controller confirms with Chris that (a) real soft reboots of the leased veggies host are authorized for this run, and (b) the bed is free. Only then:
Run: `uv run pytest tests/e2e/chaos/test_reboot_chaos.py -p no:cacheprovider --no-cov -v` (foreground, timeout 600000) → PASS, timings printed.
Gate: `uv run nox -s lint` → `make typecheck-python` → `make coverage`.

- [ ] **Step 3: Commit**

```bash
git add tests/e2e/chaos/test_reboot_chaos.py
git commit -m "feat(chaos): reboot scenarios — happy-path, reboot x tunnel, reboot x link recovery"
```

---

### Task 10: Embedded console-client-death scenario (opt-in zephyr leg)

The remote-side dual of the CLI interrupt work on the embedded surface, from `todo/zephyr-console-wedge-2026-08-01.md`. Behind the separate `chaos and embedded` selector (`make chaos-embedded`) because it can wedge a board. SIGKILL otto mid-handshake / mid-command on a telnet console session; assert the NEXT client gets a working shell within a bound, else fail naming the board. Regression-guards the console-wedge candidate cause.

**Files:**
- Create: `tests/e2e/chaos/test_console_chaos.py`

**Interfaces:**
- Consumes: `spawn_otto`; a zephyr console target (via basil hop → telnet console, from `tech1/lab.json`'s sprout entries); a "next client gets a shell" probe.
- Produces: nothing downstream.

- [ ] **Step 1: Write the scenario**

```python
"""Console-client-death chaos (todo/zephyr-console-wedge-2026-08-01.md).
Behind `make chaos-embedded` — CAN WEDGE A BOARD. SIGKILL otto mid-console-
session; assert the NEXT client gets a working shell within a bound, else FAIL
NAMING THE BOARD. Regression-guards the 2026-08-01 fat-board wedge's
client-death candidate cause. Recovery criterion is a SUSTAINED shell (N
round-trips over a settle window), not one accept — this incident is the
standing counterexample where accept != shell (todo/chaos-reboot-followups.md
§4).
"""

import re
import time

import pytest

from otto.logger.mode import LogMode

pytestmark = [
    pytest.mark.chaos,
    pytest.mark.stability,
    pytest.mark.embedded,
    pytest.mark.xdist_group("chaos_lane"),
    pytest.mark.timeout(600),
]


def test_console_client_death_leaves_next_client_a_shell(tmp_path):
    """Implementer: build a zephyr console target (sprout via basil hop, telnet
    console — mirror tests/e2e/cov/test_embedded_coverage_e2e.py / the embedded
    stability suite for how the console session is driven through basil). Start
    an `otto host <sprout_id> run <cmd>` over the console, wait for the command-
    running phase marker (or the handshake-start log line for the mid-handshake
    variant), SIGKILL otto. Then, from a FRESH otto invocation, open a new
    console session and assert a sustained shell: N consecutive `run` round-
    trips succeed over a settle window. If any round-trip fails, FAIL naming
    the board (e.g. sprout/192.0.2.1) — that is the wedge reproduced. NEVER
    reboot or power-cycle the board here; only otto is killed. If the board
    DOES wedge, that is a real finding: STOP, report it named, and note the
    board needs a manual restart (do not attempt recovery from the test).
    """
    ...
```

Implementer note: this is the highest-risk scenario in the plan. Read the embedded stability suite and `test_embedded_coverage_e2e.py` for the exact console-session plumbing (basil hop → telnet 192.0.2.1:23) before writing a line. The "sustained shell" recovery check is deliberately N round-trips over a window (the accept≠shell lesson). Keep N small (3) and the window short (a few seconds) to bound board stress. If driving a deterministic mid-handshake SIGKILL from the CLI boundary proves unreliable, ship only the mid-command variant and park the mid-handshake case in the ledger — do not fabricate timing.

- [ ] **Step 2: Controller gate — confirm zephyr-wedge authorization, then run**

Before running: the controller confirms with Chris that a scenario which may re-wedge the fat board is authorized, and that the zephyr bed is healthy going in (the 2026-08-01 wedge was recovered). Only then:
Run: `uv run pytest tests/e2e/chaos/test_console_chaos.py -m "chaos and embedded" -p no:cacheprovider --no-cov -v` (foreground, timeout 600000).
Gate: `uv run nox -s lint` → `make typecheck-python` → `make coverage`.

- [ ] **Step 3: Commit**

```bash
git add tests/e2e/chaos/test_console_chaos.py
git commit -m "feat(chaos): embedded console-client-death scenario (opt-in zephyr leg)"
```

---

## Self-review

Run after all tasks are drafted (checklist, not a dispatch). Recorded here so the executing controller can confirm coverage before the final review.

**Spec coverage (tier-3 section + reboot amendment + four followups):**
- `nox -s chaos` + `make chaos` lane, excluded like tunnel_stability → Task 1 (marker + sessions + legs + guards).
- BedHygiene oracle (tunnel leftovers, impair timers, FD, shell history, qdiscs, staging) → Task 3. Console-responsiveness probe → Task 10's sustained-shell check (the incident's specific probe). sshd session count + `docker compose ls` deliberately omitted (docker rides Plan 5; sshd-count has no existing probe and no scenario needs it — noted in non-goals-by-silence; add if a scenario later needs it).
- Seeded randomized injection, printed/reproducible → Task 4.
- Host/session scenarios (connection drop, SIGINT-immune remote, slow-close deadline) → Tasks 5 + 6.
- Transfer scenarios (both directions, no orphaned nc, partial-file policy) → Task 7.
- Tunnel/link (launch-window, rollback-window, leftover assertions, SIGKILL recovery) → Task 8.
- Reboot amendment (happy-path, mid-command, ×tunnel, ×link) → Task 9.
- Console-client-death + sustained-shell recovery → Task 10.
- Followups closed: realsignal §4 (bed prototype exercised, Task 2) and §5 (nohup characterization, Task 5); teardown §1 (nc GET reap, Task 7). Deferred-with-reason: realsignal §2/§3/§6, reboot §1/§2/§3/§4, teardown §2 — all in non-goals.

**Placeholder scan:** the `...` bodies in Tasks 6/8/9/10 are deliberate implementer-fill points with concrete written instructions and a named fallback each (never bare "TODO"); every one names the exact files/helpers to mirror. The tier-1 (Task 7 unit) and pure-logic (Tasks 3/4) tests are fully written. This matches the Plan 3 precedent where bed-touching scenario bodies carried structured notes rather than untested verbatim code.

**Type/name consistency:** `ChaosBed`(element/ip/target), `chaos_bed`, `chaos_rng`, `run_probe`, `probe_host`, `leased_bed` defined in Task 2 and used verbatim in 5-10. `HygieneSnapshot`/`snapshot_host`/`diff_snapshots`/`format_hygiene_report` defined Task 3, used 5-10 (autouse) + 6/9 (manual). `resolve_seed`/`offset_in`/`chaos_rng` defined Task 4, used 5-9. `no_hygiene_bracket` marker registered Task 3, used 6/9. Marker string `chaos` registered Task 1, stamped everywhere. `_tunnel_target`/`_make_hop_target` are named implementer helpers (Tasks 6/8) — flagged as to-be-factored, not referenced across tasks.

**Known executor risks (surfaced, not hidden):** (1) the reservation gate on bed targets — decided once in Task 2 Step 3 (`-R` or backend-free SUT), inherited by all; (2) data-plane hop routing for the SSH blackhole — Task 6 carries the SIGSTOP fallback; (3) deterministic mid-rollback / mid-handshake interrupts from the CLI boundary — Tasks 8/10 carry ledger-park fallbacks rather than timing-fabrication; (4) `--transfer nc` position on the command line — Task 7 note. None block earlier tasks.

# Test-suite speedup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Cut the wall-clock of `make coverage` and `make nox` by removing real serialization bottlenecks (the carrot transfer funnel, late-scheduled long poles, the single-worker e2e chains, the per-Python unit re-run) without dropping a single integration/e2e scenario.

**Architecture:** Six independent, independently-measurable interventions. A cross-worker file-lock "host pool" spreads Unix transfer/command tests across `{carrot, tomato, pepper}`; a collection reorder front-loads the long poles; the `interact_e2e` group splits by terminal; nox runs the bed-free unit tier concurrently across Pythons; and (infra-gated) docker tests spread across per-VM daemons. A lab-data cleanup removes the now-unneeded Unix-host hops first.

**Tech Stack:** pytest, pytest-xdist (`-n auto --dist loadgroup`), pytest-asyncio, `fcntl.flock` cross-worker locks, nox/nox_uv, coverage.py, the otto host library + lab-data JSON.

**Design spec:** [docs/superpowers/specs/2026-06-23-test-suite-speedup-design.md](../specs/2026-06-23-test-suite-speedup-design.md). Empirical baseline: [todo/test-host-pool-and-speed.md](../../../todo/test-host-pool-and-speed.md).

## Global Constraints

- **No scenario may be dropped, weakened, or merged away.** Coverage equivalence is the *detector*, not the goal. Prove it: `pytest --collect-only -q` yields the **same set of test IDs** before/after (modulo deliberate group-id renames), and a green run shows the same passed count.
- **Coverage stays equivalent** — `make coverage` ≥ the 90% gate, within noise of ~91.85%.
- **`make nox` keeps the full 5× Python matrix for the bed.** Win from per-run speed + de-duplicating the *unit* tier only — never from running the bed on fewer interpreters.
- **otto stays server-less.** The pool is a test-harness file lock, not a coordinator service or a change to otto's reservation backend.
- **Embedded backends are NOT poolable**; `--dist loadgroup`, the single-client console lock, and per-device grouping are preserved.
- **Every speed claim is validated with repetition** — bed wall-clock is noisy (~±20s). Report the **median over N≥3 runs**, never a single clock. A wedged bed or a flaky run **fails loudly** (host named) — never a silent skip.
- **Stage-only.** Do NOT `git commit` (the `prepare-commit-msg` hook needs `/dev/tty`). Each task's final step **stages** its changes; the controller hands Chris a paste-able commit message. (Per-task diffs come from `git stash create` snapshots, as in the restructure SDD flow.)
- **Work in a fresh worktree off `main`** (the restructure is merged; this is a new workstream). `uv sync` once before the ty/docs gates.

---

## Phase 1 — Lab-data hop simplification

Removes the Unix-host hops from lab data so it reflects reality (Unix VMs are directly reachable); keeps the embedded `sprout* → basil_seed` hops. Hop *functionality* stays covered by `tests/integration/host/test_hop_integration.py`, which builds explicit `carrot → tomato` chains via the `hop_host` fixture and does not read any host's lab-data hop. This unblocks pepper being a clean, direct pool member (Phase 4).

### Task 1: Remove Unix-host hops from `hosts.json` (tech1 + tech2)

**Files:**
- Modify: `tests/_fixtures/lab_data/tech1/hosts.json` (remove the `"hop": "carrot_seed"` line in the `pepper` entry, ~line 49)
- Modify: `tests/_fixtures/lab_data/tech2/hosts.json` (audit the two hops `orange_seed`/`apple_seed`; remove those whose host is Unix `os_type`, keep any whose host is embedded)
- Test: `tests/unit/test_lab_data_hops.py` (new — a pure-data guard, no VM)

**Interfaces:**
- Consumes: `tests/_fixtures/labdata.py::lab_data_path(tech)` (existing helper returning the `hosts.json` path), `host_data(ne)`.
- Produces: the invariant "no `unix` host carries a `hop`" that Phase 4 relies on for direct pepper leasing.

- [ ] **Step 1: Write the failing guard test**

```python
# tests/unit/test_lab_data_hops.py
"""Guard: lab data only defines hops where a hop is physically required.

Unix VMs are directly reachable on the private net; the hop was test
scaffolding (hop *functionality* is covered by test_hop_integration.py).
Only embedded (Zephyr) hosts legitimately hop, through basil.
"""
from __future__ import annotations

import json

import pytest

from tests._fixtures.labdata import lab_data_path

_TECHS = ("tech1", "tech2")


@pytest.mark.parametrize("tech", _TECHS)
def test_no_unix_host_defines_a_hop(tech: str) -> None:
    hosts = json.loads(lab_data_path(tech).read_text())
    offenders = [
        h["element"]
        for h in hosts
        if h.get("os_type") == "unix" and "hop" in h
    ]
    assert not offenders, (
        f"{tech}: Unix hosts must be directly reachable (no hop) — "
        f"hop functionality is covered by test_hop_integration.py. Offenders: {offenders}"
    )


@pytest.mark.parametrize("tech", _TECHS)
def test_embedded_hops_are_preserved(tech: str) -> None:
    hosts = json.loads(lab_data_path(tech).read_text())
    embedded = [h for h in hosts if h.get("os_type") != "unix"]
    # Every embedded host that needs a hop still declares one (regression guard
    # against an over-eager sweep deleting the real basil hops).
    hopped = [h["element"] for h in embedded if "hop" in h]
    assert hopped, f"{tech}: expected embedded hosts to retain their (basil) hop"
```

> If `lab_data_path` does not accept a tech name, check its actual signature in `tests/_fixtures/labdata.py` and adapt (it may be `lab_data_dir(tech) / "hosts.json"`). Do not invent an API — read the file first.

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/unit/test_lab_data_hops.py -v`
Expected: `test_no_unix_host_defines_a_hop[tech1]` FAILS (offender: `pepper`); tech2 fails iff its hops are Unix.

- [ ] **Step 3: Remove the Unix-host hops**

In `tests/_fixtures/lab_data/tech1/hosts.json`, delete the `"hop": "carrot_seed",` line from the `pepper` entry (keep `docker_capable`). In `tests/_fixtures/lab_data/tech2/hosts.json`, for each of the two hop lines, check the owning host's `os_type`: delete the line if `unix`, keep it if embedded.

- [ ] **Step 4: Run the guard + the hop coverage test**

Run: `uv run pytest tests/unit/test_lab_data_hops.py -v` → PASS.
Run (live bed): `uv run pytest tests/integration/host/test_hop_integration.py -v` → PASS (proves hop coverage is intact and independent of the removed lab-data hops).

- [ ] **Step 5: Verify the pepper consumers still pass with direct pepper**

Run (live bed): `uv run pytest tests/e2e/docker -m integration -v` and `uv run pytest -k "host3" -m integration -v`
Expected: PASS. (docker_e2e + `host3` reach pepper directly now; pepper is directly reachable at `10.10.200.13`.) If anything routed *only* via the pepper hop, surface it — do not paper over it.

- [ ] **Step 6: Stage**

```bash
git add tests/_fixtures/lab_data/tech1/hosts.json tests/_fixtures/lab_data/tech2/hosts.json tests/unit/test_lab_data_hops.py
# Stage only — controller hands Chris the commit message.
```

---

## Phase 2 — Split `interact_e2e` ssh/telnet

The `interact_e2e` group pins both terminal parametrizations to one worker. They use independent PTYs/subprocesses and distinct protocols/ports, so keying the group by terminal lets ssh and telnet run on two workers concurrently.

### Task 2: Key the `interact_e2e` xdist_group by terminal type

**Files:**
- Modify: `tests/e2e/host/test_interact_e2e.py` (the `xdist_group("interact_e2e")` marker application — locate it first; the module docstring at line 21 says all tests carry it, applied per parametrized class or via `pytestmark`)
- Test: the existing `test_interact_e2e.py` tests + a collection assertion

**Interfaces:**
- Consumes: nothing new.
- Produces: two groups `interact_e2e_ssh` / `interact_e2e_telnet` replacing the single `interact_e2e`.

- [ ] **Step 1: Locate the current marker**

Run: `grep -n "xdist_group\|interact_e2e\|parametrize\|pytestmark" tests/e2e/host/test_interact_e2e.py`
Note whether the group is a module-level `pytestmark`, a class decorator, or a `pytest.param(..., marks=...)`, and how the term ("ssh"/"telnet") is parametrized.

> A marker rename is not classic TDD (no meaningful failing unit test —
> asserting xdist_group assignment requires running collection). Verify it
> operationally: same collected IDs, and ssh/telnet landing on different
> workers in a live run.

- [ ] **Step 2: Change the marker to be term-keyed**

Replace the single `xdist_group("interact_e2e")` with a per-term group. If the term ("ssh"/"telnet") is a class/param-level parametrize, apply the group in the same `pytest.param`'s `marks` (e.g. `marks=pytest.mark.xdist_group(f"interact_e2e_{term}")`); if it is a static `pytestmark` or class decorator, convert it to a parametrized marks list keyed off the term id. Keep every other marker (`integration`, etc.) unchanged.

- [ ] **Step 3: Verify the regrouping (collection)**

Run: `grep -n 'xdist_group' tests/e2e/host/test_interact_e2e.py` → no bare `"interact_e2e"` remains; only the term-keyed ids.
Run: `uv run pytest tests/e2e/host/test_interact_e2e.py --collect-only -q` → collects the **same test IDs** as before (no scenario lost).

- [ ] **Step 4: Run the live tests on multiple workers**

Run (live bed): `uv run pytest tests/e2e/host/test_interact_e2e.py -m integration -v`
Expected: all PASS (ssh + telnet); the `-v` worker tags (`[gw0]`/`[gw1]`) show the ssh and telnet groups landing on different workers.

- [ ] **Step 5: Measure (median of 3)**

Run 3×: `uv run pytest tests/e2e/host/test_interact_e2e.py -m integration -q --no-cov`; record the median wall before/after. Expect a modest drop (the two ~6.5s SIGWINCH tests overlap instead of serializing).

- [ ] **Step 6: Stage**

```bash
git add tests/e2e/host/test_interact_e2e.py
```

---

## Phase 3 — Front-load the long poles

`sprout_cov` (~18.8s) and the e2e chains finish last only because `--dist loadgroup` dispatches their groups late. **This phase is spike-first** — the spec flags that front-of-collection may not steer loadgroup dispatch. If no cheap mechanism works, this phase is dropped (it is not load-bearing).

### Task 3: Spike — does collection order (or a hint) steer loadgroup dispatch?

**Files:**
- Create: `docs/superpowers/specs/2026-06-23-frontload-spike-findings.md` (the finding)

- [ ] **Step 1: Add a temporary reorder hook**

In a scratch branch, add to `tests/conftest.py`:

```python
# SPIKE ONLY — measure, then keep or revert based on findings.
_HEAVY_GROUPS = {"sprout_cov", "docker_e2e", "coverage_e2e", "zephyr_fanout"}

def pytest_collection_modifyitems(config, items) -> None:
    def is_heavy(item):
        m = item.get_closest_marker("xdist_group")
        return bool(m and m.args and m.args[0] in _HEAVY_GROUPS)
    items.sort(key=lambda it: 0 if is_heavy(it) else 1)
```

> Note: this repo already defines `pytest_collection_modifyitems` in `tests/integration/host/conftest.py` (the embedded grouping). Two hooks at different conftest levels both run; ensure the reorder runs *after* grouping markers are applied (root conftest hooks run after deeper ones for `modifyitems`? verify — if not, do the reorder in the same hook that already exists, or guard ordering).

- [ ] **Step 2: Measure with and without the hook**

Run 3× each, median wall, `--no-cov`, full gate scope:
`uv run pytest -m "not stability" -q` (with hook) vs the Phase-0 baseline (~80s).
Capture when `sprout_cov` finishes (grep the `-v` log for its completion %).

- [ ] **Step 3: Record the finding + decision**

Write `2026-06-23-frontload-spike-findings.md`: did the heavy groups dispatch earlier? did wall-clock drop beyond noise (±20s)? Decision: **KEEP** (proceed to Task 4) or **DROP** (revert; close Phase 3).

### Task 4: Productionize the reorder hook (only if the spike says KEEP)

**Files:**
- Modify: `tests/conftest.py` (or the existing `tests/integration/host/conftest.py` hook) — add the validated reorder
- Test: `tests/unit/test_frontload_ordering.py` (collection-order assertion, no VM)

- [ ] **Step 1: Write the failing ordering test**

```python
# tests/unit/test_frontload_ordering.py
"""Heavy serial groups are collected ahead of the parallel pool so loadgroup
dispatches them first (validated by the Phase-3 spike)."""
def test_heavy_groups_sort_first():
    from tests.conftest import _HEAVY_GROUPS, _frontload_key  # names finalized in impl
    # Given a small list of (group, name) fakes, _frontload_key puts heavy first.
    assert _frontload_key(("sprout_cov", "t")) < _frontload_key((None, "u"))
```

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/unit/test_frontload_ordering.py -v` → FAIL (`_frontload_key` not defined).

- [ ] **Step 3: Implement the reorder** as a named, tested helper `_frontload_key(group_and_name)` plus the `pytest_collection_modifyitems` reorder that sorts heavy groups first, in the conftest the spike validated (root `tests/conftest.py`, or fold into the existing `tests/integration/host/conftest.py` hook if ordering requires it). Use the exact mechanism the spike found to actually steer dispatch — not just collection order if that proved insufficient.

- [ ] **Step 4: Run to verify pass** — the ordering test passes; `uv run pytest -m "not stability" -q` (live bed) stays green; median wall over 3 runs improved beyond the ±20s noise floor.

- [ ] **Step 5: Stage** `tests/conftest.py` (or the host conftest) + `tests/unit/test_frontload_ordering.py`.

> If the spike said DROP, skip Task 4 entirely and record the drop in the plan ledger.

---

## Phase 4 — Unix host-pool lease (the #1 lever)

Spreads transfer/command tests off carrot across `{carrot, tomato, pepper}` via a cross-worker file lock built on the proven `_console_lock` fd-flock idiom. Depends on Phase 1 (pepper direct).

### Task 5: The lease primitive

**Files:**
- Create: `tests/_fixtures/_host_pool.py`
- Test: `tests/unit/test_host_pool_lease.py` (no VM — pure lock mechanics over `tmp_path`)

**Interfaces:**
- Consumes: `fcntl`, `tmp_path` (a shared `lock_dir`).
- Produces: `UNIX_POOL: tuple[str, ...]` and `lease_unix_host(lock_dir: Path, candidates: Sequence[str] = UNIX_POOL) -> Iterator[str]` (a context manager yielding the leased element name).

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_host_pool_lease.py
"""Cross-worker host-pool lease mechanics (no VM — flock over a tmp dir)."""
from __future__ import annotations

import multiprocessing as mp
import time
from pathlib import Path

from tests._fixtures._host_pool import lease_unix_host, UNIX_POOL


def test_lease_yields_a_pool_member(tmp_path: Path) -> None:
    with lease_unix_host(tmp_path) as element:
        assert element in UNIX_POOL


def test_two_leases_pick_distinct_hosts(tmp_path: Path) -> None:
    with lease_unix_host(tmp_path) as a:
        with lease_unix_host(tmp_path) as b:
            assert a != b  # second lease skips the busy first host


def test_lease_releases_on_exit(tmp_path: Path) -> None:
    with lease_unix_host(tmp_path, candidates=["carrot"]) as a:
        assert a == "carrot"
    # carrot is free again — re-leasing the single-host pool succeeds immediately
    start = time.monotonic()
    with lease_unix_host(tmp_path, candidates=["carrot"]) as b:
        assert b == "carrot"
    assert time.monotonic() - start < 1.0


def _hold(lock_dir: str, secs: float, q) -> None:
    with lease_unix_host(Path(lock_dir), candidates=["carrot"]):
        q.put("held")
        time.sleep(secs)


def test_lease_is_cross_process(tmp_path: Path) -> None:
    """A second process blocks until the first releases (cross-worker safety)."""
    q = mp.Queue()
    p = mp.Process(target=_hold, args=(str(tmp_path), 0.5, q))
    p.start()
    assert q.get(timeout=5) == "held"
    start = time.monotonic()
    with lease_unix_host(tmp_path, candidates=["carrot"]):
        waited = time.monotonic() - start
    p.join()
    assert waited >= 0.3  # had to wait for the other process to release
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/test_host_pool_lease.py -v`
Expected: FAIL (module does not exist).

- [ ] **Step 3: Implement the primitive**

```python
# tests/_fixtures/_host_pool.py
"""Cross-worker lease of one free Unix host from a small pool.

A transfer/command test that needs *a* Unix host (not a specific one) leases
whichever pool member is free, spreading load off the historical favourite
(carrot). Built on the same fd-flock idiom as ``_console_lock``: a non-blocking
``LOCK_EX`` on a per-host lock file claims it; closing the fd in ``finally``
releases it even if a pytest-timeout signal interrupts the holder.
"""
from __future__ import annotations

import fcntl
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Sequence

# The veggies-lab Unix peers with identical transfer backends. Pepper is leased
# directly (no carrot hop — see the Phase-1 lab-data simplification).
UNIX_POOL: tuple[str, ...] = ("carrot", "tomato", "pepper")

_POLL_SECONDS = 0.05


@contextmanager
def lease_unix_host(
    lock_dir: Path, candidates: Sequence[str] = UNIX_POOL
) -> Iterator[str]:
    """Lease the first free host in ``candidates``; yield its element name.

    ``lock_dir`` must be common to every xdist worker (use
    ``tmp_path_factory.getbasetemp().parent``). Polls until a host is free;
    holds an exclusive flock for the lease, released by closing the fd in
    ``finally`` — correct even if a pytest-timeout signal interrupts the holder.
    """
    while True:
        for element in candidates:
            fd = os.open(
                str(lock_dir / f"unix_pool.{element}"),
                os.O_RDWR | os.O_CREAT,
                0o644,
            )
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                os.close(fd)  # busy — try the next candidate
                continue
            try:
                yield element
                return
            finally:
                os.close(fd)  # releases the flock
        time.sleep(_POLL_SECONDS)  # all busy — back off and retry
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/unit/test_host_pool_lease.py -v` → PASS (all four).

- [ ] **Step 5: Stage**

```bash
git add tests/_fixtures/_host_pool.py tests/unit/test_host_pool_lease.py
```

### Task 6: Make `transfer_host` pool-aware + audit for host-specific assumptions

**Files:**
- Modify: `tests/conftest.py:566-580` (`transfer_host` fixture)
- Modify (if needed): transfer tests asserting carrot-specific values
- Test: existing `tests/integration/host/test_unix_host_integration.py` transfer tests (live bed)

**Interfaces:**
- Consumes: `lease_unix_host`, `tmp_path_factory`.
- Produces: `transfer_host` now leases a pool member instead of hardcoding `"carrot"`.

- [ ] **Step 1: Audit transfer tests for carrot-specific assumptions**

Run: `grep -rn "carrot\|10\.10\.200\.11\|transfer_host" tests/integration tests/e2e --include=*.py`
List any test that asserts a carrot-specific hostname/IP/path. Each such test either (a) is broadened to read the leased host's identity, or (b) opts out with an explicit pin (see Step 4). Record the list in the task report.

- [ ] **Step 2: Change `transfer_host` to lease**

```python
@pytest_asyncio.fixture
async def transfer_host(request, tmp_path_factory):
    """Integration host leased from the Unix pool, parameterized by transfer
    type ('scp', 'sftp', 'ftp', 'nc') or a ``(transfer, term)`` tuple.

    Leases a free host from ``UNIX_POOL`` instead of always using carrot, so
    the transfer tests spread across the veggies-lab peers (carrot/tomato/pepper)
    rather than serializing on one VM.
    """
    param = request.param
    lock_dir = tmp_path_factory.getbasetemp().parent
    with lease_unix_host(lock_dir) as element:
        if isinstance(param, tuple):
            transfer, term = param
            h = make_host(element, transfer=transfer, term=term)
        else:
            h = make_host(element, transfer=param)
        try:
            yield h
        finally:
            await h.close()
```

Add the import at the top of `tests/conftest.py`: `from tests._fixtures._host_pool import lease_unix_host`.

- [ ] **Step 3: Run the transfer tests (live bed) + repeat**

Run 3×: `uv run pytest tests/integration/host/test_unix_host_integration.py -k "Transfer" -m integration -q`
Expected: all PASS each run (scenarios unchanged), now spread across hosts. Confirm via `-v` that different `gw` workers hit different VMs.

- [ ] **Step 4: Provide a pin opt-out for any carrot-specific test found in Step 1**

If the audit found a test that must run on a specific host, give it an explicit host (bypass the lease) rather than weakening the assertion — e.g. a small `pinned_transfer_host` fixture or a direct `make_host("carrot", ...)`. Do not delete the assertion.

- [ ] **Step 5: Equivalence + stage**

Run: `uv run pytest --collect-only -q -m integration | sort > /tmp/after.txt` and diff against a pre-change collection — the set of test IDs is unchanged.
```bash
git add tests/conftest.py  # + any audited test files
```

### Task 7: Measure the host-pool win (median of 3) + full gate

- [ ] **Step 1:** Run 3× `make coverage`; record median wall + coverage% (must stay ≥90%, ~91.85%).
- [ ] **Step 2:** Compare to the Phase-0 baseline; attribute the delta to the pool. Record in the task report.
- [ ] **Step 3:** Run the full gate: `make coverage`, `ty check`, `make docs`, `uv run pytest tests/unit/test_tier_marker_invariants.py`. All green.

---

## Phase 5 — nox: run the unit×5 sessions concurrently

`make nox` runs `tests_all` (full suite) sequentially on all 5 Pythons (~804s). The bed must stay sequential, but the bed-free unit tier is re-run inside each. Split so the unit sessions run concurrently while the bed sessions stay sequential — full 5× matrix preserved. **Mechanism is decided by a short spike** (nox has no built-in parallel session runner).

### Task 8: Spike — choose the unit-concurrency mechanism

**Files:**
- Create: `docs/superpowers/specs/2026-06-23-nox-concurrency-findings.md`

- [ ] **Step 1:** Evaluate two options and pick one:
  - **(A) Makefile orchestration:** a `make nox` target that launches the five `tests_unit-3.x` sessions in parallel (`nox -s "tests_unit-3.10" & … wait`) and runs the bed (`tests_unix` + `tests_embedded`, or a bed-only `tests_all`) sequentially. Coverage gate stays on the bed session.
  - **(B) A single bed session ×5 sequential + a unit session ×5 parallel**, splitting `tests_all` into `tests_bed` (marker `integration or embedded`, `--cov-fail-under=85`) + reuse of `tests_unit`.
- [ ] **Step 2:** Confirm the concurrent unit sessions don't collide (each is bed-free, its own venv + coverage file; verify coverage files don't clobber — use per-session `--cov-report` paths / `COVERAGE_FILE`).
- [ ] **Step 3:** Record the choice + the exact commands in the findings doc.

### Task 9: Implement the chosen nox structure

**Files:**
- Modify: `noxfile.py` (add a bed-only session if option B) and/or `Makefile` (the `nox` target)
- Test: a dry-run assertion + a real `make nox`

- [ ] **Step 1:** Implement per the spike. Keep per-session JUnit (`_junitxml`) and the 85% bed coverage gate.
- [ ] **Step 2:** `make nox` runs green, 5/5 Pythons, full bed on each. Record median wall over 2 runs vs the 804s baseline.
- [ ] **Step 3:** Stage `noxfile.py` / `Makefile`.

> Coverage gate note: the unit sessions enforce their own threshold (currently 80%); the bed session keeps 85%. Do not let splitting drop the combined guarantee — if the matrix previously asserted a combined 85%, keep a session that does so.

---

## Phase 6 — docker_e2e parallelization (infra-gated)

**Blocked on a Chris-owned Vagrant change:** docker on all three Unix VMs + the otto test images present on each. Until that lands, docker_e2e stays one group, front-loaded (Phase 3) — no regression. This phase is additive.

### Task 10: Verify the infra (gate)

- [ ] **Step 1:** After Chris's redeploy, confirm each of `{carrot, tomato, pepper}` has a working docker daemon and the otto test images: for each, `otto host <id> run "docker info"` (or the lab equivalent) succeeds and the required images are present.
- [ ] **Step 2:** If any VM lacks docker or images, STOP — report to Chris; do not proceed.

### Task 11: Make docker tests pool consumers

**Files:**
- Modify: `tests/_fixtures/lab_data/tech1/hosts.json` (add `"docker_capable": true` to carrot + tomato)
- Modify: `tests/e2e/docker/` fixtures (the docker target host + the `docker_e2e` group)
- Modify: the docker host fixture so the target daemon is the **leased** host, not hard-pinned to pepper
- Modify: the completion-cache / docker-CLI tests that assert the docker-host id list
- Test: the existing docker e2e tests (live bed)

**Interfaces:**
- Consumes: `lease_unix_host` (Phase 4), the per-test `fresh_suffix` (already unique).

> The `Vagrantfile` provisioning (docker on all three VMs) lands separately, *before* execution, so Chris can redeploy — this task is purely test-side.

- [ ] **Step 1: Flip the lab-data flag + fix the coupled selection tests.** Add `"docker_capable": true` to carrot + tomato in `tech1/hosts.json`. This changes `collect_docker_capable_host_ids` (`src/otto/configmodule/completion_cache.py:542`) from `[pepper]` to all three, which flows into docker-CLI parent selection (`src/otto/cli/docker.py:229`, `src/otto/docker/compose.py:422`) and the completion cache/schema. Run `uv run pytest tests/unit/docker tests/unit/configmodule -v` and update any test that snapshots the docker-host list (and the completion-cache schema/version if it embeds it). Do NOT weaken an assertion — update it to expect the 3-host set.
- [ ] **Step 2:** Replace the single `xdist_group("docker_e2e")` with per-lease grouping (or remove the group and lease a host per test, so xdist distributes them and each runs against its own daemon). Parametrize the docker target off the leased element.
- [ ] **Step 3:** Run the docker e2e tests (live bed) 3×; all PASS, now spread across daemons. Confirm via `-v` that tests hit different hosts and no compose project/container-name collisions occur (the `fresh_suffix` guarantees unique names).
- [ ] **Step 4:** Measure median wall vs the grouped baseline; expect ~3× on the docker chain.
- [ ] **Step 5:** Equivalence (same test IDs) + full gate green + stage.

---

## Validation & equivalence protocol (applies to every phase)

1. **Scenario equivalence:** `pytest --collect-only -q` before/after → identical test-ID set (modulo deliberate group renames). Same passed count green.
2. **Coverage equivalence:** `make coverage` ≥ 90%, within noise of ~91.85%.
3. **Speed:** median over N≥3 runs (`--no-cov` for scheduling isolation; with-cov for the real gate), report spread.
4. **Full gate before "done":** `make coverage`, `make nox` (5/5), `ty check`, `make docs`, `tests/unit/test_tier_marker_invariants.py`.

A wedged bed / flaky run fails loudly (host named) and is distinguished from a regression by re-running — never a silent skip.

## Sequencing

Phase 1 → 2 → 3(spike) → 4 → 5 → 6(infra-gated). Phases 1, 2, 4, 5 are deterministic; 3 and 6 lead with a spike/gate and may slot in or drop without blocking the rest. Each phase lands with its own median-of-N measurement and the full gate.

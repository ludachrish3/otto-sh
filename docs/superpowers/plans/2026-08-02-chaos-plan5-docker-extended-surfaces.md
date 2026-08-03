# Chaos Plan 5 — Docker + Extended Surfaces (privilege, login/terminal) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close out the chaos-hardening workstream's final surface set — the tier-3 docker scenario catalog (kill / pause / restart / daemon-restart / pile-up / exec-never-returns), the privilege (`as_user`) interrupt scenario, and the login/terminal state assertions — plus the docker probes BedHygiene deliberately deferred to this plan, a GitHub-nightly loopback leg for the docker slice, and the Plan 4 deferred-minors cleanup.

**Architecture:** Docker chaos scenarios are **library-driven** (in-process `Repo`/`Lab`/`compose_up` against a real docker daemon through a real asyncssh `UnixHost` parent), venue-selected by `OTTO_CHAOS_DOCKER`: default = bed host **pepper** (`10.10.200.13`, the designated docker host the orphan reaper patrols); `loopback` = an sshd on `127.0.0.1` wrapping the local daemon (GitHub `ubuntu-latest`, reusing tier-2's `_sshd.py` harness). Privilege chaos rides the existing leased-bed + hygiene-bracket machinery. Login/terminal gaps (SIGWINCH uninstall, stdin-reader join) are provable in-process, so they land as hostless unit tests on `_run_bridge`. Two small product changes shield the last unshielded compensating actions this plan's scenarios exercise: `composed()`'s teardown and `HostSession.as_user`'s undo chain.

**Tech Stack:** pytest + pytest-asyncio, asyncssh, docker + docker compose (real daemons only — no docker mocking outside `tests/unit/`), the Plan 3 loopback-sshd harness (`tests/integration/chaos/_sshd.py`), the Plan 4 tier-3 toolkit (`tests/e2e/chaos/_bed.py`, `_seed.py`, `tests/_fixtures/bed_hygiene.py`), `otto.lifecycle.compensate`.

## Judgment calls made by this plan (reviewers: these are deliberate)

1. **Docker venue = pepper on the lab leg, fixed — not the chaos lease pool.** Tomato is telnet-first (cannot parent containers: `DockerContainerHost._make_session` requires `term == "ssh"`); pepper is the host `reap_orphan_docker_stacks` patrols and the one whose daemon serves only tests. The chaos lease (carrot/tomato) stays orthogonal, so docker scenarios and session/reboot scenarios never contend. Pepper access is serialized via the same fd-flock (`lease_unix_host(lock_dir, ["pepper"])`) the docker suites use.
2. **Library-driven scenarios, not `spawn_otto`.** The chaos here is remote-side (`docker kill/pause/restart`, daemon restart), not signal-side — signal machinery is already covered by tiers 1–2 and Plan 4. Driving `host.run()`/`composed()` in-process gives seeded-offset injection without SUT/CLI plumbing and works identically on both venues. The one spec entry that pairs an in-container hang with an interrupt ("exec-that-never-returns + interrupt") is realized as a seeded task-cancel against `run()` — the same cancellation path a CLI SIGINT produces after Plan 1's `run_command` translates the signal into task cancellation.
3. **`pytest.mark.integration` as the docker module's primary resource marker, even though the nightly runs it hostlessly.** The e2e conftest demands exactly one of {hostless, integration, embedded}; the lab leg (default venue) genuinely needs a VM. The nightly job selects by explicit file path with `OTTO_CHAOS_DOCKER=loopback`, where the marker is inert. No new pytest marker (`docker`) is introduced — the repo has never had one, and selection-by-file + `xdist_group` is how every existing docker suite already serializes.
4. **The tier-3 hygiene bracket becomes lazy** (`request.getfixturevalue` after the opt-out check) instead of growing a venue branch. Today `_bed_hygiene_bracket(request, chaos_bed)` instantiates the session bed lease even for `no_hygiene_bracket` tests — harmless on the lab, fatal on a GitHub runner with no bed route. Laziness also stops `test_console_chaos.py` (embedded) from pointlessly leasing a unix host.
5. **Two product `compensate()` additions ride this plan:** `composed()`'s `finally` (an interrupt landing during `compose_down` currently tears the teardown — the pile-up scenario hammers exactly this window) and `HostSession.as_user`'s undo loop (the named-session twin of the shielded `PosixPrivilege.as_user`; the spec's call-site list named only `privilege.py`, and the session variant was simply missed). Both are small, mirror existing shielded siblings, and get deterministic unit tests. Docker `put`/`get` staging-rm compensation was considered and **deferred** — no Plan 5 scenario exercises docker transfer, and the spec's docker catalog doesn't name it.
6. **`docker restart` (not stop/start) is the reboot analog, and same-container-id is asserted** — that is what distinguishes restart (id preserved, session channel dies, `SessionManager` rebuilds against the same cid) from a compose recreate (new id, re-registration path). The scenario locks the cheap recovery contract in.
7. **Pile-up rounds recover with `compose_down` after each interrupted flow.** A cancel landing inside `compose_up` (before `composed()`'s `try`) legitimately strands a half-up stack — teardown never had a chance; the spec's SIGKILL pattern (characterize + recovery reconciles) applies. Zero-accumulation is asserted after recovery, plus explicit `cleanup_project()` so the staging probe ends clean.
8. **Loopback certification is post-merge** via a `workflow_dispatch` nightly run (Plan 4's `chaos-tier2` precedent) — the dev VM has no docker daemon (`command -v docker` is empty), so the loopback path cannot run locally. Pre-merge certification is the pepper leg. The venue split only changes parent-host construction; every scenario line is venue-shared.
9. **The Plan 4 deferred-minors cleanup rides along as one task** so the single certifying `make chaos` bed run covers both the minors and the new scenarios (they were fable-triaged as "need ONE certifying bed run together").
10. **Daemon-restart requires passwordless sudo and fails loud if absent** (`sudo -n true` probed in the harness smoke test, failure names the host) — never skip on host-down/misconfig, per standing rule.

## Global Constraints

- **Default gates unchanged.** `make coverage` selection must be identical before/after: every new e2e module double-stamps `chaos` + `stability` (guard G5 enforces; modules must sit **flat** in `tests/e2e/chaos/` — the guard globs non-recursively) and carries exactly one primary resource marker + `xdist_group("chaos_lane")` + a `timeout`.
- **Real docker only in the chaos lane and existing docker suites** — the #139 default-lane docker rule stands; unit tests mock.
- **Seeded injection only in tier 3**; every random offset comes from the `chaos_rng` fixture (printed seed = reproduce handle) via `offset_in(rng, lo, hi)`.
- **Compose project names must contain `-e2e-`** (reaper coverage: `_ORPHAN_PROJECT_FRAGMENTS = ("-e2e-", "-noexist-")`).
- **Bed discipline:** docker chaos targets pepper only; the chaos lane assumes exclusive bed use; never touch eth1; scenarios end with the bed clean (the module hygiene bracket enforces it); real reboots/power stay out of this plan entirely.
- **No `from __future__ import annotations`** in any new file. No bare `# noqa` — every suppression carries a same-line `— reason`.
- **Gates per task (run FOREGROUND, `timeout: 600000` on the Bash call, never background):** scoped pytest RED then GREEN → `uv run nox -s lint` → `make typecheck-python` → `make coverage` for hostless tasks. Bed tasks additionally run their scoped scenario leg against the live bed (controller confirms the bed is free first).
- **Commits per task** with an `Assisted-by:` trailer; worktree self-commit OK, never push.
- `DEFAULT_COMMAND_TIMEOUT` is 30 s; docker `cp`/`compose` calls that scale with payload deliberately pass `float("inf")` — do not "fix" that.

## Execution venue notes (controller)

- Tasks 1, 2, 3, 8, 10 are hostless. Tasks 4–7 and 9's certification need the bed (pepper for docker, the chaos lease for privilege, veggies hosts for the minors' `make chaos` run). **Get Chris's explicit go-ahead for the bed leg — including docker daemon restarts on pepper — before dispatching Task 4.**
- The plan doc is authored untracked in the MAIN checkout: copy it into the SDD worktree, commit it there as part of Task 1's commit, and **delete the stray main-checkout copy immediately** (Plan 4's squash-merge was blocked by exactly this stray).
- Final certification: `make coverage` (hostless) + one `make chaos` bed run (new modules + minors together) + post-merge `workflow_dispatch` of nightly.yml for the loopback slice.

---

## File structure

- `tests/e2e/chaos/conftest.py` — modify: lazy bed lease in `_bed_hygiene_bracket` (extract testable `_hygiene_bracket_impl`).
- `tests/unit/test_bed_hygiene.py` — extend: docker-probe snapshot/diff tests; lazy-bracket unit tests appended here too (same guard-family home as the marker invariants is wrong — this file already owns hygiene behavior).
- `tests/_fixtures/bed_hygiene.py` — extend: `docker_containers` + `docker_networks` probes/fields/diff.
- `src/otto/docker/compose.py` — modify: shield `composed()` teardown with `compensate()`.
- `src/otto/host/session.py` — modify: shield `HostSession.as_user` undo with `compensate()`.
- `tests/unit/docker/test_compose.py`, `tests/unit/host/test_session.py` — extend: cancellation tests for both shields.
- `tests/e2e/chaos/_docker.py` — create: venue resolution + parent-host construction (pepper | loopback).
- `tests/e2e/chaos/test_docker_chaos.py` — create: fixtures + smoke + all six docker scenarios.
- `tests/e2e/chaos/test_privilege_chaos.py` — create: the as_user interrupt scenario.
- `tests/unit/host/test_interact.py` — extend: `_run_bridge` SIGWINCH-uninstall + reader-join + termios-restore tests.
- `tests/e2e/chaos/_bed.py`, `test_tunnel_link_chaos.py`, `test_connection_drop.py`, `test_reboot_chaos.py`, `test_transfer_chaos.py` — modify: Plan 4 minors (helper hoist, expire bump, reap hardening).
- `.github/workflows/nightly.yml` — extend: `chaos-docker` loopback job.
- `docs/contributing.md`, `todo/chaos-*-followups.md` — bookkeeping.

---

### Task 1: Lazy bed lease in the tier-3 hygiene bracket

The autouse `_bed_hygiene_bracket` takes `chaos_bed` as a direct parameter, so pytest instantiates the session-wide bed lease before the `no_hygiene_bracket` opt-out is ever consulted. Docker scenarios on a GitHub runner (and the embedded console module today) must be able to opt out without a bed route existing at all.

**Files:**
- Modify: `tests/e2e/chaos/conftest.py`
- Test: `tests/unit/test_bed_hygiene.py`

**Interfaces:**
- Produces: `_hygiene_bracket_impl(request) -> Iterator[None]` (module-level generator function in `tests/e2e/chaos/conftest.py`; the fixture delegates to it). Contract: consults `request.node.get_closest_marker("no_hygiene_bracket")` FIRST; only when absent does it call `request.getfixturevalue("chaos_bed")`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_bed_hygiene.py`:

```python
# ---------------------------------------------------------------------------
# Tier-3 bracket laziness: the bed lease must not be instantiated for tests
# that opt out via @pytest.mark.no_hygiene_bracket. On a GitHub runner
# (loopback docker venue) there is no bed route at all, so an eager
# `chaos_bed` parameter would fail every opted-out test at fixture setup.
# ---------------------------------------------------------------------------


class _StubNode:
    def __init__(self, marker: object) -> None:
        self._marker = marker

    def get_closest_marker(self, name: str) -> object:
        assert name == "no_hygiene_bracket"
        return self._marker


class _StubRequest:
    def __init__(self, marker: object) -> None:
        self.node = _StubNode(marker)
        self.fixture_requests: list[str] = []

    def getfixturevalue(self, name: str):
        self.fixture_requests.append(name)
        raise _LeaseTouched(name)


class _LeaseTouched(Exception):
    pass


def test_opted_out_bracket_never_touches_the_bed_lease():
    from tests.e2e.chaos.conftest import _hygiene_bracket_impl

    request = _StubRequest(marker=object())  # marker present -> opt out
    gen = _hygiene_bracket_impl(request)
    next(gen)  # runs to the bare yield without requesting any fixture
    assert request.fixture_requests == []
    with pytest.raises(StopIteration):
        next(gen)


def test_bracketed_test_requests_the_lease_lazily():
    from tests.e2e.chaos.conftest import _hygiene_bracket_impl

    request = _StubRequest(marker=None)  # no marker -> bracket engages
    gen = _hygiene_bracket_impl(request)
    with pytest.raises(_LeaseTouched):
        next(gen)  # first thing the engaged branch does is request chaos_bed
    assert request.fixture_requests == ["chaos_bed"]
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/test_bed_hygiene.py -k bracket -v` (foreground, timeout 600000)
Expected: FAIL — `ImportError: cannot import name '_hygiene_bracket_impl'`.

- [ ] **Step 3: Implement**

In `tests/e2e/chaos/conftest.py`, replace `_bed_hygiene_bracket` with a delegating fixture plus the impl (keep the existing docstring, moved onto the impl; drop the `chaos_bed` parameter):

```python
def _hygiene_bracket_impl(request):
    """Snapshot/diff the leased host around EVERY scenario (spec: BedHygiene).

    [... existing docstring body unchanged ...]

    The bed lease is requested LAZILY (getfixturevalue after the opt-out
    check) so `no_hygiene_bracket` tests — the embedded console module and
    the docker module, which on the GitHub loopback venue has no bed route
    at all — never instantiate the session lease.
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

    chaos_bed = request.getfixturevalue("chaos_bed")

    async def _snap():
        async with probe_host(chaos_bed.element) as host:
            return await snapshot_host(host)

    before = asyncio.run(_snap())
    yield
    after = asyncio.run(_snap())
    leftovers = diff_snapshots(before, after)
    assert not leftovers, format_hygiene_report(chaos_bed.element, leftovers)


@pytest.fixture(autouse=True)
def _bed_hygiene_bracket(request):
    yield from _hygiene_bracket_impl(request)
```

- [ ] **Step 4: Run tests to verify pass**

Run: `uv run pytest tests/unit/test_bed_hygiene.py tests/unit/test_tier_marker_invariants.py -v` — all green.

- [ ] **Step 5: Gates + commit**

`uv run nox -s lint` → `make typecheck-python` → `make coverage` (foreground, timeout 600000, never background). Commit:
`test(chaos): request the tier-3 bed lease lazily in the hygiene bracket` (this commit also carries the plan doc copied into the worktree).

---

### Task 2: BedHygiene docker probes

`tests/_fixtures/bed_hygiene.py` reserved docker probes for this plan (its docstring says so). Add container + network snapshots; the staging glob already covers `/tmp/otto-docker*`.

**Files:**
- Modify: `tests/_fixtures/bed_hygiene.py`
- Test: `tests/unit/test_bed_hygiene.py`

**Interfaces:**
- Produces: `HygieneSnapshot` gains `docker_containers: frozenset[str]` and `docker_networks: frozenset[str]` (both default-empty on docker-less hosts); `diff_snapshots` reports NEW-ONLY entries for both, labeled `"docker container"` / `"docker network"`.

- [ ] **Step 1: Write the failing tests**

Extend `tests/unit/test_bed_hygiene.py`, following the module's existing scripted-host pattern (a stub host whose `exec` returns canned output per probe command — reuse the existing helper the current snapshot tests use). New tests:

```python
@pytest.mark.asyncio
async def test_snapshot_captures_docker_containers_and_networks(scripted_host_factory):
    host = scripted_host_factory(
        {
            _DOCKER_PS_PROBE: "abc123 otto-repo1-e2e-x-api-1\n",
            _DOCKER_NET_PROBE: "bridge\notto-repo1-e2e-x_default\n",
        }
    )
    snap = await snapshot_host(host)
    assert snap.docker_containers == frozenset({"abc123 otto-repo1-e2e-x-api-1"})
    assert "otto-repo1-e2e-x_default" in snap.docker_networks


@pytest.mark.asyncio
async def test_snapshot_on_dockerless_host_is_empty_not_an_error(scripted_host_factory):
    host = scripted_host_factory({_DOCKER_PS_PROBE: "", _DOCKER_NET_PROBE: ""})
    snap = await snapshot_host(host)
    assert snap.docker_containers == frozenset()
    assert snap.docker_networks == frozenset()


def test_diff_reports_new_docker_leftovers_only(snapshot_factory):
    before = snapshot_factory(docker_containers=frozenset({"old x"}), docker_networks=frozenset({"bridge"}))
    after = snapshot_factory(
        docker_containers=frozenset({"old x", "new leaked-1"}),
        docker_networks=frozenset({"bridge", "otto-leak_default"}),
    )
    leftovers = diff_snapshots(before, after)
    joined = "\n".join(str(item) for item in leftovers)
    assert "leaked-1" in joined and "otto-leak_default" in joined
    assert "old x" not in joined  # pre-existing dirt is never misattributed
```

Adapt names to the module's real helper fixtures — the assertions and NEW-ONLY semantics are the requirement; if the existing tests build snapshots/hosts differently, mirror them exactly.

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/unit/test_bed_hygiene.py -v`: FAIL (unknown fields/probes).

- [ ] **Step 3: Implement**

In `tests/_fixtures/bed_hygiene.py`:

```python
# Docker accumulation probes (Plan 5). `-a` deliberately: exited containers
# are accumulation too (the pile-up failure mode). Tolerant of docker-less
# hosts — the guard collapses to empty output, never an error.
_DOCKER_PS_PROBE = (
    "command -v docker >/dev/null 2>&1 && docker ps -a --format '{{.ID}} {{.Names}}' || true"
)
_DOCKER_NET_PROBE = (
    "command -v docker >/dev/null 2>&1 && docker network ls --format '{{.Name}}' || true"
)
```

- Add `docker_containers: frozenset[str]` and `docker_networks: frozenset[str]` to `HygieneSnapshot`.
- In `snapshot_host`, run both probes (same `_PROBE_TIMEOUT`), split lines, strip, drop empties.
- In `diff_snapshots`, add NEW-ONLY entries (`sorted(after.x - before.x)`) labeled `docker container` / `docker network`, same shape as the existing leftovers.
- Update the module docstring: delete the "Docker probes deliberately absent — they ride Plan 5" sentence, describe the two probes.

- [ ] **Step 4: GREEN** — `uv run pytest tests/unit/test_bed_hygiene.py -v`.

- [ ] **Step 5: Gates + commit** — lint → typecheck → `make coverage` (foreground, timeout 600000). Commit: `test(chaos): BedHygiene docker container/network probes`.

---

### Task 3: Shield `composed()` teardown and `HostSession.as_user` undo

Two unshielded compensating actions remain on the surfaces this plan tests. `composed()`'s `finally` awaits `compose_down` bare — a cancel landing there half-tears the stack. `HostSession.as_user`'s `finally` runs the undo loop bare — the `PosixPrivilege` twin was shielded in Plan 2, this one was missed.

**Files:**
- Modify: `src/otto/docker/compose.py` (the `composed()` finally, ~line 385)
- Modify: `src/otto/host/session.py` (`HostSession.as_user` finally, ~line 1097)
- Test: `tests/unit/docker/test_compose.py`, `tests/unit/host/test_session.py`

**Interfaces:**
- Consumes: `otto.lifecycle.compensate(coro, *, deadline=None, what=...)` — import lazily inside the function (matches `privilege.py:161`'s convention; keeps the import-budget guard quiet).
- Produces: no signature changes; behavior only.

- [ ] **Step 1: Write the failing test — composed()**

Append to `tests/unit/docker/test_compose.py`, reusing the module's `_make_repo`/`_make_lab`/`_ok` helpers exactly as `test_composed_tears_down_when_own_true` does:

```python
@pytest.mark.asyncio
async def test_composed_teardown_survives_cancellation(tmp_path):
    """A cancel landing during composed()'s finally must not half-tear the
    stack: compose_down still completes, then the cancel re-raises
    (spec: shielded compensating actions)."""
    repo = _make_repo(tmp_path)
    lab = _make_lab()
    parent = lab.hosts["pepper_seed"]

    down_started = asyncio.Event()
    release_down = asyncio.Event()
    down_commands: list[str] = []

    async def exec_side_effect(cmd, *_, **__):
        if "label=com.docker.compose.project=" in cmd and "service=" not in cmd:
            return _ok("xyz\n")
        if "config" in cmd and "--services" in cmd:
            return _ok("api\n")
        if "label=com.docker.compose.project=" in cmd and "service=" in cmd:
            return _ok("xyz\n")
        if "compose" in cmd and " down" in cmd:
            down_started.set()
            await release_down.wait()  # hold the down so a cancel CAN land mid-teardown
            down_commands.append(cmd)
        return _ok()

    parent.exec.side_effect = exec_side_effect  # type: ignore[union-attr]

    inside = asyncio.Event()
    hold_body = asyncio.Event()

    async def flow() -> None:
        async with composed(repo, lab, own=True):
            inside.set()
            await hold_body.wait()

    task = asyncio.ensure_future(flow())
    await inside.wait()
    task.cancel()  # cancel the body -> finally's compose_down starts
    await down_started.wait()
    task.cancel()  # second cancel lands MID-teardown: compensate must hold it
    await asyncio.sleep(0)
    release_down.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert down_commands, "compose down was torn mid-flight instead of completing"
```

- [ ] **Step 2: Write the failing test — HostSession.as_user**

Append to `tests/unit/host/test_session.py`, mirroring `test_host_session_as_user_restores_previous`'s scaffolding (AsyncMock ShellSession + `HostSession("n", shell, ...)` constructor) and `test_as_user_undo_survives_cancellation`'s cancellation choreography (`tests/unit/host/test_privilege.py:530`):

```python
@pytest.mark.asyncio
async def test_host_session_as_user_undo_survives_cancellation():
    """The named-session twin of PosixPrivilege.as_user's shielded undo: a
    cancellation landing while the undo chain runs must still restore the
    prior user before re-raising."""
    from otto.host.login_proxy import Cred
    from otto.host.session import HostSession

    shell = AsyncMock(spec=ShellSession)
    shell.current_user = "alice"
    shell.expect.return_value = "Password:"

    async def _yielding_send(*_a, **_k) -> None:
        await asyncio.sleep(0)  # a real suspension per send, so a cancel CAN land mid-undo

    shell.send.side_effect = _yielding_send
    hs = HostSession(
        "n",
        shell,
        lambda *_: None,
        lambda *_: None,
        lambda _: None,
        creds=[Cred(login="root", password="rootpw")],
        host_id="n",
    )

    inside = asyncio.Event()
    release = asyncio.Event()

    async def body() -> None:
        async with hs.as_user("root"):
            inside.set()
            await release.wait()

    task = asyncio.ensure_future(body())
    await inside.wait()
    task.cancel()  # lands at release.wait(); the finally-undo starts
    await asyncio.sleep(0)
    task.cancel()  # second cancel, mid-undo: must be held by compensate
    with pytest.raises(asyncio.CancelledError):
        await task
    sent = [c.args[0] for c in shell.send.await_args_list]
    assert "exit\n" in sent, "the undo hop never ran — cancel tore the unwind"
    assert shell.current_user == "alice"
```

If the second `task.cancel()` needs an extra `await asyncio.sleep(0)` to land inside the undo (event-loop step granularity), match whatever the privilege test at `test_privilege.py:530` does — keep the two tests structurally identical.

- [ ] **Step 3: RED** — `uv run pytest tests/unit/docker/test_compose.py::test_composed_teardown_survives_cancellation tests/unit/host/test_session.py::test_host_session_as_user_undo_survives_cancellation -v`
Expected: both FAIL — the mid-teardown cancel currently aborts the awaited coroutine (no `exit\n` sent / no completed down).

- [ ] **Step 4: Implement**

`src/otto/docker/compose.py`, in `composed()`:

```python
    finally:
        if own or not was_up:
            # Teardown is a compensating action: an interrupt landing while
            # compose_down runs must not strand a half-torn stack (chaos
            # spec: shielded compensating actions). compensate() holds the
            # cancellation until the down completes (bounded by the teardown
            # deadline), then re-raises it.
            from ..lifecycle import compensate

            await compensate(
                compose_down(repo, lab, on=on, project_name=proj),
                what=f"docker compose down {proj}",
            )
```

`src/otto/host/session.py`, in `HostSession.as_user`'s `finally` (hoist the loop into a local coroutine so `compensate` can shield the whole unwind):

```python
        finally:

            async def _undo() -> None:
                for i, hop in enumerate(reversed(applied)):
                    via_login = applied[-i - 2].login if i + 1 < len(applied) else prev
                    # Full via cred (password/params intact), mirroring the
                    # forward path — keeps forward/undo symmetric for custom
                    # undo callables.
                    via = cred_for(self._creds, via_login) or Cred(login=via_login)
                    await run_undo(self, hop, via, self._host_id, self._history_prefix)
                self._session.current_user = prev

            # Same contract as PosixPrivilege.as_user's shielded undo: hold a
            # cancellation until every hop is unwound, then re-raise.
            from ..lifecycle import compensate

            await compensate(
                _undo(),
                what=f"{self._host_id}: session as_user undo to {prev or 'login user'!r}",
            )
```

- [ ] **Step 5: GREEN** — rerun the two tests, then the full modules: `uv run pytest tests/unit/docker/test_compose.py tests/unit/host/test_session.py tests/unit/host/test_privilege.py -v`.

- [ ] **Step 6: Gates + commit** — lint → typecheck → `make coverage` (foreground, timeout 600000). `src/` changed: confirm the import-budget guard passes inside `make coverage`, and skim `make docs` risk — both edits are body-only, no public signatures, no new rendered API, so no docs rebuild is required. Commit: `fix(lifecycle): shield composed() teardown and session as_user undo with compensate()`.

---

### Task 4: Docker venue harness + smoke tests  **[BED from here through Task 7 — controller confirms authorization + bed free]**

**Files:**
- Create: `tests/e2e/chaos/_docker.py`
- Create: `tests/e2e/chaos/test_docker_chaos.py` (fixtures + smoke; Tasks 5–6 extend it)

**Interfaces:**
- Consumes: `tests/integration/chaos/_sshd.py` (`free_port`, `generate_keypairs`, `write_sshd_config`, `LoopbackSshd`), `otto.host.unix_host.UnixHost`, `otto.host.options.SshOptions`, `otto.host.login_proxy.Cred`, `otto.config.repo.Repo`, `otto.config.lab.Lab`, `otto.docker` (`build_images`, `compose_up`, `compose_down`), `otto.docker.staging.cleanup_project`, `tests._fixtures._host_pool.lease_unix_host`, Task 2's extended `snapshot_host`/`diff_snapshots`/`format_hygiene_report`.
- Produces (in `_docker.py`):
  - `docker_venue() -> str` — `os.environ.get("OTTO_CHAOS_DOCKER", "pepper")`, values `"pepper" | "loopback"`.
  - `pepper_parent() -> UnixHost` — the exact constructor `tests/integration/test_docker_compose.py` uses (ip `10.10.200.13`, element `pepper`, creds vagrant/vagrant, board seed, is_virtual, term ssh, transfer scp, `docker_capable=True`).
  - `loopback_parent(work_dir: Path) -> Iterator[UnixHost]` — contextmanager: keypairs → sshd config → `LoopbackSshd` start on a free port → yield `UnixHost(ip="127.0.0.1", element="loopback", creds=[Cred(login=<getpass.getuser()>, password="unused-pubkey-auth")], board="seed", is_virtual=True, term="ssh", transfer="sftp", docker_capable=True, ssh_options=SshOptions(port=port, client_keys=[str(client_key)]))`; `finally` stops sshd. (Verify `SshOptions`' field names against `src/otto/host/options.py:74` before writing — mirror whatever `UnixHost` reads at `unix_host.py:440`.)
  - `fresh_project() -> str` — `f"otto-repo1-e2e-{uuid.uuid4().hex[:8]}"` (contains `-e2e-`: reaper-coverable).

Fixtures at the top of `test_docker_chaos.py` (module-scoped where noted):

```python
pytestmark = [
    pytest.mark.chaos,
    pytest.mark.stability,
    pytest.mark.integration,
    pytest.mark.no_hygiene_bracket,  # venue-aware bracket below owns hygiene
    pytest.mark.xdist_group("chaos_lane"),
    pytest.mark.timeout(300),
]

REPO1_DIR = Path(__file__).parents[2] / "repo1"


@pytest.fixture(scope="module")
def docker_parent(tmp_path_factory) -> Iterator[UnixHost]:
    """The docker-capable SSH parent for this venue.

    pepper (default): fd-flock lease so docker chaos never races the docker
    e2e/integration suites on the same daemon. loopback: tier-2's sshd
    harness wrapping the runner's own daemon — no bed route touched.
    """
    if docker_venue() == "loopback":
        with loopback_parent(tmp_path_factory.mktemp("loopback-sshd")) as host:
            yield host
        return
    lock_dir = tmp_path_factory.getbasetemp().parent
    with lease_unix_host(lock_dir, ["pepper"]) as _element:
        host = pepper_parent()
        yield host
        _close(host)  # asyncio.run(host.close()) helper


@pytest.fixture(scope="module")
def docker_repo() -> Repo:
    return Repo(sut_dir=REPO1_DIR)


@pytest.fixture
def docker_lab(docker_parent) -> Lab:
    lab = Lab(name="docker_chaos")
    lab.hosts[docker_parent.id] = docker_parent
    return lab


@pytest.fixture(scope="module")
def built_image(...):  # build_images(repo, parent, rebuild=False) once; assert Success/Skipped
```

plus the module's own hygiene bracket (function-scoped, autouse **in this module only**):

```python
@pytest.fixture(autouse=True)
def _docker_hygiene_bracket(docker_parent):
    """Task 2's docker-extended BedHygiene around every scenario, snapshotting
    the DOCKER parent (pepper or loopback) over fresh probe connections —
    the bed bracket is opted out module-wide (no bed lease on the loopback
    venue)."""
```

Each side runs `asyncio.run` over a **fresh** host built the same way `docker_parent` was (probe independence, mirroring `probe_host`) — for loopback, connect a fresh `UnixHost` with the same `ssh_options`; do NOT reuse the scenario's host object.

And the scenario workhorse:

```python
@pytest.fixture
def api_host(docker_parent, docker_repo, docker_lab, built_image):
    """A running repo1 `api` container host with a unique -e2e- project;
    always composed down + staging cleaned, whatever the scenario did."""
    proj = fresh_project()
    hosts = _run(compose_up(docker_repo, docker_lab, on=docker_parent.id,
                            project_name=proj, build=False))
    yield hosts["api"]
    _run(_teardown(docker_repo, docker_lab, docker_parent, proj))
    # _teardown: compose_down(..., project_name=proj)  then
    #            cleanup_project(docker_parent, proj)  — staging must not
    #            survive into the bracket's after-snapshot.
```

(`_run` = the module's asyncio.run bridge; scenarios that killed/paused their container must leave it in a state `compose down` can remove — `docker rm -f` inside `compose down` handles all of kill/pause/restart states, and `docker unpause` in the scenario's own `finally` keeps the paused case from wedging `down`.)

Smoke tests (write these first — they are the harness's RED/GREEN):

```python
def test_harness_docker_ready(docker_parent):
    """Fail loud, naming the host, if the venue can't do chaos: docker
    reachable AND passwordless sudo (the daemon-restart scenario's needs)."""
    ver = _run(docker_parent.exec("docker version --format '{{.Server.Version}}'", timeout=30))
    assert ver.status == Status.Success, f"{docker_parent.element}: docker unusable: {ver.value}"
    sudo = _run(docker_parent.exec("sudo -n true", timeout=15))
    assert sudo.status == Status.Success, (
        f"{docker_parent.element}: passwordless sudo required for daemon-restart chaos"
    )


def test_compose_roundtrip_is_hygiene_clean(api_host):
    result = _run(api_host.run("cat /etc/repo1-marker.txt", timeout=30))
    assert result.status == Status.Success
    # the autouse bracket certifies zero container/network/staging residue
```

- [ ] **Step 1:** Write `_docker.py` + the fixtures + the two smoke tests.
- [ ] **Step 2:** Collection + guard check (hostless): `uv run pytest tests/e2e/chaos --collect-only -q` and `uv run pytest tests/unit/test_tier_marker_invariants.py -v` — G5 must pass with the new module.
- [ ] **Step 3:** Bed run (controller-gated): `uv run pytest tests/e2e/chaos/test_docker_chaos.py -p no:cacheprovider --no-cov -v` (foreground, timeout 600000). Both smoke tests green against pepper; brackets clean.
- [ ] **Step 4:** Gates + commit — lint → typecheck → `make coverage`. Commit: `test(chaos): docker chaos venue harness (pepper | loopback) + smoke`.

---

### Task 5: Container-level docker scenarios (kill / pause / restart)

Extend `test_docker_chaos.py` with four scenarios. All follow one shape: start a real workload on the container host, inject the docker event at a seeded offset, assert the in-flight result is a **truthful `CommandResult`** (never a raw traceback), then assert the documented recovery works. Async orchestration runs inside a single `asyncio.run` main per test (the module's `_run` bridge); `chaos_rng` provides offsets via `offset_in`.

**Files:** Modify: `tests/e2e/chaos/test_docker_chaos.py`

- [ ] **Step 1: Write the scenarios**

```python
def test_docker_kill_mid_run(api_host, docker_parent, chaos_rng):
    """`docker kill` while a persistent-session command runs: the in-flight
    run surfaces an error result, and after `docker start` the SAME host
    object serves a fresh working shell (SessionManager rebuild)."""

    async def scenario() -> None:
        cid = api_host.container_id
        run_task = asyncio.create_task(api_host.run("sleep 300", timeout=30))
        await asyncio.sleep(offset_in(chaos_rng, 0.3, 2.0))
        kill = await docker_parent.exec(f"docker kill {cid}", timeout=30)
        assert kill.status == Status.Success, kill.value
        result = await run_task
        assert result.status == Status.Error, "a killed container must yield an error result"
        restart = await docker_parent.exec(f"docker start {cid}", timeout=30)
        assert restart.status == Status.Success, restart.value
        after = await api_host.run("echo alive", timeout=30)
        assert after.status == Status.Success and "alive" in after.value

    _run(scenario())


def test_docker_pause_wedges_run_into_timeout(api_host, docker_parent, chaos_rng):
    """`docker pause` freezes the exec channel: the bounded run times out
    truthfully (timed_out=True), and after unpause the host serves again."""

    async def scenario() -> None:
        cid = api_host.container_id
        try:
            run_task = asyncio.create_task(api_host.run("sleep 300", timeout=6))
            await asyncio.sleep(offset_in(chaos_rng, 0.3, 2.0))
            pause = await docker_parent.exec(f"docker pause {cid}", timeout=30)
            assert pause.status == Status.Success, pause.value
            result = await run_task
            assert result.status == Status.Error and result.timed_out is True
        finally:
            await docker_parent.exec(f"docker unpause {cid}", timeout=30)
        after = await api_host.run("echo thawed", timeout=30)
        assert after.status == Status.Success and "thawed" in after.value

    _run(scenario())


def test_docker_restart_mid_session_is_the_reboot_analog(api_host, docker_parent, chaos_rng):
    """`docker restart` mid-session (the CI-viable reboot stand-in): the
    container id is PRESERVED, the in-flight run errors truthfully, and the
    next run gets a rebuilt session with fresh shell state."""

    async def scenario() -> None:
        cid = api_host.container_id
        marker = await api_host.run("cd /etc && pwd", timeout=30)
        assert "/etc" in marker.value  # session state exists pre-restart
        run_task = asyncio.create_task(api_host.run("sleep 300", timeout=30))
        await asyncio.sleep(offset_in(chaos_rng, 0.3, 2.0))
        restart = await docker_parent.exec(f"docker restart {cid}", timeout=60)
        assert restart.status == Status.Success, restart.value
        result = await run_task
        assert result.status == Status.Error
        assert api_host.container_id == cid, "docker restart must preserve the container id"
        after = await api_host.run("pwd", timeout=30)
        assert after.status == Status.Success
        assert "/etc" not in after.value, "rebuilt session must have fresh shell state"

    _run(scenario())


def test_docker_restart_mid_exec(api_host, docker_parent, chaos_rng):
    """The stateless exec path during a restart: the wrapped `docker exec`
    fails on the parent and comes back as an error result, not a traceback."""

    async def scenario() -> None:
        cid = api_host.container_id
        exec_task = asyncio.create_task(api_host.exec("sleep 300", timeout=30))
        await asyncio.sleep(offset_in(chaos_rng, 0.3, 2.0))
        await docker_parent.exec(f"docker restart {cid}", timeout=60)
        result = await exec_task
        assert result.status == Status.Error
        after = await api_host.exec("echo back", timeout=30)
        assert after.status == Status.Success and "back" in after.value

    _run(scenario())
```

The "exec-that-never-returns inside a container + interrupt" spec entry:

```python
def test_exec_never_returns_then_interrupt(api_host, chaos_rng):
    """An in-container command that never returns, interrupted by task
    cancellation (the same path a CLI SIGINT produces post-Plan-1): the
    cancel propagates, the session recovers, no zombie remains in the
    container's process table."""

    async def scenario() -> None:
        run_task = asyncio.create_task(api_host.run("sleep 400", timeout=300))
        await asyncio.sleep(offset_in(chaos_rng, 0.5, 2.5))
        run_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await run_task
        after = await api_host.run("echo recovered", timeout=30)
        assert after.status == Status.Success and "recovered" in after.value
        table = await api_host.exec("ps -o args= 2>/dev/null || ps", timeout=30)
        assert "sleep 400" not in table.value, (
            "interrupted in-container command must not survive as a zombie"
        )

    _run(scenario())
```

(Duration `400` is argv-visible and unique in this module — the marker-in-ARGV rule; no comments, no substring-prefix collisions with the 311–314 family in `test_session_chaos.py`.)

- [ ] **Step 2: RED sanity (hostless):** `--collect-only` + marker guard still green.
- [ ] **Step 3: Bed run (controller-gated):** `uv run pytest tests/e2e/chaos/test_docker_chaos.py -p no:cacheprovider --no-cov -v` — all green, brackets clean. If an assertion about product behavior proves wrong on the real daemon (e.g. pause lets the write, not the read, hang), adjust the ASSERTION to the observed truthful-result contract and record the finding — never weaken "truthful result, then recovery works".
- [ ] **Step 4: Gates + commit** — lint → typecheck → `make coverage`. Commit: `test(chaos): container-level docker chaos — kill, pause, restart, hung exec`.

---

### Task 6: Daemon restart + composed() pile-up

**Files:** Modify: `tests/e2e/chaos/test_docker_chaos.py`

- [ ] **Step 1: Write the scenarios**

```python
def test_daemon_restart_under_open_session(api_host, docker_parent):
    """Restart the docker daemon while a container session is open: the next
    command errors truthfully; after the daemon returns and the container is
    started, the same host object serves again. repo1's compose sets no
    restart policy, so the container stays stopped after the daemon bounce —
    that is part of the characterized contract."""

    async def scenario() -> None:
        cid = api_host.container_id
        opened = await api_host.run("echo open", timeout=30)
        assert opened.status == Status.Success
        bounce = await docker_parent.exec("sudo -n systemctl restart docker", timeout=90)
        assert bounce.status == Status.Success, bounce.value
        for _ in range(30):  # daemon readiness poll, <=30s
            info = await docker_parent.exec("docker info >/dev/null 2>&1 && echo ready || true", timeout=15)
            if "ready" in info.value:
                break
            await asyncio.sleep(1.0)
        else:
            raise AssertionError(f"{docker_parent.element}: docker daemon never came back")
        mid = await api_host.run("echo during", timeout=15)
        assert mid.status == Status.Error, "a stopped container must not answer"
        start = await docker_parent.exec(f"docker start {cid}", timeout=60)
        assert start.status == Status.Success, start.value
        after = await api_host.run("echo revived", timeout=30)
        assert after.status == Status.Success and "revived" in after.value

    _run(scenario())
```

Pile-up (uses its own project, not `api_host` — the flows themselves compose up/down):

```python
def test_composed_pileup_leaves_zero_accumulation(docker_parent, docker_repo, docker_lab,
                                                  built_image, chaos_rng):
    """N interrupted composed() flows in a row: after per-round recovery,
    zero container accumulation, zero network accumulation, and no staging
    growth (spec's pile-up scenario). Cancels land everywhere — during up,
    during the body, during the shielded down."""
    proj = fresh_project()
    rounds = 5

    async def one_flow() -> None:
        async with composed(docker_repo, docker_lab, on=docker_parent.id,
                            project_name=proj, own=True, build=False):
            await asyncio.sleep(8.0)  # body window; most cancels land before this expires

    async def scenario() -> None:
        baseline_ps = await docker_parent.exec(_DOCKER_PS_PROBE, timeout=30)
        baseline_net = await docker_parent.exec(_DOCKER_NET_PROBE, timeout=30)
        for round_no in range(rounds):
            task = asyncio.create_task(one_flow())
            await asyncio.sleep(offset_in(chaos_rng, 0.05, 6.0))
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            # Recovery, the spec's SIGKILL pattern: a cancel inside compose_up
            # legitimately strands a half-up stack (composed()'s finally never
            # armed); down must reconcile any half-state, idempotently.
            await compose_down(docker_repo, docker_lab, on=docker_parent.id, project_name=proj)
        await cleanup_project(docker_parent, proj)
        end_ps = await docker_parent.exec(_DOCKER_PS_PROBE, timeout=30)
        end_net = await docker_parent.exec(_DOCKER_NET_PROBE, timeout=30)
        assert set(end_ps.value.split("\n")) <= set(baseline_ps.value.split("\n")) | {""}, (
            f"container accumulation after {rounds} interrupted flows:\n{end_ps.value}"
        )
        assert set(end_net.value.split("\n")) <= set(baseline_net.value.split("\n")) | {""}, (
            f"network accumulation after {rounds} interrupted flows:\n{end_net.value}"
        )
        staging = await docker_parent.exec(f"ls -d /tmp/otto-docker/{proj} 2>/dev/null || true", timeout=30)
        assert not staging.value.strip(), f"staging survived cleanup: {staging.value}"

    _run(scenario())
```

(Import `_DOCKER_PS_PROBE`/`_DOCKER_NET_PROBE` from `tests._fixtures.bed_hygiene`; import `cleanup_project` from `otto.docker.staging`, `compose_down`/`composed` from `otto.docker`. Check `cleanup_project`'s exact signature at `staging.py:130` — it takes the parent host and project string.)

- [ ] **Step 2: Bed run (controller-gated):** full module again, foreground, timeout 600000. The daemon-restart test intentionally disturbs pepper's daemon — confirm no other bed user first (exclusive-use assumption).
- [ ] **Step 3: Gates + commit** — lint → typecheck → `make coverage`. Commit: `test(chaos): docker daemon-restart and composed() pile-up scenarios`.

---

### Task 7: Privilege chaos — interrupt inside `as_user` on a leased bed host

**Files:**
- Create: `tests/e2e/chaos/test_privilege_chaos.py`

**Interfaces:**
- Consumes: `chaos_bed` + the (now lazy) default hygiene bracket — this module does NOT opt out; `tests/_fixtures/tunnel_bed.py::build_bed_host(ne, **overrides)` to build the full-creds host; `offset_in`/`chaos_rng`; the bed's `test`/`Password1` cred (present on every veggies VM in `tests/_fixtures/lab_data/tech1/lab.json`).

```python
pytestmark = [
    pytest.mark.chaos,
    pytest.mark.stability,
    pytest.mark.integration,
    pytest.mark.xdist_group("chaos_lane"),
    pytest.mark.timeout(300),
]
```

- [ ] **Step 1: Write the scenario**

```python
def test_cancel_inside_as_user_restores_login_user(chaos_bed, chaos_rng):
    """Interrupt a block running as another user: the shielded undo unwinds
    the switch, the SAME session answers as the login user afterwards, and
    the session stays usable (spec: privilege surface)."""

    async def scenario() -> None:
        host = build_bed_host(chaos_bed.element)
        try:
            login_user = (await host.run("whoami", timeout=30)).value.strip()

            switched = asyncio.Event()

            async def body() -> None:
                async with host.as_user("test"):
                    who = await host.run("whoami", timeout=30)
                    assert who.value.strip() == "test", who.value
                    switched.set()
                    await asyncio.sleep(60)  # the cancel window

            task = asyncio.create_task(body())
            await asyncio.wait_for(switched.wait(), timeout=60)
            await asyncio.sleep(offset_in(chaos_rng, 0.05, 1.5))
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

            who_now = await host.run("whoami", timeout=30)
            assert who_now.status == Status.Success
            assert who_now.value.strip() == login_user, (
                f"session stranded as {who_now.value.strip()!r} after interrupted as_user"
            )
            ok = await host.run("echo usable", timeout=30)
            assert ok.status == Status.Success and "usable" in ok.value
        finally:
            await host.close()

    asyncio.run(scenario())
```

A second case drives the same choreography through a **named session** (`HostSession.as_user`, the path Task 3 shielded) if the host API exposes named sessions on bed hosts the way `test_proxy_user_stability_integration.py` uses them — check that module first; if named-session access needs plumbing the bed host doesn't have, the unit test from Task 3 stands as the named-session proof and this module keeps the one default-session scenario (note the decision in the module docstring).

- [ ] **Step 2: Bed run (controller-gated):** `uv run pytest tests/e2e/chaos/test_privilege_chaos.py -p no:cacheprovider --no-cov -v` — green, hygiene bracket clean (this module exercises the default bracket, which after Task 1 must still engage — a regression here means laziness broke the engaged branch).
- [ ] **Step 3: Gates + commit** — lint → typecheck → `make coverage`. Commit: `test(chaos): interrupted as_user restores the login user on a live session`.

---

### Task 8: Login/terminal bridge-state unit tests (hostless)

SIGWINCH-uninstall and stdin-reader-join have zero direct coverage (the one existing `_run_bridge` test mocks both away). These are in-process contracts — provable deterministically at unit tier against the real reader thread and real termios on a PTY.

**Files:**
- Modify: `tests/unit/host/test_interact.py` (extend `TestRunBridge`)

**Interfaces:**
- Consumes: `_run_bridge(*, write_remote, read_remote, install_sigwinch, on_output_line, banner=None)`; `install_sigwinch` returns its uninstaller — instrumented closures observe both edges without touching real signal handlers. The real `_spawn_stdin_reader` selects on fd 0 with a 0.1 s poll and exits on `shutdown.set()`, so the join is provable without feeding stdin. Wrap it to capture the returned future:

```python
    @pytest.mark.asyncio
    async def test_bridge_exit_uninstalls_sigwinch_and_joins_reader(self):
        """Graceful remote-EOF exit: the SIGWINCH uninstaller runs, the real
        stdin reader thread completes, and termios on the (PTY) stdin is
        restored — the three login/terminal state guarantees the chaos spec
        names and nothing previously asserted."""
        import pty
        import termios

        remote_chunks = [b"welcome\n", b""]

        async def read_remote() -> bytes:
            return remote_chunks.pop(0)

        async def write_remote(data: bytes) -> None:
            return None

        events: list[str] = []

        def install_sigwinch():
            events.append("installed")
            return lambda: events.append("uninstalled")

        real_spawn = interact._spawn_stdin_reader
        captured: list[asyncio.Future] = []

        def spying_spawn(loop, queue, shutdown):
            fut = real_spawn(loop, queue, shutdown)
            captured.append(fut)
            return fut

        master_fd, slave_fd = pty.openpty()
        try:
            saved = termios.tcgetattr(slave_fd)

            class _PtyStdin:
                def isatty(self) -> bool:
                    return True

                def fileno(self) -> int:
                    return slave_fd

            with (
                patch.object(interact, "_spawn_stdin_reader", spying_spawn),
                patch.object(interact.sys, "stdin", _PtyStdin()),
                patch.object(interact.os, "write"),
            ):
                await _run_bridge(
                    write_remote=write_remote,
                    read_remote=read_remote,
                    install_sigwinch=install_sigwinch,
                    on_output_line=lambda _line: None,
                )

            assert events == ["installed", "uninstalled"], events
            assert captured and captured[0].done(), "stdin reader thread never joined"
            assert termios.tcgetattr(slave_fd) == saved, "termios not restored"
        finally:
            os.close(master_fd)
            os.close(slave_fd)
```

and the cancellation twin:

```python
    @pytest.mark.asyncio
    async def test_cancelled_bridge_still_uninstalls_and_joins(self):
        """A cancel mid-bridge (the SIGTERM-during-login shape after Plan 1's
        signal translation) takes the same finally: uninstall + join +
        restore all still happen, then the cancel propagates."""
```

Same scaffolding; `read_remote` blocks forever (`await asyncio.Event().wait()`), start `_run_bridge` as a task, let it settle (`await asyncio.sleep(0.05)`), `task.cancel()`, `pytest.raises(asyncio.CancelledError)` on await, then the same three assertions. (The reader-join drain is bounded at 0.5 s inside the bridge and the thread's poll is 0.1 s, so `captured[0].done()` may need a ≤1 s polling loop after the task completes — poll, don't sleep a fixed guess.)

- [ ] **Step 1: RED** — the first test fails against a broken contract only; since the product code is believed correct, prove the tests are live instead: temporarily comment `uninstall_sigwinch()` out of `interact.py`'s finally, watch the first test fail, restore. Record the mutation-proof in the task report (Plan 4 discipline: a guard must be proven able to fail).
- [ ] **Step 2: GREEN** — `uv run pytest tests/unit/host/test_interact.py -v`.
- [ ] **Step 3: Gates + commit** — lint → typecheck → `make coverage`. Commit: `test(host): bridge exit uninstalls SIGWINCH, joins the stdin reader, restores termios`.

---

### Task 9: Plan 4 deferred-minors cleanup (fable-triaged batch)

One mechanical pass over the Plan 4 chaos modules; the final `make chaos` bed run certifies the batch together with Tasks 4–7.

**Files:** `tests/e2e/chaos/_bed.py`, `test_tunnel_link_chaos.py`, `test_connection_drop.py`, `test_transfer_chaos.py`, `test_reboot_chaos.py`

- [ ] **Step 1: Apply the seven items**

1. **Hoist duplicated helpers** into `tests/e2e/chaos/_bed.py`: `_veggies_link_id()` (currently copied in three modules) and `_tunnel_target` + `_assert_eth2_netem_free` (copied in two). Public names in `_bed.py` (`veggies_link_id`, `tunnel_target`, `assert_eth2_netem_free`), imports updated, byte-identical behavior.
2. **`_LINK_IMPAIR_EXPIRE = 60` → `180`** (worst case observed ≈ 90 s; the expire is the backstop, not the schedule).
3. **Transfer module `finally`: reap a surviving listener** — after the existing reconcile, `pkill -f` the module's unique nc listener needle (argv-visible, per the marker rule) so an assertion failure can't strand it.
4. **Assert `link repair` exit code** where it is currently fire-and-forget.
5. **`finally` blocks `p.signal(9)`** any still-alive spawned `OttoProc` before `wait` (belt for the assert-failure path).
6. **`p.wait(timeout=600.0)` → `620.0`** where it exactly equals the CLI's own default (zero margin means the test races the product's own timeout).
7. **Fix the noqa-suppresses-E501 quirk** at `test_reboot_chaos.py:134/189/248` — restructure the lines so the `# noqa` carries only its stated justification and E501 is genuinely not triggered (no silent double-suppression).

- [ ] **Step 2: Hostless verification:** `uv run pytest tests/e2e/chaos --collect-only -q`, marker guard, `uv run nox -s lint`, `make typecheck-python`, `make coverage` (all foreground, timeout 600000).
- [ ] **Step 3: Commit** — `test(chaos): plan-4 deferred minors — helper hoist, expire margin, reap hardening`.

---

### Task 10: Nightly loopback job + docs + follow-up bookkeeping

**Files:**
- Modify: `.github/workflows/nightly.yml`
- Modify: `docs/contributing.md` (the test-lanes table)
- Modify: `todo/chaos-*-followups.md` (mark items this plan delivered)

- [ ] **Step 1: Add the `chaos-docker` job** (mirror `chaos-tier2`'s checkout/uv/python/sync steps; NO node/`make web` — no monitor involvement; keep the sshd-ensure step):

```yaml
  chaos-docker:
    runs-on: ubuntu-latest
    timeout-minutes: 30
    steps:
      - uses: actions/checkout@v7.0.1
      - name: Set up uv
        uses: astral-sh/setup-uv@v9.0.0
        with:
          enable-cache: true
      - name: Set up Python 3.10
        run: uv python install 3.10
      - name: Install dependencies
        run: uv sync --all-extras --dev
      - name: Ensure sshd is available
        run: command -v sshd || { sudo apt-get update && sudo apt-get install -y openssh-server; }
      - name: Tier-3 docker chaos (loopback sshd, runner daemon)
        run: OTTO_CHAOS_DOCKER=loopback uv run pytest tests/e2e/chaos/test_docker_chaos.py -p no:cacheprovider --no-cov
```

Add `chaos-docker` to the `report-failure` job's `needs:` list (and its issue-body enumeration, matching how `chaos-tier2` appears). Match the pinned action versions used elsewhere in the file exactly.

- [ ] **Step 2: docs/contributing.md** — in the lanes table, extend the chaos-lane row (or add one) naming the docker venue split: lab leg `make chaos` (pepper), GitHub nightly `chaos-docker` (loopback sshd + runner daemon), selection `-m "chaos and not embedded"`.
- [ ] **Step 3: Follow-up bookkeeping** — grep `todo/chaos-*-followups.md` for items this plan closed (docker probes in BedHygiene, composed()/session-as_user shielding, the seven minors, SIGWINCH/reader-join coverage) and mark them done with a one-line pointer at the delivering task/commit; leave everything else untouched.
- [ ] **Step 4: Verification** — `uv run python -c "import yaml, pathlib; yaml.safe_load(pathlib.Path('.github/workflows/nightly.yml').read_text())"` (parse check), then full gates: lint → typecheck → `make coverage`.
- [ ] **Step 5: Commit** — `ci(chaos): nightly loopback docker-chaos job + lane docs`.

---

## Final certification (controller)

1. `make coverage` on the branch tip — EXIT 0, foreground, timeout 600000.
2. **One `make chaos` bed run** (controller confirms bed free + Chris's authorization current): certifies the new docker/privilege scenarios AND the Task 9 minors together on the live bed. `make chaos-embedded` is untouched by this plan — run only if the final review asks.
3. Whole-branch **fable** review (Chris's standing directive), one fix wave, re-review.
4. Post-merge: `workflow_dispatch` nightly.yml, confirm `chaos-tier2` still green and `chaos-docker` green on its first pristine run (the loopback venue's first real execution — the dev VM cannot run it).

## Out of scope / deferred (recorded so nothing is rediscovered)

- Docker `put`/`get` staging-rm `compensate()` shielding — no scenario here exercises docker transfer; revisit if a docker-transfer chaos scenario is ever specced.
- `as_user` chaos **on a container** (`DockerContainerHost` inherits `PosixPrivilege` with `creds=[]` — ad-hoc passwordless `su` only; untested product corner, noted, not specced).
- Known-unfixed product edges from Plan 4's closure: `_ensure_initialized`'s `IncompleteReadError`-only suppression window (still open). The other edge listed here at spec time — `BrokenPipeError` on write to an already-closed asyncssh channel — was found and fixed incidentally by Task 6's `test_daemon_restart_under_open_session`; see `fix(host): surface a write to a dead session as a CommandResult, not BrokenPipeError` (commit `6b0d749f`).
- Real-reboot × docker interactions (pepper reboot mid-compose) — the spec's reboot venue rule keeps real reboots on the leased chaos host, and pepper is deliberately not it.

## Self-review

- **Spec coverage:** docker kill mid-exec ✓ (Task 5, kill mid-run — the persistent-exec channel, which is the channel the spec's phrase names), docker pause → deadline ✓ (Task 5), daemon restart under open session ✓ (Task 6), pile-up N interrupted composed() flows with zero stack/container accumulation + no staging growth ✓ (Task 6), exec-that-never-returns + interrupt ✓ (Task 5), `docker restart` reboot analog mid-exec AND mid-session ✓ (Task 5, two tests), privilege interrupt → original user on same session ✓ (Task 7), login/terminal termios+SIGWINCH+reader-join ✓ (Task 8 fills exactly the uncovered two; termios re-asserted in the same tests; tier-2 SIGTERM-during-login already lands in Plan 3's `test_signal_login.py`), BedHygiene `docker ps`/staging probes ✓ (Task 2; `compose ls` is realized as the network+container probes — compose project accumulation is visible as containers/networks, and `compose ls` output is a strict subset of that signal), GitHub nightly docker slice via loopback ✓ (Task 10), lab leg via `make chaos` ✓ (marker selection picks the new integration-stamped modules up automatically).
- **Placeholder scan:** every step carries real code or an exact recipe anchored to a named existing pattern (`_make_repo`/`_make_lab`, `build_bed_host`, `chaos-tier2` yaml); the two spots where the implementer must mirror a convention rather than paste (Task 2 fixture names, Task 7 named-session availability) say exactly which file settles the question. No TBDs.
- **Type consistency:** `docker_parent` (module fixture) feeds `docker_lab`/`built_image`/`api_host` consistently; `fresh_project()` return is a `str` used by `compose_up(project_name=...)` and `cleanup_project(parent, proj)`; `_hygiene_bracket_impl(request)` name matches between conftest and its unit test; `HygieneSnapshot` field names (`docker_containers`, `docker_networks`) match Task 2's tests and Task 6's probe imports.

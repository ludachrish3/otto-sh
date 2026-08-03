"""Docker chaos: venue harness + smoke (chaos hardening spec, Plan 5).

Docker-specific chaos scenarios (Tasks 5-7 append to this module) share this
harness: a docker-capable SSH parent (``docker_parent`` — pepper by default,
or the hermetic loopback sshd via ``OTTO_CHAOS_DOCKER=loopback``), repo1's
one-service compose stack staged under a fresh, uniquely-named project per
scenario (``api_host``), and a module-local hygiene bracket that snapshots
the DOCKER parent rather than the lane's leased veggies bed host — this
module opts out of the base ``_bed_hygiene_bracket`` (``no_hygiene_bracket``)
since the loopback venue has no bed route to lease at all.

pepper is serialized against ``tests/integration/test_docker_compose.py``'s
own docker suite via the same fd-flock lease that module holds
(``lease_unix_host(..., ["pepper"])``), so this lane's docker chaos never
races that daemon.
"""

import asyncio
import contextlib
import dataclasses
import logging
from collections.abc import Iterator
from pathlib import Path

import pytest

from otto.config.lab import Lab
from otto.config.repo import Repo
from otto.docker import build_images, compose_down, compose_up, composed
from otto.docker.staging import cleanup_project
from otto.host.docker_host import DockerContainerHost
from otto.host.host import Host
from otto.host.unix_host import UnixHost
from otto.utils import Status
from tests._fixtures._host_pool import lease_unix_host
from tests._fixtures.bed_hygiene import (
    _DOCKER_NET_PROBE,
    _DOCKER_PS_PROBE,
    diff_snapshots,
    format_hygiene_report,
    snapshot_host,
)
from tests.e2e.chaos._docker import docker_venue, fresh_project, loopback_parent, pepper_parent
from tests.e2e.chaos._seed import offset_in

logger = logging.getLogger(__name__)

pytestmark = [
    pytest.mark.chaos,
    pytest.mark.stability,
    pytest.mark.integration,
    pytest.mark.no_hygiene_bracket,  # venue-aware bracket below owns hygiene
    pytest.mark.xdist_group("chaos_lane"),
    pytest.mark.timeout(300),
]

REPO1_DIR = Path(__file__).parents[2] / "repo1"


# Every host this module has live at any given moment: `docker_parent`
# (added once, for the whole module) plus whatever `compose_up` registers
# (added/removed per scenario by the `api_host` fixture). `_run()` closes all
# of them at the end of EVERY call -- see its docstring for why.
_active_hosts: "list[Host]" = []


def _run(coro):
    """Sync bridge for the (async) otto.docker/host API.

    ``docker_parent`` is module-scoped and reused across many separate
    calls — including twice within ``test_harness_docker_ready`` alone, and
    again across ``built_image``/``api_host``/the test body/its teardown.
    ``ConnectionManager.ssh()`` caches and reuses its SSH connection across
    calls, and asyncssh binds a connection's internal futures to whichever
    loop was running when it opened; ``asyncio.run()`` tears that loop down
    when THIS call returns, so the NEXT call would reuse a connection wired
    to an already-closed loop and raise ``RuntimeError: ... attached to a
    different loop`` (confirmed against pepper — see the task report).

    There is also no safe way to close the stale connection from a LATER
    call — anything that touches it needs the very loop that is already
    gone, and this repo's session-wide orphaned-loop reaper
    (``tests/_fixtures/_loop_reaper.py``, wired into every test's teardown
    via ``tests/conftest.py``) actively closes any loop a test leaves open
    at its own boundary, so keeping ONE loop alive for the whole module
    isn't an option either (confirmed: it gets reaped between tests, same
    symptom one call later).

    So every call gets its own throwaway loop (like
    ``tests/e2e/chaos/_bed.py``'s ``run_probe``), and closes every
    currently-tracked host — ``docker_parent`` plus anything ``compose_up``
    registered — before that loop tears down. ``Host.close()`` is a no-op on
    an already-closed connection, so this is safe to do unconditionally on
    every call regardless of whether THIS particular coroutine touched any
    of them; the next call that needs a host simply reconnects fresh.

    Closes are ranked child-first: a ``DockerContainerHost``'s docker-exec
    channel must drain while its parent's connection is still alive
    (``DockerContainerHost.close()``'s documented contract, docker_host.py)
    — closing ``docker_parent`` first would sever that mid-drain. And each
    close is individually guarded (log-and-continue, the same idiom as
    ``ConnectionManager.close()``'s ``_teardown_step``): a raising close()
    must not skip the remaining hosts, and — since this whole pass runs in
    the ``finally`` below — must never override a real exception propagating
    out of ``coro`` with mere teardown noise.
    """

    async def _wrapped():
        try:
            return await coro
        finally:
            ranked = sorted(
                _active_hosts, key=lambda h: 0 if isinstance(h, DockerContainerHost) else 1
            )
            for host in ranked:
                try:
                    await host.close()
                except Exception as e:  # noqa: BLE001, PERF203 — log-and-continue teardown; must not mask the scenario's own exception or skip the remaining hosts
                    logger.warning(f"docker chaos bridge: {host!r} close failed: {e}")

    return asyncio.run(_wrapped())


def _close(host: UnixHost) -> None:
    """asyncio.run(host.close()) helper."""
    _run(host.close())


@pytest.fixture(scope="module")
def docker_parent(tmp_path_factory) -> "Iterator[UnixHost]":
    """The docker-capable SSH parent for this venue.

    pepper (default): fd-flock lease so docker chaos never races the docker
    e2e/integration suites on the same daemon. loopback: tier-2's sshd
    harness wrapping the runner's own daemon — no bed route touched.
    """
    if docker_venue() == "loopback":
        with loopback_parent(tmp_path_factory.mktemp("loopback-sshd")) as host:
            _active_hosts.append(host)
            try:
                yield host
            finally:
                _active_hosts.remove(host)
                _close(host)
        return
    lock_dir = tmp_path_factory.getbasetemp().parent
    with lease_unix_host(lock_dir, ["pepper"]) as _element:
        host = pepper_parent()
        _active_hosts.append(host)
        try:
            yield host
        finally:
            _active_hosts.remove(host)
            _close(host)


@pytest.fixture(scope="module")
def docker_repo() -> Repo:
    return Repo(sut_dir=REPO1_DIR)


@pytest.fixture
def docker_lab(docker_parent) -> Lab:
    lab = Lab(name="docker_chaos")
    lab.hosts[docker_parent.id] = docker_parent
    return lab


@pytest.fixture(scope="module")
def built_image(docker_parent, docker_repo):
    """Ensure the repo1 ``api`` image is built once for this module."""
    results = _run(build_images(docker_repo, docker_parent, rebuild=False))
    assert results["api"][0] in (Status.Success, Status.Skipped), results
    return results


async def _teardown(repo: Repo, lab: Lab, parent: UnixHost, project: str) -> None:
    """compose down, then clean the staging tree — staging must not survive
    into the hygiene bracket's after-snapshot."""
    down = await compose_down(repo, lab, on=parent.id, project_name=project)
    # compose_down logs-and-returns on failure rather than raising (see its
    # docstring) — check the status explicitly so a failed teardown surfaces
    # as a named assertion here instead of silently leaving a stack for the
    # autouse hygiene bracket's after-snapshot to (also silently) miss.
    assert down.is_ok, f"{project}: teardown compose_down failed (status={down})"
    await cleanup_project(parent, project)


@pytest.fixture
def api_host(docker_parent, docker_repo, docker_lab, built_image):
    """A running repo1 ``api`` container host with a unique ``-e2e-`` project;
    always composed down + staging cleaned, whatever the scenario did."""
    proj = fresh_project()
    hosts = _run(
        compose_up(docker_repo, docker_lab, on=docker_parent.id, project_name=proj, build=False)
    )
    api = hosts["api"]
    _active_hosts.append(api)
    try:
        yield api
    finally:
        try:
            _run(_teardown(docker_repo, docker_lab, docker_parent, proj))
        finally:
            _active_hosts.remove(api)


async def _snapshot_probe(base: UnixHost):
    """Snapshot over an independent connection mirroring ``base``'s
    connection params — never ``base`` itself (probe independence, mirroring
    ``tests/e2e/chaos/_bed.py``'s ``probe_host``). ``dataclasses.replace``
    with no overrides reconstructs the host from its own current field
    values (ip/creds/term/transfer/ssh_options/...), so this is exactly "a
    fresh host built the same way docker_parent was" for either venue: for
    pepper those fields are the constant literals ``pepper_parent()`` always
    uses; for loopback it keeps the already-running sshd's port/client key
    rather than starting a second daemon per snapshot.
    """
    probe = dataclasses.replace(base)
    try:
        return await snapshot_host(probe)
    finally:
        await probe.close()


@pytest.fixture(autouse=True)
def _docker_hygiene_bracket(docker_parent):
    """Task 2's docker-extended BedHygiene around every scenario, snapshotting
    the DOCKER parent (pepper or loopback) over fresh probe connections — the
    bed bracket is opted out module-wide (no bed lease on the loopback
    venue)."""
    before = _run(_snapshot_probe(docker_parent))
    yield
    after = _run(_snapshot_probe(docker_parent))
    leftovers = diff_snapshots(before, after)
    assert not leftovers, format_hygiene_report(docker_parent.element, leftovers)


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
        assert after.status == Status.Success, after.only.value
        assert "alive" in after.only.value

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
            assert result.status == Status.Error
            assert result.only.timed_out is True
        finally:
            await docker_parent.exec(f"docker unpause {cid}", timeout=30)
        after = await api_host.run("echo thawed", timeout=30)
        assert after.status == Status.Success, after.only.value
        assert "thawed" in after.only.value

    _run(scenario())


def test_docker_restart_mid_session_is_the_reboot_analog(api_host, docker_parent, chaos_rng):
    """`docker restart` mid-session (the CI-viable reboot stand-in): the
    container id is PRESERVED, the in-flight run errors truthfully, and the
    next run gets a rebuilt session with fresh shell state."""

    async def scenario() -> None:
        cid = api_host.container_id
        marker = await api_host.run("cd /etc && pwd", timeout=30)
        assert "/etc" in marker.only.value  # session state exists pre-restart
        run_task = asyncio.create_task(api_host.run("sleep 300", timeout=30))
        await asyncio.sleep(offset_in(chaos_rng, 0.3, 2.0))
        restart = await docker_parent.exec(f"docker restart {cid}", timeout=60)
        assert restart.status == Status.Success, restart.value
        result = await run_task
        assert result.status == Status.Error
        assert api_host.container_id == cid, "docker restart must preserve the container id"
        after = await api_host.run("pwd", timeout=30)
        assert after.status == Status.Success
        assert "/etc" not in after.only.value, "rebuilt session must have fresh shell state"

    _run(scenario())


def test_docker_restart_mid_exec(api_host, docker_parent, chaos_rng):
    """The stateless exec path during a restart: the wrapped `docker exec`
    fails on the parent and comes back as an error result, not a traceback."""

    async def scenario() -> None:
        cid = api_host.container_id
        exec_task = asyncio.create_task(api_host.exec("sleep 300", timeout=30))
        await asyncio.sleep(offset_in(chaos_rng, 0.3, 2.0))
        restart = await docker_parent.exec(f"docker restart {cid}", timeout=60)
        assert restart.status == Status.Success, restart.value
        result = await exec_task
        # Observed on pepper: Status.Failed, retcode=137 (SIGKILL) — the
        # wrapped `docker exec` process itself is killed when the container
        # it's attached to is torn down mid-restart, so it surfaces as an
        # ordinary nonzero-exit CommandResult (Status.Failed) rather than the
        # Status.Error the persistent-session path uses for a dead channel.
        # Either way it is a truthful CommandResult, never a raw traceback —
        # assert on that documented contract rather than a specific Status.
        assert not result.is_ok, (
            f"a docker exec racing a container restart must not report success: {result!r}"
        )
        # Falsifiability: `not is_ok` alone is also satisfied by an ordinary
        # 30s exec timeout, which would pass even if `docker restart` above
        # silently failed to fire any chaos at all. `restart.status ==
        # Success` (asserted above) confirms the chaos command itself landed;
        # this confirms the exec's own failure was the restart tearing its
        # channel down, not its unrelated timeout expiring on its own clock.
        assert not result.timed_out, "must be killed by the restart, not by the exec's own timeout"
        after = await api_host.exec("echo back", timeout=30)
        assert after.status == Status.Success, after.value
        assert "back" in after.value

    _run(scenario())


def test_exec_never_returns_then_interrupt(api_host, chaos_rng):
    """An in-container command that never returns, interrupted by task
    cancellation (the same path a CLI SIGINT produces post-Plan-1): the
    cancel propagates, the session recovers, no zombie remains in the
    container's process table."""

    async def scenario() -> None:
        run_task = asyncio.create_task(api_host.run("sleep 400", timeout=300))
        await asyncio.sleep(offset_in(chaos_rng, 0.5, 2.5))
        # Positive control: prove the probe can actually SEE the remote
        # process before trusting its later absence — otherwise "not seen"
        # after the interrupt is unfalsifiable (it would also read as a pass
        # if `ps` itself failed or came back empty in this busybox image).
        ps_probe = "ps -o args= 2>/dev/null || ps"
        before = await api_host.exec(ps_probe, timeout=30)
        assert before.status == Status.Success, before.value
        assert "sleep 400" in before.value, "positive control: sleep 400 not observed running"
        run_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await run_task
        after = await api_host.run("echo recovered", timeout=30)
        assert after.status == Status.Success, after.only.value
        assert "recovered" in after.only.value
        table = await api_host.exec(ps_probe, timeout=30)
        assert table.status == Status.Success, table.value
        assert "sleep 400" not in table.value, (
            "interrupted in-container command must not survive as a zombie"
        )

    _run(scenario())


def test_daemon_restart_under_open_session(api_host, docker_parent):
    """Restart the docker daemon while a container session is open: the next
    command errors truthfully; after the daemon returns and the container is
    started, the same host object serves again. repo1's compose sets no
    restart policy, so the container stays stopped after the daemon bounce —
    that is part of the characterized contract."""

    async def scenario() -> None:
        cid = api_host.container_id
        opened = await api_host.run("echo open", timeout=30)
        assert opened.status == Status.Success, opened.only.value
        bounce = await docker_parent.exec("sudo -n systemctl restart docker", timeout=90)
        assert bounce.status == Status.Success, bounce.value
        for _ in range(30):  # daemon readiness poll, <=30s
            info = await docker_parent.exec(
                "docker info >/dev/null 2>&1 && echo ready || true", timeout=15
            )
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
        assert after.status == Status.Success, after.only.value
        assert "revived" in after.only.value

    _run(scenario())


def test_composed_pileup_leaves_zero_accumulation(
    docker_parent, docker_repo, docker_lab, built_image, chaos_rng
):
    """5 interrupted ``composed(..., own=True)`` flows in a row: after
    per-round recovery, zero container accumulation, zero network
    accumulation, and no staging growth (spec's pile-up scenario). Cancels
    land either during ``compose_up`` (before ``composed()``'s try/finally
    arms — unshielded, can legitimately strand a half-up stack) or during the
    body (Task 3's ``compensate()`` then holds the cancellation while the
    ``finally``'s ``compose_down`` runs to completion, re-raising once it's
    done). Either way, the per-round ``compose_down`` below reconciles
    whatever state was left, idempotently — this test uses its own project,
    not the ``api_host`` fixture, since the flows themselves compose up/down."""
    proj = fresh_project()
    rounds = 5

    async def one_flow(entered: dict[str, bool]) -> None:
        async with composed(
            docker_repo, docker_lab, on=docker_parent.id, project_name=proj, own=True, build=False
        ):
            entered["body"] = True
            await asyncio.sleep(8.0)  # body window; most cancels land before this expires

    async def scenario() -> None:
        baseline_ps = await docker_parent.exec(_DOCKER_PS_PROBE, timeout=30)
        baseline_net = await docker_parent.exec(_DOCKER_NET_PROBE, timeout=30)
        # Oracle honesty: _DOCKER_PS_PROBE/_DOCKER_NET_PROBE end in `|| true`
        # so a docker-less host degrades to empty output rather than a shell
        # error -- but the exec ITSELF (the ssh channel running that probe)
        # can still fail/time out independently of that fallback. Assert it
        # explicitly so a probe failure surfaces as a named assertion instead
        # of silently deflating the baseline to empty and making every later
        # `⊆ baseline` check pass vacuously.
        assert baseline_ps.is_ok, f"baseline docker ps probe failed: {baseline_ps.value}"
        assert baseline_net.is_ok, f"baseline docker network probe failed: {baseline_net.value}"
        landings = []
        for round_no in range(rounds):
            entered = {"body": False}
            task = asyncio.create_task(one_flow(entered))
            offset = offset_in(chaos_rng, 0.05, 6.0)
            await asyncio.sleep(offset)
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            phase = "body (shielded compose_down)" if entered["body"] else "compose_up (unshielded)"
            landings.append(f"round {round_no}: offset={offset:.3f}s -> {phase}")
            # Recovery, the spec's SIGKILL pattern: a cancel inside compose_up
            # legitimately strands a half-up stack (composed()'s finally never
            # armed); down must reconcile any half-state, idempotently.
            down = await compose_down(
                docker_repo, docker_lab, on=docker_parent.id, project_name=proj
            )
            # compose_down logs-and-returns on failure rather than raising --
            # if the daemon itself died mid-pileup (it runs right after the
            # daemon-restart scenario), a silently-failed down here would
            # strand this round's stack while the loop reads as green.
            assert down.is_ok, f"round {round_no}: recovery compose_down failed (status={down})"
        logger.info(f"[pileup] cancel landings: {'; '.join(landings)}")
        await cleanup_project(docker_parent, proj)
        end_ps = await docker_parent.exec(_DOCKER_PS_PROBE, timeout=30)
        end_net = await docker_parent.exec(_DOCKER_NET_PROBE, timeout=30)
        assert end_ps.is_ok, f"end docker ps probe failed: {end_ps.value}"
        assert end_net.is_ok, f"end docker network probe failed: {end_net.value}"
        assert set(end_ps.value.split("\n")) <= set(baseline_ps.value.split("\n")) | {""}, (
            f"container accumulation after {rounds} interrupted flows:\n{end_ps.value}"
        )
        assert set(end_net.value.split("\n")) <= set(baseline_net.value.split("\n")) | {""}, (
            f"network accumulation after {rounds} interrupted flows:\n{end_net.value}"
        )
        staging = await docker_parent.exec(
            f"ls -d /tmp/otto-docker/{proj} 2>/dev/null || true", timeout=30
        )
        assert not staging.value.strip(), f"staging survived cleanup: {staging.value}"

    _run(scenario())

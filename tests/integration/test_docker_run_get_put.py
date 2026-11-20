"""Run / get / put integration tests against a real container.

Exercises the docker exec wrapper and the two-step file transfer through
a parent SSH connection.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from pathlib import Path

import pytest
import pytest_asyncio

from otto.config.project import Project
from otto.docker import build_images, compose_down, compose_up
from otto.host.element import Element
from otto.host.exec_route import ExecRoute, container_route
from otto.host.login_proxy import Cred
from otto.host.unix_host import UnixHost
from otto.labs.loader import Lab
from otto.logger.mode import LogMode
from otto.logger.relay import log_lines
from otto.utils import Status
from tests._fixtures.paths import TESTS_ROOT
from tests._fixtures.scope_boundary import held_for_scope
from tests.conformance._log import _watching_records, logged_lines, output_record
from tests.conformance._started import output_seen

PROJECT1_DIR = TESTS_ROOT / "project1"

# All docker integration tests share project1's staging dir under the login's
# private root on test3 (/tmp/otto-docker-<login>/). Pin them to one xdist
# worker so concurrent `rm -rf` calls during compose_up don't race.
pytestmark = [
    pytest.mark.xdist_group("docker_e2e"),
    # This module drives docker on test3: it asks for the orphan-stack reap.
    # The reap is requested, never ambient (tests/unit/test_docker_reaper_scope.py).
    pytest.mark.usefixtures("reap_orphan_docker_stacks"),
]


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def stack(test3_lease, ottopath_env_module):
    """Bring up project1's compose stack on test3 once per module, yield the api
    container host to all tests, then tear down. Module scope avoids paying
    ~10s of compose_up overhead for each of the 10+ tests in this file —
    the tests are written to be order-independent (each one explicitly sets
    the state it depends on, so cwd/env from a prior test never matters).

    ``loop_scope="module"`` matches the fixture scope so pytest-asyncio
    creates a module-scoped event loop that the fixture can outlive its
    creating test under (the default ``function`` loop_scope would close
    after the first test and corrupt the cached SSH connection).

    ``ottopath_env_module`` because this fixture runs at MODULE scope, ahead of
    the function-scoped ``_default_ottopath_env``: ``compose_up`` walks
    ``get_projects()``, which needs the SUT env, and the module-scoped bracket also
    keeps the bootstrap memo it primes from outliving this module.

    The parent and the container host outlive the test that connected them,
    so the fixture holds the module loop's cleanup boundary for its scope
    (``held_for_scope``). Its release closes whatever the teardown did not.
    Normally that is nothing: ``compose_down`` already closed and unregistered
    the container host, and ``parent.close()`` follows. Only when the setup
    or ``compose_down`` raises does the release close leftovers, and then it
    closes the parent too, since the raise skips ``parent.close()``."""
    async with held_for_scope("test_docker_run_get_put.py's loop"):
        parent = UnixHost(
            ip="10.10.200.13",
            element=Element(name="test3"),
            creds=[Cred(login="vagrant", password="vagrant")],
            is_virtual=True,
            term="ssh",
            transfer="scp",
            docker_capable=True,
        )
        project = Project(root=PROJECT1_DIR)
        lab = Lab(name="docker_run_test")
        lab.hosts[parent.id] = parent

        build_results = await build_images(project=project, parent=parent)
        assert build_results["project1-api"].is_ok, build_results
        hosts = await compose_up(project=project, lab=lab, parent=parent.id)
        try:
            yield hosts["api"]
        finally:
            await compose_down(project=project, lab=lab, parent=parent.id)
            await parent.close()


@pytest.mark.asyncio(loop_scope="module")
async def test_exec_returns_output_from_container(stack):
    result = await stack.exec("echo hello-from-container")
    assert result.status is Status.Success
    assert "hello-from-container" in result.value


@pytest.mark.asyncio(loop_scope="module")
async def test_exec_failing_command_reports_nonzero(stack):
    result = await stack.exec("false")
    assert result.status is Status.Failed
    assert result.retcode != 0


@pytest.mark.asyncio(loop_scope="module")
async def test_marker_file_present(stack):
    """The Dockerfile bakes in /etc/project1-marker.txt — it should be readable."""
    result = await stack.exec("cat /etc/project1-marker.txt")
    assert result.status is Status.Success
    assert "project1-fixture" in result.value


@pytest.mark.asyncio(loop_scope="module")
async def test_put_then_get_roundtrip(stack, tmp_path):
    src = tmp_path / "payload.bin"
    src.write_bytes(b"otto-docker-roundtrip-" + b"\xab" * 256)

    res = await stack.put([src], Path("/tmp"))
    assert res.status is Status.Success, res.msg

    # Verify the bytes inside the container.
    cat = await stack.exec("wc -c /tmp/payload.bin")
    assert cat.status is Status.Success
    assert "/tmp/payload.bin" in cat.value

    out_dir = tmp_path / "back"
    out_dir.mkdir()
    res = await stack.get(Path("/tmp/payload.bin"), out_dir)
    assert res.status is Status.Success, res.msg
    assert (out_dir / "payload.bin").read_bytes() == src.read_bytes()


@pytest.mark.asyncio(loop_scope="module")
async def test_run_chained_commands_in_one_string(stack):
    """Multiple commands in a single string share state via shell `&&`."""
    result = await stack.exec("cd /tmp && echo $PWD")
    assert result.status is Status.Success
    assert "/tmp" in result.value


@pytest.mark.asyncio(loop_scope="module")
async def test_run_preserves_cwd_across_calls(stack):
    """run() uses a persistent shell session — `cd` in one call must persist
    into the next, no `&&` required."""
    result = await stack.run(["cd /tmp", "pwd"])
    assert result.status is Status.Success
    assert result[-1].value == "/tmp"


@pytest.mark.asyncio(loop_scope="module")
async def test_run_preserves_env_across_calls(stack):
    """Env vars exported in one run() call must persist into the next."""
    result = await stack.run(["export OTTO_TEST_VAR=docker_persist_ok", "echo $OTTO_TEST_VAR"])
    assert result.status is Status.Success
    assert "docker_persist_ok" in result[-1].value


@pytest.mark.asyncio(loop_scope="module")
async def test_run_state_persists_across_separate_run_invocations(stack):
    """Two separate run() invocations on the same host share shell state."""
    r1 = await stack.run("cd /var")
    assert r1.status is Status.Success
    r2 = await stack.run("pwd")
    assert r2.status is Status.Success
    assert r2.only.value == "/var"


@pytest.mark.asyncio(loop_scope="module")
async def test_run_timeout_recovers_session(stack):
    """A timed-out run() must mark the session for recovery without killing
    the host's ability to run further commands. ``-it`` allocates a TTY so
    the SIGINT-on-Ctrl+C path actually works inside the container."""
    timed_out = await stack.run("sleep 2", timeout=0.1)
    assert timed_out.status is not Status.Success  # error or timeout
    # Session should recover; next command should succeed.
    recovered = await stack.run("echo recovered")
    assert recovered.status is Status.Success
    assert "recovered" in recovered.only.value


@pytest.mark.serial_timing
@pytest.mark.asyncio(loop_scope="module")
async def test_exec_remains_concurrent_safe(stack):
    """exec() must stay stateless and concurrent — two parallel sleeps
    must finish materially faster than two serial sleeps."""
    import asyncio
    import time

    # Self-calibrating: a hardcoded wall-clock threshold is flaky on busy
    # CI hosts where `docker exec` startup balloons. Compare parallel to
    # serial in the same run instead — if exec were serialized internally,
    # parallel would be ~equal to serial; with real concurrency it's roughly
    # half plus one startup cost.
    #
    # serial_timing even so: self-calibration absorbs a uniformly slow host,
    # not sibling-worker load landing INSIDE the parallel measurement window
    # — observed in a loaded gate as parallel 1.34s vs serial 0.86s, a false
    # "serialized". The discriminator rejects a path by relative elapsed
    # time, so it runs only in the -n0 lane.
    start = time.monotonic()
    s1 = await stack.exec("sleep 0.2")
    s2 = await stack.exec("sleep 0.2")
    serial = time.monotonic() - start

    start = time.monotonic()
    a, b = await asyncio.gather(
        stack.exec("sleep 0.2"),
        stack.exec("sleep 0.2"),
    )
    parallel = time.monotonic() - start

    assert s1.status is Status.Success
    assert s2.status is Status.Success
    assert a.status is Status.Success
    assert b.status is Status.Success
    # Parallel should save roughly one sleep's worth of wall time. Require
    # ≥120ms savings — generous enough to absorb scheduling jitter, tight
    # enough that genuine serialization (savings ≈ 0) fails.
    assert serial - parallel > 0.12, (
        f"exec serialized: serial={serial:.2f}s parallel={parallel:.2f}s"
    )


@pytest.mark.asyncio(loop_scope="module")
@pytest.mark.parametrize(
    ("expects", "route"),
    [
        (None, ExecRoute.SSH_CHANNEL.value),
        ([("never-printed", "")], ExecRoute.POOLED_SHELL.value),
    ],
    ids=["channel", "pooled"],
)
async def test_exec_logs_its_lines_on_the_container_while_it_runs(stack, expects, route):
    """The issued command and its first lines reach the container's log before the command ends.

    The command prints two complete lines (a terminal mapping may hold back
    the newest one), then waits on a gate file the test removes only after
    the first line was seen, so "seen while it runs" is decided by events,
    never by a clock. The parent keeps only the ``docker exec`` wrapper, at
    DEBUG. A real expect pair forces the parent's pooled shell.
    """
    parent = stack.parent
    token = uuid.uuid4().hex
    gate = f"/tmp/otto-exec-log-gate-{token}"
    cmd = (
        f"echo first-{token}; echo second-{token}; "
        f'i=0; while [ -e "{gate}" ] && [ $i -lt 600 ]; do sleep 0.1; i=$((i+1)); done; '
        f'[ ! -e "{gate}" ] || exit 73; echo last-{token}'
    )
    # Built first: it resolves the container, so container_id is set below.
    wrapped = await stack._docker_exec(cmd, user=stack._effective_user(None))
    in_ctr = f"docker exec {stack.container_id}"
    made = await parent.exec(
        f'{in_ctr} sh -c \'touch "{gate}" && [ -e "{gate}" ]\'', log=LogMode.NEVER
    )
    assert made.is_ok, made

    parent_cmds: list[logging.LogRecord] = []
    parent_outputs: list[logging.LogRecord] = []
    container_cmds: list[tuple[str, object]] = []
    container_output_modes: list[object] = []

    def on_record(record: logging.LogRecord) -> None:
        host = getattr(record, "host", None)
        relayed = getattr(record, "relayed", None)
        if host is parent and relayed == wrapped:
            parent_cmds.append(record)
        if (
            host is parent
            and relayed is not None
            and token in relayed
            and output_record(record, parent)
        ):
            parent_outputs.append(record)
        if host is stack and relayed == cmd and record.levelno == logging.INFO:
            container_cmds.append((relayed, getattr(record, "log_mode", None)))
        if host is stack and relayed is not None and token in relayed:
            found = output_record(record, stack)
            if found is not None:
                container_output_modes.append(found.log_mode)

    first = f"first-{token}"
    seen_on = None
    with (
        output_seen(stack, first) as on_ctr,
        output_seen(parent, first) as on_parent,
        logged_lines(stack) as lines,
        _watching_records(on_record, level=logging.DEBUG),
    ):
        running = asyncio.ensure_future(stack.exec(cmd, expects=expects, timeout=120))
        witnesses = {
            asyncio.ensure_future(on_ctr.wait()): "container",
            asyncio.ensure_future(on_parent.wait()): "parent",
        }
        try:
            try:
                done, _ = await asyncio.wait(
                    {*witnesses, running}, timeout=90, return_when=asyncio.FIRST_COMPLETED
                )
                seen_on = next((witnesses[w] for w in done if w in witnesses), None)
                ended = (running.exception() or running.result()) if running.done() else None
                assert seen_on == "container", (
                    f"first line witnessed on {seen_on!r} (expected 'container'); "
                    f"command done={running.done()}: {ended}"
                )
                assert not running.done(), (
                    f"the command ended before its first line was seen: {ended}"
                )
            finally:
                # The parent's own ssh channel, independent of the command's route.
                # Checked after the block: an assertion here would replace the
                # liveness failure above, which names the host that saw the line.
                removed = await parent.exec(f'{in_ctr} rm -f "{gate}"', log=LogMode.NEVER)
        finally:
            try:
                try:
                    # Bounded: once the gate is gone, the inner loop ends by itself.
                    result = await asyncio.wait_for(running, timeout=90)
                except BaseException:
                    running.cancel()
                    await asyncio.gather(running, return_exceptions=True)
                    raise
            finally:
                for w in witnesses:
                    w.cancel()
                await asyncio.gather(*witnesses, return_exceptions=True)

    assert removed.is_ok, removed
    assert result.is_ok, (seen_on, result)
    assert result.route == container_route(route), (seen_on, result)
    # The fixture's parent and container are built at their default mode and
    # nothing here suppresses either, so the chain stands at NORMAL.
    expected_mode = LogMode.NORMAL
    assert container_cmds == [(cmd, expected_mode)]
    assert container_output_modes, "no output record reached the container"
    assert set(container_output_modes) == {expected_mode}, container_output_modes
    assert lines == log_lines(result.value)
    markers = [line.rstrip("\r") for line in lines]  # the terminal mapping may keep a CR
    assert markers == [f"first-{token}", f"second-{token}", f"last-{token}"], lines
    assert [(r.levelno, getattr(r, "log_mode", None)) for r in parent_cmds] == [
        (logging.DEBUG, expected_mode)
    ], parent_cmds
    assert parent_outputs == []

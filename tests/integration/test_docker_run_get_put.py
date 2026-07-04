"""Run / get / put integration tests against a real container.

Exercises the docker exec wrapper and the two-step file transfer through
a parent SSH connection.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio

from otto.configmodule.lab import Lab
from otto.configmodule.repo import Repo
from otto.docker import build_images, compose_down, compose_up
from otto.host.login_proxy import Cred
from otto.host.unix_host import UnixHost
from otto.utils import Status
from tests._fixtures._host_pool import lease_unix_host

REPO1_DIR = Path(__file__).parent.parent / "repo1"

# All docker integration tests share /tmp/otto-docker/repo1/ on pepper
# (compose staging dir). Pin them to one xdist worker so concurrent
# `rm -rf` calls during compose_up don't race.
pytestmark = pytest.mark.xdist_group("docker_e2e")


@pytest.fixture(scope="module")
def pepper_lease(tmp_path_factory):
    """Hold the pepper fd-flock for the entire module so no e2e docker test
    can race against the integration docker tests on the same daemon."""
    lock_dir = tmp_path_factory.getbasetemp().parent
    with lease_unix_host(lock_dir, ["pepper"]) as _element:
        yield _element


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def stack(pepper_lease):
    """Bring up repo1's compose stack on pepper once per module, yield the api
    container host to all tests, then tear down. Module scope avoids paying
    ~10s of compose_up overhead for each of the 10+ tests in this file —
    the tests are written to be order-independent (each one explicitly sets
    the state it depends on, so cwd/env from a prior test never matters).

    ``loop_scope="module"`` matches the fixture scope so pytest-asyncio
    creates a module-scoped event loop that the fixture can outlive its
    creating test under (the default ``function`` loop_scope would close
    after the first test and corrupt the cached SSH connection)."""
    parent = UnixHost(
        ip="10.10.200.13",
        element="pepper",
        creds=[Cred(login="vagrant", password="vagrant")],
        board="seed",
        is_virtual=True,
        term="ssh",
        transfer="scp",
        docker_capable=True,
    )
    repo = Repo(sut_dir=REPO1_DIR)
    lab = Lab(name="docker_run_test")
    lab.hosts[parent.id] = parent

    build_results = await build_images(repo, parent, rebuild=False)
    assert build_results["api"][0] in (Status.Success, Status.Skipped)
    hosts = await compose_up(repo, lab, on=parent.id)
    try:
        yield hosts["api"]
    finally:
        await compose_down(repo, lab, on=parent.id)
        await parent.close()


@pytest.mark.asyncio(loop_scope="module")
async def test_oneshot_returns_output_from_container(stack):
    result = await stack.oneshot("echo hello-from-container")
    assert result.status is Status.Success
    assert "hello-from-container" in result.value


@pytest.mark.asyncio(loop_scope="module")
async def test_oneshot_failing_command_reports_nonzero(stack):
    result = await stack.oneshot("false")
    assert result.status is Status.Failed
    assert result.retcode != 0


@pytest.mark.asyncio(loop_scope="module")
async def test_marker_file_present(stack):
    """The Dockerfile bakes in /etc/repo1-marker.txt — it should be readable."""
    result = await stack.oneshot("cat /etc/repo1-marker.txt")
    assert result.status is Status.Success
    assert "repo1-fixture" in result.value


@pytest.mark.asyncio(loop_scope="module")
async def test_put_then_get_roundtrip(stack, tmp_path):
    src = tmp_path / "payload.bin"
    src.write_bytes(b"otto-docker-roundtrip-" + b"\xab" * 256)

    res = await stack.put([src], Path("/tmp"))
    assert res.status is Status.Success, res.msg

    # Verify the bytes inside the container.
    cat = await stack.oneshot("wc -c /tmp/payload.bin")
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
    result = await stack.oneshot("cd /tmp && echo $PWD")
    assert result.status is Status.Success
    assert "/tmp" in result.value


@pytest.mark.asyncio(loop_scope="module")
async def test_run_preserves_cwd_across_calls(stack):
    """run() uses a persistent shell session — `cd` in one call must persist
    into the next, no `&&` required."""
    result = await stack.run(["cd /tmp", "pwd"])
    assert result.status is Status.Success
    assert result[-1].value.strip() == "/tmp"


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
    assert r2.only.value.strip() == "/var"


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


@pytest.mark.asyncio(loop_scope="module")
async def test_oneshot_remains_concurrent_safe(stack):
    """oneshot() must stay stateless and concurrent — two parallel sleeps
    must finish materially faster than two serial sleeps."""
    import asyncio
    import time

    # Self-calibrating: a hardcoded wall-clock threshold is flaky on busy
    # CI hosts where `docker exec` startup balloons. Compare parallel to
    # serial in the same run instead — if oneshot were serialized internally,
    # parallel would be ~equal to serial; with real concurrency it's roughly
    # half plus one startup cost.
    start = time.monotonic()
    s1 = await stack.oneshot("sleep 0.2")
    s2 = await stack.oneshot("sleep 0.2")
    serial = time.monotonic() - start

    start = time.monotonic()
    a, b = await asyncio.gather(
        stack.oneshot("sleep 0.2"),
        stack.oneshot("sleep 0.2"),
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
        f"oneshot serialized: serial={serial:.2f}s parallel={parallel:.2f}s"
    )

"""Run / get / put integration tests against a real container.

Exercises the docker exec wrapper and the two-step file transfer through
a parent SSH connection.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio

from src.otto.configmodule.lab import Lab
from src.otto.configmodule.repo import Repo
from src.otto.docker import build_images, compose_down, compose_up
from src.otto.host.remoteHost import RemoteHost
from src.otto.utils import Status

REPO1_DIR = Path(__file__).parent.parent / "repo1"


@pytest_asyncio.fixture
async def stack():
    """Bring up repo1's compose stack on pepper, yield the api container host, then tear down."""
    parent = RemoteHost(
        ip="10.10.200.13",
        ne="pepper",
        creds={"vagrant": "vagrant"},
        board="seed",
        is_virtual=True,
        term="ssh",
        transfer="scp",
        docker_capable=True,
    )
    repo = Repo(sutDir=REPO1_DIR)
    lab = Lab(name="docker_run_test")
    lab.hosts[parent.id] = parent

    build_results = await build_images(repo, parent, rebuild=False)
    assert build_results["api"][0] in (Status.Success, Status.Skipped)
    hosts = await compose_up(repo, lab)
    try:
        yield hosts["api"]
    finally:
        await compose_down(repo, lab)
        await parent.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_oneshot_returns_output_from_container(stack):
    result = await stack.oneshot("echo hello-from-container")
    assert result.status is Status.Success
    assert "hello-from-container" in result.output


@pytest.mark.integration
@pytest.mark.asyncio
async def test_oneshot_failing_command_reports_nonzero(stack):
    result = await stack.oneshot("false")
    assert result.status is Status.Failed
    assert result.retcode != 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_marker_file_present(stack):
    """The Dockerfile bakes in /etc/repo1-marker.txt — it should be readable."""
    result = await stack.oneshot("cat /etc/repo1-marker.txt")
    assert result.status is Status.Success
    assert "repo1-fixture" in result.output


@pytest.mark.integration
@pytest.mark.asyncio
async def test_put_then_get_roundtrip(stack, tmp_path):
    src = tmp_path / "payload.bin"
    src.write_bytes(b"otto-docker-roundtrip-" + b"\xab" * 256)

    status, msg = await stack.put([src], Path("/tmp"))
    assert status is Status.Success, msg

    # Verify the bytes inside the container.
    cat = await stack.oneshot("wc -c /tmp/payload.bin")
    assert cat.status is Status.Success
    assert "/tmp/payload.bin" in cat.output

    out_dir = tmp_path / "back"
    out_dir.mkdir()
    status, msg = await stack.get(Path("/tmp/payload.bin"), out_dir)
    assert status is Status.Success, msg
    assert (out_dir / "payload.bin").read_bytes() == src.read_bytes()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_run_chained_commands_in_one_string(stack):
    """Multiple commands in a single string share state via shell `&&`."""
    result = await stack.oneshot("cd /tmp && echo $PWD")
    assert result.status is Status.Success
    assert "/tmp" in result.output

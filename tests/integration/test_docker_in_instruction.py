"""End-to-end test: drive the docker library from inside an instruction.

Validates that ``otto.docker`` is genuinely usable as a library — an
``@instruction()`` coroutine can call ``composed(...)``, run commands
inside the resulting container hosts, and exit cleanly.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio

from otto.configmodule.lab import Lab
from otto.configmodule.repo import Repo
from otto.docker import build_images, composed
from otto.host.remoteHost import RemoteHost
from otto.utils import Status

REPO1_DIR = Path(__file__).parent.parent / "repo1"

# All docker integration tests share /tmp/otto-docker/repo1/ on pepper
# (compose staging dir). Pin them to one xdist worker so concurrent
# `rm -rf` calls during compose_up don't race.
pytestmark = pytest.mark.xdist_group("docker_e2e")


@pytest_asyncio.fixture
async def parent_lab():
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
    lab = Lab(name="docker_inst_test")
    lab.hosts[parent.id] = parent
    yield parent, lab
    await parent.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_instruction_uses_composed_context_manager(parent_lab):
    """Simulate an instruction using composed() as its lifecycle scope."""
    parent, lab = parent_lab
    repo = Repo(sutDir=REPO1_DIR)
    container_id = f"{parent.id}.repo1.api"

    build_results = await build_images(repo, parent, rebuild=False)
    assert build_results["api"][0] in (Status.Success, Status.Skipped)

    # Mimic: @instruction async def my_workflow(): async with composed(...) as ...
    async def my_workflow() -> str:
        async with composed(repo, lab, on=parent.id, own=True) as containers:
            api = containers["api"]
            res = await api.oneshot("hostname")
            assert res.status is Status.Success
            return res.output.strip()

    hostname = await my_workflow()
    assert hostname  # any non-empty hostname is success

    # After the context manager exits with own=True, the host should be gone.
    assert container_id not in lab.hosts


@pytest.mark.integration
@pytest.mark.asyncio
async def test_session_fixture_holds_stack_for_inner_users(parent_lab):
    """A suite-level fixture using own=True should not be torn down by an inner own=False call."""
    parent, lab = parent_lab
    repo = Repo(sutDir=REPO1_DIR)
    container_id = f"{parent.id}.repo1.api"

    build_results = await build_images(repo, parent, rebuild=False)
    assert build_results["api"][0] in (Status.Success, Status.Skipped)

    async with composed(repo, lab, on=parent.id, own=True) as outer_hosts:
        outer_id = outer_hosts["api"].container_id

        # Inner user: default own=False — must reuse and NOT teardown on exit.
        async with composed(repo, lab, on=parent.id, own=False) as inner_hosts:
            assert inner_hosts["api"].container_id == outer_id

        # Container must still be running after inner exit.
        assert container_id in lab.hosts
        still = await outer_hosts["api"].oneshot("echo still-here")
        assert still.status is Status.Success

    # After outer exit (own=True), it's gone.
    assert container_id not in lab.hosts

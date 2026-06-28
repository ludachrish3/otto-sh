"""End-to-end compose lifecycle integration tests.

Brings up repo1's compose stack on pepper (test3), exercises run/get/put
against the resulting container host, and tears the stack down again.

Requires:
    vagrant up test3
"""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio

from otto.configmodule.lab import Lab
from otto.configmodule.repo import Repo
from otto.docker import build_images, compose_down, compose_up, composed
from otto.host.docker_host import DockerContainerHost
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


@pytest_asyncio.fixture
async def parent(pepper_lease):
    h = UnixHost(
        ip="10.10.200.13",
        element="pepper",
        creds={"vagrant": "vagrant"},
        board="seed",
        is_virtual=True,
        term="ssh",
        transfer="scp",
        docker_capable=True,
    )
    yield h
    await h.close()


@pytest.fixture
def repo1():
    return Repo(sut_dir=REPO1_DIR)


@pytest_asyncio.fixture
async def lab_with_parent(parent):
    """A Lab with just the parent host in it — compose_up() needs a Lab to register hosts."""
    lab = Lab(name="docker_test")
    lab.hosts[parent.id] = parent
    yield lab


@pytest_asyncio.fixture
async def built_image(parent, repo1):
    """Ensure the repo1 image is built once for tests in this file."""
    results = await build_images(repo1, parent, rebuild=False)
    assert results["api"][0] in (Status.Success, Status.Skipped), results
    return results


@pytest.mark.asyncio
async def test_compose_up_registers_container_host(parent, lab_with_parent, repo1, built_image):
    hosts = await compose_up(repo1, lab_with_parent, on=parent.id)
    try:
        assert "api" in hosts
        api = hosts["api"]
        assert isinstance(api, DockerContainerHost)
        assert api.id in lab_with_parent.hosts
        # The container id must be a real docker id, not the placeholder marker.
        assert len(api.container_id) >= 12
    finally:
        await compose_down(repo1, lab_with_parent, on=parent.id)


@pytest.mark.asyncio
async def test_compose_down_unregisters(parent, lab_with_parent, repo1, built_image):
    hosts = await compose_up(repo1, lab_with_parent, on=parent.id)
    api_id = hosts["api"].id
    await compose_down(repo1, lab_with_parent, on=parent.id)
    assert api_id not in lab_with_parent.hosts


@pytest.mark.asyncio
async def test_composed_context_manager_owns_lifecycle(parent, lab_with_parent, repo1, built_image):
    seen_id = None
    async with composed(repo1, lab_with_parent, on=parent.id, own=True) as hosts:
        seen_id = hosts["api"].id
        assert seen_id in lab_with_parent.hosts
    # After exit, the host must be gone (own=True forces teardown).
    assert seen_id not in lab_with_parent.hosts


@pytest.mark.asyncio
async def test_compose_up_idempotent(parent, lab_with_parent, repo1, built_image):
    """Running compose_up twice in a row reuses the same stack."""
    first = await compose_up(repo1, lab_with_parent, on=parent.id)
    try:
        cid_first = first["api"].container_id
        second = await compose_up(repo1, lab_with_parent, on=parent.id)
        cid_second = second["api"].container_id
        assert cid_first == cid_second, "second compose_up must reuse the running container"
    finally:
        await compose_down(repo1, lab_with_parent, on=parent.id)

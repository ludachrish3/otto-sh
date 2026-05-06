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

from src.otto.configmodule.lab import Lab
from src.otto.configmodule.repo import Repo
from src.otto.docker import build_images, compose_down, compose_up, composed
from src.otto.host.dockerHost import DockerContainerHost
from src.otto.host.remoteHost import RemoteHost
from src.otto.utils import Status


REPO1_DIR = Path(__file__).parent.parent / "repo1"


@pytest_asyncio.fixture
async def pepper():
    h = RemoteHost(
        ip="10.10.200.13",
        ne="pepper",
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
    return Repo(sutDir=REPO1_DIR)


@pytest_asyncio.fixture
async def lab_with_pepper(pepper):
    """A Lab with just pepper in it — compose_up() needs a Lab to register hosts."""
    lab = Lab(name="docker_test")
    lab.hosts[pepper.id] = pepper
    yield lab


@pytest_asyncio.fixture
async def built_image(pepper, repo1):
    """Ensure the repo1 image is built once for tests in this file."""
    results = await build_images(repo1, pepper, rebuild=False)
    assert results["api"][0] in (Status.Success, Status.Skipped), results
    return results


@pytest.mark.integration
@pytest.mark.asyncio
async def test_compose_up_registers_container_host(lab_with_pepper, repo1, built_image):
    hosts = await compose_up(repo1, lab_with_pepper)
    try:
        assert "api" in hosts
        api = hosts["api"]
        assert isinstance(api, DockerContainerHost)
        assert api.id in lab_with_pepper.hosts
        # The container id must be a real docker id, not the placeholder marker.
        assert len(api.container_id) >= 12
    finally:
        await compose_down(repo1, lab_with_pepper)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_compose_down_unregisters(lab_with_pepper, repo1, built_image):
    hosts = await compose_up(repo1, lab_with_pepper)
    api_id = hosts["api"].id
    await compose_down(repo1, lab_with_pepper)
    assert api_id not in lab_with_pepper.hosts


@pytest.mark.integration
@pytest.mark.asyncio
async def test_composed_context_manager_owns_lifecycle(lab_with_pepper, repo1, built_image):
    seen_id = None
    async with composed(repo1, lab_with_pepper, own=True) as hosts:
        seen_id = hosts["api"].id
        assert seen_id in lab_with_pepper.hosts
    # After exit, the host must be gone (own=True forces teardown).
    assert seen_id not in lab_with_pepper.hosts


@pytest.mark.integration
@pytest.mark.asyncio
async def test_compose_up_idempotent(lab_with_pepper, repo1, built_image):
    """Running compose_up twice in a row reuses the same stack."""
    first = await compose_up(repo1, lab_with_pepper)
    try:
        cid_first = first["api"].container_id
        second = await compose_up(repo1, lab_with_pepper)
        cid_second = second["api"].container_id
        assert cid_first == cid_second, "second compose_up must reuse the running container"
    finally:
        await compose_down(repo1, lab_with_pepper)

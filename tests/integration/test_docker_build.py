"""Docker-build integration tests.

Requires:
    vagrant up test3   (docker.io provisioned, vagrant user in docker group)

Run with:
    pytest tests/integration/test_docker_build.py
"""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio

from src.otto.configmodule.repo import Repo
from src.otto.docker import build_images
from src.otto.docker.build import image_full_tag, image_latest_tag
from src.otto.host.remoteHost import RemoteHost
from src.otto.utils import Status


REPO1_DIR = Path(__file__).parent.parent / "repo1"


@pytest_asyncio.fixture
async def pepper():
    """Direct (non-hopped) connection to test3 / pepper for docker tests."""
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


@pytest.mark.integration
@pytest.mark.asyncio
async def test_build_succeeds(pepper, repo1):
    results = await build_images(repo1, pepper, rebuild=True)
    assert "api" in results
    status, msg = results["api"]
    assert status is Status.Success, f"build failed: {msg}"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_build_skips_when_image_exists(pepper, repo1):
    # First build (force) → fresh build.
    first = await build_images(repo1, pepper, rebuild=True)
    assert first["api"][0] is Status.Success

    # Second build without --rebuild → must short-circuit on `docker image inspect`.
    second = await build_images(repo1, pepper, rebuild=False)
    assert second["api"][0] is Status.Skipped


@pytest.mark.integration
@pytest.mark.asyncio
async def test_build_tags_locally(pepper, repo1):
    await build_images(repo1, pepper, rebuild=False)
    # The :latest mirror should be pullable via `docker image inspect`.
    image = repo1.docker_settings.images[0]
    latest = image_latest_tag(repo1.docker_settings.registry_url, repo1.name, image)
    result = await pepper.oneshot(f"docker image inspect {latest}")
    assert result.status.is_ok, f"latest tag missing: {result.output}"

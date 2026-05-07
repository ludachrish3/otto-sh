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

from otto.configmodule.repo import Repo
from otto.docker import build_images
from otto.docker.build import image_full_tag, image_latest_tag
from otto.host.remoteHost import RemoteHost
from otto.utils import Status


REPO1_DIR = Path(__file__).parent.parent / "repo1"

# All docker integration tests share /tmp/otto-docker/repo1/ on pepper
# (build-context staging dir). Pin them to one xdist worker so concurrent
# `rm -rf` calls during stage_image_context don't race.
pytestmark = pytest.mark.xdist_group("docker_e2e")


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def parent():
    """Direct (non-hopped) connection to test3 / pepper for docker tests.

    Module-scoped so the three tests in this file share a single SSH
    connection — the connection has no per-test state, and the savings
    are real (~1s of asyncssh handshake per test)."""
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
@pytest.mark.asyncio(loop_scope="module")
async def test_build_succeeds(parent, repo1):
    results = await build_images(repo1, parent, rebuild=True)
    assert "api" in results
    status, msg = results["api"]
    assert status is Status.Success, f"build failed: {msg}"


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_build_skips_when_image_exists(parent, repo1):
    # First build (force) → fresh build.
    first = await build_images(repo1, parent, rebuild=True)
    assert first["api"][0] is Status.Success

    # Second build without --rebuild → must short-circuit on `docker image inspect`.
    second = await build_images(repo1, parent, rebuild=False)
    assert second["api"][0] is Status.Skipped


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
async def test_build_tags_locally(parent, repo1):
    await build_images(repo1, parent, rebuild=False)
    # The :latest mirror should be pullable via `docker image inspect`.
    image = repo1.docker_settings.images[0]
    latest = image_latest_tag(repo1.docker_settings.registry_url, repo1.name, image)
    result = await parent.oneshot(f"docker image inspect {latest}")
    assert result.status.is_ok, f"latest tag missing: {result.output}"

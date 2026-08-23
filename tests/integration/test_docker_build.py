"""Docker-build integration tests.

Requires:
    vagrant up test3   (docker.io provisioned, vagrant user in docker group)

Run with:
    pytest tests/integration/test_docker_build.py
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from otto.config.repo import Repo
from otto.docker import build_images
from otto.docker.build import image_latest_tag
from otto.host.login_proxy import Cred
from otto.host.unix_host import UnixHost
from otto.utils import Status
from tests._fixtures._host_pool import lease_unix_host
from tests._fixtures.paths import TESTS_ROOT

REPO1_DIR = TESTS_ROOT / "repo1"

# All docker integration tests share /tmp/otto-docker/repo1/ on test3
# (build-context staging dir). Pin them to one xdist worker so concurrent
# `rm -rf` calls during stage_image_context don't race.
pytestmark = pytest.mark.xdist_group("docker_e2e")


@pytest.fixture(scope="module")
def test3_lease(tmp_path_factory):
    """Hold the test3 fd-flock for the entire module so no e2e docker test
    can race against the integration docker tests on the same daemon."""
    lock_dir = tmp_path_factory.getbasetemp().parent
    with lease_unix_host(lock_dir, ["test3"]) as _element:
        yield _element


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def parent(test3_lease):
    """Direct (non-hopped) connection to test3 / test3 for docker tests.

    Module-scoped so the three tests in this file share a single SSH
    connection — the connection has no per-test state, and the savings
    are real (~1s of asyncssh handshake per test)."""
    h = UnixHost(
        ip="10.10.200.13",
        element="test3",
        creds=[Cred(login="vagrant", password="vagrant")],
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


@pytest.mark.asyncio(loop_scope="module")
async def test_build_succeeds(parent, repo1):
    results = await build_images(repo1, parent, rebuild=True)
    assert "api" in results
    res = results["api"]
    assert res.status is Status.Success, f"build failed: {res.value}"


@pytest.mark.asyncio(loop_scope="module")
async def test_build_skips_when_image_exists(parent, repo1):
    # First build (force) → fresh build.
    first = await build_images(repo1, parent, rebuild=True)
    assert first["api"].status is Status.Success

    # Second build without --rebuild → must short-circuit on `docker image inspect`.
    second = await build_images(repo1, parent, rebuild=False)
    assert second["api"].status is Status.Skipped


@pytest.mark.asyncio(loop_scope="module")
async def test_build_tags_locally(parent, repo1):
    await build_images(repo1, parent, rebuild=False)
    # The :latest mirror should be pullable via `docker image inspect`.
    image = repo1.docker_settings.images[0]
    latest = image_latest_tag(repo1.docker_settings.registry_url, repo1.name, image)
    result = await parent.exec(f"docker image inspect {latest}")
    assert result.status.is_ok, f"latest tag missing: {result.value}"

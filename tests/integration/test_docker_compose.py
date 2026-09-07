"""End-to-end compose lifecycle integration tests.

Brings up repo1's compose stack on test3, exercises run/get/put
against the resulting container host, and tears the stack down again.

Requires:
    vagrant up test3
"""

from __future__ import annotations

import shlex
from dataclasses import replace
from pathlib import Path

import pytest
import pytest_asyncio

from otto.config.lab import Lab
from otto.config.repo import DockerCompose, Repo
from otto.docker import build_images, compose_down, compose_up, composed
from otto.docker.compose import get_user_compose_project
from otto.host.docker_host import DockerContainerHost
from otto.host.element import Element
from otto.host.login_proxy import Cred
from otto.host.unix_host import UnixHost
from otto.utils import Status
from tests._fixtures._host_pool import lease_unix_host
from tests._fixtures.paths import TESTS_ROOT

REPO1_DIR = TESTS_ROOT / "repo1"

# All docker integration tests share /tmp/otto-docker/repo1/ on test3
# (compose staging dir). Pin them to one xdist worker so concurrent
# `rm -rf` calls during compose_up don't race.
pytestmark = [
    pytest.mark.xdist_group("docker_e2e"),
    # This module drives docker on test3: it asks for the orphan-stack reap.
    # The reap is requested, never ambient (tests/unit/test_docker_reaper_scope.py).
    pytest.mark.usefixtures("reap_orphan_docker_stacks"),
]


@pytest.fixture(scope="module")
def test3_lease(tmp_path_factory):
    """Hold the test3 fd-flock for the entire module so no e2e docker test
    can race against the integration docker tests on the same daemon."""
    lock_dir = tmp_path_factory.getbasetemp().parent
    with lease_unix_host(lock_dir, ["test3"]) as _element:
        yield _element


@pytest_asyncio.fixture
async def parent(test3_lease):
    h = UnixHost(
        ip="10.10.200.13",
        element=Element("test3"),
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
    assert results["api"].status in (Status.Success, Status.Skipped), results
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


@pytest.mark.asyncio
async def test_bind_mount_reachable_through_parent(
    parent, lab_with_parent, repo1, built_image, tmp_path
):
    """Prove the mount table docker inspect built is REAL, not merely mocked.

    Every unit test for this feature (Mount, parent_path/container_path,
    inspect_mounts, register_stack_hosts) drove the translation against a
    hand-built table or a mocked parent -- nothing yet proved that a real
    `docker inspect` on a real daemon, reached through a real SSH parent,
    produces a mount table that actually locates a real file. This test's
    only job is that proof:

    1. a real absolute-path bind mount, its source directory created on the
       parent explicitly (never relying on docker's own auto-create);
    2. the registered host's `mounts` names it with the right
       container_path/parent_path/kind;
    3. a file is written INSIDE the container;
    4. it is read back THROUGH THE PARENT at the translated path, and its
       CONTENTS (not just a success status) are what gets asserted --
       the content is what shows the translation landed on the right file.

    Brings up its own single-service stack (a fresh compose file + a
    dedicated compose project) rather than adding the mount to the shared
    `docker/compose.yml` the other tests in this module use, so this proof
    cannot perturb their assertions or their shared stack's lifecycle.
    """
    # Per-user (so concurrent users on the shared test3 never collide) and
    # carrying the `-e2e-` infix `_ORPHAN_PROJECT_FRAGMENTS` matches, so an
    # interrupted run's NETWORK is reapable too -- `<project>_default` keeps
    # the infix only if the project name itself has it.
    project = get_user_compose_project("mount-e2e-probe")
    host_dir = Path(f"/tmp/{project}")  # same string: one name to reason about

    compose_yaml = tmp_path / "mounts.yml"
    compose_yaml.write_text(
        "services:\n"
        "  mountcheck:\n"
        "    image: repo1-api:latest\n"
        '    command: ["sh", "-c", "while sleep 3600; do :; done"]\n'
        "    volumes:\n"
        f"      - {host_dir}:/var/lib/app\n"
    )
    # In-memory only, on this test's own `repo1` instance (function-scoped —
    # a fresh Repo per test) -- the committed settings.toml and its shared
    # docker/compose.yml are never touched.
    repo1.docker_settings = replace(
        repo1.docker_settings,
        composes=(DockerCompose(path=compose_yaml, services=("mountcheck",), name="mounts_e2e"),),
    )

    try:
        mkdir = await parent.exec(
            f"rm -rf {shlex.quote(str(host_dir))} && mkdir -p {shlex.quote(str(host_dir))}"
        )
        assert mkdir.status.is_ok, mkdir.value

        hosts = await compose_up(
            repo1, lab_with_parent, on=parent.id, project_name=project, build=False
        )
        ctr = hosts["mountcheck"]

        found = ctr.mount_for("/var/lib/app")
        assert found is not None, ctr.mounts
        assert found.container_path == Path("/var/lib/app")
        assert found.parent_path == host_dir
        assert found.kind == "bind"

        written = await ctr.exec("echo hi > /var/lib/app/probe.txt")
        assert written.status.is_ok, written.value

        parent_probe = ctr.parent_path("/var/lib/app/probe.txt")
        assert parent_probe == host_dir / "probe.txt"

        fetched = await parent.get([parent_probe], tmp_path)
        assert fetched.status == Status.Success, fetched.msg
        landed = (tmp_path / "probe.txt").read_text()
        assert landed.strip() == "hi", (
            f"expected 'hi' through the parent-side mount, got {landed!r}"
        )
    finally:
        await compose_down(repo1, lab_with_parent, on=parent.id, project_name=project)
        await parent.exec(f"rm -rf {shlex.quote(str(host_dir))}")

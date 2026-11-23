"""Fixtures shared by the docker e2e modules."""

import logging
import os
import uuid

import pytest

from scripts.bed_lease import load_labs
from tests._fixtures._host_pool import lease_unix_host
from tests._fixtures.docker_reap import docker_parents
from tests._fixtures.labdata import lab_data_path
from tests.e2e._otto_subprocess import PREPARATION_TIMEOUT, PROJECT1

from ._cli import (
    _DEFAULT_PARENT,
    _MERGED_USE_CASE,
    _PROJECT1_USE_CASE,
    PROJECT2,
    _run_otto,
    staging_root_of,
)

# Docker container hosts require an SSH-based UnixHost parent (see
# DockerContainerHost._make_session: term must be 'ssh'). test2 defaults to
# telnet (it's first in its valid_terms list), so it cannot host containers.
# The pool is the one rule `docker_parents` spells, over the loaded unix lab
# (test1 and test3; `tests/unit/test_docker_reap.py` pins it), so the pool
# and the bed reap's docker step cannot drift apart.
_DOCKER_POOL = tuple(docker_parents(load_labs(lab_data_path("tech1"), ["unix"]).hosts.values()))


@pytest.fixture
def docker_host() -> str:  # type: ignore[type-arg]
    """Lease one docker-capable, SSH-based host from the pool for this test's duration.

    Yields the host's id, e.g. ``"test1"``.  The fd-flock on
    the pool lock file (``unix_pool.<element>``) ensures at most one test
    runs against each docker daemon at a time, while xdist can distribute
    different tests to different workers/daemons concurrently.

    The pool is ``_DOCKER_POOL`` (test1 + test3, by ``docker_parents``) because
    ``DockerContainerHost`` requires its parent to have ``term='ssh'``.
    test2 defaults to telnet (telnet is first in its valid_terms),
    so it cannot serve as a docker container parent.
    """
    with lease_unix_host(_DOCKER_POOL) as element:
        yield element


@pytest.fixture
def default_parent_host() -> str:  # type: ignore[type-arg]
    """Lease :data:`_DEFAULT_PARENT` — the host a docker verb resolves to with no flag.

    Same fd-flock as ``docker_host``, narrowed to one element. Used by the
    tests that address a container id from a second otto process, where the
    id must be one the default parent minted rather than one ``--parent`` invented.
    """
    with lease_unix_host([_DEFAULT_PARENT]) as element:
        yield element


@pytest.fixture
def fresh_suffix() -> str:
    """A short unique compose-project suffix so each test has its own stack.

    The ``e2e-`` prefix is load-bearing beyond uniqueness: it is what puts a
    reapable ``-e2e-`` infix into the resulting ``<lab>-<usecase>-<suffix>``
    compose project, which is how ``tests/integration/conftest.py``'s orphan
    reaper finds stacks a crashed run left behind.
    ``tests/unit/test_docker_reaper_scope.py`` pins that agreement.
    """
    return "e2e-" + uuid.uuid4().hex[:8]


def _tear_down(host: str, suffix: str, xdir) -> None:
    """Take this suffix's stacks down on *host*, then remove what it staged there.

    Tears down BOTH declared use-cases (``project1`` and the merged
    ``integration``), because a use-case is the unit of deployment and each
    one gets its own compose project: a test that brought up ``integration``
    leaves a stack ``down project1`` would never touch. A half-torn-down stack
    leaks a docker network on each run; enough leaks (~30) and the docker
    daemon runs out of subnet pools and subsequent ``compose up``s fail with
    ``all predefined address pools have been fully subnetted``.

    Both projects are in OTTOPATH so the merged use-case resolves the same set
    of fragments the test deployed. ``provide`` is deliberately NOT passed: the
    compose project is derived from (lab, use-case, suffix) alone, so one
    ``down`` reaps the stack whichever provider won.

    ``down`` keeps what it staged (the next ``down`` reads it back), so the
    staging this suffix owns is removed afterwards: each use-case's tree, only
    after ITS ``down`` succeeded (a failed one leaves a stack whose next
    ``down`` needs that tree, so it stays and a warning names it), and each
    project's build key, which no ``down`` reads. All of them carry the test's
    own suffix, so no other session's tree is touched.
    """
    from otto.docker.build import build_staging_key
    from otto.docker.compose import use_case_project
    from otto.docker.staging import cleanup_command

    keys = [build_staging_key(name, suffix) for name in ("project1", "project2")]
    for use_case in (_PROJECT1_USE_CASE, _MERGED_USE_CASE):
        down = _run_otto(
            "docker",
            "compose",
            "down",
            use_case,
            "--parent",
            host,
            roots=f"{PROJECT1}{os.pathsep}{PROJECT2}",
            xdir=xdir,
            compose_suffix=suffix,
        )
        key = use_case_project("unix", use_case, suffix)
        if down.returncode == 0:
            keys.append(key)
        else:
            logging.getLogger(__name__).warning(
                "%s: `down %s` failed, so its staging %s stays for the next down:\n%s%s",
                host,
                use_case,
                key,
                down.stdout,
                down.stderr,
            )
    root = staging_root_of(host)
    _run_otto("host", host, "exec", " && ".join(cleanup_command(root, k) for k in keys), xdir=xdir)


@pytest.fixture
def teardown_after(fresh_suffix, docker_host, tmp_path):
    """Yield the suffix; on test exit, tear its stacks and staging down (:func:`_tear_down`).

    Runs even if the test failed mid-flight. Idempotent: ``down`` is harmless
    when the stack isn't up, and ``rm -rf`` when the tree is gone.
    """
    yield fresh_suffix
    _tear_down(docker_host, fresh_suffix, tmp_path)


@pytest.fixture
def teardown_default_parent_after(fresh_suffix, default_parent_host, tmp_path):
    """``teardown_after``, for the tests that lease :data:`_DEFAULT_PARENT`."""
    yield fresh_suffix
    _tear_down(default_parent_host, fresh_suffix, tmp_path)


def _build_sample_images(host: str, suffix: str, xdir) -> None:
    """Build every sample project's declared images on *host*, through the real CLI.

    The one invocation ``otto docker build --parent <host>`` over both sample
    projects, the same one ``test_e2e_multi_project_build_builds_every_loaded_project_on_the_host``
    drives. *suffix* is the test's own compose suffix, so the build's staging key
    (``build_staging_key(project, suffix)``) is one :func:`_tear_down` already
    removes. A cold layer cache pulls base images: that is preparation, and it
    runs in setup under :data:`PREPARATION_TIMEOUT`.

    *xdir* is the build's own output directory, never the test's: a test that
    asserts its commands wrote a ``docker`` run directory, or reads the one
    completion cache under its ``tmp_path``, must not find this build's there.
    """
    built = _run_otto(
        "docker",
        "build",
        "--parent",
        host,
        roots=f"{PROJECT1}{os.pathsep}{PROJECT2}",
        xdir=xdir,
        compose_suffix=suffix,
        timeout=PREPARATION_TIMEOUT,
    )
    assert built.returncode == 0, (
        f"building the sample images on {host} failed\n"
        f"stdout:\n{built.stdout}\nstderr:\n{built.stderr}"
    )


@pytest.fixture
def built_images(docker_host, fresh_suffix, tmp_path_factory) -> None:
    """The sample projects' images, built on the leased ``docker_host`` before the body.

    pytest-timeout times a test's body only, so a test whose subject is not the
    image build gets its images here, in setup, instead of running
    ``compose up --build`` in the body (#1274). Request it with
    ``teardown_after``, which shares the lease and the suffix: the teardown's
    staging cleanup is what removes this build's staging key from the host.

    Function scoped on purpose. ``test_docker_honesty.py`` removes images
    (``_rmi``) and the host a test leases differs from one test to the next, so a
    session-scoped build would make the tests order-dependent. With a warm layer
    cache the build is a no-op, so running it per test costs little.
    """
    _build_sample_images(docker_host, fresh_suffix, tmp_path_factory.mktemp("image-build"))


@pytest.fixture
def built_images_default_parent(default_parent_host, fresh_suffix, tmp_path_factory) -> None:
    """:func:`built_images`, on the leased :data:`_DEFAULT_PARENT` host.

    Request it with ``teardown_default_parent_after``, for the same reason.
    """
    _build_sample_images(default_parent_host, fresh_suffix, tmp_path_factory.mktemp("image-build"))

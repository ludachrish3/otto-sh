"""Fixtures shared by the docker e2e modules."""

import os
import uuid

import pytest

from tests._fixtures._host_pool import lease_unix_host
from tests.e2e._otto_subprocess import REPO1

from ._cli import _DEFAULT_PARENT, _MERGED_USE_CASE, _REPO1_USE_CASE, REPO2, _run_otto

# Docker container hosts require an SSH-based UnixHost parent (see
# DockerContainerHost._make_session: term must be 'ssh').  test2 defaults
# to telnet (it's first in its valid_terms list), so it cannot host containers.
# Restrict the docker lease pool to the SSH-first unix peers only.
_DOCKER_POOL = ("test1", "test3")


@pytest.fixture
def docker_host(tmp_path_factory) -> str:  # type: ignore[type-arg]
    """Lease one docker-capable, SSH-based host from the pool for this test's duration.

    Yields the host's id, e.g. ``"test1"``.  The fd-flock on
    the pool lock file (``unix_pool.<element>``) ensures at most one test
    runs against each docker daemon at a time, while xdist can distribute
    different tests to different workers/daemons concurrently.

    The pool is restricted to ``_DOCKER_POOL`` (test1 + test3) because
    ``DockerContainerHost`` requires its parent to have ``term='ssh'``.
    test2 defaults to telnet (telnet is first in its valid_terms),
    so it cannot serve as a docker container parent.
    """
    lock_dir = tmp_path_factory.getbasetemp().parent
    with lease_unix_host(lock_dir, _DOCKER_POOL) as element:
        yield element


@pytest.fixture
def default_parent_host(tmp_path_factory) -> str:  # type: ignore[type-arg]
    """Lease :data:`_DEFAULT_PARENT` — the host a docker verb resolves to with no flag.

    Same fd-flock as ``docker_host``, narrowed to one element. Used by the
    tests that address a container id from a second otto process, where the
    id must be one the default parent minted rather than one ``--parent`` invented.
    """
    lock_dir = tmp_path_factory.getbasetemp().parent
    with lease_unix_host(lock_dir, [_DEFAULT_PARENT]) as element:
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


@pytest.fixture
def teardown_after(fresh_suffix, docker_host, tmp_path):
    """Yield the suffix; on test exit, ensure the stack is torn down even if
    the test failed mid-flight. Idempotent — `down` is harmless when the
    stack isn't up.

    Tears down BOTH declared use-cases (``repo1`` and the merged
    ``integration``), because a use-case is now the unit of deployment and
    each one gets its own compose project: a test that brought up
    ``integration`` leaves a stack ``down repo1`` would never touch. A
    half-torn-down stack leaks a docker network on each run; enough leaks
    (~30) and the docker daemon runs out of subnet pools and subsequent
    ``compose up``s fail with ``all predefined address pools have been fully
    subnetted``.
    """
    yield fresh_suffix
    # Both repos in SUT_DIRS so the merged use-case resolves the same set of
    # fragments the test deployed. --parent <docker_host> targets the daemon the
    # test used. `provide` is deliberately NOT passed: the compose project is
    # derived from (lab, use-case, suffix) alone, so one `down` reaps the
    # stack whichever provider won.
    for use_case in (_REPO1_USE_CASE, _MERGED_USE_CASE):
        _run_otto(
            "docker",
            "compose",
            "down",
            use_case,
            "--parent",
            docker_host,
            sut_dirs=f"{REPO1}{os.pathsep}{REPO2}",
            xdir=tmp_path,
            compose_suffix=fresh_suffix,
        )


@pytest.fixture
def teardown_default_parent_after(fresh_suffix, default_parent_host, tmp_path):
    """``teardown_after``, for the tests that lease :data:`_DEFAULT_PARENT`."""
    yield fresh_suffix
    for use_case in (_REPO1_USE_CASE, _MERGED_USE_CASE):
        _run_otto(
            "docker",
            "compose",
            "down",
            use_case,
            "--parent",
            default_parent_host,
            sut_dirs=f"{REPO1}{os.pathsep}{REPO2}",
            xdir=tmp_path,
            compose_suffix=fresh_suffix,
        )

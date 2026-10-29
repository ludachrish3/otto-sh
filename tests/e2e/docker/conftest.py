"""Fixtures shared by the docker e2e modules."""

import logging
import os
import uuid

import pytest

from tests._fixtures._host_pool import lease_unix_host
from tests.e2e._otto_subprocess import PROJECT1

from ._cli import (
    _DEFAULT_PARENT,
    _MERGED_USE_CASE,
    _PROJECT1_USE_CASE,
    PROJECT2,
    _run_otto,
    staging_root_of,
)

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

"""Fixtures shared by the docker e2e modules."""

import pytest

from tests._fixtures._host_pool import lease_unix_host

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

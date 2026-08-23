"""End-to-end CLI tests for ``otto run`` against a real docker container host.

Invokes the installed ``otto`` entrypoint as a subprocess so the full
dispatch path — repo discovery, lab loading, docker-stack bring-up, the
``run`` subcommand, and the ``DockerContainerHost`` exec path —
runs exactly as the user runs it.

Requirements:
    vagrant up test1 test3
    Both VMs must have docker installed and running.

Each test leases one docker-capable host from {test1, test3} via the
same fd-flock mechanism as the docker e2e tests.  The ``xdist_group``
keeps all subprocess-coverage tests in one worker so the SQLite coverage
context table is only finalised once (avoids the "no such table: context"
race introduced in 248d15b).
"""

import subprocess
import uuid
from pathlib import Path

import pytest

from tests._fixtures._host_pool import lease_unix_host
from tests.e2e._otto_subprocess import REPO1, assert_output_dir, run_otto

# Docker container hosts require an SSH-based parent (DockerContainerHost
# uses docker exec via the parent's SSH session).  test2 defaults to
# telnet, so restrict the pool to SSH-first peers.
_DOCKER_POOL = ("test1", "test3")

pytestmark = [pytest.mark.integration, pytest.mark.xdist_group("run_exec_e2e")]


# ---------------------------------------------------------------------------
# Subprocess helper (mirrors test_docker_e2e_cli._run_otto)
# ---------------------------------------------------------------------------


def _run_otto(
    *args: str,
    sut_dirs: Path = REPO1,
    lab: str = "unix",
    xdir: Path | None = None,
    compose_suffix: str | None = None,
    timeout: int = 180,
) -> subprocess.CompletedProcess[str]:
    """Run ``otto -R --lab <lab> <args>`` through the shared subprocess harness.

    ``compose_suffix`` rides along as ``OTTO_COMPOSE_SUFFIX`` so each test's
    docker stack is its own; the rest of the environment (subprocess coverage
    + the otto keys) comes from ``run_otto``.
    """
    return run_otto(
        list(args),
        xdir=xdir,
        sut_dirs=sut_dirs,
        lab=lab,
        extra_argv_prefix=["-R"],
        extra_env=(None if compose_suffix is None else {"OTTO_COMPOSE_SUFFIX": compose_suffix}),
        timeout=timeout,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def docker_host(tmp_path_factory) -> str:  # type: ignore[type-arg]
    """Lease one SSH-capable docker host from the pool; yield its host id."""
    lock_dir = tmp_path_factory.getbasetemp().parent
    with lease_unix_host(lock_dir, _DOCKER_POOL) as element:
        yield element


@pytest.fixture
def fresh_suffix() -> str:
    """A unique compose-project suffix so stacks from different test runs never collide."""
    return "run-e2e-" + uuid.uuid4().hex[:8]


@pytest.fixture
def teardown_after(fresh_suffix, docker_host, tmp_path):
    """Yield the compose suffix; run ``otto docker down`` on exit even if the test failed."""
    yield fresh_suffix
    _run_otto(
        "docker",
        "down",
        "--on",
        docker_host,
        xdir=tmp_path,
        compose_suffix=fresh_suffix,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_run_instruction_on_container(teardown_after, docker_host, tmp_path):
    """``otto run run-on-container --on <container_id>`` must execute inside
    the running container and return the fixture marker text.

    Flow:
    1. Bring the repo1 docker stack up on the leased host.
    2. Run the ``run-on-container`` instruction targeting the api container.
    3. Assert exit 0 and that ``"repo1-fixture"`` appears in stdout (the
       instruction prints the raw ``cat /etc/repo1-marker.txt`` output).
    4. The ``teardown_after`` fixture runs ``otto docker down`` on exit.
    """
    suffix = teardown_after
    container_id = f"{docker_host}.repo1.api"

    # Step 1: bring the stack up.
    up = _run_otto("docker", "up", "--on", docker_host, xdir=tmp_path, compose_suffix=suffix)
    assert up.returncode == 0, (
        f"`docker up` failed — cannot proceed with run test\n"
        f"stdout:\n{up.stdout}\nstderr:\n{up.stderr}"
    )
    assert container_id in up.stdout, (
        f"expected {container_id!r} to be registered after `docker up`:\n{up.stdout}"
    )

    # Step 2 + 3: run the instruction and assert marker output.
    run = _run_otto(
        "run",
        "run-on-container",
        "--on",
        container_id,
        xdir=tmp_path,
        compose_suffix=suffix,
    )
    assert run.returncode == 0, (
        f"`otto run run-on-container` should succeed\nstdout:\n{run.stdout}\nstderr:\n{run.stderr}"
    )
    assert "repo1-fixture" in run.stdout, (
        f"expected 'repo1-fixture' marker in stdout from the container:\n{run.stdout}"
    )
    # the run instruction does real work on a host → run output dir created
    assert_output_dir(tmp_path, "run")

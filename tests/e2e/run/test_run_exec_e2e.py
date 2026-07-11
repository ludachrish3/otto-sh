"""End-to-end CLI tests for ``otto run`` against a real docker container host.

Invokes the installed ``otto`` entrypoint as a subprocess so the full
dispatch path — repo discovery, lab loading, docker-stack bring-up, the
``run`` subcommand, and the ``DockerContainerHost`` exec path —
runs exactly as the user runs it.

Requirements:
    vagrant up test1 test3   (carrot=test1, pepper=test3)
    Both VMs must have docker installed and running.

Each test leases one docker-capable host from {carrot, pepper} via the
same fd-flock mechanism as the docker e2e tests.  The ``xdist_group``
keeps all subprocess-coverage tests in one worker so the SQLite coverage
context table is only finalised once (avoids the "no such table: context"
race introduced in 248d15b).
"""

import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from tests._fixtures._host_pool import lease_unix_host
from tests.e2e._otto_subprocess import assert_output_dir

# Docker container hosts require an SSH-based parent (DockerContainerHost
# uses docker exec via the parent's SSH session).  tomato_seed defaults to
# telnet, so restrict the pool to SSH-first peers.
_DOCKER_POOL = ("carrot", "pepper")

PROJECT_ROOT = Path(__file__).resolve().parents[3]
REPO1 = PROJECT_ROOT / "tests" / "repo1"
OTTO_BIN = Path(sys.executable).parent / "otto"
COVERAGERC = PROJECT_ROOT / ".coveragerc"
COVERAGE_BOOTSTRAP = PROJECT_ROOT / "tests" / "_coverage_bootstrap"

pytestmark = [pytest.mark.integration, pytest.mark.xdist_group("run_exec_e2e")]


# ---------------------------------------------------------------------------
# Subprocess helper (mirrors test_docker_e2e_cli._run_otto)
# ---------------------------------------------------------------------------


def _run_otto(
    *args: str,
    sut_dirs: str = str(REPO1),
    lab: str = "veggies",
    xdir: Path | None = None,
    compose_suffix: str | None = None,
    timeout: int = 180,
) -> subprocess.CompletedProcess[str]:
    """Run ``otto <args>`` as a subprocess with subprocess-coverage env."""
    env: dict[str, str] = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "OTTO_SUT_DIRS": sut_dirs,
        "COVERAGE_PROCESS_START": str(COVERAGERC),
        "PYTHONPATH": os.pathsep.join(
            [str(COVERAGE_BOOTSTRAP), os.environ.get("PYTHONPATH", "")]
        ).rstrip(os.pathsep),
    }
    if xdir is not None:
        env["OTTO_XDIR"] = str(xdir)
    if compose_suffix is not None:
        env["OTTO_COMPOSE_SUFFIX"] = compose_suffix

    full_argv = [str(OTTO_BIN), "--lab", lab, "-R", *args]
    return subprocess.run(
        full_argv,
        env=env,
        capture_output=True,
        text=True,
        cwd=str(PROJECT_ROOT),
        timeout=timeout,
        check=False,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def docker_host(tmp_path_factory) -> str:  # type: ignore[type-arg]
    """Lease one SSH-capable docker host from the pool; yield its seed id."""
    lock_dir = tmp_path_factory.getbasetemp().parent
    with lease_unix_host(lock_dir, _DOCKER_POOL) as element:
        yield f"{element}_seed"


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

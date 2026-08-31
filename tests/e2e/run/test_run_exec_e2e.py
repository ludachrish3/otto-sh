"""End-to-end CLI tests for ``otto run`` against a real docker container host.

Invokes the installed ``otto`` entrypoint as a subprocess so the full
dispatch path — repo discovery, lab loading, docker-stack bring-up, the
``run`` subcommand, and the ``DockerContainerHost`` exec path —
runs exactly as the user runs it.

Requirements:
    vagrant up test3
    The VM must have docker installed and running.

The host is leased from a one-element pool via the same fd-flock mechanism
as the docker e2e tests.  The ``xdist_group`` keeps all subprocess-coverage
tests in one worker so the SQLite coverage context table is only finalised
once (avoids the "no such table: context" race introduced in 248d15b).
"""

import subprocess
import uuid
from pathlib import Path

import pytest

from tests._fixtures._host_pool import lease_unix_host
from tests.e2e._otto_subprocess import REPO1, assert_output_dir, run_otto

# Docker container hosts require an SSH-based parent (DockerContainerHost
# uses docker exec via the parent's SSH session), which rules out test2
# (telnet is first in its valid_terms).
#
# The pool is ONE element, and that is a placement fact rather than a
# preference: repo1's `[[docker.use_cases]]` fragments declare
# ``role = "docker"``, and test3 is the only element in the `unix` fixture lab
# tagged ``"roles": ["docker"]`` (spec §5 knob 3). Placeholder registration
# therefore only mints ``test3.<usecase>.<service>`` ids — and the test below
# hands such an id to a SECOND otto process, which knows only what placement
# gave it. `--on test1` would register `test1.repo1.api` inside the
# invocation that deployed it and nowhere else.
_DOCKER_POOL = ("test3",)

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


# The use-case repo1 declares for its own stack (spec §14: naming the
# fragment after the repo keeps container ids literally unchanged). repo1 now
# declares two use-cases, so a bare `otto docker up` is ambiguous and refuses.
_REPO1_USE_CASE = "repo1"


@pytest.fixture
def fresh_suffix() -> str:
    """A unique compose-project suffix so stacks from different test runs never collide.

    The ``e2e-`` infix is load-bearing beyond uniqueness: it is what makes the
    resulting ``<lab>-<usecase>-<suffix>`` compose project reapable by
    ``tests/integration/conftest.py``'s orphan sweep.
    ``tests/unit/test_docker_reaper_scope.py`` pins that agreement.
    """
    return "run-e2e-" + uuid.uuid4().hex[:8]


@pytest.fixture
def teardown_after(fresh_suffix, docker_host, tmp_path):
    """Yield the compose suffix; run ``otto docker down`` on exit even if the test failed."""
    yield fresh_suffix
    _run_otto(
        "docker",
        "down",
        _REPO1_USE_CASE,
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
    up = _run_otto(
        "docker", "up", _REPO1_USE_CASE, "--on", docker_host, xdir=tmp_path, compose_suffix=suffix
    )
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

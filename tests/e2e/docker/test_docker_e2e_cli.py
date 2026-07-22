"""End-to-end CLI tests for `otto docker` and the `otto host <container>` family.

These tests invoke the installed ``otto`` entrypoint as a subprocess so the
**full** dispatch path runs — repo discovery, lab loading, declared-container
synthesis, the docker subcommand, and the host subcommand — exactly as the
user runs it. This is what catches CLI/library-seam bugs that mocked unit
tests miss (e.g. a missing build step before compose, or a missing lab
filter when multiple repos are loaded).

Requirements:
    vagrant up test1 test2 test3   (carrot=test1, tomato=test2, pepper=test3)
    All three VMs must have docker installed and running.

Each test leases one docker-capable host from the pool {carrot, tomato,
pepper} via the same fd-flock mechanism as the transfer-host pool, so
tests distribute across all three daemons and never race on the same one.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from tests._fixtures._host_pool import lease_unix_host
from tests.e2e._otto_subprocess import assert_output_dir

# Docker container hosts require an SSH-based UnixHost parent (see
# DockerContainerHost._make_session: term must be 'ssh').  tomato_seed defaults
# to telnet (it's first in its valid_terms list), so it cannot host containers.
# Restrict the docker lease pool to the SSH-first unix peers only.
_DOCKER_POOL = ("carrot", "pepper")


PROJECT_ROOT = Path(__file__).resolve().parents[3]
REPO1 = PROJECT_ROOT / "tests" / "repo1"
REPO2 = PROJECT_ROOT / "tests" / "repo2"
OTTO_BIN = Path(sys.executable).parent / "otto"
COVERAGERC = PROJECT_ROOT / ".coveragerc"
# Same subprocess-coverage bootstrap that test_completion_cache.py and
# test_coverage_e2e.py use: prepending tests/_coverage_bootstrap to
# PYTHONPATH makes each subprocess run sitecustomize.py, which calls
# coverage.process_startup() so the otto subprocess's line execution
# is merged into the parent test run's coverage data.
COVERAGE_BOOTSTRAP = PROJECT_ROOT / "tests" / "_coverage_bootstrap"

# Each test leases one docker-capable host from UNIX_POOL via the
# ``docker_host`` fixture below, and runs ``otto`` as subprocesses under
# subprocess coverage (COVERAGE_PROCESS_START).  These tests are pinned to a
# single xdist worker via ``xdist_group("docker_e2e")``: spreading
# subprocess-coverage docker tests across workers makes several workers
# finalize coverage concurrently, which trips a coverage.py SQLite
# schema-init race ("no such table: context") during ``cov.save()``.  The
# per-host fd-flock still guards against same-daemon contention.
# (Un-grouping these for daemon-pool parallelism in 248d15b reintroduced the
# race; see tests/integration/test_docker_*.py, which kept the group.)
pytestmark = [pytest.mark.integration, pytest.mark.xdist_group("docker_e2e")]


# ---------------------------------------------------------------------------
# Subprocess helper
# ---------------------------------------------------------------------------


def _run_otto(
    *args: str,
    sut_dirs: str = str(REPO1),
    lab: str = "veggies",
    xdir: Path | None = None,
    compose_suffix: str | None = None,
    timeout: int = 180,
) -> subprocess.CompletedProcess[str]:
    """Run `otto <args>` as a subprocess with a clean environment.

    *compose_suffix* gets baked into ``OTTO_COMPOSE_SUFFIX`` so every test
    can use a unique docker compose project name (e.g. ``otto-repo1-<uuid>``)
    and never collide with concurrent runs on the same docker host.
    """
    env: dict[str, str] = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "OTTO_SUT_DIRS": sut_dirs,
        # Subprocess coverage: coverage_bootstrap/sitecustomize.py runs
        # coverage.process_startup() when this env var points at a config.
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


@pytest.fixture
def docker_host(tmp_path_factory) -> str:  # type: ignore[type-arg]
    """Lease one docker-capable, SSH-based host from the pool for this test's duration.

    Yields the host's seed id, e.g. ``"carrot_seed"``.  The fd-flock on
    the pool lock file (``unix_pool.<element>``) ensures at most one test
    runs against each docker daemon at a time, while xdist can distribute
    different tests to different workers/daemons concurrently.

    The pool is restricted to ``_DOCKER_POOL`` (carrot + pepper) because
    ``DockerContainerHost`` requires its parent to have ``term='ssh'``.
    tomato_seed defaults to telnet (telnet is first in its valid_terms),
    so it cannot serve as a docker container parent.
    """
    lock_dir = tmp_path_factory.getbasetemp().parent
    with lease_unix_host(lock_dir, _DOCKER_POOL) as element:
        yield f"{element}_seed"


@pytest.fixture
def fresh_suffix() -> str:
    """A short unique compose-project suffix so each test has its own stack."""
    return "e2e-" + uuid.uuid4().hex[:8]


@pytest.fixture
def teardown_after(fresh_suffix, docker_host, tmp_path):
    """Yield the suffix; on test exit, ensure the stack is torn down even if
    the test failed mid-flight. Idempotent — `down` is harmless when the
    stack isn't up. Tears down both repo1 and repo2 because the multi-repo
    tests bring up stacks for both, and a half-torn-down repo2 stack leaks
    a docker network on each run; enough leaks (~30) and the docker daemon
    runs out of subnet pools and subsequent ``compose up``s fail with
    ``all predefined address pools have been fully subnetted``."""
    yield fresh_suffix
    # Run a single ``otto docker down`` against both repos so any partially-
    # created repo2 stack from a multi-repo test is cleaned up too.
    # Pass --on <docker_host> so down targets the same daemon the test used.
    _run_otto(
        "docker",
        "down",
        "--on",
        docker_host,
        sut_dirs=f"{REPO1}{os.pathsep}{REPO2}",
        xdir=tmp_path,
        compose_suffix=fresh_suffix,
    )


# ---------------------------------------------------------------------------
# Happy path: build → up → host run/put/get → down
# ---------------------------------------------------------------------------


def test_e2e_up_then_down(teardown_after, docker_host, tmp_path):
    """The bug that started this whole thread: `otto docker up` must build
    images first when the compose file references locally-built ones."""
    suffix = teardown_after
    up = _run_otto("docker", "up", "--on", docker_host, xdir=tmp_path, compose_suffix=suffix)
    assert up.returncode == 0, (
        f"`docker up` should succeed end-to-end\nstdout:\n{up.stdout}\nstderr:\n{up.stderr}"
    )
    assert "container(s) registered" in up.stdout
    assert f"{docker_host}.repo1.api" in up.stdout
    assert "pull access denied" not in (up.stdout + up.stderr), (
        "we must build before composing — pull errors mean we didn't"
    )

    down = _run_otto("docker", "down", "--on", docker_host, xdir=tmp_path, compose_suffix=suffix)
    assert down.returncode == 0, down.stderr
    assert "stack down" in down.stdout
    # docker orchestration runs on a docker host → docker output dir created
    assert_output_dir(tmp_path, "docker")


def test_e2e_host_run_against_running_container(teardown_after, docker_host, tmp_path):
    """Once a stack is up, `otto host <id> run` must execute inside the container."""
    suffix = teardown_after
    up = _run_otto("docker", "up", "--on", docker_host, xdir=tmp_path, compose_suffix=suffix)
    assert up.returncode == 0, up.stderr

    run = _run_otto(
        "host",
        f"{docker_host}.repo1.api",
        "run",
        "cat /etc/repo1-marker.txt",
        xdir=tmp_path,
        compose_suffix=suffix,
    )
    assert run.returncode == 0, (
        f"`otto host <container> run` should reach the running container\n"
        f"stdout:\n{run.stdout}\nstderr:\n{run.stderr}"
    )
    assert "repo1-fixture" in run.stdout, run.stdout
    # docker orchestration runs on a docker host → docker output dir created
    assert_output_dir(tmp_path, "docker")


def test_e2e_host_put_get_roundtrip(teardown_after, docker_host, tmp_path):
    """Two-step put / get through `docker cp` and the parent's SSH."""
    suffix = teardown_after
    up = _run_otto("docker", "up", "--on", docker_host, xdir=tmp_path, compose_suffix=suffix)
    assert up.returncode == 0, up.stderr

    payload = tmp_path / "payload.bin"
    payload.write_bytes(b"e2e-payload-" + b"\xab" * 256)

    put = _run_otto(
        "host",
        f"{docker_host}.repo1.api",
        "put",
        str(payload),
        "/tmp",
        "--mode",
        "755",
        xdir=tmp_path,
        compose_suffix=suffix,
    )
    assert put.returncode == 0, f"put failed:\n{put.stderr}"
    assert "Transfer complete" in put.stdout

    # `--mode 755` must land as 0o755 INSIDE the container. This is the one
    # assertion that cannot be made by reading: the mode is applied by a
    # `docker exec chmod` after `docker cp`, so only a real container proves
    # it reached the right filesystem. It also pins the octal contract
    # end-to-end — decimal 755 would show as 1363.
    stat = _run_otto(
        "host",
        f"{docker_host}.repo1.api",
        "run",
        "stat -c %a /tmp/payload.bin",
        xdir=tmp_path,
        compose_suffix=suffix,
    )
    assert stat.returncode == 0, f"stat failed:\n{stat.stderr}"
    assert "755" in stat.stdout, f"expected mode 755 in container, got:\n{stat.stdout}"

    out_dir = tmp_path / "back"
    out_dir.mkdir()
    get = _run_otto(
        "host",
        f"{docker_host}.repo1.api",
        "get",
        "/tmp/payload.bin",
        str(out_dir),
        xdir=tmp_path,
        compose_suffix=suffix,
    )
    assert get.returncode == 0, f"get failed:\n{get.stderr}"
    assert (out_dir / "payload.bin").read_bytes() == payload.read_bytes()


# ---------------------------------------------------------------------------
# Idempotence and rebuild
# ---------------------------------------------------------------------------


def test_e2e_up_is_idempotent(teardown_after, docker_host, tmp_path):
    """A second `otto docker up` against a running stack must not fail or
    re-create containers."""
    suffix = teardown_after
    first = _run_otto("docker", "up", "--on", docker_host, xdir=tmp_path, compose_suffix=suffix)
    assert first.returncode == 0, first.stderr

    second = _run_otto("docker", "up", "--on", docker_host, xdir=tmp_path, compose_suffix=suffix)
    assert second.returncode == 0, (
        f"second `up` against a running stack must succeed\n"
        f"stdout:\n{second.stdout}\nstderr:\n{second.stderr}"
    )
    assert "container(s) registered" in second.stdout


def test_e2e_build_then_build_again_is_skipped(docker_host, tmp_path):
    """`otto docker build` followed by `otto docker build` must short-circuit
    on `docker image inspect`."""
    first = _run_otto("docker", "build", "--on", docker_host, xdir=tmp_path)
    assert first.returncode == 0, first.stderr

    second = _run_otto("docker", "build", "--on", docker_host, xdir=tmp_path)
    assert second.returncode == 0, second.stderr
    assert "cached" in second.stdout, f"second build should report cached, got:\n{second.stdout}"


def test_e2e_build_rebuild_forces(docker_host, tmp_path):
    """`--rebuild` must run the build even when the hash tag exists."""
    _run_otto("docker", "build", "--on", docker_host, xdir=tmp_path)

    forced = _run_otto("docker", "build", "--rebuild", "--on", docker_host, xdir=tmp_path)
    assert forced.returncode == 0, forced.stderr
    assert "built" in forced.stdout, forced.stdout
    assert "cached" not in forced.stdout, "rebuild should NOT short-circuit"


# ---------------------------------------------------------------------------
# Multi-repo lab filtering (the second user-reported bug)
# ---------------------------------------------------------------------------


def test_e2e_multi_repo_only_active_lab_runs(teardown_after, docker_host, tmp_path):
    """With both repo1 (veggies) and repo2 (fruits) loaded but only the
    veggies lab active, `otto docker up` must only operate on repo1.
    Repo2's grape_seed isn't in the active lab, so it must be skipped
    cleanly — never raise a `host not in lab` error."""
    suffix = teardown_after
    up = _run_otto(
        "docker",
        "up",
        "--on",
        docker_host,
        sut_dirs=f"{REPO1}{os.pathsep}{REPO2}",
        xdir=tmp_path,
        compose_suffix=suffix,
    )
    assert up.returncode == 0, (
        f"multi-repo `up` should silently skip repos targeting other labs\n"
        f"stdout:\n{up.stdout}\nstderr:\n{up.stderr}"
    )
    assert "not in lab" not in (up.stdout + up.stderr), (
        "repo2 (fruits-lab host) must be filtered, not raise"
    )
    # repo1's stack came up on the leased host.
    assert f"{docker_host}.repo1.api" in up.stdout
    # repo2 must be skipped *entirely* — not just deployed to a different
    # host. `_up` prints "<repo> (<project>): N container(s) registered"
    # for every composed repo, so any mention of "repo2" means it was
    # composed. Asserting against the `grape_seed.…` host id alone would
    # miss a regression where `--on <host>` wrongly overrode repo2's
    # lab filter and composed it on that host as `<host>.repo2.worker`
    # — the pre-b466020 bug that leaked an otto-repo2 network every run
    # until docker's address pool was exhausted.
    assert "repo2" not in up.stdout, (
        "repo2 targets the fruits lab and must be skipped under "
        f"--lab veggies — it was composed instead:\n{up.stdout}"
    )


def test_e2e_multi_repo_down_no_traceback(docker_host, tmp_path):
    """With both repos in SUT_DIRS, `otto docker down` must not raise a
    Python traceback for the unrelated lab.

    Repo2 targets the fruits lab (grape_seed) which is not in the active
    veggies lab. The bug (pre-b466020) raised
    ``ValueError("Docker host 'grape_seed' is not in lab 'veggies'")``.
    The fix filters repo2 out before calling compose_down so only repo1
    is processed. ``--on`` is required because repo1's compose spec has
    no ``default_host``; the host must be in the active lab to pass
    the _select_repos guard.
    """
    result = _run_otto(
        "docker",
        "down",
        "--on",
        docker_host,
        sut_dirs=f"{REPO1}{os.pathsep}{REPO2}",
        xdir=tmp_path,
    )
    # Even if nothing is up, the command must exit cleanly without a traceback.
    assert "Traceback" not in (result.stdout + result.stderr), (
        f"unexpected traceback:\n{result.stderr}"
    )
    assert "not in lab" not in (result.stdout + result.stderr)


# ---------------------------------------------------------------------------
# Listing & tab-completion sources
# ---------------------------------------------------------------------------


def test_e2e_list_hosts_includes_declared_container(tmp_path):
    """Containers must appear in `--list-hosts` *before* any `up` so the user
    can tab-complete and prepare commands.

    With no ``default_host`` in repo1's compose spec, all three docker-capable
    hosts are pre-registered, so the output must contain at least one
    ``<element>_seed.repo1.api`` id.
    """
    result = _run_otto("--list-hosts", "host", xdir=tmp_path)
    # The flag prints the host list and exits non-zero in some paths;
    # accept either rc as long as at least one declared container id appears.
    output = result.stdout + result.stderr
    declared = [f"{el}_seed.repo1.api" for el in ("carrot", "tomato", "pepper")]
    assert any(h in output for h in declared), (
        f"expected at least one of {declared} in output:\n{output}"
    )


def test_e2e_run_against_unstarted_container_auto_starts(teardown_after, docker_host, tmp_path):
    """Accessing a declared container whose stack isn't running must
    auto-start the stack (feature de361cc) rather than erroring.

    The command then succeeds against the freshly-started container — no
    ``otto docker up`` step required of the caller. ``teardown_after``
    reaps the auto-started stack so it can't leak.
    """
    suffix = teardown_after
    result = _run_otto(
        "host",
        f"{docker_host}.repo1.api",
        "run",
        "true",
        xdir=tmp_path,
        compose_suffix=suffix,
    )
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    # The api container was brought up on demand before the command ran.
    assert "Started" in output or "Running" in output, (
        f"expected the stack to be auto-started, got:\n{output}"
    )


# ---------------------------------------------------------------------------
# CLI error surface
# ---------------------------------------------------------------------------


def test_e2e_up_unknown_host_clear_error(tmp_path):
    """`otto docker up --on <unknown>` exits cleanly with a clear message."""
    result = _run_otto("docker", "up", "--on", "no_such_host", xdir=tmp_path)
    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "not in lab" in output or "no_such_host" in output, output
    assert "Traceback" not in output, f"unexpected traceback:\n{output}"


def test_e2e_ps_lists_running_containers(teardown_after, docker_host, tmp_path):
    """After `up`, `otto docker ps` must show the running container."""
    suffix = teardown_after
    _run_otto("docker", "up", "--on", docker_host, xdir=tmp_path, compose_suffix=suffix)
    ps = _run_otto("docker", "ps", "--on", docker_host, xdir=tmp_path, compose_suffix=suffix)
    assert ps.returncode == 0, ps.stderr
    # Expect the project name (or the container name embedding it) somewhere.
    assert f"otto-repo1-{suffix}" in ps.stdout or "repo1-api" in ps.stdout, ps.stdout

"""End-to-end CLI tests for `otto docker` and the `otto host <container>` family.

These tests invoke the installed ``otto`` entrypoint as a subprocess so the
**full** dispatch path runs — repo discovery, lab loading, declared-container
synthesis, the docker subcommand, and the host subcommand — exactly as the
user runs it. This is what catches CLI/library-seam bugs that mocked unit
tests miss (e.g. a missing build step before compose, or a missing lab
filter when multiple repos are loaded).

Requirements:
    vagrant up test1 test2 test3
    All three VMs must have docker installed and running.

Most tests lease one docker-capable host from {test1, test3} via the same
fd-flock mechanism as the transfer-host pool, so they distribute across two
daemons and never race on the same one. The ones that address a container id
from a second otto process lease test3 specifically — see
``_ROLE_DOCKER_HOST`` below for why that is a placement fact, not a
preference.
"""

from __future__ import annotations

import os
import subprocess
import uuid
from pathlib import Path

import pytest

from tests._fixtures._host_pool import lease_unix_host
from tests.e2e._otto_subprocess import (
    PROJECT_ROOT,
    REPO1,
    assert_no_output_dir,
    assert_output_dir,
    run_otto,
)

# Docker container hosts require an SSH-based UnixHost parent (see
# DockerContainerHost._make_session: term must be 'ssh').  test2 defaults
# to telnet (it's first in its valid_terms list), so it cannot host containers.
# Restrict the docker lease pool to the SSH-first unix peers only.
_DOCKER_POOL = ("test1", "test3")

# The one host repo1's fragments RESOLVE to without `--on`. Every
# ``[[docker.use_cases]]`` fragment of the sample repos declares
# ``role = "docker"``, and test3 is the only element in the `unix` fixture lab
# tagged ``"roles": ["docker"]`` (spec §5 knob 3). Placeholder registration
# therefore only ever mints ``test3.<usecase>.<service>`` ids, so any test that
# addresses a container id in a SEPARATE otto process — `otto host <id> ...`,
# `otto run --on <id>` — must run against this host and no other: `--on test1`
# registers `test1.repo1.api` inside the invocation that deployed it, and the
# next process knows nothing about it. Tests that only drive `docker up/down/
# build/ps --on <host>` keep the two-daemon pool above.
_ROLE_DOCKER_HOST = "test3"

# The use-cases the sample repos declare (spec §14's "name the fragment after
# the repo and container ids stay literally unchanged"): repo1 declares `repo1`
# and `integration`, repo2 declares `repo2` and `integration`. Two or more
# declared use-cases make a bare `otto docker up` ambiguous — it refuses,
# naming them — so every invocation below names the one it means.
_REPO1_USE_CASE = "repo1"
_MERGED_USE_CASE = "integration"


REPO2 = PROJECT_ROOT / "tests" / "repo2"

# Each test leases one docker-capable host from UNIX_POOL via the
# ``docker_host`` fixture below, and runs ``otto`` as subprocesses under
# subprocess coverage (see tests/e2e/_otto_subprocess.py).  These tests are pinned to a
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
    lab: str = "unix",
    xdir: Path | None = None,
    compose_suffix: str | None = None,
    env: dict[str, str] | None = None,
    timeout: int = 180,
) -> subprocess.CompletedProcess[str]:
    """Run `otto -R --lab <lab> <args>` as a subprocess with a clean environment.

    *compose_suffix* gets baked into ``OTTO_COMPOSE_SUFFIX`` so every test
    can use a unique docker compose project name (e.g.
    ``unix-repo1-<uuid>``) and never collide with concurrent runs on the
    same docker host. *env* merges last, for the few tests that need to pin
    something else about the child (``COLUMNS``, a ``pass_env`` value).

    ``OTTO_SUT_DIRS`` goes through ``extra_env`` rather than the runner's
    ``sut_dirs=``: the multi-repo tests pass an ``os.pathsep``-joined *string*
    of two repo roots, which is not a single path.
    """
    extra_env: dict[str, str] = {"OTTO_SUT_DIRS": sut_dirs}
    if compose_suffix is not None:
        extra_env["OTTO_COMPOSE_SUFFIX"] = compose_suffix
    if env is not None:
        extra_env.update(env)

    return run_otto(
        list(args),
        xdir=xdir,
        sut_dirs=None,
        lab=lab,
        extra_argv_prefix=["-R"],
        extra_env=extra_env,
        timeout=timeout,
    )


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
def role_docker_host(tmp_path_factory) -> str:  # type: ignore[type-arg]
    """Lease :data:`_ROLE_DOCKER_HOST` — the host repo1's fragments place onto.

    Same fd-flock as ``docker_host``, narrowed to one element. Used by the
    tests that address a container id from a second otto process, where the
    id must be one PLACEMENT produced rather than one ``--on`` invented.
    """
    lock_dir = tmp_path_factory.getbasetemp().parent
    with lease_unix_host(lock_dir, [_ROLE_DOCKER_HOST]) as element:
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
    # fragments the test deployed. --on <docker_host> targets the daemon the
    # test used. `provide` is deliberately NOT passed: the compose project is
    # derived from (lab, use-case, suffix) alone, so one `down` reaps the
    # stack whichever provider won.
    for use_case in (_REPO1_USE_CASE, _MERGED_USE_CASE):
        _run_otto(
            "docker",
            "down",
            use_case,
            "--on",
            docker_host,
            sut_dirs=f"{REPO1}{os.pathsep}{REPO2}",
            xdir=tmp_path,
            compose_suffix=fresh_suffix,
        )


@pytest.fixture
def teardown_role_host_after(fresh_suffix, role_docker_host, tmp_path):
    """``teardown_after``, for the tests that lease :data:`_ROLE_DOCKER_HOST`."""
    yield fresh_suffix
    for use_case in (_REPO1_USE_CASE, _MERGED_USE_CASE):
        _run_otto(
            "docker",
            "down",
            use_case,
            "--on",
            role_docker_host,
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
    up = _run_otto(
        "docker", "up", _REPO1_USE_CASE, "--on", docker_host, xdir=tmp_path, compose_suffix=suffix
    )
    assert up.returncode == 0, (
        f"`docker up` should succeed end-to-end\nstdout:\n{up.stdout}\nstderr:\n{up.stderr}"
    )
    assert "container(s) registered" in up.stdout
    assert f"{docker_host}.repo1.api" in up.stdout
    assert "pull access denied" not in (up.stdout + up.stderr), (
        "we must build before composing — pull errors mean we didn't"
    )

    down = _run_otto(
        "docker", "down", _REPO1_USE_CASE, "--on", docker_host, xdir=tmp_path, compose_suffix=suffix
    )
    assert down.returncode == 0, down.stderr
    assert f"{_REPO1_USE_CASE}: torn down." in down.stdout, down.stdout
    # docker orchestration runs on a docker host → docker output dir created
    assert_output_dir(tmp_path, "docker")


def test_e2e_host_run_against_running_container(
    teardown_role_host_after, role_docker_host, tmp_path
):
    """Once a stack is up, `otto host <id> run` must execute inside the container.

    Leases :data:`_ROLE_DOCKER_HOST`: the id below is read by a SECOND otto
    process, which knows only the placeholders placement minted for it.
    """
    suffix = teardown_role_host_after
    docker_host = role_docker_host
    up = _run_otto(
        "docker", "up", _REPO1_USE_CASE, "--on", docker_host, xdir=tmp_path, compose_suffix=suffix
    )
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


def test_e2e_host_put_get_roundtrip(teardown_role_host_after, role_docker_host, tmp_path):
    """Two-step put / get through `docker cp` and the parent's SSH.

    On :data:`_ROLE_DOCKER_HOST` for ``test_e2e_host_run_against_running_container``'s
    reason: the container id is addressed from separate otto processes.
    """
    suffix = teardown_role_host_after
    docker_host = role_docker_host
    up = _run_otto(
        "docker", "up", _REPO1_USE_CASE, "--on", docker_host, xdir=tmp_path, compose_suffix=suffix
    )
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
    first = _run_otto(
        "docker", "up", _REPO1_USE_CASE, "--on", docker_host, xdir=tmp_path, compose_suffix=suffix
    )
    assert first.returncode == 0, first.stderr

    second = _run_otto(
        "docker", "up", _REPO1_USE_CASE, "--on", docker_host, xdir=tmp_path, compose_suffix=suffix
    )
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


def test_e2e_multi_repo_build_names_the_excluded_repo(docker_host, tmp_path):
    """`otto docker build` must name the repo it excluded, and skip it cleanly.

    Repo2's ``[[docker.use_cases]]`` pins ``alt3``, a host of the unix_alt
    lab, so under ``--lab unix`` it is not applicable and `_select_repos`
    excludes it. That exclusion used to be a DEBUG log and an exit 0 with no
    output (the reported demo failure); it is a loud yellow line now. `build`
    is where `_select_repos` still runs — `up`/`down` select by use-case name
    (spec §10), not by repo — so this is the verb that pins the loudness.
    """
    result = _run_otto(
        "docker",
        "build",
        "--on",
        docker_host,
        sut_dirs=f"{REPO1}{os.pathsep}{REPO2}",
        xdir=tmp_path,
    )
    assert result.returncode == 0, (
        f"multi-repo `build` should skip repos targeting other labs (loudly, not raise)\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "not in lab" not in (result.stdout + result.stderr), (
        "repo2 (unix_alt-lab host) must be filtered, not raise"
    )
    assert "skipping repo 'repo2'" in result.stdout, (
        f"repo2 (unix_alt-lab host) must be named in a loud exclusion line:\n{result.stdout}"
    )
    # `_build` prints "<repo>/<image>: built|cached → <tag>" for every image
    # it actually builds. repo2's only image is `worker`, so its absence in
    # that form is what proves the exclusion reached the build loop and was
    # not merely announced.
    assert "repo2/worker" not in result.stdout, (
        f"repo2 was excluded but its image was built anyway:\n{result.stdout}"
    )
    assert "repo1/api" in result.stdout, result.stdout


def test_e2e_multi_repo_up_composes_only_the_named_use_case(teardown_after, docker_host, tmp_path):
    """With both repos loaded, `otto docker up repo1` must touch repo1 alone.

    A use-case is the unit of deployment now, so the narrowing is by NAME:
    only repo1 declares ``repo1``, so repo2 contributes no fragment and no
    container. Checking only the ``alt3.…`` host id would miss a regression
    where ``--on <host>`` wrongly pulled repo2's stack onto that host as
    ``<host>.repo2.worker`` — the pre-b466020 bug that leaked an otto-repo2
    network every run until docker's address pool was exhausted.
    """
    suffix = teardown_after
    up = _run_otto(
        "docker",
        "up",
        _REPO1_USE_CASE,
        "--on",
        docker_host,
        sut_dirs=f"{REPO1}{os.pathsep}{REPO2}",
        xdir=tmp_path,
        compose_suffix=suffix,
    )
    assert up.returncode == 0, (
        f"multi-repo `up <use-case>` should deploy just that use-case\n"
        f"stdout:\n{up.stdout}\nstderr:\n{up.stderr}"
    )
    assert "not in lab" not in (up.stdout + up.stderr)
    # repo1's stack came up on the leased host, under the use-case's own id.
    assert f"{docker_host}.repo1.api" in up.stdout
    not_composed = (
        f"use-case {_REPO1_USE_CASE!r} has no repo2 fragment, so nothing of "
        f"repo2's may have been composed:\n{up.stdout}"
    )
    # The exact id repo2's only service would register under if its fragment
    # had been pulled into this use-case: `<parent>.<usecase>.<service>`.
    assert f"{docker_host}.{_REPO1_USE_CASE}.worker" not in up.stdout, not_composed
    assert ".repo2." not in up.stdout, not_composed


def test_e2e_multi_repo_down_no_traceback(docker_host, tmp_path):
    """With both repos in SUT_DIRS, `otto docker down` must not raise a
    Python traceback for the unrelated lab.

    Repo2 targets the unix_alt lab (alt3) which is not in the active
    unix lab. The bug (pre-b466020) raised
    ``ValueError("Docker host 'alt3' is not in lab 'unix'")``.
    Selecting by use-case name keeps repo2 out of ``down repo1`` entirely.
    ``--on`` targets the specific leased host, which must be in the active
    lab for placement to accept it.
    """
    result = _run_otto(
        "docker",
        "down",
        _REPO1_USE_CASE,
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
    assert result.returncode == 0, result.stdout + result.stderr


# ---------------------------------------------------------------------------
# Listing & tab-completion sources
# ---------------------------------------------------------------------------


def test_e2e_list_hosts_includes_declared_container(tmp_path):
    """Containers must appear in `--list-hosts` *before* any `up` so the user
    can tab-complete and prepare commands.

    Placeholder registration walks USE-CASES now (spec §9), and each of
    repo1's fragments declares ``role = "docker"`` — a role exactly one host
    in the fixture lab carries. So the ids are exact rather than
    "one of the docker-capable hosts", and BOTH of repo1's use-cases
    contribute, which is what proves the walk is per-fragment.
    """
    result = _run_otto("--list-hosts", "host", xdir=tmp_path)
    # The flag prints the host list and exits non-zero in some paths;
    # accept either rc as long as the declared container ids appear.
    output = result.stdout + result.stderr
    declared = [
        f"{_ROLE_DOCKER_HOST}.{_REPO1_USE_CASE}.api",
        f"{_ROLE_DOCKER_HOST}.{_MERGED_USE_CASE}.api",
        f"{_ROLE_DOCKER_HOST}.{_MERGED_USE_CASE}.edge",
    ]
    missing = [h for h in declared if h not in output]
    assert not missing, f"expected {missing} in output:\n{output}"


def test_e2e_run_against_unstarted_container_auto_starts(
    teardown_role_host_after, role_docker_host, tmp_path
):
    """Accessing a declared container whose stack isn't running must
    auto-start the stack (feature de361cc) rather than erroring.

    The command then succeeds against the freshly-started container — no
    ``otto docker up`` step required of the caller.
    ``teardown_role_host_after`` reaps the auto-started stack so it can't
    leak. The id is a PLACEHOLDER's, so this must run on the host placement
    minted it for (:data:`_ROLE_DOCKER_HOST`).
    """
    suffix = teardown_role_host_after
    docker_host = role_docker_host
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
    result = _run_otto("docker", "up", _REPO1_USE_CASE, "--on", "no_such_host", xdir=tmp_path)
    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "not in lab" in output or "no_such_host" in output, output
    assert "Traceback" not in output, f"unexpected traceback:\n{output}"


def test_e2e_up_with_no_use_case_names_the_declared_ones(tmp_path):
    """A bare `otto docker up` is ambiguous now, and says so (spec §10).

    Both sample repos declare two use-cases each, so omitting the positional
    is a hard error listing them — never a silent pick, and never a no-op.
    No host is leased: the refusal is settled from configuration, above the
    first device touch, so this test contacts no daemon.
    """
    result = _run_otto(
        "docker",
        "up",
        sut_dirs=f"{REPO1}{os.pathsep}{REPO2}",
        xdir=tmp_path,
    )
    output = " ".join((result.stdout + result.stderr).split())
    assert result.returncode == 1, output
    assert "use-cases are declared" in output, output
    for name in (_REPO1_USE_CASE, _MERGED_USE_CASE, "repo2"):
        assert name in output, f"the refusal must name {name!r}:\n{output}"
    assert "Traceback" not in output, f"unexpected traceback:\n{output}"


def test_e2e_ps_lists_running_containers(teardown_after, docker_host, tmp_path):
    """After `up`, `otto docker ps` must show the running container."""
    suffix = teardown_after
    _run_otto(
        "docker", "up", _REPO1_USE_CASE, "--on", docker_host, xdir=tmp_path, compose_suffix=suffix
    )
    ps = _run_otto("docker", "ps", "--on", docker_host, xdir=tmp_path, compose_suffix=suffix)
    assert ps.returncode == 0, ps.stderr
    # The compose project is `<lab>-<usecase>-<suffix>` (spec §9) — no
    # `otto-` prefix any more: the deployment belongs to the product.
    assert f"unix-{_REPO1_USE_CASE}-{suffix}" in ps.stdout or "repo1-api" in ps.stdout, ps.stdout


# ---------------------------------------------------------------------------
# Use-cases: the merged displacement pair (spec §4, §9, §10, §12)
#
# repo1 provides the REAL `edge` at priority 10; repo2 provides a mock at 0
# and stands down. Both repos also contribute an unconditional fragment, so a
# full `integration` deployment is `api` + `worker` + `edge` in ONE compose
# project — the merge these tests exist to drive through the actual binary.
# ---------------------------------------------------------------------------

_BOTH_REPOS = f"{REPO1}{os.pathsep}{REPO2}"

# rich sizes its tables to the terminal; a subprocess has none, so it falls
# back to 80 columns and truncates cells like `repo1[core,edge]` mid-word.
# Pin a wide one so the assertions below read the values, not the ellipsis.
_WIDE = {"COLUMNS": "200"}


def _flat(text: str) -> str:
    """Collapse rich's wrapping so a rendered line can be matched as one string."""
    return " ".join(text.split())


def test_e2e_use_cases_reports_the_displacement(tmp_path):
    """`otto docker use-cases` is the inventory view (spec §10).

    Read-only: it resolves selection and placement and reports them, contacts
    nothing, and creates no output dir. Both repos' fragments must appear,
    and the loser must be annotated as displaced — the whole point of the
    verb is that you can see who won a capability before deploying.
    """
    result = _run_otto(
        "docker",
        "use-cases",
        _MERGED_USE_CASE,
        sut_dirs=_BOTH_REPOS,
        xdir=tmp_path,
        env=_WIDE,
    )
    out = _flat(result.stdout + result.stderr)
    assert result.returncode == 0, out
    assert f"use-case {_MERGED_USE_CASE}" in out, out
    # Every candidate fragment is listed — both repos, winners and losers.
    #
    # Asserted through the `provides` column, NOT the `fragment` one: the
    # fragment cell is built as `<repo>[<handles>]`, and while that cell is now
    # escape()d (T16 — unescaped, rich ate the bracketed half as console markup
    # and a repo's two fragments rendered identically), it is also the cell most
    # likely to WRAP at whatever width the e2e subprocess console picks. The
    # bracket rendering is pinned width-independently by
    # tests/unit/docker/test_cli.py; the columns asserted here are the ones that
    # prove what this verb is FOR.
    assert "edge (priority 10)" in out, f"repo1's winning provider is missing:\n{out}"
    assert "edge (priority 0)" in out, f"repo2's mock provider is missing:\n{out}"
    assert "EDGE_ADDR" in out, f"the env KEY names must be listed (never values):\n{out}"
    assert "displaced" in out, out
    # The sentence under the table names who won, at what priority, and who
    # stood down — and calls NEITHER priority the higher one.
    assert "edge goes to repo1 (priority 10); repo2 (priority 0) stands down" in out, out
    # Read-only verbs produce no per-invocation output dir.
    assert_no_output_dir(tmp_path)


def test_e2e_merged_use_case_up_then_down(teardown_after, docker_host, tmp_path):
    """`up integration` merges both repos into one stack; `down` reverses it."""
    suffix = teardown_after
    up = _run_otto(
        "docker",
        "up",
        _MERGED_USE_CASE,
        "--on",
        docker_host,
        sut_dirs=_BOTH_REPOS,
        xdir=tmp_path,
        compose_suffix=suffix,
    )
    out = up.stdout + up.stderr
    assert up.returncode == 0, out
    assert "pull access denied" not in out, (
        "both repos' images must be built before composing — a pull error means one wasn't"
    )
    # One project, one report line, three services from two repos.
    assert f"{_MERGED_USE_CASE} on {docker_host} (unix-{_MERGED_USE_CASE}-{suffix})" in up.stdout, (
        up.stdout
    )
    for service in ("api", "worker", "edge"):
        assert f"{docker_host}.{_MERGED_USE_CASE}.{service}" in up.stdout, (
            f"{service!r} is missing from the merged stack:\n{up.stdout}"
        )
    # The competition's outcome is reported on the way up, not only by `use-cases`.
    assert "edge goes to repo1 (priority 10); repo2 (priority 0) stands down" in _flat(up.stdout)

    down = _run_otto(
        "docker",
        "down",
        _MERGED_USE_CASE,
        "--on",
        docker_host,
        sut_dirs=_BOTH_REPOS,
        xdir=tmp_path,
        compose_suffix=suffix,
    )
    assert down.returncode == 0, down.stdout + down.stderr
    assert f"{_MERGED_USE_CASE}: torn down." in down.stdout, down.stdout

    ps = _run_otto("docker", "ps", "--on", docker_host, xdir=tmp_path, compose_suffix=suffix)
    assert ps.returncode == 0, ps.stderr
    assert f"unix-{_MERGED_USE_CASE}-{suffix}" not in ps.stdout, (
        f"the stack survived its teardown:\n{ps.stdout}"
    )


def test_e2e_provide_flips_the_winner(teardown_after, docker_host, tmp_path):
    """`--provide edge=repo2` hands the capability to the mock (spec §4).

    Two things are asserted, and the second is the sharper one: the winner
    can carry a LOWER priority than the fragment it displaced (the override
    narrows the field to one repo BEFORE ranking), and a loser is excluded
    WHOLE — repo1's `core` goes with its `edge`, so `api` is not deployed at
    all.
    """
    suffix = teardown_after
    up = _run_otto(
        "docker",
        "up",
        _MERGED_USE_CASE,
        "--provide",
        "edge=repo2",
        "--on",
        docker_host,
        sut_dirs=_BOTH_REPOS,
        xdir=tmp_path,
        compose_suffix=suffix,
    )
    out = up.stdout + up.stderr
    assert up.returncode == 0, out
    assert "edge goes to repo2 (priority 0); repo1 (priority 10) stands down" in _flat(up.stdout), (
        f"the report must render the record as it IS — an override can seat a "
        f"lower-priority winner:\n{up.stdout}"
    )
    for service in ("worker", "edge"):
        assert f"{docker_host}.{_MERGED_USE_CASE}.{service}" in up.stdout, up.stdout
    assert f"{docker_host}.{_MERGED_USE_CASE}.api" not in up.stdout, (
        f"repo1 lost the capability, so its whole fragment — `core` included — "
        f"must be excluded:\n{up.stdout}"
    )


def test_e2e_dry_run_prints_the_plan_and_starts_nothing(docker_host, tmp_path):
    """`otto --dry-run docker up integration` previews and touches nothing (spec §12).

    The decline carries the resolved plan AND the exact per-host compose
    command, which is what makes it a preview rather than a shrug — and the
    caller's `--env-file` is visible in that command, proving the merge ran.
    A `docker ps` afterwards proves no container was started.

    No `teardown_after`: this test brings nothing up. If it ever did, the
    `docker ps` assertion below is what would say so.
    """
    suffix = "e2e-dryrun-" + uuid.uuid4().hex[:8]
    env_file = tmp_path / "caller.env"
    env_file.write_text("CALLER_KEY=caller-value\n")

    dry = _run_otto(
        "-n",
        "docker",
        "up",
        _MERGED_USE_CASE,
        "--env-file",
        str(env_file),
        "--on",
        docker_host,
        sut_dirs=_BOTH_REPOS,
        xdir=tmp_path,
        compose_suffix=suffix,
    )
    out = _flat(dry.stdout + dry.stderr)
    assert dry.returncode == 0, f"a dry run is an answer, not a failure:\n{out}"
    assert "Traceback" not in out, out
    assert f"Resolved plan: {docker_host} <- repo1[core,edge], repo2[core]" in out, out
    assert "Displaced: edge -> repo1 (priority 10), repo2 (priority 0) stands down" in out, out
    assert "Fragment env keys: ['EDGE_ADDR']" in out, out
    # Spec §12: the EXACT command, not a description of one.
    assert f"docker compose -p unix-{_MERGED_USE_CASE}-{suffix}" in out, out
    assert "up -d --remove-orphans" in out, out
    assert "CALLER_KEY=caller-value" in out, (
        f"the caller's --env-file must have merged into the previewed command:\n{out}"
    )

    ps = _run_otto("docker", "ps", "--on", docker_host, xdir=tmp_path, compose_suffix=suffix)
    assert ps.returncode == 0, ps.stderr
    assert f"unix-{_MERGED_USE_CASE}-{suffix}" not in ps.stdout, (
        f"THE DRY RUN STARTED A STACK:\n{ps.stdout}"
    )

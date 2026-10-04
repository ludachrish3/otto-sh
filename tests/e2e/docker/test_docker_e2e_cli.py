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
``_DEFAULT_PARENT`` in ``_cli`` for why that is the lab's default parent, not a
preference.
"""

from __future__ import annotations

import os
import uuid

import pytest

from tests.e2e._otto_subprocess import REPO1, assert_no_output_dir, assert_output_dir

from ._cli import (
    _DEFAULT_PARENT,
    _MERGED_USE_CASE,
    _REPO1_USE_CASE,
    _WIDE,
    REPO2,
    _flat,
    _run_otto,
)

# Each test leases one docker-capable host from UNIX_POOL via the
# ``docker_host`` fixture (tests/e2e/docker/conftest.py), and runs ``otto`` as subprocesses under
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
# Happy path: build → up → host run/put/get → down
# ---------------------------------------------------------------------------


def test_e2e_up_then_down(teardown_after, docker_host, tmp_path):
    """`otto docker compose up` builds nothing on its own: it builds first only with
    `--build`, and this is the test that proves the flag does it.

    The compose file references a locally built image, so a pull error here would
    mean `--build` did not build it. That `up` WITHOUT the flag builds nothing is
    pinned in ``test_docker_honesty.py``.
    """
    suffix = teardown_after
    up = _run_otto(
        "docker",
        "compose",
        "up",
        _REPO1_USE_CASE,
        "--parent",
        docker_host,
        "--build",
        xdir=tmp_path,
        compose_suffix=suffix,
    )
    assert up.returncode == 0, (
        f"`docker up` should succeed end-to-end\nstdout:\n{up.stdout}\nstderr:\n{up.stderr}"
    )
    assert "container(s) registered" in up.stdout
    assert f"{docker_host}.repo1.api" in up.stdout
    assert "pull access denied" not in (up.stdout + up.stderr), (
        "`--build` must build before composing — a pull error means it did not"
    )

    down = _run_otto(
        "docker",
        "compose",
        "down",
        _REPO1_USE_CASE,
        "--parent",
        docker_host,
        xdir=tmp_path,
        compose_suffix=suffix,
    )
    assert down.returncode == 0, down.stderr
    assert f"{docker_host}: {_REPO1_USE_CASE} torn down" in down.stdout, down.stdout
    # docker orchestration runs on a docker host → docker output dir created
    assert_output_dir(tmp_path, "docker")


def test_e2e_host_run_against_running_container(
    teardown_default_parent_after, default_parent_host, tmp_path
):
    """Once a stack is up, `otto host <id> run` must execute inside the container.

    Leases :data:`_DEFAULT_PARENT`: the id below is read by a SECOND otto
    process, which knows only the placeholders the default parent minted for it.
    """
    suffix = teardown_default_parent_after
    docker_host = default_parent_host
    up = _run_otto(
        "docker",
        "compose",
        "up",
        _REPO1_USE_CASE,
        "--parent",
        docker_host,
        "--build",
        xdir=tmp_path,
        compose_suffix=suffix,
    )
    assert up.returncode == 0, up.stderr

    run = _run_otto(
        "host",
        f"{docker_host}.repo1.api",
        "exec",
        "cat /etc/repo1-marker.txt",
        xdir=tmp_path,
        compose_suffix=suffix,
    )
    assert run.returncode == 0, (
        f"`otto host <container> exec` should reach the running container\n"
        f"stdout:\n{run.stdout}\nstderr:\n{run.stderr}"
    )
    assert "repo1-fixture" in run.stdout, run.stdout
    # docker orchestration runs on a docker host → docker output dir created
    assert_output_dir(tmp_path, "docker")


def test_e2e_host_put_get_roundtrip(teardown_default_parent_after, default_parent_host, tmp_path):
    """Two-step put / get through `docker cp` and the parent's SSH.

    On :data:`_DEFAULT_PARENT` for ``test_e2e_host_run_against_running_container``'s
    reason: the container id is addressed from separate otto processes.
    """
    suffix = teardown_default_parent_after
    docker_host = default_parent_host
    up = _run_otto(
        "docker",
        "compose",
        "up",
        _REPO1_USE_CASE,
        "--parent",
        docker_host,
        "--build",
        xdir=tmp_path,
        compose_suffix=suffix,
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
        "exec",
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
# Idempotence
# ---------------------------------------------------------------------------


def test_e2e_up_is_idempotent(teardown_after, docker_host, tmp_path):
    """A second `otto docker compose up` against a running stack must not fail or
    re-create containers.

    Only the first `up` says `--build`: the second needs no build, because the
    image the first one built is already on the daemon.
    """
    suffix = teardown_after
    first = _run_otto(
        "docker",
        "compose",
        "up",
        _REPO1_USE_CASE,
        "--parent",
        docker_host,
        "--build",
        xdir=tmp_path,
        compose_suffix=suffix,
    )
    assert first.returncode == 0, first.stderr

    second = _run_otto(
        "docker",
        "compose",
        "up",
        _REPO1_USE_CASE,
        "--parent",
        docker_host,
        xdir=tmp_path,
        compose_suffix=suffix,
    )
    assert second.returncode == 0, (
        f"second `up` against a running stack must succeed\n"
        f"stdout:\n{second.stdout}\nstderr:\n{second.stderr}"
    )
    assert "container(s) registered" in second.stdout


# ---------------------------------------------------------------------------
# Multi-repo build (host-wide, ignores use-cases)
# ---------------------------------------------------------------------------


def test_e2e_multi_repo_build_builds_every_loaded_repo_on_the_host(docker_host, tmp_path):
    """`otto docker build --parent HOST` builds every loaded repo's declared
    images on that host, whatever use-cases they take part in.
    """
    result = _run_otto(
        "docker",
        "build",
        "--parent",
        docker_host,
        sut_dirs=f"{REPO1}{os.pathsep}{REPO2}",
        xdir=tmp_path,
    )
    assert result.returncode == 0, (
        f"multi-repo `build` should build every loaded repo\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    # `_render_build_report` prints "<repo>/<image>: built <refs>  <id>  (<host>)"
    # for every image it builds.
    assert "repo1/repo1-api: built " in result.stdout, result.stdout
    assert "repo2/repo2-worker: built " in result.stdout, result.stdout


def test_e2e_multi_repo_up_composes_only_the_named_use_case(teardown_after, docker_host, tmp_path):
    """With both repos loaded, `otto docker compose up repo1` must touch repo1 alone.

    A use-case is the unit of deployment now, so the narrowing is by NAME:
    only repo1 declares ``repo1``, so repo2 contributes no fragment and no
    container. Checking only the leased host's id would miss a regression
    where ``--parent <host>`` wrongly pulled repo2's stack onto that host as
    ``<host>.repo2.worker`` — the pre-b466020 bug that leaked an otto-repo2
    network every run until docker's address pool was exhausted.
    """
    suffix = teardown_after
    up = _run_otto(
        "docker",
        "compose",
        "up",
        _REPO1_USE_CASE,
        "--parent",
        docker_host,
        "--build",
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
    """With both repos in SUT_DIRS, `otto docker compose down` must not raise a
    Python traceback for the unrelated lab.

    The bug (pre-b466020) let repo2, whose host lives in another lab, take part
    in the unix lab's teardown and raise
    ``ValueError("Docker host 'alt3' is not in lab 'unix'")``.
    Selecting by use-case name keeps repo2 out of ``down repo1`` entirely.
    ``--parent`` names the specific leased host, which must be in the active
    lab for the explicit-parent check to accept it.
    """
    result = _run_otto(
        "docker",
        "compose",
        "down",
        _REPO1_USE_CASE,
        "--parent",
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

    Placeholder registration walks USE-CASES now (spec §9), and mints their ids
    under the lab's default parent — the one host ``docker_priority`` ranks in
    the fixture lab. So the ids are exact rather than "one of the
    docker-capable hosts", and BOTH of repo1's use-cases contribute, which is
    what proves the walk is per-fragment.
    """
    result = _run_otto("--list-hosts", "host", xdir=tmp_path)
    # The flag prints the host list and exits non-zero in some paths;
    # accept either rc as long as the declared container ids appear.
    output = result.stdout + result.stderr
    declared = [
        f"{_DEFAULT_PARENT}.{_REPO1_USE_CASE}.api",
        f"{_DEFAULT_PARENT}.{_MERGED_USE_CASE}.api",
        f"{_DEFAULT_PARENT}.{_MERGED_USE_CASE}.edge",
    ]
    missing = [h for h in declared if h not in output]
    assert not missing, f"expected {missing} in output:\n{output}"


def test_e2e_run_against_unstarted_container_auto_starts(
    teardown_default_parent_after, default_parent_host, tmp_path
):
    """Accessing a declared container whose stack isn't running must
    auto-start the stack (feature de361cc) rather than erroring.

    The command then succeeds against the freshly-started container — no
    ``otto docker compose up`` step required of the caller.
    ``teardown_default_parent_after`` reaps the auto-started stack so it can't
    leak. The image is built first, explicitly: starting a stack never builds.
    The id is a PLACEHOLDER's, so this must run on the host the default parent
    minted it for (:data:`_DEFAULT_PARENT`).
    """
    suffix = teardown_default_parent_after
    docker_host = default_parent_host
    # Auto-start builds nothing, so the image it composes must already be there.
    built = _run_otto(
        "docker", "build", "repo1-api", "--parent", docker_host, xdir=tmp_path, env=_WIDE
    )
    assert built.returncode == 0, built.stdout + built.stderr
    result = _run_otto(
        "host",
        f"{docker_host}.repo1.api",
        "exec",
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
    """`otto docker compose up --parent <unknown>` exits cleanly with a clear message."""
    result = _run_otto(
        "docker", "compose", "up", _REPO1_USE_CASE, "--parent", "no_such_host", xdir=tmp_path
    )
    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "not in lab" in output or "no_such_host" in output, output
    assert "Traceback" not in output, f"unexpected traceback:\n{output}"


def test_e2e_up_with_no_use_case_names_the_declared_ones(tmp_path):
    """A bare `otto docker compose up` is ambiguous now, and says so (spec §10).

    Both sample repos declare two use-cases each, so omitting the positional
    is a hard error listing them — never a silent pick, and never a no-op.
    No host is leased: the refusal is settled from configuration, above the
    first device touch, so this test contacts no daemon.
    """
    result = _run_otto(
        "docker",
        "compose",
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
        "docker",
        "compose",
        "up",
        _REPO1_USE_CASE,
        "--parent",
        docker_host,
        "--build",
        xdir=tmp_path,
        compose_suffix=suffix,
    )
    ps = _run_otto("docker", "ps", "--parent", docker_host, xdir=tmp_path, compose_suffix=suffix)
    assert ps.returncode == 0, ps.stderr
    # The compose project is `<lab>-<usecase>-<suffix>` (spec §9) — no
    # `otto-` prefix any more: the deployment belongs to the product.
    assert f"unix-{_REPO1_USE_CASE}-{suffix}" in ps.stdout or "repo1-api" in ps.stdout, ps.stdout


def test_e2e_default_parent_serves_up_ps_and_logs_with_no_flag(
    teardown_default_parent_after, default_parent_host, tmp_path
):
    """With no `--parent` anywhere, the verbs agree on the lab's default parent.

    The `unix` fixture lab has three docker-capable hosts and ranks test3 with
    ``docker_priority``, so `up integration` lands there; `ps --parent` of that
    host must show the project under its header (a flagless `ps` fans out over
    every docker-capable host, daemons this test does not lease); and
    `logs <name>` with no flag reads that container's log from the
    same daemon (docker's own "No such container" would fail it on any other).
    `--build` is the image build, unrelated to the host: `up` builds nothing
    without it.
    """
    suffix = teardown_default_parent_after
    project = f"unix-{_MERGED_USE_CASE}-{suffix}"
    up = _run_otto(
        "docker",
        "compose",
        "up",
        _MERGED_USE_CASE,
        "--build",
        xdir=tmp_path,
        compose_suffix=suffix,
    )
    assert up.returncode == 0, up.stdout + up.stderr
    assert f"{default_parent_host}.{_MERGED_USE_CASE}.api" in up.stdout, up.stdout

    ps = _run_otto(
        "docker",
        "ps",
        "--parent",
        default_parent_host,
        xdir=tmp_path,
        compose_suffix=suffix,
        env=_WIDE,
    )
    assert ps.returncode == 0, ps.stdout + ps.stderr
    blocks = {
        block.split(" ==", 1)[0]: block for block in ps.stdout.split("== ")[1:] if " ==" in block
    }
    assert project in blocks[default_parent_host], (
        f"the project is not under {default_parent_host}'s header:\n{ps.stdout}"
    )

    logs = _run_otto(
        "docker", "logs", f"{project}-api-1", xdir=tmp_path, compose_suffix=suffix, env=_WIDE
    )
    assert logs.returncode == 0, logs.stdout + logs.stderr
    assert "No such container" not in logs.stdout + logs.stderr, logs.stdout + logs.stderr


# ---------------------------------------------------------------------------
# Use-cases: the merged displacement pair (spec §4, §9, §10, §12)
#
# repo1 provides the REAL `edge` at priority 10; repo2 provides a mock at 0
# and stands down. Both repos also contribute an unconditional fragment, so a
# full `integration` deployment is `api` + `worker` + `edge` in ONE compose
# project — the merge these tests exist to drive through the actual binary.
# ---------------------------------------------------------------------------

_BOTH_REPOS = f"{REPO1}{os.pathsep}{REPO2}"


def test_e2e_use_cases_reports_the_displacement(tmp_path):
    """`otto docker use-cases` is the inventory view (spec §10).

    Read-only: it resolves selection and the parent and reports them, contacts
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
        "compose",
        "up",
        _MERGED_USE_CASE,
        "--parent",
        docker_host,
        "--build",
        sut_dirs=_BOTH_REPOS,
        xdir=tmp_path,
        compose_suffix=suffix,
    )
    out = up.stdout + up.stderr
    assert up.returncode == 0, out
    assert "pull access denied" not in out, (
        "`--build` must build both repos' images before composing — a pull error means one wasn't"
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
        "compose",
        "down",
        _MERGED_USE_CASE,
        "--parent",
        docker_host,
        sut_dirs=_BOTH_REPOS,
        xdir=tmp_path,
        compose_suffix=suffix,
    )
    assert down.returncode == 0, down.stdout + down.stderr
    assert f"{docker_host}: {_MERGED_USE_CASE} torn down" in down.stdout, down.stdout

    ps = _run_otto("docker", "ps", "--parent", docker_host, xdir=tmp_path, compose_suffix=suffix)
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
        "compose",
        "up",
        _MERGED_USE_CASE,
        "--provide",
        "edge=repo2",
        "--parent",
        docker_host,
        "--build",
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
        "compose",
        "up",
        _MERGED_USE_CASE,
        "--env-file",
        str(env_file),
        "--parent",
        docker_host,
        sut_dirs=_BOTH_REPOS,
        xdir=tmp_path,
        compose_suffix=suffix,
    )
    out = _flat(dry.stdout + dry.stderr)
    assert dry.returncode == 0, f"a dry run is an answer, not a failure:\n{out}"
    assert "Traceback" not in out, out
    assert f"Resolved plan: {docker_host} <- repo1[core,edge], repo2[core]" in out, out
    assert "Displaced: edge goes to repo1 (priority 10); repo2 (priority 0) stands down." in out, (
        out
    )
    assert "Fragment env keys: ['EDGE_ADDR']" in out, out
    # Spec §12: the EXACT command, not a description of one.
    assert f"docker compose -p unix-{_MERGED_USE_CASE}-{suffix}" in out, out
    assert "up -d --remove-orphans" in out, out
    assert "CALLER_KEY=caller-value" in out, (
        f"the caller's --env-file must have merged into the previewed command:\n{out}"
    )

    ps = _run_otto("docker", "ps", "--parent", docker_host, xdir=tmp_path, compose_suffix=suffix)
    assert ps.returncode == 0, ps.stderr
    assert f"unix-{_MERGED_USE_CASE}-{suffix}" not in ps.stdout, (
        f"THE DRY RUN STARTED A STACK:\n{ps.stdout}"
    )

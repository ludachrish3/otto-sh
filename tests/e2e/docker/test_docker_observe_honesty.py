"""What the observe verbs print is what each host's daemon holds.

Each test runs the verb through the CLI, then asks the same daemon through
the passthrough (`otto host <id> exec "docker ..."`), which shares no code
with the docker verbs, and compares. The verbs' output parsers are pinned
hostless in tests/unit/docker/test_honesty_parsers.py.
"""

import json
import uuid

import pytest

from ._cli import _REPO1_USE_CASE, _WIDE, _run_otto
from ._honesty import (
    DAEMON_IMAGE_PAIRS_COMMAND,
    DAEMON_PS_PAIRS_COMMAND,
    MARK,
    parse_compose_ps_names,
    parse_daemon_pairs,
    parse_daemon_rows,
    parse_images_rows,
    parse_ps_ids,
)

pytestmark = [pytest.mark.integration, pytest.mark.xdist_group("docker_e2e")]

_DAEMON_NAMES = f"docker ps -a --format '{MARK} {{{{.Names}}}}'"
_DAEMON_PS = f"docker ps -a --no-trunc --format '{MARK} {{{{.ID}}}} {{{{.Names}}}}'"
_DAEMON_IMAGES = f"docker images --format '{MARK} {{{{.Repository}}}}:{{{{.Tag}}}} {{{{.ID}}}}'"


def _first_line(text: str) -> str:
    """The first non-empty line: the host header must lead, whatever else is printed."""
    return next((line for line in text.splitlines() if line.strip()), "")


def _daemon_container_ids(host: str, xdir) -> "set[str]":
    out = _run_otto("host", host, "exec", _DAEMON_PS, xdir=xdir, env=_WIDE)
    assert out.returncode == 0, out.stderr
    ids: set[str] = set()
    for line in (out.stdout + out.stderr).splitlines():
        _, sep, rest = line.partition(f"{MARK} ")
        if sep and rest.split() and not rest.startswith("{{"):
            ids.add(rest.split()[0])
    return ids


def test_ps_prints_only_container_ids_the_daemon_holds(teardown_after, docker_host, tmp_path):
    suffix = teardown_after
    up = _run_otto(
        "docker",
        "compose",
        "up",
        _REPO1_USE_CASE,
        "--on",
        docker_host,
        "--build",
        xdir=tmp_path,
        compose_suffix=suffix,
    )
    assert up.returncode == 0, up.stdout + up.stderr
    # Asked of the leased host only: a fan-out would also read the other pool
    # host's daemon, which another session may be changing under this one.
    ps = _run_otto("docker", "ps", "-a", "--on", docker_host, xdir=tmp_path, env=_WIDE)
    assert ps.returncode == 0, ps.stdout + ps.stderr
    assert _first_line(ps.stdout) == f"== {docker_host} ==", ps.stdout
    printed = parse_ps_ids(ps.stdout)
    assert printed.get(docker_host), ps.stdout
    daemon = _daemon_container_ids(docker_host, tmp_path)
    for short in printed[docker_host]:
        assert any(full.startswith(short) for full in daemon), (
            f"{docker_host}: {short} is not the daemon's\n{ps.stdout}"
        )


def test_images_prints_only_references_and_ids_the_daemon_holds(docker_host, tmp_path):
    built = _run_otto("docker", "build", "repo1-api", "--on", docker_host, xdir=tmp_path, env=_WIDE)
    assert built.returncode == 0, built.stdout + built.stderr
    images = _run_otto("docker", "images", "--on", docker_host, xdir=tmp_path, env=_WIDE)
    assert images.returncode == 0, images.stdout + images.stderr
    printed = parse_images_rows(images.stdout)
    assert printed.get(docker_host), images.stdout
    listing = _run_otto("host", docker_host, "exec", _DAEMON_IMAGES, xdir=tmp_path, env=_WIDE)
    daemon = parse_daemon_rows(listing.stdout + listing.stderr)
    for reference, short in printed[docker_host].items():
        assert reference in daemon, f"{reference} is not on the daemon\n{images.stdout}"
        assert daemon[reference] == short, f"{reference}: {short} != {daemon[reference]}"


def _daemon_container_names(host: str, xdir) -> "set[str]":
    out = _run_otto("host", host, "exec", _DAEMON_NAMES, xdir=xdir, env=_WIDE)
    assert out.returncode == 0, out.stderr
    names: set[str] = set()
    for line in (out.stdout + out.stderr).splitlines():
        _, sep, rest = line.partition(f"{MARK} ")
        if sep and rest.split() and not rest.startswith("{{"):
            names.add(rest.split()[0])
    return names


def _up_repo1(docker_host: str, suffix: str, xdir) -> None:
    up = _run_otto(
        "docker",
        "compose",
        "up",
        _REPO1_USE_CASE,
        "--on",
        docker_host,
        "--build",
        xdir=xdir,
        compose_suffix=suffix,
    )
    assert up.returncode == 0, up.stdout + up.stderr


def test_compose_ps_names_only_containers_of_the_project_the_daemon_holds(
    teardown_after, docker_host, tmp_path
):
    suffix = teardown_after
    # `use_case_project(lab, use_case, suffix)`: the three slugged segments joined by `-`.
    project = f"unix-{_REPO1_USE_CASE}-{suffix}"
    _up_repo1(docker_host, suffix, tmp_path)
    ps = _run_otto(
        "docker",
        "compose",
        "ps",
        _REPO1_USE_CASE,
        "--on",
        docker_host,
        xdir=tmp_path,
        compose_suffix=suffix,
        env=_WIDE,
    )
    assert ps.returncode == 0, ps.stdout + ps.stderr
    assert _first_line(ps.stdout) == f"== {docker_host} ==", ps.stdout
    printed = parse_compose_ps_names(ps.stdout)
    assert printed.get(docker_host), ps.stdout
    daemon = _daemon_container_names(docker_host, tmp_path)
    for name in printed[docker_host]:
        assert name in daemon, f"{docker_host}: {name} is not the daemon's\n{ps.stdout}"
        assert name.startswith(f"{project}-"), f"{name} is not of project {project}\n{ps.stdout}"


def test_compose_logs_prints_the_services_log_lines_the_daemon_holds(
    teardown_after, docker_host, tmp_path
):
    suffix = teardown_after
    project = f"unix-{_REPO1_USE_CASE}-{suffix}"
    _up_repo1(docker_host, suffix, tmp_path)
    # repo1's `api` writes no log of its own, so plant a line: PID 1's stdout is what
    # docker captures, and the passthrough shares no code with the docker verbs.
    marker = f"otto-logs-{uuid.uuid4().hex[:8]}"
    planted = _run_otto(
        "host",
        docker_host,
        "exec",
        f"docker exec {project}-api-1 sh -c 'echo {marker} > /proc/1/fd/1'",
        xdir=tmp_path,
        env=_WIDE,
    )
    assert planted.returncode == 0, planted.stdout + planted.stderr
    logs = _run_otto(
        "docker",
        "compose",
        "logs",
        _REPO1_USE_CASE,
        "--on",
        docker_host,
        "--tail",
        "5",
        xdir=tmp_path,
        compose_suffix=suffix,
        env=_WIDE,
    )
    assert logs.returncode == 0, logs.stdout + logs.stderr
    header = f"== {docker_host} =="
    assert _first_line(logs.stdout) == header, logs.stdout
    # The planted line is what makes the comparison below non-vacuous.
    assert marker in logs.stdout, logs.stdout
    body = logs.stdout.split(header, 1)[1]
    printed = [line for line in body.splitlines() if line.strip()]
    # The daemon's whole log (no --tail) is a superset of any earlier tail window, so a
    # line written between the two calls cannot make the comparison flaky. Its output
    # arrives through the console relay (a prefix per line), so both sides are compared
    # with whitespace collapsed, a printed line as a substring of the relay.
    daemon = _run_otto(
        "host",
        docker_host,
        "exec",
        f"docker compose -p {project} logs",
        xdir=tmp_path,
        env=_WIDE,
    )
    assert daemon.returncode == 0, daemon.stdout + daemon.stderr
    held = " ".join((daemon.stdout + daemon.stderr).split())
    for line in printed:
        assert " ".join(line.split()) in held, f"not in the daemon's logs: {line!r}\n{held}"


def _plant_marker(host: str, project: str, xdir) -> str:
    """Write one unique line to ``api``'s stdout, the stream docker captures."""
    marker = f"otto-logs-{uuid.uuid4().hex[:8]}"
    planted = _run_otto(
        "host",
        host,
        "exec",
        f"docker exec {project}-api-1 sh -c 'echo {marker} > /proc/1/fd/1'",
        xdir=xdir,
        env=_WIDE,
    )
    assert planted.returncode == 0, planted.stdout + planted.stderr
    return marker


def _project_container_id(host: str, project: str, xdir) -> str:
    """The daemon's own id for the project's one container, running or not."""
    out = _run_otto(
        "host",
        host,
        "exec",
        f"docker ps -aq --filter label=com.docker.compose.project={project}",
        xdir=xdir,
        env=_WIDE,
    )
    assert out.returncode == 0, out.stdout + out.stderr
    ids = [
        word
        for line in (out.stdout + out.stderr).splitlines()
        for word in line.split()
        if len(word) == 12 and all(c in "0123456789abcdef" for c in word)
    ]
    assert len(ids) == 1, f"expected one container of {project}\n{out.stdout}{out.stderr}"
    return ids[0]


def _assert_lines_are_the_daemons(printed: str, host: str, cid: str, xdir) -> None:
    """Every printed line is in the daemon's whole (untailed) log of *cid*.

    Whole, so a line written between the two calls cannot make this flaky. The
    daemon's text arrives through the console relay, so both sides are compared
    with whitespace collapsed, a printed line as a substring of the relay.
    """
    daemon = _run_otto("host", host, "exec", f"docker logs {cid}", xdir=xdir, env=_WIDE)
    assert daemon.returncode == 0, daemon.stdout + daemon.stderr
    held = " ".join((daemon.stdout + daemon.stderr).split())
    lines = [line for line in printed.splitlines() if line.strip()]
    assert lines, "nothing was printed"
    for line in lines:
        assert " ".join(line.split()) in held, f"not in the daemon's logs: {line!r}\n{held}"


def test_logs_of_a_container_host_id_prints_what_docker_logs_prints(
    teardown_role_host_after, role_docker_host, tmp_path
):
    """A container host id is read by a SECOND otto process, so it must be one placement minted."""
    suffix = teardown_role_host_after
    project = f"unix-{_REPO1_USE_CASE}-{suffix}"
    _up_repo1(role_docker_host, suffix, tmp_path)
    marker = _plant_marker(role_docker_host, project, tmp_path)
    logs = _run_otto(
        "docker",
        "logs",
        f"{role_docker_host}.repo1.api",
        "--tail",
        "5",
        xdir=tmp_path,
        compose_suffix=suffix,
        env=_WIDE,
    )
    assert logs.returncode == 0, logs.stdout + logs.stderr
    assert not logs.stdout.startswith("=="), logs.stdout  # one container, no host header
    # The planted line is what makes the comparison below non-vacuous.
    assert marker in logs.stdout, logs.stdout
    cid = _project_container_id(role_docker_host, project, tmp_path)
    _assert_lines_are_the_daemons(logs.stdout, role_docker_host, cid, tmp_path)


def test_logs_of_a_stopped_container_are_still_dockers_to_print(
    teardown_role_host_after, role_docker_host, tmp_path
):
    """The lookup uses `docker ps -aq`: a stopped container is still found."""
    suffix = teardown_role_host_after
    project = f"unix-{_REPO1_USE_CASE}-{suffix}"
    _up_repo1(role_docker_host, suffix, tmp_path)
    marker = _plant_marker(role_docker_host, project, tmp_path)
    cid = _project_container_id(role_docker_host, project, tmp_path)
    stopped = _run_otto(
        "host", role_docker_host, "exec", f"docker stop {project}-api-1", xdir=tmp_path, env=_WIDE
    )
    assert stopped.returncode == 0, stopped.stdout + stopped.stderr
    logs = _run_otto(
        "docker",
        "logs",
        f"{role_docker_host}.repo1.api",
        "--tail",
        "5",
        xdir=tmp_path,
        compose_suffix=suffix,
        env=_WIDE,
    )
    assert logs.returncode == 0, logs.stdout + logs.stderr
    assert marker in logs.stdout, logs.stdout
    _assert_lines_are_the_daemons(logs.stdout, role_docker_host, cid, tmp_path)


def test_logs_with_on_of_an_unknown_name_is_dockers_error(docker_host, tmp_path):
    logs = _run_otto("docker", "logs", "no-such-ctr", "--on", docker_host, xdir=tmp_path, env=_WIDE)
    assert logs.returncode != 0, logs.stdout + logs.stderr
    assert "No such container" in logs.stdout + logs.stderr, logs.stdout + logs.stderr


def _daemon_pairs(host: str, command: str, xdir) -> "list[list[str]]":
    """``[name_or_ref, id]`` per marked row, in the daemon's order."""
    out = _run_otto("host", host, "exec", command, xdir=xdir, env=_WIDE)
    assert out.returncode == 0, out.stderr
    return parse_daemon_pairs(out.stdout + out.stderr)


def _observed_entry(xdir, host: str, kind: str) -> dict:
    """The sub-entry the subprocess wrote; its OTTO_HOME is ``xdir / "otto-home"``."""
    (cache,) = list((xdir / "otto-home").rglob("completion_cache.json"))
    return json.loads(cache.read_text())["__docker_observed__"]["hosts"][host][kind]


def test_ps_records_exactly_the_containers_the_daemon_lists(teardown_after, docker_host, tmp_path):
    """After `otto docker ps`, the host's containers entry is the daemon's own list."""
    _up_repo1(docker_host, teardown_after, tmp_path)
    ps = _run_otto("docker", "ps", "-a", "--on", docker_host, xdir=tmp_path, env=_WIDE)
    assert ps.returncode == 0, ps.stdout + ps.stderr
    pairs = _daemon_pairs(docker_host, DAEMON_PS_PAIRS_COMMAND, tmp_path)
    assert pairs, "the stack just came up; the daemon lists at least one container"
    entry = _observed_entry(tmp_path, docker_host, "containers")
    # In order: docker sorts containers by nanosecond `Created`, so two calls agree
    # (images sort by whole seconds and do not; see the images test).
    assert entry["names"] == [p[0] for p in pairs][:200]
    assert entry["ids"] == [p[1] for p in pairs][:200]


def test_images_records_the_daemons_references_minus_dangling(
    teardown_after, docker_host, tmp_path
):
    """After `otto docker images`, the images entry is the daemon's list minus dangling."""
    # `compose up --build` leaves repo1's images on the daemon, so the list is never empty.
    _up_repo1(docker_host, teardown_after, tmp_path)
    images = _run_otto("docker", "images", "--on", docker_host, xdir=tmp_path, env=_WIDE)
    assert images.returncode == 0, images.stdout + images.stderr
    pairs = [
        p
        for p in _daemon_pairs(docker_host, DAEMON_IMAGE_PAIRS_COMMAND, tmp_path)
        if p[0] != "<none>:<none>"
    ]
    assert pairs, "the stack just built its images; the daemon lists at least one"
    entry = _observed_entry(tmp_path, docker_host, "images")
    recorded = list(zip(entry["refs"], entry["ids"], strict=True))
    held = [(p[0], p[1]) for p in pairs]
    # Unlike containers (nanosecond `Created`), docker sorts images by whole-second `Created`
    # with an unstable sort, so two calls may order same-second images differently: compare
    # as sorted lists, not in order. At the cap the 200 recorded are only a subset of the list.
    if len(pairs) < 200:
        assert sorted(recorded) == sorted(held)
    else:
        assert len(recorded) == 200
        assert set(recorded) <= set(held)

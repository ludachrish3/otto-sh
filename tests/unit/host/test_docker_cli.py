"""The docker-CLI answers otto reads once (:mod:`otto.host.docker_cli`), each driven through
every caller that used to decide it on its own."""

import json
import shlex

import pytest

from otto.coverage.objects import ObjectListCache, Unavailable
from otto.host.docker_cli import (
    _INSPECT_TEMPLATE,
    ContainerState,
    image_id_of,
    inspect_container,
    missing,
    rm_container_command,
    said,
)
from otto.result import CommandResult, NotRunResult, Result
from otto.utils import Status
from tests._fixtures.private_root import trusted_private_roots  # noqa: F401 — fixture
from tests.unit.cov.test_object_list import _INSPECT, _TWO, _failed, _ok, _RefImg
from tests.unit.cov.test_object_list import _DockerHost as _CovHost
from tests.unit.host.test_docker_image_kind import _GONE, _build
from tests.unit.host.test_docker_image_kind import _DockerHost as _KindHost

_NO_SUCH_CONTAINER = [
    "Error response from daemon: No such container: app",
    "Error: No such container: app",
    "error: no such container: app",
]


# ── the no-such-container predicate ────────────────────────────────────────


@pytest.mark.parametrize("text", _NO_SUCH_CONTAINER)
@pytest.mark.parametrize("where", ["value", "msg"])
def test_missing_container_matches_dockers_answers_case_insensitively(text, where):
    kw = {"value": text} if where == "value" else {"value": "", "msg": text}
    assert missing(Result(Status.Error, **kw), "container") is True


@pytest.mark.parametrize(
    "text",
    [
        "No such object: app",
        "permission denied while trying to connect to the Docker daemon socket",
        "",
    ],
)
def test_missing_container_is_false_for_any_other_answer(text):
    assert missing(Result(Status.Error, value=text), "container") is False
    assert missing(Result(Status.Error, value="", msg=text), "container") is False


def test_missing_is_false_for_a_declined_command():
    declined = NotRunResult(
        status=Status.NotRun, command="docker rm -v app", retcode=-1, host_name="h"
    )
    # NotRunResult.value raises on read: answering False proves it was never read.
    assert missing(declined, "container") is False


def test_said_prefers_the_message_then_the_value_then_the_status():
    assert said(Result(Status.Error, value="out\n", msg="the message")) == "the message"
    assert said(Result(Status.Error, value="  the output\n")) == "the output"
    assert said(Result(Status.Error, value="")) == "status Error"


# ── one answer, every former call site ───────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", [*_GONE, _GONE[0].lower()])
async def test_one_no_such_container_answer_drives_every_former_call_site(tmp_path, answer):
    # (a) uninstall over `docker rm -f`
    host = _KindHost(answers={"docker rm": (Status.Error, answer)})
    assert (await _build(host, image="app:1").uninstall(host)).status is Status.Success

    # (b) get_debug_logs over `docker logs`
    host = _KindHost(answers={"docker logs": (Status.Error, answer)})
    logs = await _build(host, image="app:1").get_debug_logs(host, tmp_path)
    assert logs.status is Status.Success
    assert not (tmp_path / "container.log").exists()

    # (c) the coverage reader over its container inspect: no container, so the image alone
    cov = _CovHost(_TWO, answers={_INSPECT: _failed(answer)})
    listed = await ObjectListCache(tmp_path).read_container(_RefImg(), cov)
    assert not isinstance(listed, Unavailable), listed
    assert cov.issued("docker image inspect --format '{{.Id}}' ") != []
    assert cov.issued("docker commit") == []


def test_a_result_with_a_command_result_value_is_read_as_text():
    result = CommandResult(Status.Error, value="Error: No such container: x", command="c")
    assert missing(result, "container") is True


# ── the one remove-by-name builder ───────────────────────────────────────────


def test_rm_container_command_renders_both_forms_quoted():
    assert rm_container_command("app", force=False) == "docker rm -v app"
    assert rm_container_command("app", force=True) == "docker rm -f -v app"
    assert rm_container_command("a b's", force=False) == "docker rm -v 'a b'\"'\"'s'"
    assert _build(image="app:1", container_name="a b's")._rm_command(force=True) == (
        rm_container_command("a b's", force=True)
    )


# ── one image-id read ────────────────────────────────────────────────────────

_IMAGE_ID = "sha256:cbd8571d4b6e" + "0" * 52
_IMAGE_INSPECT_ID = "docker image inspect --format '{{.Id}}'"


@pytest.mark.asyncio
async def test_image_id_of_reads_the_id_and_carries_the_failing_result():
    host = _KindHost(answers={"docker image inspect": (Status.Success, "sha256:abc\n")})
    got = await image_id_of(host, "app:1")
    assert got.id == "sha256:abc"
    assert host.exec_calls == ["docker image inspect --format '{{.Id}}' app:1"]

    host = _KindHost(answers={"docker image inspect": (Status.Error, "No such image: app:1")})
    got = await image_id_of(host, "app:1")
    assert got.id is None
    assert got.result.status is Status.Error
    assert got.result.value == "No such image: app:1"

    host = _KindHost(answers={"docker image inspect": (Status.NotRun, "")})
    got = await image_id_of(host, "app:1")  # NotRunResult.value raises: never read
    assert got.id is None
    assert got.result.status is Status.NotRun


@pytest.mark.asyncio
@pytest.mark.usefixtures("trusted_private_roots")  # the build stages onto a trusted root
async def test_one_image_id_answer_drives_build_and_install(tmp_path):
    from otto.docker.build import BuildOptions, _build_one
    from tests.unit.docker.test_build import _daemon, _img, _mock_parent

    # build: the id it reports is the inspect's, read through image_id_of
    parent = _mock_parent()
    _daemon(parent, image_id=_IMAGE_ID, listing="api:latest cbd8571d4b6e\n")
    built = await _build_one(parent, "project1", _img(tmp_path), BuildOptions())
    assert built.image_id == "cbd8571d4b6e"
    issued = [c.args[0] for c in parent.exec.call_args_list]
    assert "docker image inspect --format '{{.Id}}' api:latest" in issued

    # install: the same answer is "present"; a failed one is today's refusal
    present = _KindHost(answers={"docker image inspect": (Status.Success, _IMAGE_ID)})
    assert (await _build(present, image="app:1").install(present)).is_ok
    assert "docker image inspect --format '{{.Id}}' app:1" in present.exec_calls
    absent = _KindHost(answers={"docker image inspect": (Status.Error, "No such image: app:1")})
    refused = await _build(absent, image="app:1").install(absent)
    assert refused.msg == (
        "app: image app:1 is not present on test3; load it there or set `pull = true` on the entry"
    )

    # the reader's no-container branch: the same answer is the id it scans
    no_container = _failed("Error response from daemon: No such container: img-c", msg="")
    cov = _CovHost(_TWO, answers={_INSPECT: no_container, _IMAGE_INSPECT_ID: _ok(_IMAGE_ID)})
    listed = await ObjectListCache(tmp_path).read_container(_RefImg(), cov)
    assert not isinstance(listed, Unavailable), listed
    assert cov.issued("docker create")[0].split(" -c ")[0].endswith(f" sh {_IMAGE_ID}")
    missing_image = _failed("Error response from daemon: No such image: redis:7", msg="")
    cov = _CovHost(_TWO, answers={_INSPECT: no_container, _IMAGE_INSPECT_ID: missing_image})
    listed = await ObjectListCache(tmp_path).read_container(_RefImg(), cov)
    assert isinstance(listed, Unavailable)
    assert listed.kind == "image-missing"


# ── one container inspect ────────────────────────────────────────────────────


def _inspect_json(**fields) -> str:
    """One answer to the inspect template, as the daemon prints it."""
    answer = {
        "status": "running",
        "running": True,
        "image": "sha256:feed",
        "ref": "img:1",
        "user": "",
        "groups": None,
    }
    answer.update(fields)
    return json.dumps(answer)


_INSPECT_COMMAND = f"docker container inspect --format {shlex.quote(_INSPECT_TEMPLATE)} app"


@pytest.mark.asyncio
async def test_inspect_container_parses_the_one_template():
    answer = _inspect_json(user="1000:1000", groups=["audio", "44"])
    host = _KindHost(answers={"docker container inspect": (Status.Success, answer)})
    got = await inspect_container(host, "app")
    assert host.exec_calls == [_INSPECT_COMMAND]
    assert got.state == ContainerState(
        status="running",
        running=True,
        image_id="sha256:feed",
        image_ref="img:1",
        user="1000:1000",
        group_add=["audio", "44"],
    )
    assert not got.declined
    assert not got.absent

    exited = _inspect_json(status="exited", running=False)
    host = _KindHost(answers={"docker container inspect": (Status.Success, exited)})
    state = (await inspect_container(host, "app")).state
    assert state is not None
    assert (state.status, state.running) == ("exited", False)


def test_the_template_names_exactly_the_six_fields():
    # Never `{{json .}}`: that would log the container's whole config, env included.
    assert sorted(json.loads(_INSPECT_TEMPLATE.replace("{{json ", '"').replace("}}", '"'))) == [
        "groups",
        "image",
        "ref",
        "running",
        "status",
        "user",
    ]


@pytest.mark.asyncio
async def test_null_group_add_is_no_groups():
    host = _KindHost(answers={"docker container inspect": (Status.Success, _inspect_json())})
    state = (await inspect_container(host, "app")).state
    assert state is not None
    assert state.group_add == []
    assert state.user == ""


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "answer",
    [
        "not json",
        "",
        json.dumps({"running": True, "image": "x", "ref": "y", "user": "", "groups": None}),
        _inspect_json(running="yes"),
        _inspect_json(groups="audio"),
        "[1, 2]",
    ],
    ids=["text", "empty", "no-status", "running-not-bool", "groups-not-list", "not-an-object"],
)
async def test_unparseable_inspect_output_is_no_state(answer):
    host = _KindHost(answers={"docker container inspect": (Status.Success, answer)})
    got = await inspect_container(host, "app")
    assert got.state is None
    assert got.result.status is Status.Success
    assert not got.absent


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "answer", "absent", "declined"),
    [
        (Status.Error, "Error response from daemon: No such container: app", True, False),
        (Status.Error, "Error: No such object: app", False, False),
        (Status.Error, "permission denied while trying to connect", False, False),
        (Status.NotRun, "", False, True),
    ],
    ids=["no-such-container", "no-such-object", "permission", "declined"],
)
async def test_absent_is_dockers_no_such_container_only(status, answer, absent, declined):
    host = _KindHost(answers={"docker container inspect": (status, answer)})
    got = await inspect_container(host, "app")
    assert got.state is None
    assert (got.absent, got.declined) == (absent, declined)


@pytest.mark.asyncio
@pytest.mark.parametrize("running", [True, False])
async def test_one_inspect_answer_drives_is_installed_and_uninstall(tmp_path, running):
    answer = _inspect_json(running=running, status="running" if running else "exited")
    host = _KindHost(answers={"docker container inspect": (Status.Success, answer)})
    assert await _build(host, image="app:1").is_installed(host) is running
    assert host.exec_calls == [_INSPECT_COMMAND]

    host = _KindHost(answers={"docker container inspect": (Status.Success, answer)})
    assert (await _build(host, image="docker/app.tar").uninstall(host)).is_ok
    assert host.exec_calls == [_INSPECT_COMMAND, "docker rm -f -v app", "docker image rm -- img:1"]

    # the reader: running is scanned in place, exited through a snapshot, keyed by .Image
    cov = _CovHost(_TWO, answers={_INSPECT: _ok(answer)})
    listed = await ObjectListCache(tmp_path).read_container(_RefImg(container_name="app"), cov)
    assert not isinstance(listed, Unavailable), listed
    assert bool(cov.issued("docker exec -e OTTO_SCAN_ID=")) is running
    assert bool(cov.issued("docker commit")) is not running


# ── the parent clock, the scan labels and the stale-scan sweep ───────────────

_NOW = 1700000000
_CLOCK = "date +%s"
_PS = "docker ps -a --filter label=otto.scan=1 --format '{{.ID}} {{.Label \"otto.scan.expires\"}}'"
_IMAGES = "docker image ls -q --no-trunc --filter label=otto.scan=1"
_IMAGE_INSPECT = (
    "docker image inspect --format '{{.Id}} {{index .Config.Labels \"otto.scan.expires\"}}'"
)
_RM_OLD = "docker rm -f -v c-old"
_RMI_OLD = "docker image rm -- sha256:i-old"


def _declined(cmd: str) -> NotRunResult:
    return NotRunResult(status=Status.NotRun, command=cmd, retcode=-1, host_name="lab1")


def _daemon(**over):
    """A parent with two scan containers and two scan images, one of each expired at _NOW."""

    answers = {
        _CLOCK: _ok(f"{_NOW}"),
        _PS: _ok(f"c-old {_NOW - 1}\nc-new {_NOW + 500}"),
        _IMAGES: _ok("sha256:i-old\nsha256:i-new"),
        _IMAGE_INSPECT: _ok(f"sha256:i-old {_NOW - 1}\nsha256:i-new {_NOW + 500}"),
        _RM_OLD: _ok("c-old"),
        _RMI_OLD: _ok("Deleted: sha256:i-old"),
    }
    answers.update(over)
    return _CovHost({}, answers=answers)


def _sweep_calls(host) -> list[str]:
    return [c for c in host.calls if c.startswith(("date", "docker"))]


def test_scan_labels_name_the_scan_and_its_expiry():
    from otto.host.docker_cli import scan_labels

    assert scan_labels(_NOW) == {"otto.scan": "1", "otto.scan.expires": str(_NOW)}


@pytest.mark.asyncio
async def test_the_sweep_removes_only_expired_leftovers_containers_before_images():
    from otto.host.docker_cli import sweep_stale_scans

    host = _daemon()
    report = await sweep_stale_scans(host)
    assert report.removed == ["container c-old", "image sha256:i-old"]
    assert report.errors == []
    assert _sweep_calls(host) == [
        _CLOCK,
        _PS,
        _IMAGES,
        f"{_IMAGE_INSPECT} sha256:i-old sha256:i-new",
        _RM_OLD,
        _RMI_OLD,
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("skew", [-(10**6), 10**6])
async def test_the_sweep_compares_against_the_parents_clock_not_the_runners(monkeypatch, skew):
    import time

    from otto.host.docker_cli import sweep_stale_scans

    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() + skew)
    report = await sweep_stale_scans(_daemon())
    assert report.removed == ["container c-old", "image sha256:i-old"]


@pytest.mark.asyncio
async def test_a_given_clock_is_not_read_again():
    from otto.host.docker_cli import sweep_stale_scans

    host = _daemon(**{"docker rm -f -v c-new": _ok(), "docker image rm -- sha256:i-new": _ok()})
    report = await sweep_stale_scans(host, now=_NOW + 1000)
    assert _CLOCK not in host.calls
    assert sorted(report.removed) == sorted(
        ["container c-old", "container c-new", "image sha256:i-old", "image sha256:i-new"]
    )


@pytest.mark.asyncio
async def test_an_unparseable_expiry_is_never_removed(caplog):
    from otto.host.docker_cli import sweep_stale_scans

    host = _daemon(
        **{
            _PS: _ok("c-old soon\nc-none "),
            _IMAGE_INSPECT: _ok("sha256:i-old <no value>\nsha256:i-new"),
            _IMAGES: _ok("sha256:i-old\nsha256:i-new"),
        }
    )
    with caplog.at_level("WARNING", logger="otto.host.docker_cli"):
        report = await sweep_stale_scans(host)
    assert report.removed == []
    assert report.errors == []
    assert not [c for c in host.calls if c.startswith(("docker rm ", "docker image rm"))]
    assert len([r for r in caplog.records if "expires" in r.getMessage()]) == 4


@pytest.mark.asyncio
async def test_a_listing_failure_is_an_error_not_a_raise():
    from otto.host.docker_cli import sweep_stale_scans

    host = _daemon(**{_PS: _failed("Cannot connect to the Docker daemon", msg="")})
    report = await sweep_stale_scans(host)
    assert report.removed == ["image sha256:i-old"]
    assert report.errors == [f"{_PS}: Cannot connect to the Docker daemon"]


_RAISING = [_CLOCK, _PS, _IMAGES, _IMAGE_INSPECT, _RM_OLD, _RMI_OLD]


@pytest.mark.asyncio
@pytest.mark.parametrize("raising", _RAISING, ids=["clock", "ps", "images", "inspect", "rm", "rmi"])
@pytest.mark.parametrize(
    "exc", [ConnectionResetError("lost"), TimeoutError()], ids=["reset", "timeout"]
)
async def test_a_raised_command_is_an_error_and_the_sweep_continues(raising, exc):
    from otto.host.docker_cli import sweep_stale_scans

    host = _daemon(**{raising: exc})
    report = await sweep_stale_scans(host)  # never raises
    assert len(report.errors) == 1, report.errors
    (error,) = report.errors
    assert error.startswith(raising), error
    assert type(exc).__name__ in error
    covered = {
        _CLOCK: {"container c-old", "image sha256:i-old"},
        _PS: {"container c-old"},
        _IMAGES: {"image sha256:i-old"},
        _IMAGE_INSPECT: {"image sha256:i-old"},
        _RM_OLD: {"container c-old"},
        _RMI_OLD: {"image sha256:i-old"},
    }[raising]
    assert set(report.removed) == {"container c-old", "image sha256:i-old"} - covered


@pytest.mark.asyncio
async def test_a_cancellation_is_never_swallowed():
    import asyncio

    from otto.host.docker_cli import sweep_stale_scans

    with pytest.raises(asyncio.CancelledError):
        await sweep_stale_scans(_daemon(**{_RM_OLD: asyncio.CancelledError()}))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("command", "answer", "kept"),
    [
        (_RM_OLD, "Error response from daemon: No such container: c-old", "image sha256:i-old"),
        (_RMI_OLD, "Error response from daemon: No such image: sha256:i-old", "container c-old"),
    ],
    ids=["container", "image"],
)
async def test_a_concurrent_disappearance_is_no_error(command, answer, kept):
    from otto.host.docker_cli import sweep_stale_scans

    report = await sweep_stale_scans(_daemon(**{command: _failed(answer, msg="")}))
    assert report.errors == []
    assert report.removed == [kept]


@pytest.mark.asyncio
@pytest.mark.parametrize("command", [_RM_OLD, _RMI_OLD], ids=["container", "image"])
@pytest.mark.parametrize(
    "answer",
    [
        "permission denied",
        (
            'conflict: unable to remove repository reference "otto-scan:f00d" (must force) - '
            "container 1234 is using its referenced image 5678"
        ),
    ],
    ids=["permission", "conflict"],
)
async def test_any_other_removal_failure_is_an_error(command, answer):
    from otto.host.docker_cli import sweep_stale_scans

    host = _daemon(
        **{
            _PS: _ok(f"c-old {_NOW - 1}\nc-prev {_NOW - 2}"),
            "docker rm -f -v c-prev": _ok("c-prev"),
            command: _failed(answer, msg=""),
        }
    )
    report = await sweep_stale_scans(host)
    assert report.errors == [f"{command}: {answer}"]
    assert "container c-prev" in report.removed  # the next id is still swept


@pytest.mark.asyncio
async def test_a_declined_sweep_stops_quietly_and_differs_from_a_failure():
    from otto.host.docker_cli import sweep_stale_scans

    declined = _daemon(**{_PS: _declined(_PS)})
    report = await sweep_stale_scans(declined)
    assert (report.removed, report.errors) == ([], [])
    assert _sweep_calls(declined)[-1] == _PS  # nothing after the declined listing

    failed = _daemon(**{_PS: _failed("boom", msg="")})
    report = await sweep_stale_scans(failed)
    assert report.errors == [f"{_PS}: boom"]
    assert _IMAGES in failed.calls


@pytest.mark.asyncio
async def test_the_sweep_removes_through_the_shared_builders():
    from otto.docker import image_verbs
    from otto.host.docker_cli import image_rm_command, sweep_stale_scans

    host = _daemon()
    await sweep_stale_scans(host)
    assert rm_container_command("c-old", force=True) in host.calls
    assert image_rm_command(["sha256:i-old"]) in host.calls
    # One image-removal builder: the kind's uninstall and the docker verbs share it.
    assert _build(image="docker/app.tar")._image_rm_command("x") == image_rm_command(["x"])
    assert image_verbs.image_rm_command is image_rm_command

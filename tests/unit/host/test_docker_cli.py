"""The docker-CLI answers otto reads once (:mod:`otto.host.docker_cli`), each driven through
every caller that used to decide it on its own."""

import pytest

from otto.coverage.objects import ObjectListCache, Unavailable
from otto.host.docker_cli import missing, rm_container_command, said
from otto.result import CommandResult, NotRunResult, Result
from otto.utils import Status
from tests.unit.cov.test_object_list import _INSPECT, _TWO, _RefImg
from tests.unit.cov.test_object_list import _DockerHost as _CovHost
from tests.unit.cov.test_object_list import _failed as _cov_failed
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

    # (c) the coverage reader over its container inspect
    cov = _CovHost(_TWO, answers={_INSPECT: _cov_failed(answer)})
    listed = await ObjectListCache(tmp_path).read_running(_RefImg(), cov)
    assert isinstance(listed, Unavailable)
    assert listed.kind != "unreadable"


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

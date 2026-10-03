"""Unit tests for `otto.docker.build`.

These mock the parent's `exec` so we never invoke real `docker build`.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from otto.config.repo import DockerImage
from otto.docker.build import BuildOptions, _build_one, build_command, image_references
from otto.result import CommandResult, Result
from otto.utils import Status


def _ok(out: str = "", command: str = "") -> CommandResult:
    return CommandResult(Status.Success, value=out, command=command, retcode=0)


def _fail(out: str = "boom", command: str = "") -> CommandResult:
    # `command` is carried so the fake matches what a real exec produces —
    # otherwise a test cannot tell "returned the result whole" from "rebuilt
    # a summary", which is exactly the property _build_one now promises.
    return CommandResult(Status.Failed, value=out, command=command, retcode=1)


def _mock_parent():
    parent = MagicMock()
    parent.exec = AsyncMock(return_value=_ok())
    parent.put = AsyncMock(return_value=Result(Status.Success, value={}))
    return parent


def _daemon(parent, *, image_id="sha256:cbd8571d4b6e" + "0" * 52, listing=""):
    """Answer the read-back the way a daemon does; everything else succeeds."""

    async def exec_side_effect(cmd, *_, **__):
        if cmd.startswith("docker image inspect"):
            return _ok(image_id + "\n", command=cmd)
        if cmd.startswith("docker images"):
            return _ok(listing, command=cmd)
        return _ok(command=cmd)

    parent.exec.side_effect = exec_side_effect


def _img(tmp: Path) -> DockerImage:
    df = tmp / "Dockerfile"
    df.write_text("FROM alpine\n")
    return DockerImage(name="api", dockerfile=df, context=tmp)


# ---------------------------------------------------------------------------
# References and the rendered command
# ---------------------------------------------------------------------------


def test_default_reference_is_name_at_latest(tmp_path):
    assert image_references(_img(tmp_path), BuildOptions()) == ["api:latest"]


def test_typed_tags_replace_the_default_and_nothing_is_added(tmp_path):
    options = BuildOptions(tags=["api:1.4", "registry.example:5000/team/api:1.4"])
    assert image_references(_img(tmp_path), options) == [
        "api:1.4",
        "registry.example:5000/team/api:1.4",
    ]


def test_the_command_carries_dockers_flags_in_dockers_spelling(tmp_path):
    img = _img(tmp_path)
    options = BuildOptions(tags=["api:1.4"], no_cache=True, pull=True, target="prod")

    cmd = build_command("repo1", img, options)

    assert cmd.startswith("docker build ")
    assert " -t api:1.4 " in cmd
    assert " --no-cache" in cmd
    assert " --pull" in cmd
    assert " --target prod" in cmd
    assert cmd.endswith(" /tmp/otto-docker/repo1/build/api")


def test_a_flag_build_arg_wins_over_the_declared_one_and_is_passed_verbatim(tmp_path):
    df = tmp_path / "Dockerfile"
    df.write_text("FROM alpine\n")
    img = DockerImage(
        name="api", dockerfile=df, context=tmp_path, build_args=(("PORT", "80"), ("MODE", "a"))
    )
    options = BuildOptions(build_args={"PORT": "8080", "URL": "http://x/?a=b c;rm -rf /"})

    cmd = build_command("repo1", img, options)

    assert "--build-arg PORT=8080" in cmd
    assert "PORT=80 " not in cmd
    assert not cmd.endswith("PORT=80")
    assert "--build-arg MODE=a" in cmd
    assert "--build-arg 'URL=http://x/?a=b c;rm -rf /'" in cmd


def test_the_flag_target_replaces_the_declared_one(tmp_path):
    df = tmp_path / "Dockerfile"
    df.write_text("FROM alpine\n")
    img = DockerImage(name="api", dockerfile=df, context=tmp_path, target="dev")
    assert " --target prod" in build_command("repo1", img, BuildOptions(target="prod"))
    assert " --target dev" in build_command("repo1", img, BuildOptions())


def test_a_registry_qualified_name_stages_into_one_flat_directory(tmp_path):
    df = tmp_path / "Dockerfile"
    df.write_text("FROM alpine\n")
    img = DockerImage(name="registry.example:5000/team/api", dockerfile=df, context=tmp_path)

    cmd = build_command("repo1", img, BuildOptions())

    assert " -t registry.example:5000/team/api:latest " in cmd
    context_dir = cmd.rsplit(" ", 1)[1]
    assert context_dir == "/tmp/otto-docker/repo1/build/registry.example_5000_team_api"


@pytest.mark.asyncio
async def test_the_build_runs_exactly_the_rendered_command(tmp_path):
    parent = _mock_parent()
    _daemon(parent, listing="api:latest cbd8571d4b6e\n")
    img = _img(tmp_path)
    options = BuildOptions(no_cache=True)

    await _build_one(parent, "repo1", img, options)

    cmds = [c.args[0] for c in parent.exec.call_args_list]
    assert build_command("repo1", img, options) in cmds


# ---------------------------------------------------------------------------
# _build_one — always builds
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_build_always_runs_and_asks_the_daemon_nothing_first(tmp_path):
    parent = _mock_parent()
    _daemon(parent, listing="api:latest cbd8571d4b6e\n")
    img = _img(tmp_path)

    res = await _build_one(parent, "repo1", img, BuildOptions())

    assert res.is_ok
    cmds = [c.args[0] for c in parent.exec.call_args_list]
    builds = [c for c in cmds if c.startswith("docker build ")]
    assert len(builds) == 1, cmds
    assert "-t api:latest" in builds[0]
    assert builds[0].count(" -t ") == 1, "one tag: the declared name at :latest, nothing invented"
    assert cmds.index(builds[0]) < min(
        i for i, c in enumerate(cmds) if c.startswith("docker image inspect")
    ), "the daemon is asked only after the build"
    assert not any(c.startswith("docker tag ") for c in cmds), cmds
    # `tar -xf` (extracting the just-staged build context) must be unbounded
    # — its duration IS the extraction, which scales with the context size.
    tar_call = next(c for c in parent.exec.call_args_list if c.args[0].startswith("tar -xf "))
    assert tar_call.kwargs.get("timeout") == float("inf")


@pytest.mark.asyncio
async def test_build_failure_propagates(tmp_path):
    parent = _mock_parent()
    img = _img(tmp_path)
    build_failure = _fail("syntax error in dockerfile", command="docker build ...")

    async def exec_side_effect(cmd, *_, **__):
        if cmd.startswith("docker build "):
            return build_failure
        return _ok()

    parent.exec.side_effect = exec_side_effect

    built = await _build_one(parent, "repo1", img, BuildOptions())
    res = built.result
    assert res is build_failure, "the build's own result, not a summary of it"
    assert not built.is_ok
    assert built.references == []
    assert built.image_id is None
    assert not [
        c.args[0] for c in parent.exec.call_args_list if c.args[0].startswith("docker image")
    ]
    assert res.status is not Status.Success
    # The failing build's own result comes back whole: its captured output is
    # in value, and the command/retcode the old tuple discarded survive.
    assert "syntax error" in res.value
    # The failing result comes back WHOLE. `!= 0` would be vacuous: retcode
    # defaults to -1, so a rebuilt-and-summarized result would pass it.
    assert res.retcode == 1
    assert res.command.startswith("docker build ")


# ---------------------------------------------------------------------------
# _build_one — the report is read back from the daemon
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_report_carries_what_the_daemon_lists_for_the_built_image(tmp_path):
    parent = _mock_parent()
    _daemon(parent, listing="api:latest cbd8571d4b6e\n")

    built = await _build_one(parent, "repo1", _img(tmp_path), BuildOptions())

    assert built.is_ok
    assert built.references == ["api:latest"]
    assert built.image_id == "cbd8571d4b6e"


@pytest.mark.asyncio
async def test_other_images_under_the_same_name_are_not_reported(tmp_path):
    """`docker images api` lists every tag of the repository; only the new id is ours."""
    parent = _mock_parent()
    _daemon(parent, listing="api:latest cbd8571d4b6e\napi:old 111122223333\napi:1.4 cbd8571d4b6e\n")

    built = await _build_one(parent, "repo1", _img(tmp_path), BuildOptions(tags=["api"]))

    assert built.references == ["api:latest", "api:1.4"]
    assert built.image_id == "cbd8571d4b6e"


@pytest.mark.asyncio
async def test_a_build_the_daemon_does_not_list_is_a_failure(tmp_path):
    parent = _mock_parent()
    _daemon(parent, listing="")

    built = await _build_one(parent, "repo1", _img(tmp_path), BuildOptions())

    assert not built.is_ok
    assert built.references == []
    assert built.image_id is None
    assert "api:latest" in built.result.value


# ---------------------------------------------------------------------------
# An archive context
# ---------------------------------------------------------------------------


def _archive_img(tmp: Path) -> DockerImage:
    archive = tmp / "ctx.tar.gz"
    archive.write_bytes(b"not opened by otto")
    return DockerImage(
        name="api",
        dockerfile=tmp / "docker" / "Dockerfile",
        context=archive,
        dockerfile_in_archive="docker/Dockerfile",
    )


def test_an_archive_is_given_to_docker_on_stdin_with_the_inner_dockerfile(tmp_path):
    cmd = build_command("repo1", _archive_img(tmp_path), BuildOptions())
    assert " -f docker/Dockerfile " in cmd
    assert cmd.endswith(" - < /tmp/otto-docker/repo1/build/api.ctx.tar.gz")


@pytest.mark.asyncio
async def test_an_archive_is_uploaded_unopened_and_removed_after_a_failed_build(tmp_path):
    parent = _mock_parent()
    img = _archive_img(tmp_path)
    failure = _fail("no such file: docker/Dockerfile", command="docker build ...")

    async def exec_side_effect(cmd, *_, **__):
        return failure if cmd.startswith("docker build ") else _ok(command=cmd)

    parent.exec.side_effect = exec_side_effect

    built = await _build_one(parent, "repo1", img, BuildOptions())

    assert built.result is failure, "docker's own error, whole"
    put_sources = parent.put.call_args.args[0]
    assert put_sources == [img.context], "the user's file itself, not a re-tarred copy"
    cmds = [c.args[0] for c in parent.exec.call_args_list]
    assert not any(c.startswith("tar ") for c in cmds), "otto never opens the archive"
    assert cmds[-1] == "rm -f /tmp/otto-docker/repo1/build/api.ctx.tar.gz"


@pytest.mark.asyncio
async def test_an_archive_is_removed_before_the_daemon_is_read_after_a_good_build(tmp_path):
    parent = _mock_parent()
    img = _archive_img(tmp_path)
    _daemon(parent, listing="api:latest cbd8571d4b6e\n")

    built = await _build_one(parent, "repo1", img, BuildOptions())

    assert built.references == ["api:latest"]
    cmds = [c.args[0] for c in parent.exec.call_args_list]
    removed = cmds.index("rm -f /tmp/otto-docker/repo1/build/api.ctx.tar.gz")
    assert cmds[removed - 1].startswith("docker build ")
    assert cmds[removed + 1].startswith("docker image inspect")


@pytest.mark.asyncio
async def test_a_failed_archive_removal_does_not_replace_the_builds_outcome(tmp_path):
    parent = _mock_parent()
    img = _archive_img(tmp_path)
    failure = _fail("docker's own error", command="docker build ...")

    async def exec_side_effect(cmd, *_, **__):
        if cmd.startswith("rm -f "):
            raise OSError("transport gone")
        return failure if cmd.startswith("docker build ") else _ok(command=cmd)

    parent.exec.side_effect = exec_side_effect

    built = await _build_one(parent, "repo1", img, BuildOptions())

    assert built.result is failure


@pytest.mark.asyncio
async def test_an_archive_removal_that_exits_non_zero_warns_and_keeps_the_builds_outcome(tmp_path):
    parent = _mock_parent()
    parent.id = "test3"
    img = _archive_img(tmp_path)
    failure = _fail("docker's own error", command="docker build ...")

    async def exec_side_effect(cmd, *_, **__):
        if cmd.startswith("rm -f "):
            return _fail("rm: cannot remove: Permission denied", command=cmd)
        return failure if cmd.startswith("docker build ") else _ok(command=cmd)

    parent.exec.side_effect = exec_side_effect

    with patch("otto.docker.build.logger") as log:
        built = await _build_one(parent, "repo1", img, BuildOptions())

    assert built.result is failure
    warned = [c.args[0] for c in log.warning.call_args_list]
    assert len(warned) == 1
    assert "test3" in warned[0]
    assert "/tmp/otto-docker/repo1/build/api.ctx.tar.gz" in warned[0]
    assert "Permission denied" in warned[0]

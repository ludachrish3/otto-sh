"""Staging failures carry WHICH failure they were.

Every command here runs on the parent through ``exec``, so the same non-ok
status can mean "the parent said no" or "the parent never answered" — and the
second is not a docker problem at all. ``put`` is the one exception: it
returns a plain :class:`~otto.result.Result` with no ``timed_out`` to ask
about, so its failures are only ever the first kind.
"""

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from otto.config.repo import DockerCompose
from otto.docker.staging import (
    _relative_bind_sources,
    stage_compose_files,
    warn_relative_bind_sources,
)
from otto.host.element import Element
from otto.host.errors import HostCommandError, HostUnreachableError
from otto.host.login_proxy import Cred
from otto.host.unix_host import UnixHost
from otto.result import CommandResult, Result
from otto.utils import Status


def _parent() -> UnixHost:
    return UnixHost(
        ip="10.10.200.13",
        element=Element("test3"),
        creds=[Cred(login="vagrant", password="vagrant")],
        docker_capable=True,
    )


def _fail(out: str = "denied") -> CommandResult:
    return CommandResult(Status.Failed, value=out, command="", retcode=1)


def _timed_out() -> CommandResult:
    return CommandResult(Status.Failed, value="", command="", retcode=-1, timed_out=True)


def _compose(tmp_path: Path) -> list[DockerCompose]:
    path = tmp_path / "compose.yml"
    path.write_text("services: {}\n")
    return [DockerCompose(path=path, services=["api"])]


@pytest.mark.asyncio
async def test_a_refused_prepare_is_a_command_failure(tmp_path):
    parent = _parent()
    parent.exec = AsyncMock(return_value=_fail("mkdir: permission denied"))  # type: ignore[method-assign]
    with pytest.raises(HostCommandError, match="failed to prepare the compose staging dir"):
        await stage_compose_files(parent, "proj", _compose(tmp_path))


@pytest.mark.asyncio
async def test_a_timed_out_prepare_is_an_unreachable_host(tmp_path):
    """Same message, different type: nothing was learned about the parent."""
    parent = _parent()
    parent.exec = AsyncMock(return_value=_timed_out())  # type: ignore[method-assign]
    with pytest.raises(HostUnreachableError, match="failed to prepare the compose staging dir"):
        await stage_compose_files(parent, "proj", _compose(tmp_path))


@pytest.mark.asyncio
async def test_a_failed_put_is_a_command_failure(tmp_path):
    """`put` returns a bare Result — there is no timed_out here to split on."""
    parent = _parent()
    parent.exec = AsyncMock(return_value=CommandResult(Status.Success, value="", command=""))  # type: ignore[method-assign]
    parent.put = AsyncMock(return_value=Result(Status.Error, msg="no space left"))  # type: ignore[method-assign]
    with pytest.raises(HostCommandError, match="failed to stage compose file"):
        await stage_compose_files(parent, "proj", _compose(tmp_path))


@pytest.mark.asyncio
async def test_stage_compose_files_warns_about_a_relative_bind_source(tmp_path, caplog):
    """The call site actually reads the authored file and warns -- not just the helper."""
    parent = _parent()
    parent.exec = AsyncMock(return_value=CommandResult(Status.Success, value="", command=""))  # type: ignore[method-assign]
    parent.put = AsyncMock(return_value=Result(Status.Success))  # type: ignore[method-assign]
    path = tmp_path / "compose.yml"
    path.write_text("services:\n  api:\n    volumes:\n      - ./data:/var/lib/app\n")
    composes = [DockerCompose(path=path, services=["api"])]

    await stage_compose_files(parent, "proj", composes)

    assert "./data" in caplog.text
    assert "compose/0/data" in caplog.text


@pytest.mark.asyncio
async def test_stage_compose_files_warns_with_index_0s_directory_for_the_second_file(
    tmp_path, caplog
):
    """Compose resolves every relative bind source against the FIRST `-f`
    file's directory (`_compose_cmd` passes only `-f`, no
    `--project-directory`) -- so a warning about the SECOND file's bind
    source must still name `compose/0/`, not `compose/1/`, or it points a
    reader at a directory docker will never bind."""
    parent = _parent()
    parent.exec = AsyncMock(return_value=CommandResult(Status.Success, value="", command=""))  # type: ignore[method-assign]
    parent.put = AsyncMock(return_value=Result(Status.Success))  # type: ignore[method-assign]
    first = tmp_path / "first.yml"
    first.write_text("services:\n  api:\n    image: x\n")
    second = tmp_path / "second.yml"
    second.write_text("services:\n  worker:\n    volumes:\n      - ./data:/x\n")
    composes = [
        DockerCompose(path=first, services=["api"]),
        DockerCompose(path=second, services=["worker"]),
    ]

    await stage_compose_files(parent, "proj", composes)

    assert "compose/0/data" in caplog.text
    assert "compose/1/data" not in caplog.text


@pytest.mark.asyncio
async def test_stage_compose_files_warn_false_emits_nothing(tmp_path, caplog):
    """`compose_down` re-stages purely to recover the `-f` paths for
    teardown, by which point staging's own `rm -rf` has already wiped the
    directory the advisory would name -- `warn=False` must suppress it."""
    parent = _parent()
    parent.exec = AsyncMock(return_value=CommandResult(Status.Success, value="", command=""))  # type: ignore[method-assign]
    parent.put = AsyncMock(return_value=Result(Status.Success))  # type: ignore[method-assign]
    path = tmp_path / "compose.yml"
    path.write_text("services:\n  api:\n    volumes:\n      - ./data:/var/lib/app\n")
    composes = [DockerCompose(path=path, services=["api"])]

    await stage_compose_files(parent, "proj", composes, warn=False)

    assert "./data" not in caplog.text
    # The advisory's own signature phrase, not `caplog.text == ""`: an exact
    # match would break on any unrelated record that happens to propagate,
    # which is a different failure than the one this test is about.
    assert "binds the relative source" not in caplog.text


@pytest.mark.asyncio
async def test_stage_compose_files_reads_nothing_useful_on_an_unreadable_file(tmp_path, caplog):
    """The OSError guard swallows a read failure -- `put` below is still the refusal."""
    parent = _parent()
    parent.exec = AsyncMock(return_value=CommandResult(Status.Success, value="", command=""))  # type: ignore[method-assign]
    parent.put = AsyncMock(return_value=Result(Status.Error, msg="no such file"))  # type: ignore[method-assign]
    path = tmp_path / "missing.yml"  # never written -- read_text() raises OSError
    composes = [DockerCompose(path=path, services=["api"])]

    with pytest.raises(HostCommandError, match="failed to stage compose file"):
        await stage_compose_files(parent, "proj", composes)

    assert caplog.text == ""


@pytest.mark.asyncio
async def test_stage_compose_files_reads_nothing_useful_on_non_utf8_bytes(tmp_path, caplog):
    """`read_text` raises `UnicodeDecodeError` (a `ValueError`) on non-UTF-8 bytes.

    `compose_down` wraps this staging call in ``except RuntimeError`` so a
    staging failure cannot interrupt a teardown sweep -- a bare ``ValueError``
    would sail straight through that guard, which is exactly the behaviour
    change the swallow ruling exists to prevent. Staging must still proceed
    to ``put`` (unaffected: it ships the file's own bytes, not the decoded
    text), and no warning is produced.
    """
    parent = _parent()
    parent.exec = AsyncMock(return_value=CommandResult(Status.Success, value="", command=""))  # type: ignore[method-assign]
    parent.put = AsyncMock(return_value=Result(Status.Success))  # type: ignore[method-assign]
    path = tmp_path / "latin1.yml"
    path.write_bytes(b"services:\n  api: {}\n  # caf\xe9\n")  # \xe9 alone is not valid UTF-8

    out = await stage_compose_files(parent, "proj", [DockerCompose(path=path, services=["api"])])

    assert out[0].name == "latin1.yml"
    parent.put.assert_awaited()
    assert caplog.text == ""


@pytest.mark.asyncio
async def test_stage_compose_files_proceeds_on_unparseable_compose_content(tmp_path, caplog):
    """Unparseable YAML CONTENT (not just a missing file) is swallowed too.

    Distinct from the missing-file test above: there ``authored`` is ``""``,
    which parses to ``None`` and never reaches the YAML-error branch at all.
    This drives genuinely unparseable text through the real call site.
    """
    parent = _parent()
    parent.exec = AsyncMock(return_value=CommandResult(Status.Success, value="", command=""))  # type: ignore[method-assign]
    parent.put = AsyncMock(return_value=Result(Status.Success))  # type: ignore[method-assign]
    path = tmp_path / "bad.yml"
    path.write_text("\tnot: [valid")
    composes = [DockerCompose(path=path, services=["api"])]

    out = await stage_compose_files(parent, "proj", composes)

    assert out[0].name == "bad.yml"
    parent.put.assert_awaited()
    assert caplog.text == ""


def test_relative_bind_sources_finds_the_short_form():
    text = "services:\n  api:\n    volumes:\n      - ./data:/var/lib/app\n"
    assert _relative_bind_sources("api.yaml", text) == ["./data"]


def test_relative_bind_sources_finds_the_long_form():
    text = (
        "services:\n  api:\n    volumes:\n"
        "      - type: bind\n        source: ../shared\n        target: /shared\n"
    )
    assert _relative_bind_sources("api.yaml", text) == ["../shared"]


def test_relative_bind_sources_ignores_absolute_sources():
    text = "services:\n  api:\n    volumes:\n      - /srv/data:/var/lib/app\n"
    assert _relative_bind_sources("api.yaml", text) == []


def test_relative_bind_sources_ignores_named_volumes():
    text = "services:\n  api:\n    volumes:\n      - appvol:/data\n"
    assert _relative_bind_sources("api.yaml", text) == []


def test_relative_bind_sources_ignores_a_long_form_volume():
    # `source: ./appdata` clears the "$" check and the prefix check on its
    # own -- only the `type != "bind"` check can exclude it. A named-volume
    # source like "appvol" would pass even with that type check deleted (it
    # already fails the prefix check), which proves nothing about this guard.
    text = (
        "services:\n  api:\n    volumes:\n"
        "      - type: volume\n        source: ./appdata\n        target: /data\n"
    )
    assert _relative_bind_sources("api.yaml", text) == []


def test_relative_bind_sources_skips_an_uninterpolated_source():
    # The source LOOKS relative (starts with "./") but part of it is a
    # compose interpolation resolved from the env file at up time, so a
    # static walk cannot know the real subpath -- otto must not warn about
    # a path it has not evaluated. (A source like "${DATA_DIR}/data" with no
    # leading "./" is already excluded by the prefix check below and would
    # pass even without this guard -- it proves nothing about the "$" skip.)
    text = "services:\n  api:\n    volumes:\n      - ./${DATA_DIR}/data:/var/lib/app\n"
    assert _relative_bind_sources("api.yaml", text) == []


def test_relative_bind_sources_ignores_a_mapping_volumes_value():
    """A `volumes:` mapping is not valid Compose -- must not be walked at all.

    A key that ITSELF looks like a relative source ("./data": {}) so that
    without the `isinstance(volumes, list)` guard this would produce a
    spurious warning (iterating a dict yields its keys, and a plain string
    key sails straight through `_volume_source`'s short-form branch) rather
    than merely proving nothing, as a named-volume-shaped key would.
    """
    text = "services:\n  api:\n    volumes:\n      ./data: {}\n"
    assert _relative_bind_sources("api.yaml", text) == []


def test_relative_bind_sources_ignores_a_non_list_scalar_volumes_value():
    """A `volumes:` scalar (not a list, not a mapping) is not valid Compose.

    Iterating a bare string only ever yields single characters, which can
    never satisfy the two-character ``"./"``/``"../"`` prefix check -- so a
    string value can't be used to prove the guard matters. A non-iterable
    scalar can: without ``isinstance(volumes, list)``, `for entry in 5`
    raises ``TypeError: 'int' object is not iterable``.
    """
    text = "services:\n  api:\n    volumes: 5\n"
    assert _relative_bind_sources("api.yaml", text) == []


def test_relative_bind_sources_keeps_the_ro_suffix_out_of_the_source():
    text = "services:\n  api:\n    volumes:\n      - ./data:/var/lib/app:ro\n"
    assert _relative_bind_sources("api.yaml", text) == ["./data"]


def test_relative_bind_sources_refuses_non_yaml():
    from otto.docker.resolve import UseCaseResolutionError

    with pytest.raises(UseCaseResolutionError, match="not valid YAML"):
        _relative_bind_sources("api.yaml", "\tnot: [valid")


def test_warn_is_silent_on_unparseable_yaml(caplog):
    # The warning is ADVISORY. `stage_compose_files` ships repo-authored files
    # verbatim and has never parsed them, so a compose file otto cannot read
    # must not become a staging refusal that did not exist before -- docker is
    # the one that reports it. The use-case path's own _collect_env_file_refs
    # still refuses bad YAML, so nothing is weakened there.
    warn_relative_bind_sources("api.yaml", "\tnot: [valid", Path("/tmp/otto/x/compose/0"))
    assert caplog.text == ""


def test_warn_names_the_resolved_staging_path(caplog):
    text = "services:\n  api:\n    volumes:\n      - ./data:/var/lib/app\n"
    warn_relative_bind_sources("api.yaml", text, Path("/tmp/otto/repo1/compose/0"))
    assert "/tmp/otto/repo1/compose/0/data" in caplog.text
    assert "./data" in caplog.text


def test_warn_is_silent_with_no_relative_sources(caplog):
    text = "services:\n  api:\n    volumes:\n      - /srv/data:/var/lib/app\n"
    warn_relative_bind_sources("api.yaml", text, Path("/tmp/otto/repo1/compose/0"))
    assert caplog.text == ""


def test_warn_names_the_staged_path_verbatim_not_a_local_symlink_target(tmp_path, caplog):
    """`os.path.normpath`, not `Path.resolve()`: `staged_dir` lives on the PARENT.

    `staged_dir` is never a path this filesystem can resolve for real; this
    pins that a symlink sitting locally at the same name never gets
    substituted into the message.
    """
    real_target = tmp_path / "real-target"
    real_target.mkdir()
    staged_dir = tmp_path / "compose-link"
    staged_dir.symlink_to(real_target)
    text = "services:\n  api:\n    volumes:\n      - ./data:/var/lib/app\n"

    warn_relative_bind_sources("api.yaml", text, staged_dir)

    assert str(staged_dir / "data") in caplog.text
    assert str(real_target) not in caplog.text

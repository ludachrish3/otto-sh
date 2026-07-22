"""Permission-mode support for transfers to hosts (spec 2026-07-21)."""

import shlex
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from otto.host.transfer.base import (
    MAX_FILE_MODE,
    BaseFileTransfer,
    chmod_command,
    parse_file_mode,
)
from otto.host.transfer.unix_base import UnixFileTransfer
from otto.result import CommandResult, Result
from otto.utils import Status


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("755", 0o755),
        ("0755", 0o755),
        ("0o755", 0o755),
        ("0O755", 0o755),
        ("640", 0o640),
        ("4755", 0o4755),  # setuid permitted, as chmod permits it
        ("0", 0),
        ("7777", MAX_FILE_MODE),
    ],
)
def test_string_modes_are_always_octal(raw, expected):
    result = parse_file_mode(raw)
    assert result.is_ok, result.msg
    assert result.value == expected


def test_bare_755_is_octal_not_decimal():
    # The whole point: "755" read base-10 would be 0o1363, which is NOT what
    # anyone means by a mode of 755.
    parsed = parse_file_mode("755").value
    assert parsed == 0o755
    assert parsed != 755


def test_int_mode_passes_through_untouched():
    # Python API: mode=0o755 is already a mode; never re-read as base-8.
    result = parse_file_mode(0o755)
    assert result.is_ok
    assert result.value == 0o755


def test_none_is_ok_and_means_no_mode():
    result = parse_file_mode(None)
    assert result.is_ok
    assert result.value is None


@pytest.mark.parametrize("raw", ["789", "rwx", "", "0x1ff", "u+x", "7 5 5"])
def test_non_octal_strings_are_rejected(raw):
    result = parse_file_mode(raw)
    assert result.status is Status.Error
    assert repr(raw) in result.msg


def test_negative_mode_rejected():
    result = parse_file_mode(-1)
    assert result.status is Status.Error
    assert "negative" in result.msg


@pytest.mark.parametrize("raw", ["77777", 0o10000])
def test_out_of_range_mode_rejected(raw):
    result = parse_file_mode(raw)
    assert result.status is Status.Error
    assert "out of range" in result.msg


def test_bool_is_not_a_mode():
    # bool subclasses int; True would silently become 0o1 without a guard.
    result = parse_file_mode(True)
    assert result.status is Status.Error


def test_chmod_command_is_octal_without_prefix_and_batched():
    cmd = chmod_command(0o755, [Path("/opt/a"), Path("/opt/b")])
    assert cmd == "chmod 755 -- /opt/a /opt/b"


def test_chmod_command_quotes_hostile_paths():
    cmd = chmod_command(0o644, [Path("/opt/my file"), Path("/opt/it's")])
    assert "'/opt/my file'" in cmd
    assert cmd.count("chmod") == 1
    # A bare shell would split these; quoting must survive.
    assert shlex.split(cmd)[3:] == ["/opt/my file", "/opt/it's"]


def test_chmod_command_terminates_options_before_paths():
    # shlex.quote leaves "-rf" bare (no metacharacters), and a relative
    # dest_dir collapses Path() / "-rf" to exactly that — without `--`, chmod
    # would parse the destination as option flags. Path() is not contrived:
    # it is the literal default of UnixHost.default_dest_dir.
    dest = Path() / "-rf"
    assert str(dest) == "-rf"
    assert shlex.quote(str(dest)) == "-rf"

    cmd = chmod_command(0o600, [dest])
    argv = shlex.split(cmd)
    assert argv == ["chmod", "600", "--", "-rf"]
    assert argv.index("--") < argv.index("-rf")


# ---------------------------------------------------------------------------
# The put_files seam — capability, pre-flight, _apply_mode
# ---------------------------------------------------------------------------


class _FakeBackend(BaseFileTransfer):
    """Minimal backend recording what put_files asked of it."""

    def __init__(self, name="fake", supports_mode=False, chmod_result=None, outcomes=None):
        super().__init__(name=name)
        self.supports_mode = supports_mode
        self.run_put_calls = 0
        self.apply_mode_calls: list[tuple[list[Path], int]] = []
        # `or` would be wrong here: Result.__bool__ follows is_ok, so a failing
        # chmod_result is falsy and would be silently replaced by Success.
        self._chmod_result = Result(Status.Success) if chmod_result is None else chmod_result
        self._outcomes = outcomes

    async def _run_put(self, src_files, dest_dir, progress_factory):
        self.run_put_calls += 1
        if self._outcomes is not None:
            return dict(self._outcomes)
        return {src: Result(Status.Success, value=dest_dir / src.name) for src in src_files}

    async def _run_get(self, src_files, dest_dir, progress_factory):
        return {}

    async def _apply_mode(self, dest_paths, mode):
        self.apply_mode_calls.append((list(dest_paths), mode))
        return self._chmod_result


@pytest.mark.asyncio
async def test_unsupported_backend_fails_before_any_bytes_move():
    # The point of the pre-flight check: assert _run_put was never reached.
    backend = _FakeBackend(name="zephyr1", supports_mode=False)
    result = await backend.put_files([Path("a.bin")], Path("/RAM:"), False, mode="755")
    assert not result.is_ok
    assert backend.run_put_calls == 0
    assert "zephyr1" in result.msg
    assert "_FakeBackend" in result.msg
    assert "0o755" in result.msg


@pytest.mark.asyncio
async def test_bad_octal_fails_before_any_bytes_move():
    backend = _FakeBackend(supports_mode=True)
    result = await backend.put_files([Path("a.bin")], Path("/opt"), False, mode="789")
    assert not result.is_ok
    assert backend.run_put_calls == 0
    assert "789" in result.msg


@pytest.mark.asyncio
async def test_no_mode_never_calls_apply_mode():
    backend = _FakeBackend(supports_mode=True)
    result = await backend.put_files([Path("a.bin")], Path("/opt"), False)
    assert result.is_ok
    assert backend.apply_mode_calls == []


@pytest.mark.asyncio
async def test_apply_mode_receives_only_landed_dest_paths():
    src_ok, src_bad, src_skip = Path("ok.bin"), Path("bad.bin"), Path("skip.bin")
    backend = _FakeBackend(
        supports_mode=True,
        outcomes={
            src_ok: Result(Status.Success, value=Path("/opt/ok.bin")),
            src_bad: Result(Status.Error, msg="connection reset"),
            src_skip: Result(Status.Skipped, msg="not attempted"),
        },
    )
    await backend.put_files([src_ok, src_bad, src_skip], Path("/opt"), False, mode="755")
    assert backend.apply_mode_calls == [([Path("/opt/ok.bin")], 0o755)]


@pytest.mark.asyncio
async def test_apply_mode_skipped_entirely_when_nothing_landed():
    src = Path("bad.bin")
    backend = _FakeBackend(
        supports_mode=True,
        outcomes={src: Result(Status.Error, msg="connection reset")},
    )
    result = await backend.put_files([src], Path("/opt"), False, mode="755")
    assert backend.apply_mode_calls == []
    assert not result.is_ok
    assert "connection reset" in result.msg


@pytest.mark.asyncio
async def test_chmod_failure_downgrades_but_keeps_dest_path():
    src = Path("a.bin")
    backend = _FakeBackend(
        supports_mode=True,
        chmod_result=Result(Status.Error, msg="chmod: Operation not permitted"),
    )
    result = await backend.put_files([src], Path("/opt"), False, mode="755")
    assert not result.is_ok
    entry = result.value[src]
    assert entry.status is Status.Error
    # The bytes DID land — a caller must be able to tell that apart from a
    # transfer that never happened.
    assert entry.value == Path("/opt/a.bin")
    assert "Operation not permitted" in entry.msg


@pytest.mark.asyncio
async def test_supports_mode_without_apply_mode_raises():
    class _Broken(_FakeBackend):
        _apply_mode = BaseFileTransfer._apply_mode

    backend = _Broken(supports_mode=True)
    with pytest.raises(NotImplementedError, match="_apply_mode"):
        await backend.put_files([Path("a.bin")], Path("/opt"), False, mode="755")


def test_default_backend_does_not_support_mode():
    assert BaseFileTransfer.supports_mode is False


# ---------------------------------------------------------------------------
# UnixFileTransfer — one implementation serving scp, sftp, ftp and nc
# ---------------------------------------------------------------------------


class _ConcreteUnix(UnixFileTransfer):
    """Concrete stand-in — UnixFileTransfer itself is abstract.

    Exercises the real inherited ``_apply_mode``; only the two abstract
    transfer methods are stubbed out.
    """

    async def _run_put(self, src_files, dest_dir, progress_factory):
        return {}

    async def _run_get(self, src_files, dest_dir, progress_factory):
        return {}


def _unix_backend(exec_cmd):
    return _ConcreteUnix(connections=object(), name="web1", exec_cmd=exec_cmd)


def test_every_unix_backend_supports_mode():
    from otto.host.transfer import (
        FtpFileTransfer,
        NcFileTransfer,
        ScpFileTransfer,
        SftpFileTransfer,
    )

    for cls in (ScpFileTransfer, SftpFileTransfer, FtpFileTransfer, NcFileTransfer):
        assert cls.supports_mode is True, cls.__name__


@pytest.mark.asyncio
async def test_unix_apply_mode_issues_exactly_one_batched_chmod():
    exec_cmd = AsyncMock(
        return_value=CommandResult(Status.Success, value="", command="", retcode=0)
    )
    backend = _unix_backend(exec_cmd)
    result = await backend._apply_mode([Path("/opt/a"), Path("/opt/b"), Path("/opt/c")], 0o755)
    assert result.is_ok
    # Batching is the contract, not just the outcome: N files, ONE exec.
    assert exec_cmd.await_count == 1
    assert exec_cmd.await_args.args[0] == "chmod 755 -- /opt/a /opt/b /opt/c"


@pytest.mark.asyncio
async def test_unix_apply_mode_reports_chmod_failure():
    exec_cmd = AsyncMock(
        return_value=CommandResult(
            Status.Failed,
            value="chmod: Operation not permitted",
            command="chmod",
            retcode=1,
        )
    )
    backend = _unix_backend(exec_cmd)
    result = await backend._apply_mode([Path("/opt/a")], 0o755)
    assert not result.is_ok
    assert "Operation not permitted" in result.msg


# ---------------------------------------------------------------------------
# LocalFileTransfer — real chmod; EmbeddedFileTransfer — loud refusal
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_local_transfer_really_chmods(tmp_path):
    from otto.host.local_host import LocalFileTransfer

    src_dir, dest_dir = tmp_path / "src", tmp_path / "dest"
    src_dir.mkdir()
    src = src_dir / "app.bin"
    src.write_text("#!/bin/sh\n")
    src.chmod(0o600)

    backend = LocalFileTransfer(name="local")
    result = await backend.put_files([src], dest_dir, False, mode="755")

    assert result.is_ok, result.msg
    assert (dest_dir / "app.bin").stat().st_mode & 0o7777 == 0o755


@pytest.mark.asyncio
async def test_local_transfer_without_mode_leaves_copy2_permissions(tmp_path):
    from otto.host.local_host import LocalFileTransfer

    src_dir, dest_dir = tmp_path / "src", tmp_path / "dest"
    src_dir.mkdir()
    src = src_dir / "app.bin"
    src.write_text("x")
    src.chmod(0o600)

    backend = LocalFileTransfer(name="local")
    assert (await backend.put_files([src], dest_dir, False)).is_ok
    # shutil.copy2 preserves the source mode; no mode means we do not touch it.
    assert (dest_dir / "app.bin").stat().st_mode & 0o7777 == 0o600


@pytest.mark.asyncio
async def test_embedded_transfer_refuses_mode_loudly():
    from otto.host.transfer.console import ConsoleFileTransfer

    backend = ConsoleFileTransfer(name="zephyr1", exec_cmd=AsyncMock())
    result = await backend.put_files([Path("app.bin")], Path("/RAM:"), False, mode="755")

    assert not result.is_ok
    assert "zephyr1" in result.msg
    assert "no permission model" in result.msg
    # Never silently ignored.
    assert ConsoleFileTransfer.supports_mode is False


# ---------------------------------------------------------------------------
# The host `put` surface
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "host_cls_path",
    [
        "otto.host.unix_host:UnixHost",
        "otto.host.embedded_host:EmbeddedHost",
        "otto.host.local_host:LocalHost",
        "otto.host.docker_host:DockerContainerHost",
    ],
)
def test_put_exposes_mode_as_a_string_cli_option(host_cls_path):
    import importlib
    from typing import get_args

    from otto.cli.param_synth import build_cli_binding

    module_name, cls_name = host_cls_path.split(":")
    host_cls = getattr(importlib.import_module(module_name), cls_name)

    binding = build_cli_binding(host_cls.put)
    assert "mode" in [p.name for p in binding.params], cls_name
    mode_param = next(p for p in binding.params if p.name == "mode")
    # Typer must see a STRING, so it can never coerce "755" to decimal 755.
    assert get_args(mode_param.annotation)[0] is str, cls_name
    assert mode_param.default is None, cls_name


@pytest.mark.asyncio
async def test_docker_put_chmods_inside_the_container(monkeypatch):
    from otto.host.docker_host import DockerContainerHost
    from otto.host.transfer.base import aggregate_transfer

    host = DockerContainerHost.__new__(DockerContainerHost)
    src = Path("app.bin")
    dest_dir = Path("/opt/bin")

    parent_execs: list[str] = []
    staged_put_kwargs: dict = {}

    async def fake_parent_exec(cmd, *a, **kw):
        parent_execs.append(cmd)
        return CommandResult(Status.Success, value="", command=cmd, retcode=0)

    async def fake_parent_put(files, stage, *a, **kw):
        staged_put_kwargs["args"] = a
        staged_put_kwargs["kwargs"] = kw
        return aggregate_transfer({f: Result(Status.Success, value=stage / f.name) for f in files})

    container_execs: list[str] = []

    async def fake_exec(cmd, *a, **kw):
        container_execs.append(cmd)
        return CommandResult(Status.Success, value="", command=cmd, retcode=0)

    parent = type(
        "P",
        (),
        {"exec": staticmethod(fake_parent_exec), "put": staticmethod(fake_parent_put)},
    )()
    monkeypatch.setattr(type(host), "parent", property(lambda self: parent))
    monkeypatch.setattr(type(host), "container_id", property(lambda self: "abc123"))
    monkeypatch.setattr(host, "exec", fake_exec, raising=False)
    monkeypatch.setattr(host, "_ensure_running", AsyncMock(), raising=False)

    result = await host.put([src], dest_dir, mode="755")

    assert result.is_ok, result.msg
    # chmod runs INSIDE the container, not on the parent, and exactly once.
    assert container_execs == ["chmod 755 -- /opt/bin/app.bin"]
    assert not any("chmod" in c for c in parent_execs)
    # The staging put must NOT carry the mode — staging is deleted anyway, and
    # relying on `docker cp` to preserve it is exactly what we are avoiding.
    assert not staged_put_kwargs["args"]
    assert not staged_put_kwargs["kwargs"]


@pytest.mark.asyncio
async def test_docker_put_rejects_bad_octal_before_running(monkeypatch):
    from otto.host.docker_host import DockerContainerHost

    host = DockerContainerHost.__new__(DockerContainerHost)
    ensure_running = AsyncMock()
    monkeypatch.setattr(host, "_ensure_running", ensure_running, raising=False)

    result = await host.put([Path("app.bin")], Path("/opt/bin"), mode="789")

    assert not result.is_ok
    assert "789" in result.msg
    ensure_running.assert_not_awaited()

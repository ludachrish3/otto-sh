"""Unit tests for dynamic host-method CLI exposure."""
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import typer
from typer.testing import CliRunner

from otto.cli.expose import (
    HostGroup,
    bind_cli_args,
    collect_exposed_methods,
    exposed_cli_names,
    host_class_for_id,
    make_method_command,
)
from otto.host.unix_host import UnixHost
from otto.utils import Status, cli_exposed


def test_cli_exposed_sets_markers_with_dashed_default_name():
    @cli_exposed
    async def soft_reboot(self):
        ...
    assert soft_reboot.__cli_exposed__ is True
    assert soft_reboot.__cli_name__ == "soft-reboot"
    assert soft_reboot.__cli_help__ is None


def test_cli_exposed_accepts_explicit_name_and_help():
    @cli_exposed(name="pwr", help="power it")
    async def power(self, state=None):
        ...
    assert power.__cli_name__ == "pwr"
    assert power.__cli_help__ == "power it"


class _Sample:
    @cli_exposed
    async def reboot(self, hard: bool = False):
        ...
    @cli_exposed(name="pwr")
    async def power(self, state: str):
        ...
    async def not_exposed(self):
        ...


def test_collect_finds_only_exposed_methods():
    found = collect_exposed_methods(_Sample)
    assert found == {"reboot": "reboot", "pwr": "power"}


def test_bind_cli_args_coerces_by_annotation():
    sample = _Sample()
    assert bind_cli_args(sample.reboot, ["true"]) == [True]
    assert bind_cli_args(sample.reboot, ["0"]) == [False]
    assert bind_cli_args(sample.power, ["on"]) == ["on"]


def test_bind_cli_args_empty_uses_no_positionals():
    sample = _Sample()
    assert bind_cli_args(sample.reboot, []) == []


@pytest.mark.asyncio
async def test_make_method_command_dispatches_and_closes():
    seen = {}

    class _Host:
        id = "h1"
        close = AsyncMock()

        async def reboot(self, hard: bool = False):
            seen["hard"] = hard
            return (Status.Success, "")

    host = _Host()

    class _Ctx:
        obj = host

    cmd = make_method_command("reboot")
    await cmd(_Ctx(), ["true"])
    assert seen["hard"] is True  # coerced from the string "true" via the bool annotation
    host.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_make_method_command_failure_tuple_exits_nonzero():
    class _Host:
        id = "h1"
        close = AsyncMock()

        async def reboot(self, hard: bool = False):
            return (Status.Failed, "did not come back")

    host = _Host()

    class _Ctx:
        obj = host

    cmd = make_method_command("reboot")
    with pytest.raises(typer.Exit) as ei:
        await cmd(_Ctx(), [])
    assert ei.value.exit_code == 1
    host.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_make_method_command_unsupported_method_errors():
    host = AsyncMock(spec=["close", "id"])  # no 'flash_firmware' attr
    host.close = AsyncMock()

    class _Ctx:
        obj = host

    cmd = make_method_command("flash_firmware")
    with pytest.raises(typer.Exit):
        await cmd(_Ctx(), [])


class _FakeUnix:
    id = "u1"

    @cli_exposed
    async def reboot(self):
        ...

    @cli_exposed
    async def mkdir(self, path: str):
        ...


class _FakeEmbedded:
    id = "e1"

    @cli_exposed
    async def reboot(self):
        ...
    # no mkdir


def _make_app(monkeypatch, hosts: dict[str, type]):
    import otto.host.os_profile as op

    monkeypatch.setattr(op, "_HOST_CLASSES", {c.__name__: c for c in set(hosts.values())})
    monkeypatch.setattr(
        "otto.cli.expose.host_class_for_id",
        lambda hid: hosts.get(hid),
    )
    app = typer.Typer(name="host", cls=HostGroup)

    @app.callback(invoke_without_command=True)
    def main(ctx: typer.Context, host_id: str = typer.Argument("")):
        if ctx.resilient_parsing:
            return

    return app


def test_exposed_cli_names_per_class():
    assert exposed_cli_names(_FakeUnix) == {"reboot", "mkdir"}
    assert exposed_cli_names(_FakeEmbedded) == {"reboot"}


def test_menu_scoped_to_resolved_class(monkeypatch):
    app = _make_app(monkeypatch, {"u1": _FakeUnix, "e1": _FakeEmbedded})
    r = CliRunner()
    out_u = r.invoke(app, ["u1", "--help"]).stdout
    out_e = r.invoke(app, ["e1", "--help"]).stdout
    assert "mkdir" in out_u and "reboot" in out_u
    assert "reboot" in out_e and "mkdir" not in out_e


def test_out_of_class_verb_is_no_such_command(monkeypatch):
    app = _make_app(monkeypatch, {"e1": _FakeEmbedded})
    r = CliRunner()
    res = r.invoke(app, ["e1", "mkdir", "/x"])
    assert res.exit_code == 2


def test_lifecycle_and_fileops_verbs_are_exposed():
    from otto.host.embedded_host import EmbeddedHost
    from otto.host.file_ops import PosixFileOps
    from otto.host.host import BaseHost

    base = collect_exposed_methods(BaseHost)
    assert {"reboot", "power", "shutdown", "install"} <= set(base)

    fileops = collect_exposed_methods(PosixFileOps)
    assert {"exists", "ls", "mkdir", "cp", "mv", "read-file", "write-file", "rm"} <= set(fileops)

    emb = collect_exposed_methods(EmbeddedHost)
    assert {"exists", "ls", "rm"} <= set(emb)
    # _no_fileop overrides stay hidden for embedded:
    assert "mkdir" not in emb and "cp" not in emb and "write-file" not in emb

    # Production scoping resolves the CONCRETE class (type(get_host(id))), so the
    # concrete UnixHost must expose the full lifecycle + file-op set — including
    # UnixHost's own `shutdown` override.
    unix = set(collect_exposed_methods(UnixHost))
    assert {
        "reboot", "power", "shutdown", "install", "uninstall", "stage",
        "is-installed", "is-uninstalled",
        "exists", "ls", "mkdir", "cp", "mv", "read-file", "write-file", "rm",
    } <= unix


def test_end_to_end_dispatch_through_host_group(monkeypatch):
    reboot_calls: list[bool] = []
    close_calls: list[None] = []

    class _FakeUnixLocal:
        id = "u1"

        @cli_exposed
        async def reboot(self, hard: bool = False) -> tuple[Status, str]:
            reboot_calls.append(hard)
            return (Status.Success, "ok")

        async def close(self) -> None:
            close_calls.append(None)

    import otto.host.os_profile as op
    monkeypatch.setattr(op, "_HOST_CLASSES", {"unix": _FakeUnixLocal})
    monkeypatch.setattr("otto.cli.expose.host_class_for_id", lambda hid: _FakeUnixLocal)

    app = typer.Typer(name="host", cls=HostGroup)
    host = _FakeUnixLocal()

    @app.callback(invoke_without_command=True)
    def main(ctx: typer.Context, host_id: str = typer.Argument("")):
        if ctx.resilient_parsing:
            return
        ctx.obj = host

    res = CliRunner().invoke(app, ["u1", "reboot", "true"])
    assert res.exit_code == 0
    assert reboot_calls == [True]
    assert close_calls == [None]

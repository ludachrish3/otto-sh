"""Unit tests for dynamic host-method CLI exposure."""
import inspect
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import typer
from typer.testing import CliRunner

from otto.cli.expose import (
    HostGroup,
    _render_result,
    collect_exposed_methods,
    exposed_cli_names,
    host_class_for_id,
    make_method_command,
)
from otto.host.unix_host import UnixHost
from otto.utils import Arg, Status, cli_exposed
from typing import Annotated


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


# ---------------------------------------------------------------------------
# make_method_command — new two-arg form
# ---------------------------------------------------------------------------

def test_make_method_command_signature_has_real_params():
    @cli_exposed
    async def reboot(self, hard: bool = False) -> tuple:
        ...
    cmd = make_method_command("reboot", reboot)
    params = inspect.signature(cmd).parameters
    assert "ctx" in params and "hard" in params
    assert params["hard"].annotation.__args__[0] is bool  # Annotated[bool, Option()]


@pytest.mark.asyncio
async def test_make_method_command_dispatches_kwargs_and_closes():
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

    @cli_exposed
    async def reboot(self, hard: bool = False):
        ...
    cmd = make_method_command("reboot", reboot)
    await cmd(_Ctx(), hard=True)
    assert seen["hard"] is True
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

    @cli_exposed
    async def reboot(self, hard: bool = False):
        ...
    cmd = make_method_command("reboot", reboot)
    with pytest.raises(typer.Exit) as ei:
        await cmd(_Ctx(), hard=False)
    assert ei.value.exit_code == 1
    host.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_make_method_command_unsupported_method_errors():
    host = AsyncMock(spec=["close", "id"])  # no 'flash_firmware' attr
    host.close = AsyncMock()

    class _Ctx:
        obj = host

    @cli_exposed
    async def flash_firmware(self, path: str):
        ...
    cmd = make_method_command("flash_firmware", flash_firmware)
    with pytest.raises(typer.Exit):
        await cmd(_Ctx(), path="/some/file")


# ---------------------------------------------------------------------------
# _render_result
# ---------------------------------------------------------------------------

def test_render_runresult_failure_exits_nonzero():
    class _RR:
        class status:
            is_ok = False
    with pytest.raises(typer.Exit):
        _render_result(_RR())


def test_render_runresult_ok_returns_silently():
    class _RR:
        class status:
            is_ok = True
    # no exception, no output
    _render_result(_RR())


def test_render_success_message_on_ok_empty_tuple():
    # Should not raise; prints the success message (visual output tested via capsys in
    # a richer harness; here we just confirm no exception).
    _render_result((Status.Success, ""), success="Transfer complete.")


def test_render_failure_tuple_exits_nonzero():
    with pytest.raises(typer.Exit) as ei:
        _render_result((Status.Failed, "bad"))
    assert ei.value.exit_code == 1


# ---------------------------------------------------------------------------
# Positional-argument end-to-end smoke tests (Refinement #3)
# ---------------------------------------------------------------------------

def test_positional_scalar_arg_routes_correctly():
    """A no-default positional param becomes a CLI Argument and parses correctly."""

    @cli_exposed
    async def greet(self, name: str) -> tuple:
        ...

    seen: dict = {}

    class _Host:
        id = "h1"

        async def greet(self, name: str):
            seen["name"] = name
            return (Status.Success, "")

        async def close(self):
            pass

    host = _Host()

    app = typer.Typer()

    @app.callback(invoke_without_command=True)
    def main(ctx: typer.Context):
        ctx.obj = host

    cmd_fn = make_method_command("greet", greet)
    from otto.utils import async_typer_command  # noqa: PLC0415
    app.command("greet")(async_typer_command(cmd_fn))

    r = CliRunner().invoke(app, ["greet", "Alice"])
    assert r.exit_code == 0, r.output
    assert seen["name"] == "Alice"


def test_variadic_then_scalar_routes_correctly():
    """Variadic list arg followed by a no-default scalar routes positionals correctly."""
    from typing import Annotated, Sequence

    from otto.utils import Arg

    @cli_exposed
    async def transfer(
        self,
        sources: Annotated[str | Sequence[str], Arg(variadic=True, elem_type=str)],
        dest: str,
    ) -> tuple:
        ...

    seen: dict = {}

    class _Host:
        id = "h1"

        async def transfer(self, sources: list, dest: str):
            seen["sources"] = sources
            seen["dest"] = dest
            return (Status.Success, "")

        async def close(self):
            pass

    host = _Host()

    app = typer.Typer()

    @app.callback(invoke_without_command=True)
    def main(ctx: typer.Context):
        ctx.obj = host

    cmd_fn = make_method_command("transfer", transfer)
    from otto.utils import async_typer_command  # noqa: PLC0415
    app.command("transfer")(async_typer_command(cmd_fn))

    r = CliRunner().invoke(app, ["transfer", "A", "B", "DEST"])
    assert r.exit_code == 0, r.output
    assert seen["sources"] == ["A", "B"]
    assert seen["dest"] == "DEST"


# ---------------------------------------------------------------------------
# Class scoping and HostGroup tests
# ---------------------------------------------------------------------------

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


def test_run_and_login_exposed_on_base_host():
    """login and run appear in collect_exposed_methods(BaseHost)."""
    from otto.host.host import BaseHost

    base = collect_exposed_methods(BaseHost)
    # login maps to the 'interact' attribute; run maps to 'run'
    assert "login" in base
    assert base["login"] == "interact"
    assert "run" in base
    assert base["run"] == "run"


def test_run_cli_binding_markers():
    """build_cli_binding resolves the @cli_exposed markers on BaseHost.run.

    Validates:
    - cmds is a positional variadic list[str]
    - timeout is an option
    - sudo is an option (flag-style, bool default False)
    - expects and log are in binding.excluded
    """
    import inspect
    import typer

    from otto.cli.param_synth import build_cli_binding
    from otto.host.host import BaseHost

    binding = build_cli_binding(BaseHost.run)

    param_names = [p.name for p in binding.params]
    by_name = {p.name: p for p in binding.params}

    # cmds: variadic list[str] positional
    assert "cmds" in param_names
    cmds_p = by_name["cmds"]
    # Annotated[list[str], typer.Argument(...)] — origin is list or annotation is Annotated
    ann_args = getattr(cmds_p.annotation, "__args__", ())
    assert ann_args and ann_args[0] == list[str], f"Expected list[str] base, got {ann_args}"
    meta = getattr(cmds_p.annotation, "__metadata__", ())
    assert any(isinstance(m, typer.models.ArgumentInfo) for m in meta), "cmds must be a positional Argument"

    # timeout: option
    assert "timeout" in param_names
    timeout_p = by_name["timeout"]
    timeout_meta = getattr(timeout_p.annotation, "__metadata__", ())
    assert any(isinstance(m, typer.models.OptionInfo) for m in timeout_meta), "timeout must be an Option"

    # sudo: option (bool with default False — synthesised as a flag)
    assert "sudo" in param_names
    sudo_p = by_name["sudo"]
    assert sudo_p.default is False
    sudo_meta = getattr(sudo_p.annotation, "__metadata__", ())
    assert any(isinstance(m, typer.models.OptionInfo) for m in sudo_meta), "sudo must be an Option"

    # expects and log are excluded
    assert "expects" in binding.excluded, "expects must be excluded"
    assert "log" in binding.excluded, "log must be excluded"
    assert binding.excluded["log"] is True  # default value preserved
    assert binding.excluded["expects"] is None  # default value preserved


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

    # hard: bool = False → Annotated[bool, typer.Option()] → --hard/--no-hard flag
    res = CliRunner().invoke(app, ["u1", "reboot", "--hard"])
    assert res.exit_code == 0, res.output
    assert reboot_calls == [True]
    assert close_calls == [None]


# ---------------------------------------------------------------------------
# Task 7: concrete put/get @cli_exposed decoration
# ---------------------------------------------------------------------------

def test_put_get_success_markers():
    """UnixHost.put/__cli_success__ == 'Transfer complete.' and get == 'Download complete.'"""
    from otto.host.unix_host import UnixHost

    assert getattr(UnixHost.put, "__cli_success__", None) == "Transfer complete."
    assert getattr(UnixHost.get, "__cli_success__", None) == "Download complete."


def test_put_get_in_collect_exposed_all_four():
    """put and get appear in collect_exposed_methods for all four concrete host classes."""
    from otto.host.docker_host import DockerContainerHost
    from otto.host.embedded_host import EmbeddedHost
    from otto.host.local_host import LocalHost
    from otto.host.unix_host import UnixHost

    for cls in (UnixHost, LocalHost, DockerContainerHost, EmbeddedHost):
        exposed = set(collect_exposed_methods(cls))
        assert "put" in exposed, f"{cls.__name__} missing 'put'"
        assert "get" in exposed, f"{cls.__name__} missing 'get'"


def test_build_cli_binding_unix_put():
    """build_cli_binding(UnixHost.put) produces correct positional variadic + excluded show_progress."""
    from otto.cli.param_synth import build_cli_binding
    from otto.host.unix_host import UnixHost

    binding = build_cli_binding(UnixHost.put)
    param_names = [p.name for p in binding.params]

    # src_files: variadic positional list[Path]
    assert "src_files" in param_names
    src_p = next(p for p in binding.params if p.name == "src_files")
    ann_args = getattr(src_p.annotation, "__args__", ())
    assert ann_args and ann_args[0] == list[Path], f"Expected list[Path] base, got {ann_args}"

    # dest_dir: plain positional (no default → Argument)
    assert "dest_dir" in param_names

    # show_progress: excluded
    assert "show_progress" in binding.excluded
    assert binding.excluded["show_progress"] is True


def test_build_cli_binding_unix_get():
    """build_cli_binding(UnixHost.get) produces correct positional variadic + excluded show_progress."""
    from otto.cli.param_synth import build_cli_binding
    from otto.host.unix_host import UnixHost

    binding = build_cli_binding(UnixHost.get)
    param_names = [p.name for p in binding.params]

    assert "src_files" in param_names
    src_p = next(p for p in binding.params if p.name == "src_files")
    ann_args = getattr(src_p.annotation, "__args__", ())
    assert ann_args and ann_args[0] == list[Path], f"Expected list[Path] base, got {ann_args}"

    assert "dest_dir" in param_names
    assert "show_progress" in binding.excluded


def test_build_cli_binding_embedded_put():
    """build_cli_binding(EmbeddedHost.put) resolves type hints correctly via __future__ annotations."""
    from otto.cli.param_synth import build_cli_binding
    from otto.host.embedded_host import EmbeddedHost

    binding = build_cli_binding(EmbeddedHost.put)
    param_names = [p.name for p in binding.params]

    assert "src_files" in param_names
    src_p = next(p for p in binding.params if p.name == "src_files")
    ann_args = getattr(src_p.annotation, "__args__", ())
    assert ann_args and ann_args[0] == list[Path], f"Expected list[Path] base, got {ann_args}"

    assert "dest_dir" in param_names
    assert "show_progress" in binding.excluded


def test_build_cli_binding_docker_put_no_show_progress():
    """build_cli_binding(DockerContainerHost.put) has no show_progress (docker doesn't have it)."""
    from otto.cli.param_synth import build_cli_binding
    from otto.host.docker_host import DockerContainerHost

    binding = build_cli_binding(DockerContainerHost.put)
    param_names = [p.name for p in binding.params]

    assert "src_files" in param_names
    assert "dest_dir" in param_names
    assert "show_progress" not in binding.excluded
    assert "show_progress" not in param_names


# ---------------------------------------------------------------------------
# Task 8: ls path and power state stay positional
# ---------------------------------------------------------------------------

def test_ls_path_and_power_state_real_binding():
    """build_cli_binding on the REAL PosixFileOps.ls / BaseHost.power proves Arg() markers."""
    import typer

    from otto.cli.param_synth import build_cli_binding
    from otto.host.file_ops import PosixFileOps
    from otto.host.host import BaseHost

    # PosixFileOps.ls — path must be a positional Argument with default "."
    ls_binding = build_cli_binding(PosixFileOps.ls)
    ls_by_name = {p.name: p for p in ls_binding.params}
    assert "path" in ls_by_name, "path must be in ls binding params"
    path_p = ls_by_name["path"]
    path_meta = getattr(path_p.annotation, "__metadata__", ())
    assert any(isinstance(m, typer.models.ArgumentInfo) for m in path_meta), (
        "ls path must carry ArgumentInfo (positional), not OptionInfo"
    )
    assert path_p.default == ".", f"ls path default must be '.', got {path_p.default!r}"

    # BaseHost.power — state must be a positional Argument with default None
    power_binding = build_cli_binding(BaseHost.power)
    power_by_name = {p.name: p for p in power_binding.params}
    assert "state" in power_by_name, "state must be in power binding params"
    state_p = power_by_name["state"]
    state_meta = getattr(state_p.annotation, "__metadata__", ())
    assert any(isinstance(m, typer.models.ArgumentInfo) for m in state_meta), (
        "power state must carry ArgumentInfo (positional), not OptionInfo"
    )
    assert state_p.default is None, f"power state default must be None, got {state_p.default!r}"


def test_ls_path_stays_positional_and_power_state_positional(monkeypatch):
    """End-to-end: ls /var/log --all and power on route correctly through HostGroup."""
    captured: dict = {}

    class _H:
        id = "h1"

        @cli_exposed
        async def ls(self, path: "Annotated[str, Arg()]" = ".", all: bool = False):
            captured["ls"] = (path, all)
            return ["x"]

        @cli_exposed
        async def power(self, state: "Annotated[str | None, Arg()]" = None):
            captured["power"] = state
            return (Status.Success, "")

        async def close(self):
            pass

    import otto.host.os_profile as op
    monkeypatch.setattr(op, "_HOST_CLASSES", {"h": _H})
    monkeypatch.setattr("otto.cli.expose.host_class_for_id", lambda hid: _H)
    app = typer.Typer(name="host", cls=HostGroup)
    host = _H()

    @app.callback(invoke_without_command=True)
    def main(ctx: typer.Context, host_id: str = typer.Argument("")):
        if ctx.resilient_parsing:
            return
        ctx.obj = host

    res_ls = CliRunner().invoke(app, ["h1", "ls", "/var/log", "--all"])
    assert res_ls.exit_code == 0, res_ls.output
    assert captured["ls"] == ("/var/log", True)

    res_power = CliRunner().invoke(app, ["h1", "power", "on"])
    assert res_power.exit_code == 0, res_power.output
    assert captured["power"] == "on"


def test_class_for_skips_host_build_during_completion(monkeypatch):
    """During shell completion (``ctx.resilient_parsing``), verb scoping must NOT
    build the host: ``_class_for`` returns ``None`` (→ the full unscoped menu) so
    completion never pays the ``get_host`` lab-load/host-construction cost.
    """
    from otto.cli import expose as expose_mod

    calls: list[str | None] = []
    monkeypatch.setattr(
        expose_mod, "host_class_for_id", lambda hid: calls.append(hid) or None
    )
    grp = HostGroup(name="host")

    class _CompletionCtx:
        resilient_parsing = True
        params = {"host_id": "u1"}

    assert grp._class_for(_CompletionCtx()) is None
    assert calls == []  # host not resolved/built while completing

    class _DispatchCtx:
        resilient_parsing = False
        params = {"host_id": "u1"}

    grp._class_for(_DispatchCtx())
    assert calls == ["u1"]  # real dispatch still resolves the class for scoping


# ---------------------------------------------------------------------------
# Task 1: Per-class CLI parsers
# ---------------------------------------------------------------------------

def test_class_command_builds_parser_from_the_given_class():
    """The same verb name on two classes yields parsers shaped by each class."""
    from typing import Annotated
    from otto.cli.expose import HostGroup
    from otto.utils import cli_exposed, Arg, Opt

    class HostX:
        @cli_exposed
        async def frob(self, target: Annotated[str, Arg()]) -> None:
            ...

    class HostY:
        @cli_exposed
        async def frob(self, target: Annotated[str | None, Opt()] = None) -> None:
            ...

    g = HostGroup(name="host")
    cmd_x = g._class_command(HostX, "frob", "frob")
    cmd_y = g._class_command(HostY, "frob", "frob")
    px = {p.name: p for p in cmd_x.params}
    py = {p.name: p for p in cmd_y.params}
    assert px["target"].param_type_name == "argument"   # required positional
    assert py["target"].param_type_name == "option"      # --target


def test_class_command_caches_per_class_and_verb():
    from typing import Annotated
    from otto.cli.expose import HostGroup
    from otto.utils import cli_exposed, Arg

    class HostX:
        @cli_exposed
        async def frob(self, target: Annotated[str, Arg()]) -> None:
            ...

    g = HostGroup(name="host")
    first = g._class_command(HostX, "frob", "frob")
    second = g._class_command(HostX, "frob", "frob")
    assert first is second  # cached, not rebuilt


def test_get_command_uses_resolved_class_parser(monkeypatch):
    from typing import Annotated
    from unittest.mock import MagicMock
    from otto.cli.expose import HostGroup
    from otto.utils import cli_exposed, Opt

    class HostY:
        @cli_exposed
        async def frob(self, target: Annotated[str | None, Opt()] = None) -> None:
            ...

    g = HostGroup(name="host")
    monkeypatch.setattr(g, "_class_for", lambda ctx: HostY)
    cmd = g.get_command(MagicMock(), "frob")
    params = {p.name: p for p in cmd.params}
    assert params["target"].param_type_name == "option"


# ---------------------------------------------------------------------------
# Task 5: Embedded load/unload CLI retrofit
# ---------------------------------------------------------------------------

def test_embedded_and_unix_load_have_per_class_signatures():
    """Same verb name, divergent signatures: embedded `load` requires a
    positional `name`; unix `load` exposes it as the `--name` option."""
    from otto.cli.expose import HostGroup
    from otto.host.embedded_host import ZephyrHost
    from otto.host.unix_host import UnixHost

    g = HostGroup(name="host")
    emb = {p.name: p for p in g._class_command(ZephyrHost, "load", "load").params}
    unix = {p.name: p for p in g._class_command(UnixHost, "load", "load").params}
    assert emb["name"].param_type_name == "argument"   # embedded: required positional
    assert emb["name"].required is True
    assert unix["name"].param_type_name == "option"      # unix: --name


def test_embedded_load_unload_are_cli_exposed():
    from otto.cli.expose import collect_exposed_methods
    from otto.host.embedded_host import ZephyrHost
    verbs = collect_exposed_methods(ZephyrHost)
    assert "load" in verbs and "unload" in verbs

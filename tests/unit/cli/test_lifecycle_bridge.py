"""The leaf-invoke lifecycle bridge: registration IS the opt-in (wave 2).

Pins the user contract from the command-lifecycle-uniformity spec: a
third-party author writes a plain ``async def`` on their own Typer app,
registers it, and the leaf-invoke wrapper bridges the un-awaited coroutine
typer returns through ``run_command`` — full policy, zero knowledge of it.

These tests drive the PRODUCTION seam (``resolve_spec_command`` +
``wrap_leaf_callbacks``, exactly what the root dispatch composes) rather
than a bare sub-app: a bare app never passes through the wrapper, which is
the documented loud-failure mode, not the contract under test. Invocation
uses ``cmd.main(standalone_mode=False)`` — the wrapped object is a command
from typer's vendored click fork, so the real click's ``CliRunner`` is not
a safe driver for it.
"""

import asyncio
import functools
import inspect

import pytest
import typer

from otto.cli.invoke import wrap_leaf_callbacks
from otto.cli.registry import CommandSpec, resolve_spec_command
from tests._fixtures.bootstrapstub import bootstrap_stub


@pytest.fixture
def run_command_recorder(monkeypatch):
    """Count run_command entries while still executing the coroutine.

    Patches ``otto.lifecycle.run_command`` — both the bridge and a
    self-bridging wrapper resolve it lazily from the module at call time,
    so every policy entry in this process lands here.
    """
    from otto import lifecycle

    calls: list[object] = []

    def _recording_run_command(coro, **kwargs):
        calls.append(coro)
        return asyncio.run(coro)

    monkeypatch.setattr(lifecycle, "run_command", _recording_run_command)
    return calls


@pytest.fixture
def _quiet_preamble(monkeypatch):
    """Neutralize the preamble's process-level legs for a lab_free spec.

    ``command_preamble`` on a ``lab_free`` spec runs only the bootstrap
    error gate; stub it so these tests need no discovery environment.
    """

    import otto.bootstrap as bootstrap_mod

    monkeypatch.setattr(bootstrap_mod, "bootstrap", bootstrap_stub)


def _wrapped(spec: CommandSpec):
    return wrap_leaf_callbacks(resolve_spec_command(spec), spec)


def _invoke(cmd, args: list[str]):
    return cmd.main(args, prog_name=cmd.name or "x", standalone_mode=False)


@pytest.mark.usefixtures("_quiet_preamble")
def test_plain_async_leaf_runs_under_run_command(run_command_recorder, capsys):
    """The acceptance criterion: plain async def + registration = full policy."""
    app = typer.Typer()

    @app.command()
    async def deploy(target: str = "prod") -> None:
        """Deploy."""
        typer.echo(f"deployed {target}")

    spec = CommandSpec(name="deploy-tool", loader=app, lab_free=True, output_dir=False)
    cmd = _wrapped(spec)
    _invoke(cmd, ["--target", "staging"])
    assert capsys.readouterr().out == "deployed staging\n", "the coroutine must actually run"
    assert len(run_command_recorder) == 1, "exactly one policy entry for one invocation"


@pytest.mark.usefixtures("_quiet_preamble")
def test_async_function_loader_bridged_without_self_wrap(run_command_recorder, capsys):
    """The registry's function-loader lane rides the same bridge (no self-wrap)."""

    async def hello(name: str = "world") -> None:
        """Say hello."""
        typer.echo(f"hi {name}")

    spec = CommandSpec(name="hello", loader=hello, lab_free=True, output_dir=False)
    cmd = _wrapped(spec)
    _invoke(cmd, ["--name", "otto"])
    assert capsys.readouterr().out == "hi otto\n"
    assert len(run_command_recorder) == 1


@pytest.mark.usefixtures("_quiet_preamble")
def test_self_wrapped_leaf_enters_policy_exactly_once(run_command_recorder, capsys):
    """A leaf that already bridges itself into the policy is not double-bridged.

    The retired ``@async_typer_command`` migration pattern — and any
    third-party sync wrapper that drives ``run_command`` itself — returns a
    plain value from a sync callback, so the bridge's coroutine check skips
    it: one run_command entry, not two (a second would be a nested
    asyncio.run, a hard error).
    """
    app = typer.Typer()

    async def _body() -> None:
        typer.echo("legacy ran")

    @functools.wraps(_body)
    def _self_bridged() -> None:
        from otto import lifecycle

        return lifecycle.run_command(_body())

    app.command("legacy")(_self_bridged)

    spec = CommandSpec(name="legacy", loader=app, lab_free=True, output_dir=False)
    cmd = _wrapped(spec)
    _invoke(cmd, [])
    assert capsys.readouterr().out == "legacy ran\n"
    assert len(run_command_recorder) == 1


@pytest.mark.usefixtures("_quiet_preamble")
def test_sync_leaf_skips_the_bridge(run_command_recorder, capsys):
    """A sync leaf (which may drive run_command itself, per phase) is untouched."""
    app = typer.Typer()

    @app.command()
    def status() -> None:
        """Status."""
        typer.echo("ok")

    spec = CommandSpec(name="status", loader=app, lab_free=True, output_dir=False)
    cmd = _wrapped(spec)
    _invoke(cmd, [])
    assert capsys.readouterr().out == "ok\n"
    assert run_command_recorder == []


@pytest.mark.usefixtures("_quiet_preamble")
def test_interrupt_exit_code_propagates_through_the_bridge(monkeypatch):
    """SystemExit(128+n) from the policy surfaces unchanged at the new seam.

    The other bridge tests replace run_command with a bare asyncio.run, so
    this pins the exit-code half of the contract against typer/vendored-click
    upgrades growing an except clause that could swallow it.
    """
    from otto import lifecycle

    def _interrupted_run_command(coro, **kwargs):
        coro.close()
        raise SystemExit(130)

    monkeypatch.setattr(lifecycle, "run_command", _interrupted_run_command)
    app = typer.Typer()

    @app.command()
    async def watch() -> None:
        """Watch."""

    spec = CommandSpec(name="watch", loader=app, lab_free=True, output_dir=False)
    cmd = _wrapped(spec)
    with pytest.raises(SystemExit) as excinfo:
        _invoke(cmd, [])
    assert excinfo.value.code == 130


def test_async_group_callback_rejected_at_wrap_time():
    """A group callback can never reach the bridge (typer discards its return
    value), so an async one must fail LOUDLY at wrap time, naming the group —
    not silently no-op with exit 0."""
    app = typer.Typer(name="tool")

    @app.callback()
    async def shared(verbose: bool = False) -> None:
        """Shared options."""

    @app.command()
    def go() -> None:
        """Go."""

    spec = CommandSpec(name="tool", loader=app, lab_free=True, output_dir=False)
    with pytest.raises(TypeError, match=r"async group callback.*'tool'"):
        _wrapped(spec)


def test_sync_group_callback_passes_wrap_time():
    """The rejection is differential: the same shape with a plain def wraps fine."""
    app = typer.Typer(name="tool2")

    @app.callback()
    def shared(verbose: bool = False) -> None:
        """Shared options."""

    @app.command()
    def go() -> None:
        """Go."""

    spec = CommandSpec(name="tool2", loader=app, lab_free=True, output_dir=False)
    assert _wrapped(spec) is not None


def test_unwrapped_async_leaf_fails_loud_not_silent():
    """Outside otto's dispatch a bare async leaf must NOT silently no-op.

    The spec's inverted failure mode: the un-awaited coroutine escapes as the
    callback's return value (surfacing as a RuntimeWarning at GC in real
    use). Assert the observable half of the contract — the body never ran —
    plus the mechanism (the invoke result IS a coroutine), so this test
    fails if typer ever starts awaiting callbacks itself (at which point the
    bridge and this test should both be revisited).
    """
    app = typer.Typer()
    ran: list[str] = []

    @app.command()
    async def ghost() -> None:
        """Ghost."""
        ran.append("ran")

    cmd = typer.main.get_command(app)  # deliberately NOT wrapped
    result = cmd.main([], prog_name="ghost", standalone_mode=False)
    assert ran == [], "the body must not run without the bridge"
    assert inspect.iscoroutine(result)
    result.close()  # avoid the un-awaited-coroutine RuntimeWarning in-harness

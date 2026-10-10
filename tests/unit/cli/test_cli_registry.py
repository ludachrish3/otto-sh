"""CLI command registry: spec storage, lazy loaders, collision policy."""

import contextlib
import dataclasses
import io
import sys

import pytest
import typer

from otto.cli.registry import (
    CLI_COMMANDS,
    CommandSpec,
    cli_command,
    register_cli_command,
    resolve_spec_command,
)
from otto.registry import DuplicateRegistration
from tests._fixtures.bootstrapstub import bootstrap_stub
from tests._fixtures.registrant import from_module


@pytest.fixture(autouse=True)
def _clean_registry():
    before = set(CLI_COMMANDS.names())
    yield
    for name in list(CLI_COMMANDS.names()):
        if name not in before:
            CLI_COMMANDS.unregister(name)


def test_register_multi_command_typer_app_resolves_to_group():
    sub = typer.Typer(name="mytool")

    @sub.command()
    def status() -> None:
        """Show status."""
        typer.echo("ok")

    @sub.command()
    def reset() -> None:
        """Reset."""
        typer.echo("reset")

    register_cli_command("mytool", sub, help="My tool.")
    spec = CLI_COMMANDS.get("mytool")
    assert spec.help == "My tool."
    cmd = resolve_spec_command(spec)
    assert "status" in cmd.commands  # a multi-command app stays a group
    assert "reset" in cmd.commands


def test_single_command_typer_app_flattens_like_typer_native():
    # Mirrors Typer's native rule: a one-command, callback-free, subgroup-free
    # app collapses into a bare leaf under the spec name (the `monitor` shape),
    # NOT a group with a nested same-named subcommand.
    sub = typer.Typer(name="solo")

    @sub.command()
    def go(count: int = 1) -> None:
        """Go."""
        typer.echo(str(count))

    register_cli_command("solo", sub, help="Solo.")
    cmd = resolve_spec_command(CLI_COMMANDS.get("solo"))
    assert not hasattr(cmd, "commands"), "single-command app must flatten to a leaf"
    assert cmd.name == "solo"
    param_names = {p.name for p in cmd.params}
    assert "count" in param_names  # the leaf's own option, surfaced directly
    # completion belongs to the root app, not the flattened leaf
    assert "install_completion" not in param_names
    assert "show_completion" not in param_names


def test_register_function_resolves_to_command():
    async def hello(name: str = "world") -> None:
        """Say hello."""
        typer.echo(f"hi {name}")

    register_cli_command("hello", hello, help="Say hello.")
    cmd = resolve_spec_command(CLI_COMMANDS.get("hello"))
    assert not hasattr(cmd, "commands")  # a leaf command, not a group


def test_lazy_module_attr_loader_imports_only_on_resolve(tmp_path, monkeypatch, purge_tmp_imports):
    mod_dir = tmp_path / "fake_pkg"
    mod_dir.mkdir()
    (mod_dir / "__init__.py").write_text("")
    (mod_dir / "cmds.py").write_text(
        "import typer\n"
        "lazy_app = typer.Typer(name='lazy')\n"
        "@lazy_app.command()\n"
        "def go() -> None:\n"
        "    '''Go.'''\n"
        "    typer.echo('went')\n"
        "@lazy_app.command()\n"  # two commands keep it a group (not flattened)
        "def stop() -> None:\n"
        "    '''Stop.'''\n"
        "    typer.echo('stopped')\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    register_cli_command("lazy", "fake_pkg.cmds:lazy_app", help="Lazy.")
    assert "fake_pkg.cmds" not in sys.modules  # registration alone imports nothing
    cmd = resolve_spec_command(CLI_COMMANDS.get("lazy"))
    assert "fake_pkg.cmds" in sys.modules
    assert "go" in cmd.commands


class TestLiveAppHelpFallback:
    """``help=`` omitted for a LIVE Typer app → the app's own help flows into
    the spec (the single source of truth for root help + the completion cache).
    Lazy string loaders have nothing to read without importing — they keep
    ``help=None`` and render the placeholder."""

    def test_app_help_flows_into_spec(self):
        app = typer.Typer(name="mytool", help="My tool does things.")

        @app.command()
        def one() -> None: ...

        @app.command()
        def two() -> None: ...

        register_cli_command("mytool", app)
        assert CLI_COMMANDS.get("mytool").help == "My tool does things."

    def test_group_callback_docstring_flows_into_spec(self):
        app = typer.Typer(name="cbtool")

        @app.callback()
        def main() -> None:
            """Callback-doc help."""

        @app.command()
        def sub() -> None: ...

        register_cli_command("cbtool", app)
        assert CLI_COMMANDS.get("cbtool").help == "Callback-doc help."

    def test_flattened_single_command_app_uses_its_commands_help(self):
        # The monitor shape: the flattened leaf IS the command, so its help
        # (here the function docstring) is what native add_typer would show.
        app = typer.Typer(name="solo")

        @app.command()
        def solo() -> None:
            """Run the solo thing."""

        register_cli_command("solo", app)
        assert CLI_COMMANDS.get("solo").help == "Run the solo thing."

    def test_explicit_help_wins_over_app_help(self):
        app = typer.Typer(help="App help.")

        @app.command()
        def c() -> None: ...

        register_cli_command("expl", app, help="Explicit help.")
        assert CLI_COMMANDS.get("expl").help == "Explicit help."

    def test_string_loader_without_help_stays_none(self):
        register_cli_command("lazystr", "fake_pkg.nonexistent:app")
        assert CLI_COMMANDS.get("lazystr").help is None

    def test_helpless_app_stays_none(self):
        app = typer.Typer(name="bare")

        @app.command()
        def x() -> None: ...

        @app.command()
        def y() -> None: ...

        register_cli_command("bare", app)
        assert CLI_COMMANDS.get("bare").help is None


def test_prepare_command_target_is_idempotent_by_contract():
    """Double preparation must be identity, not a lucky no-op.

    The dispatch path prepares @cli_command targets twice (decoration +
    resolve_spec_command's function-loader branch). The sentinel guarantees
    the second pass returns the SAME object even if the wrappers ever stop
    erasing their own triggers (e.g. _inject_ctx preserving annotations).
    """
    from otto.cli.invoke import prepare_command_target
    from otto.context import OttoContext

    async def cmd(ctx: OttoContext, who: str = "x") -> None: ...

    prepared = prepare_command_target(cmd)
    assert prepared is not cmd  # ctx injection actually wrapped it
    assert prepare_command_target(prepared) is prepared


def test_prepare_command_target_refuses_a_verb_it_cannot_bind():
    """Only ``run`` and the verb-less shape have a call-time binding.

    Another verb's flags would be added to the signature and then never bound,
    so the command is refused where it is built -- an already-prepared
    callable too, which the idempotency short-circuit would otherwise hand
    back unchanged.
    """
    from otto.cli.invoke import prepare_command_target
    from otto.context import OttoContext

    async def cmd(who: str = "x") -> None: ...

    with pytest.raises(ValueError, match="no call-time binding for verb 'test'"):
        prepare_command_target(cmd, verb="test")

    async def with_ctx(ctx: OttoContext, who: str = "x") -> None: ...

    prepared = prepare_command_target(with_ctx)
    assert prepared is not with_ctx  # ctx injection wrapped it, so it carries the sentinel
    with pytest.raises(ValueError, match="no call-time binding for verb 'test'"):
        prepare_command_target(prepared, verb="test")


def test_collision_is_loud_and_names_both_origins():
    from_module("acme.cli", register_cli_command, "clash", typer.Typer(name="clash"))
    with pytest.raises(DuplicateRegistration) as ei:
        from_module("acme.other", register_cli_command, "clash", typer.Typer(name="clash"))
    assert str(ei.value) == (
        "CLI command 'clash' is already registered by 'acme.cli'; second registration "
        "from 'acme.other'. Pass overwrite=True to replace it deliberately."
    )


def test_cli_command_decorator_registers_and_runs(monkeypatch):
    @cli_command(name="greet", help="Greet.", lab_free=True)
    async def greet(who: str = "world") -> None:
        """Greet someone."""
        typer.echo(f"hello {who}")

    spec = CLI_COMMANDS.get("greet")
    # Drive the PRODUCTION dispatch shape: resolve + wrap_leaf_callbacks. An
    # async leaf is deliberately inert outside the wrapper since the wave-2
    # lifecycle bridge (resolve_spec_command no longer self-wraps loaders) —
    # tests/unit/cli/test_lifecycle_bridge.py pins that loud-failure contract.

    from otto import bootstrap as bootstrap_mod
    from otto.cli.invoke import wrap_leaf_callbacks

    monkeypatch.setattr(bootstrap_mod, "bootstrap", bootstrap_stub)
    cmd = wrap_leaf_callbacks(resolve_spec_command(spec), spec)

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), pytest.raises(SystemExit) as ei:
        cmd.main(args=["--who", "bob"], prog_name="greet", standalone_mode=True)
    assert ei.value.code == 0
    assert "hello bob" in buf.getvalue()


def test_spec_defaults():
    register_cli_command("d", typer.Typer(name="d"))
    spec = CLI_COMMANDS.get("d")
    assert spec.lab_free is False
    assert spec.output_dir is True
    assert spec.gate is True
    assert CLI_COMMANDS.origin("d") == __name__


def test_command_spec_is_frozen():
    spec = CommandSpec(name="x", loader=None)
    with pytest.raises(dataclasses.FrozenInstanceError):
        spec.name = "y"  # ty: ignore[invalid-assignment]


# ── The lab-bound-must-be-async rule ─────────────────────────────────────────


def test_lab_bound_cli_command_rejects_a_sync_handler():
    """The same hole `@instruction` closed, one decorator over.

    A sync `@cli_command` that calls `ctx.all_hosts()` registers every host
    into a scope that is never entered, so nothing sweeps them — and the
    guide's own canonical example is a lab-touching `ping`.
    """
    with pytest.raises(TypeError, match=r"lab-bound command.*async def"):

        @cli_command(name="_unit_sync_labbound")
        def _unit_sync_labbound() -> None:  # pragma: no cover — never registered
            pass

    assert "_unit_sync_labbound" not in CLI_COMMANDS


def test_lab_free_cli_command_may_be_sync():
    """The other side of the line, and the reason it is `lab_free` and not
    "which decorator": a command that touches no hosts has nothing to sweep,
    so sync is exactly right for it."""

    @cli_command(name="_unit_sync_labfree", lab_free=True)
    def _unit_sync_labfree() -> None:
        pass

    assert "_unit_sync_labfree" in CLI_COMMANDS


def test_run_is_registered_with_async_leaves_and_test_is_not():
    """The flag on the REAL registration, which the seam tests cannot see.

    They build a local lane app and pass `async_leaves=True` themselves, so
    dropping it from `builtin_commands.py` left the entire suite green while
    `otto run` silently stopped enforcing anything.

    `test` deliberately does NOT carry it: every `otto test <Suite>` leaf is
    sync because `pytest.main` is.
    """
    from otto.cli.builtin_commands import register_builtin_commands

    register_builtin_commands()
    assert CLI_COMMANDS.get("run").async_leaves is True
    assert CLI_COMMANDS.get("test").async_leaves is False

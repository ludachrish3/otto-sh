"""CLI command registry: spec storage, lazy loaders, collision policy."""

import contextlib
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


def test_lazy_module_attr_loader_imports_only_on_resolve(tmp_path, monkeypatch):
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


def test_collision_is_loud_and_names_both_origins():
    register_cli_command("clash", typer.Typer(name="clash"))
    with pytest.raises(ValueError, match="already registered") as ei:
        register_cli_command("clash", typer.Typer(name="clash"))
    msg = str(ei.value)
    # CLI commands have no overwrite escape hatch — the collision hint must not
    # point at the (nonexistent) overwrite= knob.
    assert "CLI command names cannot be overwritten; pick a unique name." in msg
    assert "overwrite=True" not in msg


def test_cli_command_decorator_registers_and_runs():
    @cli_command(name="greet", help="Greet.")
    async def greet(who: str = "world") -> None:
        """Greet someone."""
        typer.echo(f"hello {who}")

    spec = CLI_COMMANDS.get("greet")
    cmd = resolve_spec_command(spec)

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
    assert spec.origin  # auto-captured


def test_command_spec_is_frozen():
    spec = CommandSpec(name="x", loader=None)
    with pytest.raises(Exception):  # noqa: B017, PT011 — dataclasses.FrozenInstanceError
        spec.name = "y"  # ty: ignore[invalid-assignment]

"""The root group never serves a replaced command stale (R §3.2)."""

import typer

from otto.cli.main import app
from otto.cli.registry import CLI_COMMANDS, cli_command, register_cli_command


def _group():
    return typer.main.get_command(app)


def _leaf(text: str):
    sub = typer.Typer(name="swap")

    @sub.command(help=f"Say {text}.")
    def go() -> None:
        typer.echo(text)

    return sub


def test_a_replaced_command_is_rebuilt():
    register_cli_command("swap", _leaf("one"), lab_free=True)
    group = _group()
    ctx = group.make_context("otto", ["--help"], resilient_parsing=True)
    assert group._dispatch_target(ctx) is None  # not the dispatch target: the stub cache
    first = group.get_command(ctx, "swap")
    assert "Say one." in first.help
    register_cli_command("swap", _leaf("two"), lab_free=True, overwrite=True)
    second = group.get_command(ctx, "swap")
    assert second is not first
    assert "Say two." in second.help


def test_a_replaced_dispatch_target_is_rebuilt():
    """The real-command cache, which the dispatch target resolves through, is dropped too."""
    register_cli_command("swap", _leaf("one"), lab_free=True)
    group = _group()
    ctx = group.make_context("otto", ["swap"], resilient_parsing=True)
    assert group._dispatch_target(ctx) == "swap"
    first = group.get_command(ctx, "swap")
    assert first.help == "Say one."
    register_cli_command("swap", _leaf("two"), lab_free=True, overwrite=True)
    second = group.get_command(ctx, "swap")
    assert second is not first
    assert second.help == "Say two."


def test_an_unchanged_table_reuses_the_cached_command():
    register_cli_command("keep", _leaf("one"), lab_free=True)
    group = _group()
    ctx = group.make_context("otto", ["--help"], resilient_parsing=True)
    assert group.get_command(ctx, "keep") is group.get_command(ctx, "keep")


def test_the_decorator_credits_the_decorating_module():
    @cli_command(name="credited", lab_free=True)
    def credited() -> None: ...

    assert CLI_COMMANDS.origin("credited") == __name__

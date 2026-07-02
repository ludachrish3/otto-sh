"""e2e fixture: third-party top-level CLI commands (spec 2026-07-01)."""

import typer

from otto import cli_command, register_cli_command


@cli_command(name="e2e-hello", help="Print a plugin greeting.", lab_free=True)
async def e2e_hello(who: str = "world") -> None:
    """Print a plugin greeting."""
    typer.echo(f"hello {who}")


e2etool = typer.Typer(name="e2etool", help="Plugin tool group.")


@e2etool.command()
def ping() -> None:
    """Pong."""
    typer.echo("pong")


@e2etool.command()
def pong() -> None:
    """Ping."""
    # A second command keeps e2etool a genuine GROUP (a single-command app
    # flattens into a bare leaf, per resolve_spec_command), so this fixture
    # exercises the group-loader dispatch path `otto e2etool <subcommand>`.
    typer.echo("ping")


register_cli_command("e2etool", e2etool, help="Plugin tool group.", lab_free=True)

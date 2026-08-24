"""``otto env`` — build and maintain this workspace's orchestration venv.

Thin by design: every decision lives in :mod:`otto.env`, and this module is
the Typer surface plus the presentation. The one thing it owns outright is the
``--`` passthrough, which is otto's FIRST: ``ignore_unknown_options`` on the
sub-app plus a ``list[str] | None`` Argument is what turns
``otto env sync -- --no-index --find-links ../wheels`` into three verbatim
installer arguments, and it composes with a ``--backend`` before the ``--``.
There is no other site in otto to copy this from.

Both verbs are LAB-FREE and act on the DISCOVERED repo set rather than the
active one: an environment is workspace-scoped, so which labs today's command
happens to load must not change what gets installed into it.
"""

import logging
from typing import TYPE_CHECKING, Annotated

import typer

if TYPE_CHECKING:
    from ..env import EnvBuild

logger = logging.getLogger(__name__)

env_app = typer.Typer(
    name="env",
    no_args_is_help=True,
    # ignore_unknown_options is what lets `--` hand the rest to the installer.
    context_settings={"help_option_names": ["-h", "--help"], "ignore_unknown_options": True},
    help="Create and maintain this workspace's orchestration environment.",
)

_BACKEND = Annotated[
    str | None,
    typer.Option("--backend", help="Installer to use: uv or pip. Overrides [env] backend."),
]
_INSTALLER_ARGS = Annotated[
    list[str] | None,
    typer.Argument(help="After `--`, arguments passed verbatim to the installer."),
]


def _report(build: "EnvBuild", *, verb: str) -> None:
    """Print what the run did, and the line that gets the operator into the env."""
    from ..env.backends import activation_line

    typer.echo(f"{verb} {build.env}")
    for name in build.installed:
        typer.echo(f"  installed (editable): {name}")
    for name in build.skipped:
        typer.echo(f"  skipped, no pyproject.toml: {name}")
    typer.echo(f"  backend: {build.meta.backend}")
    typer.echo(f"\nActivate it with:\n  {activation_line(build.env)}")


@env_app.command("create")
def create(
    backend: _BACKEND = None,
    force: Annotated[
        bool, typer.Option("--force", help="Remove an existing environment and rebuild it.")
    ] = False,
    installer_args: _INSTALLER_ARGS = None,
) -> None:
    """Build this workspace's orchestration environment from scratch."""
    from ..env import create_env

    build = create_env(force=force, backend_flag=backend, passthrough=list(installer_args or []))
    _report(build, verb="created")


@env_app.command("sync")
def sync(
    backend: _BACKEND = None,
    installer_args: _INSTALLER_ARGS = None,
) -> None:
    """Bring this workspace's orchestration environment up to date."""
    from ..env import sync_env

    build = sync_env(backend_flag=backend, passthrough=list(installer_args or []))
    _report(build, verb="synced")


@env_app.command("show")
def show() -> None:
    """Report this workspace's orchestration environment and what is in it."""
    from rich import box
    from rich.console import Console
    from rich.table import Table

    from ..env import env_status

    status = env_status()
    console = Console()

    if not status.exists:
        typer.echo(f"no environment for this workspace ({status.env})")
        typer.echo("build one with:\n  otto env create")
        return

    typer.echo(f"environment: {status.env}")
    if status.meta is None:
        typer.echo("  metadata:    unreadable — `otto env create --force` rebuilds it")
    else:
        typer.echo(f"  backend:     {status.meta.backend}")
        typer.echo(f"  otto:        {status.meta.otto_version}")

    table = Table(box=box.ROUNDED)
    table.add_column("repo")
    table.add_column("distribution")
    table.add_column("installed")
    table.add_column("state")
    for repo in status.repos:
        if repo.dist_name is None:
            installed, state = "—", "no pyproject (libs ride sys.path)"
        else:
            installed = {True: "yes", False: "no", None: "?"}[repo.installed]
            state = "stale — run `otto env sync`" if repo.stale else "current"
        table.add_row(repo.name, repo.dist_name or "—", installed, state)
    console.print(table)

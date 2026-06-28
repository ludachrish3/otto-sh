"""
otto docker — build images and bring up/down compose stacks on lab hosts.

Subcommands:
    otto docker build [--rebuild] [--on <host>] [<image>...]
    otto docker up    [--on <host>]
    otto docker down  [--on <host>]
    otto docker ps    [--on <host>]

All four are thin wrappers around the library API in :mod:`otto.docker`,
which is also what instructions and suites import directly.
"""

from __future__ import annotations

from typing import Annotated

import typer
from rich import print as rprint
from rich.table import Table

from ..configmodule import Repo, get_lab, get_repos
from ..context import get_context
from ..docker import (
    build_images,
    compose_down,
    compose_ps,
    compose_up,
    get_user_compose_project,
)
from ..host.unix_host import UnixHost
from ..logger import get_otto_logger, management
from ..utils import Status, async_typer_command

logger = get_otto_logger()

docker_app = typer.Typer(
    name="docker",
    help="Build images and orchestrate compose stacks on docker-capable lab hosts.",
    no_args_is_help=True,
    context_settings={
        "help_option_names": ["-h", "--help"],
    },
)


@docker_app.callback()
def docker_callback(ctx: typer.Context) -> None:
    """Build images and orchestrate compose stacks on docker-capable lab hosts."""
    if ctx.resilient_parsing:
        return
    # Mirror run/host/test/cov: set up this invocation's output directory
    # (which also prunes old logs per the retention policy), only for a real
    # subcommand — never on group ``--help``/no-args.
    if ctx.invoked_subcommand is not None:
        get_context().output_dir = management.create_output_dir("docker", ctx.invoked_subcommand)


def _docker_host_completer(ctx: typer.Context, incomplete: str) -> list[str]:  # noqa: ARG001 — required by Typer autocompletion callback signature
    """Shell-completion source for ``--on``. Limits suggestions to
    docker-capable hosts so users don't tab into a parent that can't run
    containers.

    Prefers the cached entry written by the slow path
    (``cache['docker_hosts']``); falls through to a live ``hosts.json``
    scan on cache miss so first-run completion still works.
    """
    from ..configmodule import get_completion_names, get_repos
    from ..configmodule.completion_cache import collect_docker_capable_host_ids

    cached = get_completion_names()
    if cached is not None and isinstance(cached.get("docker_hosts"), list):
        ids = cached["docker_hosts"]
    else:
        ids = collect_docker_capable_host_ids(get_repos())

    return sorted(h for h in ids if h.startswith(incomplete))


def _select_repos(repo_name: str | None, on: str | None = None):
    """Filter loaded repos by name AND by lab applicability.

    A repo is "applicable" if either:
      - the user passed --on <host> and that host is in the active lab, or
      - one of the repo's [[docker.composes]].default_host values is in
        the active lab.

    A multi-repo workspace can declare docker stacks on hosts that belong to
    different labs (e.g. repo1 → veggies/pepper_seed, repo2 → fruits/grape_seed).
    Only one lab is active per `otto` invocation, so iterating over a repo
    whose target host isn't loaded yields a confusing "not in lab" error.
    Filter those out and let the user know via a DEBUG log.

    If *on* is explicitly provided but does not name a host in the active
    lab, that's a user error — fail fast rather than silently skipping
    every repo and exiting 0.
    """
    lab = get_lab()

    if on is not None and on not in lab.hosts:
        rprint(
            f"[red]--on {on!r} is not a host in the active lab {lab.name!r}. "
            f"Available hosts: {sorted(lab.hosts)}"
        )
        raise typer.Exit(1)

    docker_repos = [
        r for r in get_repos() if r.docker_settings.composes or r.docker_settings.images
    ]
    if repo_name is not None:
        matches = [r for r in docker_repos if r.name == repo_name]
        if not matches:
            rprint(f"[red]No loaded repo named {repo_name!r} with a [docker] section.")
            raise typer.Exit(1)
        docker_repos = matches

    applicable: list[Repo] = []
    for r in docker_repos:
        # Lab applicability is determined by the repo's declared default_hosts,
        # not by --on. --on is a runtime override of where to deploy, not a
        # signal of which repos belong to the active lab — using [on] here
        # would incorrectly keep every repo whenever the override is in lab.
        candidates: list[str] = [
            c.default_host for c in r.docker_settings.composes if c.default_host
        ]
        # A repo with no candidate hosts at all (no default_host) is kept —
        # _resolve_parent will surface a clear error of its own.
        if not candidates or any(c in lab.hosts for c in candidates):
            applicable.append(r)
        else:
            logger.debug(
                f"docker: skipping repo {r.name!r} — its docker hosts "
                f"{candidates} are not in active lab {lab.name!r}"
            )
    return applicable


def _resolve_parent_for_repo(repo, lab, on: str | None) -> UnixHost:
    """Reuse compose._resolve_parent — public via private import to avoid duplicate logic."""
    from ..docker.compose import _resolve_parent

    return _resolve_parent(repo, lab, on, list(repo.docker_settings.composes))


async def _build(
    repo: Annotated[
        str | None, typer.Option("--repo", help="Restrict to a single repo by name.")
    ] = None,
    on: Annotated[
        str | None,
        typer.Option(
            "--on", help="Lab host id to build on.", autocompletion=_docker_host_completer
        ),
    ] = None,
    rebuild: Annotated[
        bool, typer.Option("--rebuild", help="Force rebuild even if context-hash tag exists.")
    ] = False,
    image: Annotated[
        list[str] | None, typer.Argument(help="Image names to build (default: all).")
    ] = None,
) -> None:
    """Build docker images declared in selected repos."""
    lab = get_lab()
    selected_repos = _select_repos(repo, on=on)
    any_failed = False
    for r in selected_repos:
        if not r.docker_settings.images:
            continue
        parent = _resolve_parent_for_repo(r, lab, on)
        results = await build_images(r, parent, image_names=image, rebuild=rebuild)
        for name, (status, msg) in results.items():
            if status is Status.Skipped:
                rprint(f"[dim]{r.name}/{name}: cached → {msg}")
            elif status is Status.Success:
                rprint(f"[green]{r.name}/{name}: built → {msg}")
            else:
                any_failed = True
                rprint(f"[red]{r.name}/{name}: FAILED\n{msg}")
    if any_failed:
        raise typer.Exit(1)


async def _up(
    repo: Annotated[
        str | None, typer.Option("--repo", help="Restrict to a single repo by name.")
    ] = None,
    on: Annotated[
        str | None,
        typer.Option(
            "--on", help="Lab host id to compose on.", autocompletion=_docker_host_completer
        ),
    ] = None,
    no_build: Annotated[
        bool, typer.Option("--no-build", help="Skip the implicit build step before compose up.")
    ] = False,
) -> None:
    """Bring up compose stacks for selected repos and register their containers.

    By default each repo's declared images are built first (idempotent via
    the context-hash skip). Pass --no-build if your compose file references
    only published images and otto's build step is unnecessary.
    """
    lab = get_lab()
    selected_repos = _select_repos(repo, on=on)
    for r in selected_repos:
        if not r.docker_settings.composes:
            continue
        hosts = await compose_up(r, lab, on=on, build=not no_build)
        proj = get_user_compose_project(r.name)
        rprint(f"[green]{r.name} ({proj}): {len(hosts)} container(s) registered:")
        for host in hosts.values():
            rprint(f"  - {host.id}  →  {host.container_id[:12]}")


async def _down(
    repo: Annotated[
        str | None, typer.Option("--repo", help="Restrict to a single repo by name.")
    ] = None,
    on: Annotated[
        str | None,
        typer.Option(
            "--on", help="Lab host id to compose on.", autocompletion=_docker_host_completer
        ),
    ] = None,
) -> None:
    """Tear down compose stacks for selected repos."""
    lab = get_lab()
    selected_repos = _select_repos(repo, on=on)
    any_failed = False
    for r in selected_repos:
        if not r.docker_settings.composes:
            continue
        status = await compose_down(r, lab, on=on)
        if status is Status.Skipped:
            rprint(f"[dim]{r.name}: nothing to tear down.")
        elif status.is_ok:
            rprint(f"[green]{r.name}: stack down.")
        else:
            any_failed = True
            rprint(f"[red]{r.name}: tear-down reported {status}.")
    if any_failed:
        raise typer.Exit(1)


async def _ps(
    on: Annotated[
        str | None,
        typer.Option(
            "--on",
            help="Specific docker-capable host to query (default: all).",
            autocompletion=_docker_host_completer,
        ),
    ] = None,
) -> None:
    """List running containers on docker-capable lab hosts."""
    lab = get_lab()
    parents: list[UnixHost] = []
    if on:
        host = lab.hosts.get(on)
        if not isinstance(host, UnixHost) or not host.docker_capable:
            rprint(f"[red]{on!r} is not a docker-capable lab host.")
            raise typer.Exit(1)
        parents = [host]
    else:
        parents = [h for h in lab.hosts.values() if isinstance(h, UnixHost) and h.docker_capable]

    table = Table("host", "container_id", "image", "status", "names")
    for parent in parents:
        rows = await compose_ps(parent)
        for row in rows:
            table.add_row(
                parent.id,
                str(row.get("ID", ""))[:12],
                str(row.get("Image", "")),
                str(row.get("Status", "")),
                str(row.get("Names", "")),
            )
    rprint(table)


docker_app.command(name="build")(async_typer_command(_build))
docker_app.command(name="up")(async_typer_command(_up))
docker_app.command(name="down")(async_typer_command(_down))
docker_app.command(name="ps")(async_typer_command(_ps))

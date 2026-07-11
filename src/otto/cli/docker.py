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

import logging
from typing import Annotated, Any

import typer
from rich import print as rprint
from rich.table import Table

from ..config import Repo, get_lab, get_repos
from ..config.lab import Lab
from ..docker import (
    build_images,
    compose_down,
    compose_ps,
    compose_up,
    get_user_compose_project,
)
from ..host.unix_host import UnixHost
from ..utils import Status, async_typer_command

logger = logging.getLogger(__name__)

docker_app = typer.Typer(
    name="docker",
    help="Build images and orchestrate compose stacks on docker-capable lab hosts.",
    no_args_is_help=True,
    context_settings={
        "help_option_names": ["-h", "--help"],
    },
)


# Read-only docker subcommands that produce no artifacts → no output dir.
_NO_OUTPUT_DIR_SUBCOMMANDS = frozenset({"ps"})


@docker_app.callback()
def docker_callback(ctx: typer.Context) -> None:
    """Build images and orchestrate compose stacks on docker-capable lab hosts.

    Output-dir creation moved to the shared leaf-invoke
    :func:`~otto.cli.invoke.command_preamble`; the read-only ``ps`` leaf opts
    out via its ``__cli_output_dir__ = False`` marker (see below), so a
    ``--help`` invocation can never create a spurious dir.
    """
    if ctx.resilient_parsing:
        return


def _docker_host_completer(ctx: typer.Context, incomplete: str) -> list[str]:  # noqa: ARG001 — required by Typer autocompletion callback signature
    """Shell-completion source for ``--on``.

    Limits suggestions to docker-capable hosts so users don't tab into a parent that can't run
    containers.

    Prefers the cached entry written by the slow path
    (``cache['docker_hosts']``); falls through to a live ``lab.json``
    scan on cache miss so first-run completion still works.
    """
    from ..config import get_completion_names, get_repos
    from ..config.completion_cache import collect_docker_capable_host_ids

    cached = get_completion_names()
    if cached is not None and isinstance(cached.get("docker_hosts"), list):
        ids = cached["docker_hosts"]
    else:
        ids = collect_docker_capable_host_ids(get_repos())

    return sorted(h for h in ids if h.startswith(incomplete))


def _select_repos(repo_name: str | None, on: str | None = None) -> list[Repo]:
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


def _resolve_parent_for_repo(repo: Repo, lab: Lab, on: str | None) -> UnixHost:
    """Reuse compose._resolve_parent — public via private import to avoid duplicate logic."""
    from ..docker.compose import _resolve_parent

    return _resolve_parent(repo, lab, on, list(repo.docker_settings.composes))


def _canonicalize_on(lab: Lab, on: str | None) -> str | None:
    """Resolve a ``--on`` CLI value to its canonical host id.

    ``--on`` is a CLI host-id INPUT — like the ``otto host`` positional and
    ``--hop`` — so per the host-id rules it accepts both canonical ids and
    positional element-slug handles (``dut1``). Everything downstream
    (``_select_repos``'s ``lab.hosts`` membership check, ``_resolve_parent``'s
    ``lab.hosts[...]`` lookup) is canonical-id-only, so resolve the handle
    here, once, at the CLI boundary — never pass a raw handle further in.
    """
    if on is None:
        return None
    host = lab.resolve_handle(on)
    if host is None:
        rprint(
            f"[red]--on {on!r} is not a host in the active lab {lab.name!r}. "
            f"Available hosts: {sorted(lab.hosts)}"
        )
        raise typer.Exit(1)
    return host.id


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
    on = _canonicalize_on(lab, on)
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
    on = _canonicalize_on(lab, on)
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
    on = _canonicalize_on(lab, on)
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
        # --on is a CLI host-id input: accept a canonical id or a positional
        # handle (e.g. dut1), same as `otto host`.
        host = lab.resolve_handle(on)
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


# Read-only docker subcommands (`ps`) produce no artifacts → opt them out of
# the per-command output dir. The leaf-invoke preamble reads `__cli_output_dir__`
# off the command callback (default True); `functools.wraps` in
# async_typer_command carries the marker through to the wrapper. This keeps
# `_NO_OUTPUT_DIR_SUBCOMMANDS` the single source of truth for the policy.
_DOCKER_SUBCOMMANDS: dict[str, Any] = {
    "build": _build,
    "up": _up,
    "down": _down,
    "ps": _ps,
}
for _sub_name, _sub_fn in _DOCKER_SUBCOMMANDS.items():
    if _sub_name in _NO_OUTPUT_DIR_SUBCOMMANDS:
        _sub_fn.__cli_output_dir__ = False
    docker_app.command(name=_sub_name)(async_typer_command(_sub_fn))

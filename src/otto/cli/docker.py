"""
otto docker — build images and deploy use-case stacks on lab hosts.

Subcommands::

    otto docker use-cases       [USE_CASE]
    otto docker build           [IMAGE...] --on H [--repo NAME] [--rebuild]
    otto docker compose build   [USE_CASE [IMAGE...]] [--on H] [--provide CAP=REPO] [--rebuild]
    otto docker compose up      [USE_CASE [SERVICE...]] [--on H] [--no-build]
                                 [--provide CAP=REPO] [--env K=V] [--env-file FILE]
    otto docker compose down    [USE_CASE [SERVICE...]] [--on H] [--provide CAP=REPO]
    otto docker ps              [--on H]

``compose build``/``compose up``/``compose down`` speak USE-CASES (spec §10): one named,
cross-repo deployment resolved by the provider competition (§4) and placed by
role (§5), not a per-repo loop over ``[[docker.composes]]``. ``build`` builds
images only: it stages a repo's declared images onto one lab host and knows
nothing about a composition, so it takes ``--on`` (required), never a
use-case; ``ps`` is unchanged.

Every leaf is a thin wrapper around the library API in :mod:`otto.docker`,
which is also what instructions and suites import directly.

IMPORT BUDGET: everything from :mod:`otto.docker` and the host classes is
imported FUNCTION-SCOPE, in the command that uses it. A module-scope import
would put the compose and build machinery and the whole Unix host stack on the
``otto docker --help`` path, which ``tests/unit/import_budget`` gates.
"""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Literal, TypeVar

import typer
from rich import print as rprint
from rich.markup import escape

from ..config import get_repos
from ..utils import Status
from .completers import completion_source
from .invoke import fail, print_error

if TYPE_CHECKING:
    from collections.abc import Coroutine

    from ..config.repo import DockerUseCase
    from ..docker.deployment import UseCaseStack
    from ..docker.reports import BuildReport, HostReport
    from ..docker.resolve import Displacement

_T = TypeVar("_T")

docker_app = typer.Typer(
    name="docker",
    help=(
        "Build images (`build`), inspect (`ps`, `use-cases`) and run use-case stacks (`compose`)."
    ),
    no_args_is_help=True,
    context_settings={
        "help_option_names": ["-h", "--help"],
    },
)

compose_app = typer.Typer(
    name="compose",
    help="Build, deploy and tear down use-case stacks (docker compose, one layer up).",
    no_args_is_help=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)
docker_app.add_typer(compose_app, name="compose")


@docker_app.callback()
def docker_callback(ctx: typer.Context) -> None:
    """Build images, inspect, and run use-case stacks via ``compose``.

    Output-dir creation moved to the shared leaf-invoke
    :func:`~otto.cli.invoke.command_preamble`; the read-only ``ps`` leaf opts
    out via its ``__cli_output_dir__ = False`` marker (see below), so a
    ``--help`` invocation can never create a spurious dir.
    """
    if ctx.resilient_parsing:
        return


@completion_source(kind="payload", key="docker_hosts", lab_scoped=True, intersect=True, sort=True)
def _docker_host_completer(ctx: typer.Context, incomplete: str) -> list[str]:
    """Shell-completion source for ``--on``.

    Limits suggestions to docker-capable hosts so users don't tab into a parent that can't run
    containers. Lab-scoped like every other host-id completer (issue #138):
    when a lab is selected, only docker-capable hosts in that lab are offered.

    Prefers the cached entry written by the slow path
    (``cache['docker_hosts']``); falls through to a live ``lab.json``
    scan on cache miss so first-run completion still works.
    """
    from ..config import get_completion_names, get_repos
    from ..config.completion_cache import collect_docker_capable_host_ids
    from .completers import lab_scoped_host_ids, selected_lab_names

    cached = get_completion_names()
    if cached is not None and isinstance(cached.get("docker_hosts"), list):
        ids = cached["docker_hosts"]
    else:
        ids = collect_docker_capable_host_ids(get_repos())

    if selected_lab_names(ctx):
        in_lab = set(lab_scoped_host_ids(ctx))
        ids = [h for h in ids if h in in_lab]

    return sorted(h for h in ids if h.startswith(incomplete))


@completion_source(kind="payload", key="docker_use_cases", sort=True)
def _use_case_completer(ctx: typer.Context, incomplete: str) -> list[str]:  # noqa: ARG001 — required by Typer autocompletion callback signature
    """Shell-completion source for the ``USE_CASE`` positional.

    Mirrors :func:`_docker_host_completer`'s cache/fallback shape exactly:
    prefer the entry the slow path wrote (``cache['docker_use_cases']``), fall
    through to a live scan of the active repos' ``[[docker.use_cases]]`` on a
    miss so first-run completion still works. Not lab-scoped, unlike the host
    completer: a use-case is declared by REPOS, and which lab is selected
    decides where its fragments land, not whether it exists.
    """
    from ..config import get_completion_names, get_repos
    from ..config.completion_cache import collect_docker_use_case_names

    cached = get_completion_names()
    if cached is not None and isinstance(cached.get("docker_use_cases"), list):
        names = cached["docker_use_cases"]
    else:
        names = collect_docker_use_case_names(get_repos())

    return sorted(n for n in names if n.startswith(incomplete))


def _default_use_case(use_case: str | None) -> str:
    """Resolve an omitted ``USE_CASE`` positional, or refuse loudly.

    An EXPLICIT name is passed straight through, unchecked: ``select_fragments``
    owns "no active repo declares that" and phrases it with the declared set,
    and checking it twice would give one mistake two different messages
    depending on which verb the user typed.

    Omitting it is only unambiguous when exactly one use-case is declared.
    Zero and many are both hard errors (exit 1) rather than a quiet no-op —
    the same loudness contract every build verb's refusal carries one layer
    down (:class:`~otto.docker.build_verbs.DockerBuildError`).
    """
    if use_case is not None:
        return use_case

    from ..docker.resolve import declared_use_cases

    names = sorted(declared_use_cases(get_repos()))
    if not names:
        fail(
            "otto docker: no active repo declares [[docker.use_cases]] — there is "
            "nothing to deploy. Declare a [[docker.use_cases]] fragment in a repo's "
            ".otto/settings.toml (see the docker guide), or name a repo's compose "
            "stack through the library API."
        )
    if len(names) > 1:
        fail(
            f"otto docker: {len(names)} use-cases are declared ({', '.join(names)}) — "
            f"name the one you mean, e.g. `otto docker compose up {names[0]}`."
        )
    return names[0]


def _parse_pairs(values: "list[str] | None", *, form: str, flag: str) -> dict[str, str]:
    """Split repeatable ``K=V`` CLI values on the FIRST ``=``.

    First ``=`` only, so a value may contain more of them (a URL, a base64
    blob). A missing ``=``, or an empty key, is a USAGE error — ``typer``'s
    ``BadParameter`` — not a silently dropped argument: the user asked for
    something otto could not read, and exit 2 with the form spelled out is the
    honest answer.
    """
    out: dict[str, str] = {}
    for raw in values or ():
        key, sep, value = raw.partition("=")
        if not sep or not key.strip():
            raise typer.BadParameter(f"{raw!r} is not a {form} pair; write it as {flag}.")
        out[key.strip()] = value
    return out


def _parse_provide(values: "list[str] | None") -> dict[str, str]:
    """``--provide edge=repo1`` -> ``{"edge": "repo1"}`` (spec §4's tie knob)."""
    return _parse_pairs(values, form="CAPABILITY=REPO", flag="--provide CAPABILITY=REPO")


def _parse_env(values: "list[str] | None") -> dict[str, str]:
    """``--env K=V`` -> ``{"K": "V"}`` (spec §6's last merge layer)."""
    return _parse_pairs(values, form="KEY=VALUE", flag="--env KEY=VALUE")


def _print_displacements(displaced: "list[Displacement]") -> None:
    """Name every fragment the provider competition excluded (spec §4).

    For the verbs whose library call has no log line of its own (``use-cases``
    and the build verbs); ``compose up`` leaves it to ``deploy``'s log line,
    which reaches the console at the default log level. The sentence is
    :meth:`~otto.docker.resolve.Displacement.describe`'s.
    """
    for d in displaced:
        line = f"docker: {d.describe()}"
        rprint(f"[yellow]{escape(line)}")


def _print_stack_report(stack: "UseCaseStack") -> None:
    """Report what :func:`~otto.docker.deployment.deploy` registered, per host."""
    if not stack.by_host:
        # Not reachable from a resolvable selection (a winner always
        # participates, and a `services=` narrowing that matches nothing is
        # refused in the library) — but if it ever is, it says so rather than
        # exiting 0 having printed nothing at all.
        msg = escape(f"docker: {stack.use_case} registered no container on any host")
        rprint(f"[yellow]{msg}")
        return
    for host_id, hosts in stack.by_host.items():
        project = stack.projects.get(host_id, "?")
        headline = (
            f"{stack.use_case} on {host_id} ({project}): {len(hosts)} container(s) registered:"
        )
        rprint(f"[green]{escape(headline)}")
        for host in hosts.values():
            rprint(f"  - {escape(host.id)}  →  {escape(host.container_id[:12])}")


class _Declined:
    """The type of :data:`_DECLINED`.

    A class, not a bare ``object()``, so the verbs' ``isinstance`` narrowing
    tells a type checker which arm holds the library's value and which holds
    the sentinel.
    """


_DECLINED = _Declined()
"""Sentinel distinguishing "a dry run declined" from a verb that returns None."""


_BUILD_FLAGS: "dict[str, str]" = {"host": "--on", "repo": "--repo", "images": "IMAGE"}


async def _run_docker(action: "Coroutine[Any, Any, _T]") -> "_T | _Declined":
    """Await a docker library call, rendering its three refusal shapes.

    ``deploy``/``teardown``/the build verbs refuse in exactly three ways, and
    they mean different things to the process:

    * :class:`~otto.result.CommandNotRunError` — a dry run's decline. It is the
      ANSWER the user asked for (it carries spec §12's resolved plan and, for
      ``deploy``, the exact compose command), so it prints and exits 0. Caught
      here rather than left to the boundary frame, which would print it as
      ``error:`` and exit 1 — turning a successful preview into a failure.
    * :class:`~otto.docker.resolve.UseCaseResolutionError` — a configuration
      refusal (an ``--on`` naming no lab host, a provider tie, an unresolvable
      role). The LIBRARY's phrase is what reaches the user, verbatim: this
      layer keeps no second copy that could drift from it.
    * :class:`~otto.docker.build_verbs.DockerBuildError` — a build verb's
      input refusal, field-named. Spelled in this command's flags at this one
      site via :func:`~otto.cli.invoke.usage_error_from` (exit 2), the way
      ``otto test`` spells a bad ``--cov-dir``.

    Returns the call's value, or :data:`_DECLINED` when it declined. A
    sentinel, not a bare ``object()`` or ``None``, so a caller's
    ``isinstance(report, _Declined)`` narrowing is unambiguous regardless of
    what the library call itself returns on success.
    """
    from ..docker.build_verbs import DockerBuildError
    from ..docker.resolve import UseCaseResolutionError
    from ..host.host import is_dry_run
    from ..result import CommandNotRunError
    from .invoke import usage_error_from

    try:
        return await action
    except CommandNotRunError as e:
        if not is_dry_run():
            # CHECKED, not assumed. Exit 0 is right BECAUSE this is a dry run;
            # every raise site of this class is a dry-run arm today, but that
            # is a fact about the library right now, not a property of the
            # class. If one ever escapes a REAL deployment, swallowing it here
            # would report success for a stack nobody brought up.
            raise
        rprint(f"[magenta]{escape(str(e))}[/magenta]")
        return _DECLINED
    except UseCaseResolutionError as e:
        fail(e)
    except DockerBuildError as e:
        raise usage_error_from(e, flags=_BUILD_FLAGS) from e


def _render_build_report(report: "BuildReport") -> None:
    """Print a build report one image per line; exit 1 when any image failed."""
    _print_displacements(report.displaced)
    for entry in report.repos:
        if entry.kind == "no_images":
            msg = (
                f"docker: {entry.repo} declares no [[docker.images]] — "
                f"nothing to build on {entry.host}"
            )
            rprint(f"[yellow]{escape(msg)}")
            continue
        for name, res in entry.images.items():
            # `value` on every branch: the tag on ok, the captured build output
            # on failure. Never `msg` — an exec-produced CommandResult leaves it
            # empty, so reading it here would print nothing at all.
            if res.status is Status.Skipped:
                line = escape(f"{entry.repo}/{name}: cached → {res.value}")
                rprint(f"[dim]{line} ({entry.host})")
            elif res.status is Status.Success:
                line = escape(f"{entry.repo}/{name}: built → {res.value}")
                rprint(f"[green]{line} ({entry.host})")
            else:
                print_error(f"{entry.repo}/{name}: FAILED on {entry.host}\n{res.value}")
    if not report.ok:
        raise typer.Exit(1)


def _render_host_report(report: "HostReport", *, done: str) -> None:
    """Print one line per host: ``<host>: <done>`` or its first failed command; exit 1 on any."""
    for host, results in report.hosts.items():
        bad = [r for r in results if not r.is_ok]
        if bad:
            print_error(f"{host}: FAILED — {bad[0].command}: {bad[0].value}")
        else:
            rprint(f"[green]{escape(f'{host}: {done}')}")
    if not report.ok:
        raise typer.Exit(1)


async def _build(
    image: Annotated[
        list[str] | None, typer.Argument(help="Declared image names to build (default: all).")
    ] = None,
    on: Annotated[
        str | None,
        typer.Option(
            "--on",
            help="The docker-capable lab host to build on. Required.",
            autocompletion=_docker_host_completer,
        ),
    ] = None,
    repo: Annotated[
        str | None, typer.Option("--repo", help="Restrict to a single repo by name.")
    ] = None,
    rebuild: Annotated[
        bool, typer.Option("--rebuild", help="Force rebuild even if a context-hash tag exists.")
    ] = False,
) -> None:
    """Build the selected repos' declared images on one lab host.

    Like `docker build`, this knows nothing about a composition: it needs a
    host, not a use-case, so --on is required. To build the images a
    deployment would use, on the hosts it would use, run
    `otto docker compose build`.
    """
    from ..docker.build_verbs import build_on

    report = await _run_docker(build_on(on, repo=repo, images=image, rebuild=rebuild))
    if not isinstance(report, _Declined):
        _render_build_report(report)


async def _compose_build(
    use_case: Annotated[
        str | None,
        typer.Argument(
            help="Use-case whose images to build (default: the only one declared).",
            autocompletion=_use_case_completer,
        ),
    ] = None,
    image: Annotated[
        list[str] | None,
        typer.Argument(
            help="Declared image names to build, over the use-case's winners (default: all)."
        ),
    ] = None,
    on: Annotated[
        str | None,
        typer.Option(
            "--on",
            help="Collapse every fragment onto this lab host, as `compose up` does.",
            autocompletion=_docker_host_completer,
        ),
    ] = None,
    provide: Annotated[
        list[str] | None,
        typer.Option("--provide", help="Break a provider tie: CAPABILITY=REPO. Repeatable."),
    ] = None,
    rebuild: Annotated[
        bool, typer.Option("--rebuild", help="Force rebuild even if a context-hash tag exists.")
    ] = False,
) -> None:
    """Build the images a deployment of a use-case would use, where it would use them.

    The same provider competition and placement `otto docker compose up` runs,
    so the images that get built are the ones deployment would use, on the
    hosts it would use.
    """
    from ..docker.build_verbs import compose_build

    provide_map = _parse_provide(provide)
    name = _default_use_case(use_case)
    report = await _run_docker(
        compose_build(name, on=on, provide=provide_map, images=image, rebuild=rebuild)
    )
    if not isinstance(report, _Declined):
        _render_build_report(report)


async def _compose_up(
    use_case: Annotated[
        str | None,
        typer.Argument(
            help="Use-case to deploy (default: the only one declared).",
            autocompletion=_use_case_completer,
        ),
    ] = None,
    service: Annotated[
        list[str] | None,
        typer.Argument(help="Restrict to these services (requires an explicit use-case)."),
    ] = None,
    on: Annotated[
        str | None,
        typer.Option(
            "--on",
            help="Collapse every fragment onto this lab host.",
            autocompletion=_docker_host_completer,
        ),
    ] = None,
    no_build: Annotated[
        bool, typer.Option("--no-build", help="Skip the implicit build step before compose up.")
    ] = False,
    provide: Annotated[
        list[str] | None,
        typer.Option("--provide", help="Break a provider tie: CAPABILITY=REPO. Repeatable."),
    ] = None,
    env: Annotated[
        list[str] | None,
        typer.Option("--env", help="Extra env var, KEY=VALUE. Repeatable; wins over all channels."),
    ] = None,
    env_file: Annotated[
        list[Path] | None,
        typer.Option(
            "--env-file",
            help="Local KEY=VALUE file merged before --env. Repeatable.",
            exists=True,
            dir_okay=False,
        ),
    ] = None,
) -> None:
    """Deploy a use-case: one merged compose stack per resolved host.

    The fragments that take part are chosen by the provider competition, placed
    by --on, a committed pin, or their role, and handed the assembled env
    mapping. Each participating repo's declared images are built first unless
    --no-build says otherwise. With no USE_CASE, the only declared one is
    deployed; naming SERVICEs narrows the deployment to them.
    """
    # --on is deliberately NOT canonicalized here. `deploy` resolves it
    # itself (it shares one pure prefix with `teardown`, so the two verbs
    # cannot disagree about where a deployment lives), and it owns the refusal
    # — so a host this lab does not have is named once, in one sentence,
    # however the deployment was reached.
    from ..docker.deployment import deploy

    provide_map = _parse_provide(provide)
    env_map = _parse_env(env)
    name = _default_use_case(use_case)
    stack = await _run_docker(
        deploy(
            name,
            services=service or None,
            on=on,
            provide=provide_map,
            env=env_map,
            env_files=env_file or None,
            build=not no_build,
        )
    )
    if not isinstance(stack, _Declined):
        _print_stack_report(stack)


async def _compose_down(
    use_case: Annotated[
        str | None,
        typer.Argument(
            help="Use-case to tear down (default: the only one declared).",
            autocompletion=_use_case_completer,
        ),
    ] = None,
    service: Annotated[
        list[str] | None,
        typer.Argument(help="Tear down only these services (requires an explicit use-case)."),
    ] = None,
    on: Annotated[
        str | None,
        typer.Option(
            "--on",
            help="Collapse every fragment onto this lab host.",
            autocompletion=_docker_host_completer,
        ),
    ] = None,
    provide: Annotated[
        list[str] | None,
        typer.Option("--provide", help="Break a provider tie: CAPABILITY=REPO. Repeatable."),
    ] = None,
) -> None:
    """Tear a use-case's stacks down and unregister their container hosts.

    --on and --provide are resolved exactly as `otto docker compose up` resolves them,
    so a teardown can never address a different project than the deployment it
    is undoing. Naming SERVICEs stops and removes just those, leaving the rest
    of the stack and its network standing.
    """
    from ..docker.deployment import teardown

    provide_map = _parse_provide(provide)
    name = _default_use_case(use_case)
    report = await _run_docker(teardown(name, services=service or None, on=on, provide=provide_map))
    if isinstance(report, _Declined):
        return
    scope = f" ({', '.join(service)})" if service else ""
    _render_host_report(report, done=f"{name}{scope} torn down")


def _use_cases(
    use_case: Annotated[
        str | None,
        typer.Argument(
            help="Show only this use-case (default: every declared one).",
            autocompletion=_use_case_completer,
        ),
    ] = None,
) -> None:
    """List declared use-cases: their fragments, where they land, env keys.

    Reads configuration only — nothing is contacted and nothing is started, so
    the answer is the same with or without --dry-run (the leaf opts out of the
    generic dry-run stop to say so). Values are never printed,
    only the env KEY names. Name a USE_CASE to see just that one.
    """
    # Selection (§4) and placement (§5) are pure, which is what lets this verb
    # exist at all. A refusal from either is REPORTED, not raised: this is an
    # inventory, and "one of your six use-cases cannot place its edge fragment"
    # is exactly the answer the user came for — not a reason to hide the other
    # five, and not a reason to exit 1.
    from rich.table import Table

    from ..config.fleet import get_lab
    from ..docker.resolve import (
        UseCaseResolutionError,
        declared_use_cases,
        resolve_placement,
        select_fragments,
    )

    repos = get_repos()
    declared = declared_use_cases(repos)
    if not declared:
        rprint(f"[yellow]{escape('otto docker: no active repo declares [[docker.use_cases]].')}")
        return

    # A filter naming nothing is a USER error about the argument, not a state
    # of the configuration — so it is loud (exit 1) even though a placement
    # that cannot resolve is only reported. Phrased like `select_fragments`'
    # own refusal, with the declared set named, so a typo reads the same
    # whichever verb found it.
    if use_case is not None and use_case not in declared:
        fail(
            f"otto docker use-cases: no active repo declares use-case "
            f"{use_case!r}; declared: {', '.join(sorted(declared))}"
        )
    names = [use_case] if use_case is not None else sorted(declared)

    lab = get_lab()
    for name in names:
        candidates = declared[name]
        problems: list[str] = []
        hosts: dict[int, str] = {}
        displaced: "list[Displacement]" = []
        excluded: set[int] = set()
        try:
            selection = select_fragments(name, repos)
        except UseCaseResolutionError as e:
            problems.append(str(e))
        else:
            displaced = selection.displaced
            # Keyed on the FRAGMENT's identity, never the SelectedFragment's:
            # `select_fragments` rebuilds its own wrappers from the same repo
            # tables, so the wrapper objects here and there are different while
            # the `DockerUseCase` inside them is one shared object.
            participating = {id(sf.fragment) for sf in selection.fragments}
            excluded = {id(sf.fragment) for sf in candidates} - participating
            try:
                placed = resolve_placement(selection, lab)
            except UseCaseResolutionError as e:
                problems.append(str(e))
            else:
                hosts = {
                    id(sf.fragment): host_id for host_id, frags in placed.items() for sf in frags
                }

        table = Table(
            "fragment",
            "role",
            "provides",
            "host",
            "env keys",
            "status",
            title=f"use-case {name}",
        )
        for sf in candidates:
            frag = sf.fragment
            provides = (
                f"{frag.provides} (priority {frag.priority})" if frag.provides is not None else "-"
            )
            table.add_row(
                # escape()d: the `repo[compose,...]` spelling is rich MARKUP
                # syntax, and an unescaped `[core]` is eaten as a style tag —
                # which rendered a repo's two fragments identically (found on
                # the live bed, T15). The other cells carry no bracket idiom.
                escape(f"{sf.repo.name}[{','.join(frag.composes)}]"),
                frag.role or "-",
                provides,
                hosts.get(id(frag), "-"),
                _env_key_names(frag),
                "displaced" if id(frag) in excluded else "",
            )
        rprint(table)
        # Below the table, not inside it: who won a capability and at what
        # priority is a sentence, and a sentence folded into an 11-column-wide
        # cell is unreadable at any terminal width.
        _print_displacements(displaced)
        for problem in problems:
            rprint(f"[yellow]{escape(problem)}")


def _env_key_names(frag: "DockerUseCase") -> str:
    """Return the fragment's env KEY names — never a value (spec §6 channels 1a+1b).

    Values are the product's business and can carry secrets pulled from the
    invoking shell; an inventory has no reason to print one. ``pass_env`` names
    are marked, because where the value comes from is the interesting half.
    """
    names = sorted(frag.env) + [f"{n} (shell)" for n in frag.pass_env]
    return ", ".join(names) or "-"


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
    from rich.table import Table

    from ..config.fleet import get_lab
    from ..docker import compose_ps
    from ..host.unix_host import UnixHost

    lab = get_lab()
    parents: list[UnixHost] = []
    if on:
        # --on is a CLI host-id input, same as `otto host`.
        host = lab.hosts.get(on)
        if not isinstance(host, UnixHost) or not host.docker_capable:
            fail(f"{on!r} is not a docker-capable lab host.")
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


@dataclass(frozen=True)
class _Verb:
    """One registered docker verb: its group, leaf and the policies the preamble reads."""

    name: str
    group: Literal["docker", "compose"]
    leaf: "Callable[..., Any]"
    output_dir: bool = True
    """False: read-only, no per-invocation output directory."""
    dry_run_preview: bool = False
    """True: the leaf owns its own ``--dry-run`` preview and so opts OUT of the seam
    default (``otto.cli.invoke.stop_at_dry_run_seam``, which otherwise prints a generic
    block and exits 0 ABOVE the leaf body). ``compose up``/``compose down`` resolve the
    whole pure half of the pipeline under a dry run and decline with spec §12's plan —
    the exact compose command included — so stopping at the seam would delete the
    preview this workstream exists to ship, and the build verbs likewise print their plan.
    ``use-cases`` opts out too: it is a read-only inventory of configuration that
    contacts no host, so its body is its own dry-run answer and prints identically
    either way. ``ps`` keeps the safe default.
    """


# The single source of truth for which docker verbs exist, which group each registers
# on, and the two per-leaf policies the leaf-invoke preamble reads off the RESOLVED
# command's callback (`__cli_output_dir__`, `__cli_dry_run_preview__`, both defaulting
# False when absent) -- typer's own callback shim functools-wraps the registered
# function, carrying the markers through.
_VERBS: "list[_Verb]" = [
    _Verb("build", "docker", _build, dry_run_preview=True),
    _Verb("ps", "docker", _ps, output_dir=False),
    _Verb("use-cases", "docker", _use_cases, output_dir=False, dry_run_preview=True),
    _Verb("build", "compose", _compose_build, dry_run_preview=True),
    _Verb("up", "compose", _compose_up, dry_run_preview=True),
    _Verb("down", "compose", _compose_down, dry_run_preview=True),
]
_GROUPS: "dict[str, typer.Typer]" = {"docker": docker_app, "compose": compose_app}

for _verb in _VERBS:
    _verb.leaf.__cli_output_dir__ = _verb.output_dir  # ty: ignore[unresolved-attribute]
    _verb.leaf.__cli_dry_run_preview__ = _verb.dry_run_preview  # ty: ignore[unresolved-attribute]
    _GROUPS[_verb.group].command(name=_verb.name)(_verb.leaf)

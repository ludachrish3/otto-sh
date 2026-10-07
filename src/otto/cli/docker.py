"""
otto docker — build images and deploy use-case stacks on lab hosts.

Subcommands::

    otto docker use-cases       [USE_CASE]
    otto docker build           [IMAGE...] [--parent H] [--repo NAME] [-t REF] [--no-cache]
                                 [--pull] [--build-arg K=V] [--target STAGE]
    otto docker compose build   [USE_CASE [IMAGE...]] [--parent H] [--provide CAP=REPO]
                                 [--no-cache] [--pull] [--build-arg K=V]
    otto docker compose up      [USE_CASE [SERVICE...]] [--parent H] [--build]
                                 [--force-recreate] [--pull POLICY]
                                 [--provide CAP=REPO] [--env K=V] [--env-file FILE]
    otto docker compose down    [USE_CASE [SERVICE...]] [--parent H] [--provide CAP=REPO]
    otto docker ps              [-a] [--parent H]
    otto docker images          [--parent H]
    otto docker logs            CONTAINER [--tail N] [--since T] [-t] [-f] [--parent H]
    otto docker compose ps      [USE_CASE] [-a] [--parent H] [--provide CAP=REPO]
    otto docker compose logs    [USE_CASE [SERVICE...]] [--tail N] [--since T] [-t] [-f]
                                 [--parent H] [--provide CAP=REPO]

``compose build``/``compose up``/``compose down`` speak USE-CASES (spec §10): one named,
cross-repo deployment resolved by the provider competition (§4) onto one
parent (§2), not a per-repo loop over ``[[docker.composes]]``. ``build`` builds
images only: it stages a repo's declared images onto one lab host and knows
nothing about a composition, so it takes a host (``--parent``), never a
use-case. ``ps``, ``images``, ``logs``, ``compose ps`` and ``compose logs`` print
docker's output as docker printed it.

Every leaf is a thin wrapper around the library API in :mod:`otto.docker`,
which is also what instructions and suites import directly.

IMPORT BUDGET: everything from :mod:`otto.docker` and the host classes is
imported FUNCTION-SCOPE, in the command that uses it. A module-scope import
would put the compose and build machinery and the whole Unix host stack on the
``otto docker --help`` path, which ``tests/unit/import_budget`` gates.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Literal, TypeVar

import typer
from rich import print as rprint
from rich.markup import escape

from .completers import completion_source
from .invoke import fail, print_error

if TYPE_CHECKING:
    from collections.abc import Coroutine

    from ..config.completion_cache import ObservedDockerState
    from ..config.repo import DockerUseCase
    from ..docker.deployment import UseCaseStack
    from ..docker.observe import LogsTarget, ObserveReport
    from ..docker.reports import BuildReport, HostReport
    from ..docker.resolve import Displacement
    from ..host.unix_host import UnixHost

logger = logging.getLogger(__name__)

_T = TypeVar("_T")

docker_app = typer.Typer(
    name="docker",
    help=(
        "Build images (`build`), inspect (`ps`, `images`, `logs`, `use-cases`) "
        "and run use-case stacks (`compose`: build, up, down, ps, logs)."
    ),
    no_args_is_help=True,
    context_settings={
        "help_option_names": ["-h", "--help"],
    },
)

compose_app = typer.Typer(
    name="compose",
    help=(
        "Build, deploy, inspect (`ps`, `logs`) and tear down use-case stacks "
        "(docker compose, one layer up)."
    ),
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
    """Shell-completion source for ``--parent``.

    Limits suggestions to docker-capable hosts so users don't tab into a parent that can't run
    containers. Lab-scoped like every other host-id completer (issue #138):
    when a lab is selected, only docker-capable hosts in that lab are offered.

    Prefers the cached entry written by the slow path
    (``cache['docker_hosts']``); falls through to a live ``lab.json``
    scan on cache miss so first-run completion still works.
    """
    from ..bootstrap import get_completion_names, get_repos
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
    from ..bootstrap import get_completion_names, get_repos
    from ..config.completion_cache import collect_docker_use_case_names

    cached = get_completion_names()
    if cached is not None and isinstance(cached.get("docker_use_cases"), list):
        names = cached["docker_use_cases"]
    else:
        names = collect_docker_use_case_names(get_repos())

    return sorted(n for n in names if n.startswith(incomplete))


def _names_or(key: str, collect: "Callable[[], Any]") -> Any:
    """Return the cached ``names`` entry for *key*, else the collector's live answer (cold run)."""
    from ..bootstrap import get_completion_names

    cached = get_completion_names()
    if cached is not None and key in cached:
        return cached[key]
    return collect()


@completion_source(kind="payload", key="docker_images", sort=True)
def _image_completer(ctx: typer.Context, incomplete: str) -> list[str]:  # noqa: ARG001 — required by Typer autocompletion callback signature
    """Shell-completion source for ``IMAGE``: every declared ``[[docker.images]]`` name."""
    from ..bootstrap import get_repos
    from ..config.completion_cache import collect_docker_image_names

    try:
        names = _names_or("docker_images", lambda: collect_docker_image_names(get_repos()))
        return sorted(n for n in names if n.startswith(incomplete))
    except Exception:  # noqa: BLE001 — completion never crashes the shell
        return []


@completion_source(
    kind="payload", key="docker_services_by_use_case", by_positional="use_case", sort=True
)
def _service_completer(ctx: typer.Context, incomplete: str) -> list[str]:
    """Shell-completion source for ``SERVICE``: the services of the ``USE_CASE`` on the line.

    Nothing is offered until a use-case is on the line (the services of an
    unnamed use-case would be a guess).
    """
    from ..bootstrap import get_repos
    from ..config.completion_cache import collect_docker_services_by_use_case

    try:
        use_case = (
            ctx.params.get("use_case") if isinstance(getattr(ctx, "params", None), dict) else None
        )
        if not isinstance(use_case, str) or not use_case:
            return []
        by_use_case = _names_or(
            "docker_services_by_use_case",
            lambda: collect_docker_services_by_use_case(get_repos()),
        )
        return sorted(s for s in by_use_case.get(use_case, []) if s.startswith(incomplete))
    except Exception:  # noqa: BLE001 — completion never crashes the shell
        return []


@completion_source(kind="payload", key="repos", sort=True)
def _repo_completer(ctx: typer.Context, incomplete: str) -> list[str]:  # noqa: ARG001 — required by Typer autocompletion callback signature
    """Shell-completion source for ``--repo``: every active repo's name."""
    from ..bootstrap import get_repos
    from ..config.completion_cache import collect_repo_names

    try:
        names = _names_or("repos", lambda: collect_repo_names(get_repos()))
        return sorted(n for n in names if n.startswith(incomplete))
    except Exception:  # noqa: BLE001 — completion never crashes the shell
        return []


def _discovered_repos() -> "list[Any]":
    """Return the active repos as discovery found them: settings and lab data, no init code.

    The observed-state readers need the repos only to reach the cache file, so
    a TAB goes through ``otto.bootstrap.discover()`` — NEVER ``get_repos()``,
    which runs bootstrap and with it the user's init code inside a TAB.
    """
    from .. import bootstrap

    return bootstrap.discover().repos


def _selected_labs_for_tab(ctx: typer.Context) -> list[str]:
    """Return the lab selection a TAB sees (``-l``/``OTTO_LAB``) -- a seam the tests patch."""
    from .completers import selected_lab_names

    return selected_lab_names(ctx)


def _default_parent_for_tab(ctx: typer.Context) -> "str | None":
    """Return the parent the verb would default to, from the cache alone (never bootstrap).

    The names cache carries ``docker_default_parent_by_lab``; the selected lab's
    entry is the answer. With no lab selected, the map's only DISTINCT value is
    the answer (two labs that default to the same host still answer it);
    labs defaulting to different hosts are a question TAB does not guess at.
    An absent entry (the rule refuses for that lab) is ``None``, and so is any
    selection that includes one: the verb runs the rule over the labs merged,
    which a refusing lab can turn into a refusal.

    The bash shim mirrors this as ``otto._shim_complete._default_parent``; change both or neither.
    """
    from ..bootstrap import get_completion_names

    cached = get_completion_names()
    by_lab = cached.get("docker_default_parent_by_lab") if cached else None
    if not isinstance(by_lab, dict):
        return None
    labs = _selected_labs_for_tab(ctx)
    if labs:
        parents = {by_lab.get(lab) for lab in labs}
        only = parents.pop() if len(parents) == 1 else None
        return only if isinstance(only, str) else None
    values = {v for v in by_lab.values() if isinstance(v, str)}
    return values.pop() if len(values) == 1 else None


def _observed_for_tab(ctx: typer.Context) -> "ObservedDockerState | None":
    """Return the parent's last-observed docker state: ``--parent`` on the line, else the default.

    Cache only -- a TAB never asks a host and never bootstraps (the repos come
    from discovery, which runs no init code).
    """
    from ..config.completion_cache import read_docker_observed

    parent = ctx.params.get("parent") if isinstance(getattr(ctx, "params", None), dict) else None
    if not isinstance(parent, str):
        parent = _default_parent_for_tab(ctx)
    if parent is None:
        return None
    return read_docker_observed(_discovered_repos(), parent)


@completion_source(kind="observed", key="containers", by_option="parent")
def _container_completer(ctx: typer.Context, incomplete: str) -> list[str]:
    """Shell-completion source for ``CONTAINER``: the parent's observed names, then ids.

    The parent is ``--parent`` when it is on the line, else the one the verb
    would default to.
    """
    try:
        state = _observed_for_tab(ctx)
        if state is None:
            return []
        return [
            c for c in [*state.container_names, *state.container_ids] if c.startswith(incomplete)
        ]
    except Exception:  # noqa: BLE001 — completion never crashes the shell
        return []


@completion_source(kind="observed", key="images", by_option="parent")
def _tag_completer(ctx: typer.Context, incomplete: str) -> list[str]:
    """Shell-completion source for ``--tag``: the references the parent's daemon last listed.

    The parent is ``--parent`` when it is on the line, else the one the verb
    would default to; an image reference is a per-daemon fact.
    """
    try:
        state = _observed_for_tab(ctx)
        if state is None:
            return []
        return [r for r in state.image_refs if r.startswith(incomplete)]
    except Exception:  # noqa: BLE001 — completion never crashes the shell
        return []


def _default_use_case(use_case: str | None) -> str:
    """Resolve an omitted ``USE_CASE`` positional, or refuse loudly.

    An EXPLICIT name is passed straight through, unchecked: ``select_fragments``
    owns "no active repo declares that" and phrases it with the declared set,
    and checking it twice would give one mistake two different messages
    depending on which verb the user typed.

    Omitting it is only unambiguous when exactly one use-case is declared.
    Zero and many are both hard errors (exit 1) rather than a quiet no-op —
    the same loudness contract every build verb's refusal carries one layer
    down (:class:`~otto.docker.observe.DockerVerbError`).
    """
    if use_case is not None:
        return use_case

    from ..bootstrap import get_repos
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


_DOCKER_FLAGS: "dict[str, str]" = {
    "parent": "--parent",
    "repo": "--repo",
    "images": "IMAGE",
    "tag": "--tag",
    "container": "CONTAINER",
    "follow": "--follow",
    "use_case": "USE_CASE",
    "services": "SERVICE",
}


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
      refusal (an unknown use-case, a provider tie, a service nothing
      declares). The LIBRARY's phrase is what reaches the user, verbatim: this
      layer keeps no second copy that could drift from it.
    * :class:`~otto.docker.observe.DockerVerbError` — a docker verb's
      input refusal, field-named. Spelled in this command's flags at this one
      site via :func:`~otto.cli.invoke.usage_error_from` (exit 2), the way
      ``otto test`` spells a bad ``--cov-dir``.

    Returns the call's value, or :data:`_DECLINED` when it declined. A
    sentinel, not a bare ``object()`` or ``None``, so a caller's
    ``isinstance(report, _Declined)`` narrowing is unambiguous regardless of
    what the library call itself returns on success.
    """
    from ..docker.observe import DockerVerbError
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
    except DockerVerbError as e:
        raise usage_error_from(e, flags=_DOCKER_FLAGS) from e


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
        for name, built in entry.images.items():
            if built.is_ok:
                refs = ", ".join(built.references)
                line = escape(f"{entry.repo}/{name}: built {refs}  {built.image_id}")
                rprint(f"[green]{line}  ({entry.host})")
            else:
                failed = f"{entry.repo}/{name}: FAILED on {entry.host}"
                if built.result.command:
                    failed += f"\n{built.result.command}"
                print_error(f"{failed}\n{built.result.value}")
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


def _build_hosts(report: "BuildReport") -> "list[str]":
    """Return the hosts a build report names, in build order, each once."""
    return list(dict.fromkeys(entry.host for entry in report.repos))


async def _record_observed(
    host_ids: "list[str]", *, images: bool = False, containers: bool = False
) -> None:
    """Record what the daemon answered on each host, for the next TAB; best-effort.

    Runs after the verb's own work, on the session it already holds open: the
    daemon is asked once more in a --format shape (:func:`observed_images`,
    :func:`observed_containers`) and the answer replaces that host's
    sub-entry, an empty answer included (the last container was removed). Hosts
    are asked concurrently. The cache is a completion concern, so the leaf records, the
    way ``otto tunnel`` records its ids; the library functions a suite calls
    never write it. A dry run asked the daemon nothing and records nothing.
    Any failure is one DEBUG line: a hint that could not be refreshed never
    changes a verb's output or exit.
    """
    import asyncio

    from ..host.host import is_dry_run

    if is_dry_run():
        return
    await asyncio.gather(
        *(_record_host(host_id, images=images, containers=containers) for host_id in host_ids)
    )


async def _record_host(host_id: str, *, images: bool, containers: bool) -> None:
    """Ask one host's daemon and record the answer; swallow any failure.

    The library probes let transport errors and a non-docker host id propagate
    by design, and the cache write can fail on its own; this is the one place
    that catches them all, so one host's failure never stops the next host's
    record nor changes the verb.
    """
    from ..bootstrap import get_repos
    from ..config.completion_cache import record_docker_containers, record_docker_images
    from ..docker.observe import observed_containers, observed_images

    try:
        if images:
            seen = await observed_images(host_id)
            if seen.answered:
                record_docker_images(get_repos(), host_id, refs=seen.refs, ids=seen.ids)
        if containers:
            found = await observed_containers(host_id)
            if found.answered:
                record_docker_containers(get_repos(), host_id, names=found.names, ids=found.ids)
    except Exception as e:  # noqa: BLE001 — a completion hint never fails a verb
        logger.debug(r"\[docker] observed state of %s not recorded: %r", host_id, e)


async def _build(
    image: Annotated[
        list[str] | None,
        typer.Argument(
            help="Declared image names to build (default: all).",
            autocompletion=_image_completer,
        ),
    ] = None,
    parent: Annotated[
        str | None,
        typer.Option(
            "--parent",
            help=(
                "The docker-capable lab host (the parent) to build on; defaults to the lab's "
                "only one, or its highest docker_priority. Not docker's -H."
            ),
            autocompletion=_docker_host_completer,
        ),
    ] = None,
    repo: Annotated[
        str | None,
        typer.Option(
            "--repo",
            help="Restrict to a single repo by name.",
            autocompletion=_repo_completer,
        ),
    ] = None,
    tag: Annotated[
        list[str] | None,
        typer.Option(
            "--tag",
            "-t",
            help="Image reference to tag, as `docker build -t`. Repeatable.",
            autocompletion=_tag_completer,
        ),
    ] = None,
    no_cache: Annotated[
        bool, typer.Option("--no-cache", help="Do not use docker's layer cache.")
    ] = False,
    pull: Annotated[
        bool, typer.Option("--pull", help="Always pull newer versions of the base images.")
    ] = False,
    build_arg: Annotated[
        list[str] | None,
        typer.Option("--build-arg", help="Build-time variable, KEY=VALUE. Repeatable."),
    ] = None,
    target: Annotated[
        str | None, typer.Option("--target", help="The build stage to build.")
    ] = None,
) -> None:
    """Build the selected repos' declared images on one lab host.

    Like `docker build`, this knows nothing about a composition: it needs a
    host, not a use-case, so it takes --parent (the lab's default parent when omitted).
    To build the images a deployment would use, on the parent it would use, run
    `otto docker compose build`.
    """
    from ..docker.build_verbs import build_on

    build_args = _parse_pairs(build_arg, form="KEY=VALUE", flag="--build-arg KEY=VALUE")
    report = await _run_docker(
        build_on(
            parent,
            repo=repo,
            images=image,
            tags=tag,
            no_cache=no_cache,
            pull=pull,
            build_args=build_args,
            target=target,
        )
    )
    if not isinstance(report, _Declined):
        await _record_observed(_build_hosts(report), images=True)
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
            help="Declared image names to build, over the use-case's winners (default: all).",
            autocompletion=_image_completer,
        ),
    ] = None,
    parent: Annotated[
        str | None,
        typer.Option(
            "--parent",
            help=(
                "The docker-capable lab host (the parent) to build on, as `compose up` "
                "resolves it; defaults to the lab's only one, or its highest docker_priority. "
                "Not docker's -H."
            ),
            autocompletion=_docker_host_completer,
        ),
    ] = None,
    provide: Annotated[
        list[str] | None,
        typer.Option("--provide", help="Break a provider tie: CAPABILITY=REPO. Repeatable."),
    ] = None,
    no_cache: Annotated[
        bool, typer.Option("--no-cache", help="Do not use docker's layer cache.")
    ] = False,
    pull: Annotated[
        bool, typer.Option("--pull", help="Always pull newer versions of the base images.")
    ] = False,
    build_arg: Annotated[
        list[str] | None,
        typer.Option("--build-arg", help="Build-time variable, KEY=VALUE. Repeatable."),
    ] = None,
) -> None:
    """Build the images a deployment of a use-case would use, where it would use them.

    The same provider competition and parent `otto docker compose up` uses,
    so the images that get built are the ones deployment would use, on the
    host it would use.
    """
    from ..docker.build_verbs import compose_build

    provide_map = _parse_provide(provide)
    build_args = _parse_pairs(build_arg, form="KEY=VALUE", flag="--build-arg KEY=VALUE")
    name = _default_use_case(use_case)
    report = await _run_docker(
        compose_build(
            name,
            parent=parent,
            provide=provide_map,
            images=image,
            no_cache=no_cache,
            pull=pull,
            build_args=build_args,
        )
    )
    if not isinstance(report, _Declined):
        await _record_observed(_build_hosts(report), images=True)
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
        typer.Argument(
            help="Restrict to these services (requires an explicit use-case).",
            autocompletion=_service_completer,
        ),
    ] = None,
    parent: Annotated[
        str | None,
        typer.Option(
            "--parent",
            help=(
                "The docker-capable lab host (the parent) every fragment deploys on; defaults "
                "to the lab's only one, or its highest docker_priority. Not docker's -H."
            ),
            autocompletion=_docker_host_completer,
        ),
    ] = None,
    build: Annotated[
        bool,
        typer.Option(
            "--build",
            help=(
                "Build the participating repos' declared `\\[\\[docker.images]]` first "
                "(otto's build; docker's own `--build` is not passed)."
            ),
        ),
    ] = False,
    force_recreate: Annotated[
        bool,
        typer.Option(
            "--force-recreate", help="Recreate containers even if their configuration is unchanged."
        ),
    ] = False,
    pull: Annotated[
        str | None,
        typer.Option("--pull", help="docker's `--pull` policy, passed through unchanged."),
    ] = None,
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
    """Deploy a use-case: one merged compose stack on one parent.

    The fragments that take part are chosen by the provider competition,
    deployed on one parent (--parent, or the lab's default), and handed the
    assembled env mapping. Each participating repo's declared images are
    built first only with --build; without it, `up` is docker's own, so a service whose image
    is missing fails with docker's error. With no USE_CASE, the only declared
    one is deployed; naming SERVICEs narrows the deployment to them.
    """
    # --parent is deliberately NOT canonicalized here. `deploy` resolves it
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
            parent=parent,
            provide=provide_map,
            env=env_map,
            env_files=env_file or None,
            build=build,
            force_recreate=force_recreate,
            pull=pull,
        )
    )
    if not isinstance(stack, _Declined):
        await _record_observed(list(stack.by_host), containers=True)
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
        typer.Argument(
            help="Tear down only these services (requires an explicit use-case).",
            autocompletion=_service_completer,
        ),
    ] = None,
    parent: Annotated[
        str | None,
        typer.Option(
            "--parent",
            help=(
                "The docker-capable lab host (the parent) every fragment deploys on; defaults "
                "to the lab's only one, or its highest docker_priority. Not docker's -H."
            ),
            autocompletion=_docker_host_completer,
        ),
    ] = None,
    provide: Annotated[
        list[str] | None,
        typer.Option("--provide", help="Break a provider tie: CAPABILITY=REPO. Repeatable."),
    ] = None,
) -> None:
    """Tear a use-case's stacks down and unregister their container hosts.

    --parent and --provide are resolved exactly as `otto docker compose up` resolves them,
    so a teardown can never address a different project than the deployment it
    is undoing. Naming SERVICEs stops and removes just those, leaving the rest
    of the stack and its network standing.
    """
    from ..docker.deployment import teardown

    provide_map = _parse_provide(provide)
    name = _default_use_case(use_case)
    report = await _run_docker(
        teardown(name, services=service or None, parent=parent, provide=provide_map)
    )
    if isinstance(report, _Declined):
        return
    # A host whose teardown failed (unreachable, most often) is not asked again.
    failed = report.failed
    await _record_observed([h for h in report.hosts if h not in failed], containers=True)
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
    # Selection (§4) and the parent rule (§2) are pure, which is what lets this
    # verb exist at all. A refusal from either is REPORTED, not raised: this is
    # an inventory, and "the rule cannot pick a parent here" is exactly the
    # answer the user came for — not a reason to hide the use-cases, and not
    # a reason to exit 1.
    from rich.table import Table

    from ..bootstrap import get_repos
    from ..config.fleet import get_lab
    from ..docker.observe import DockerVerbError, docker_parent
    from ..docker.resolve import (
        UseCaseResolutionError,
        declared_use_cases,
        place,
        select_fragments,
    )

    repos = get_repos()
    declared = declared_use_cases(repos)
    if not declared:
        rprint(f"[yellow]{escape('otto docker: no active repo declares [[docker.use_cases]].')}")
        return

    # A filter naming nothing is a USER error about the argument, not a state
    # of the configuration — so it is loud (exit 1) even though a parent the
    # rule cannot pick is only reported. Phrased like `select_fragments`'
    # own refusal, with the declared set named, so a typo reads the same
    # whichever verb found it.
    if use_case is not None and use_case not in declared:
        fail(
            f"otto docker use-cases: no active repo declares use-case "
            f"{use_case!r}; declared: {', '.join(sorted(declared))}"
        )
    names = [use_case] if use_case is not None else sorted(declared)

    lab = get_lab()
    # One parent for every use-case: the rule reads the lab, never a fragment.
    parent: "UnixHost | None" = None
    parent_problem = ""
    try:
        parent = docker_parent(lab, None)
    except DockerVerbError as e:
        parent_problem = str(e)
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
            if parent is None:
                problems.append(parent_problem)
            else:
                hosts = {
                    id(sf.fragment): host_id
                    for host_id, frags in place(selection, parent).items()
                    for sf in frags
                }

        table = Table(
            "fragment",
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


def _render_observe(report: "ObserveReport", *, header: bool) -> None:
    """Print each host's docker output whole; exit 1 when any host's command failed.

    *header* adds ``== <host> ==`` above each host and a blank line between
    hosts -- the fan-out verbs' shape, stable whether one host or ten were
    asked. ``logs`` passes False: one container, one host, docker's lines
    alone, and its failure exits with docker's own code (as ``--follow``
    does). ``print``, never ``rprint``: docker's text is not markup and a
    table wider than the terminal is docker's to wrap, not Rich's.
    """
    for index, host in enumerate(report.hosts):
        if header:
            if index:
                print()  # noqa: T201 -- docker's text, not markup; see the docstring
            print(f"== {host.host_id} ==")  # noqa: T201
        text = host.result.value
        if text:
            print(text, end="" if text.endswith("\n") else "\n")  # noqa: T201
    if not report.ok:
        code = 1
        if not header:
            code = max(report.hosts[0].result.retcode, 1)
        raise typer.Exit(code)


def _answered_hosts(report: "ObserveReport") -> "list[str]":
    """Return the hosts whose command succeeded: one the verb could not ask is not asked again.

    The observe leaves render first and record in a ``finally``, so docker's
    lines are on screen before the probe runs and a failed host's exit still
    records the hosts that answered; leaving the failed host out spares the
    user its connect timeout a second time.
    """
    return [h.host_id for h in report.hosts if h.result.is_ok]


async def _ps(
    all_: Annotated[
        bool,
        typer.Option("--all", "-a", help="Show every container, not only the running ones."),
    ] = False,
    parent: Annotated[
        str | None,
        typer.Option(
            "--parent",
            help=(
                "The docker-capable lab host (the parent) to ask; omitted: every "
                "docker-capable host in the lab. Not docker's -H."
            ),
            autocompletion=_docker_host_completer,
        ),
    ] = None,
) -> None:
    """Print `docker ps` from every docker-capable host, as docker printed it."""
    from ..docker.observe import list_containers

    report = await _run_docker(list_containers(parent=parent, all=all_))
    if isinstance(report, _Declined):
        return
    try:
        _render_observe(report, header=True)
    finally:
        await _record_observed(_answered_hosts(report), containers=True)


async def _images(
    parent: Annotated[
        str | None,
        typer.Option(
            "--parent",
            help=(
                "The docker-capable lab host (the parent) to ask; omitted: every "
                "docker-capable host in the lab. Not docker's -H."
            ),
            autocompletion=_docker_host_completer,
        ),
    ] = None,
) -> None:
    """Print `docker images` from every docker-capable host, as docker printed it."""
    from ..docker.observe import list_images

    report = await _run_docker(list_images(parent=parent))
    if isinstance(report, _Declined):
        return
    try:
        _render_observe(report, header=True)
    finally:
        await _record_observed(_answered_hosts(report), images=True)


async def _follow(resolve: "Coroutine[Any, Any, list[LogsTarget]]") -> None:
    """Resolve the logs targets, then follow them live and exit with docker's status.

    Both awaits go through :func:`_run_docker`, so a refusal at either step
    reaches the user the way every docker verb's does. A status of ``None``
    (Ctrl+] or stdin EOF ended it, no status reported) or ``0`` is a normal return.
    """
    from ..docker.observe import follow_logs

    targets = await _run_docker(resolve)
    if isinstance(targets, _Declined):
        return
    status = await _run_docker(follow_logs(targets))
    if isinstance(status, int) and status != 0:
        raise typer.Exit(status)


async def _logs(
    container: Annotated[
        str,
        typer.Argument(
            help=(
                "A docker container name or id on the parent "
                "(--parent, or the lab's default parent)."
            ),
            autocompletion=_container_completer,
        ),
    ],
    tail: Annotated[
        str | None,
        typer.Option("--tail", help="Number of lines from the end of the log (docker's --tail)."),
    ] = None,
    since: Annotated[
        str | None,
        typer.Option(
            "--since",
            help="Logs since a timestamp or a relative time, e.g. 10m (docker's --since).",
        ),
    ] = None,
    timestamps: Annotated[
        bool, typer.Option("--timestamps", "-t", help="Show timestamps (docker's -t).")
    ] = False,
    follow: Annotated[
        bool,
        typer.Option(
            "--follow", "-f", help="Follow the log output live (docker's -f); Ctrl-C ends it."
        ),
    ] = False,
    parent: Annotated[
        str | None,
        typer.Option(
            "--parent",
            help=(
                "The docker-capable lab host (the parent) CONTAINER is on; omitted: the lab's "
                "default parent (its only one, or its highest docker_priority). Not docker's -H."
            ),
            autocompletion=_docker_host_completer,
        ),
    ] = None,
) -> None:
    """Print one container's `docker logs`, as docker printed it."""
    from ..docker.observe import container_logs

    if follow:
        from ..docker.observe import resolve_logs

        async def _target() -> "list[LogsTarget]":
            return [
                await resolve_logs(
                    container,
                    parent=parent,
                    tail=tail,
                    since=since,
                    timestamps=timestamps,
                    follow=follow,
                )
            ]

        await _follow(_target())
        return
    report = await _run_docker(
        container_logs(container, parent=parent, tail=tail, since=since, timestamps=timestamps)
    )
    if isinstance(report, _Declined):
        return
    _render_observe(report, header=False)


async def _compose_ps(
    use_case: Annotated[
        str | None,
        typer.Argument(
            help="Use-case whose stacks to list (default: the only one declared).",
            autocompletion=_use_case_completer,
        ),
    ] = None,
    all_: Annotated[
        bool,
        typer.Option(
            "--all", "-a", help="Show every container of the project, not only the running ones."
        ),
    ] = False,
    parent: Annotated[
        str | None,
        typer.Option(
            "--parent",
            help=(
                "The docker-capable lab host (the parent) to ask; omitted: every "
                "docker-capable host in the lab. Not docker's -H."
            ),
            autocompletion=_docker_host_completer,
        ),
    ] = None,
    provide: Annotated[
        list[str] | None,
        typer.Option("--provide", help="Break a provider tie: CAPABILITY=REPO. Repeatable."),
    ] = None,
) -> None:
    """Print `docker compose ps` for a use-case's project on one parent or every capable host."""
    from ..docker.observe import compose_ps

    name = _default_use_case(use_case)
    report = await _run_docker(
        compose_ps(name, all=all_, parent=parent, provide=_parse_provide(provide))
    )
    if isinstance(report, _Declined):
        return
    try:
        _render_observe(report, header=True)
    finally:
        await _record_observed(_answered_hosts(report), containers=True)


async def _compose_logs(
    use_case: Annotated[
        str | None,
        typer.Argument(
            help="Use-case whose logs to print (default: the only one declared).",
            autocompletion=_use_case_completer,
        ),
    ] = None,
    service: Annotated[
        list[str] | None,
        typer.Argument(
            help="Only these services' logs (requires an explicit use-case).",
            autocompletion=_service_completer,
        ),
    ] = None,
    tail: Annotated[
        str | None,
        typer.Option("--tail", help="Number of lines from the end of each log (docker's --tail)."),
    ] = None,
    since: Annotated[
        str | None,
        typer.Option(
            "--since",
            help="Logs since a timestamp or a relative time, e.g. 10m (docker's --since).",
        ),
    ] = None,
    timestamps: Annotated[
        bool, typer.Option("--timestamps", "-t", help="Show timestamps (docker's -t).")
    ] = False,
    follow: Annotated[
        bool,
        typer.Option(
            "--follow", "-f", help="Follow the log output live (docker's -f); Ctrl-C ends it."
        ),
    ] = False,
    parent: Annotated[
        str | None,
        typer.Option(
            "--parent",
            help=(
                "The docker-capable lab host (the parent) to read; defaults to the lab's "
                "only one, or its highest docker_priority. Not docker's -H."
            ),
            autocompletion=_docker_host_completer,
        ),
    ] = None,
    provide: Annotated[
        list[str] | None,
        typer.Option("--provide", help="Break a provider tie: CAPABILITY=REPO. Repeatable."),
    ] = None,
) -> None:
    """Print `docker compose logs` for a use-case's project on one parent."""
    from ..docker.observe import compose_logs

    name = _default_use_case(use_case)
    if follow:
        from ..docker.observe import resolve_compose_logs

        provide_map = _parse_provide(provide)

        async def _targets() -> "list[LogsTarget]":
            return resolve_compose_logs(
                name,
                service or [],
                parent=parent,
                provide=provide_map,
                tail=tail,
                since=since,
                timestamps=timestamps,
            )

        await _follow(_targets())
        return
    report = await _run_docker(
        compose_logs(
            name,
            service or [],
            parent=parent,
            provide=_parse_provide(provide),
            tail=tail,
            since=since,
            timestamps=timestamps,
        )
    )
    if isinstance(report, _Declined):
        return
    _render_observe(report, header=True)


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
    either way. ``ps`` and the other observe verbs keep the safe default.
    """


# The single source of truth for which docker verbs exist, which group each registers
# on, and the two per-leaf policies the leaf-invoke preamble reads off the RESOLVED
# command's callback (`__cli_output_dir__`, `__cli_dry_run_preview__`, both defaulting
# False when absent) -- typer's own callback shim functools-wraps the registered
# function, carrying the markers through.
_VERBS: "list[_Verb]" = [
    _Verb("build", "docker", _build, dry_run_preview=True),
    _Verb("ps", "docker", _ps, output_dir=False),
    _Verb("images", "docker", _images, output_dir=False),
    _Verb("logs", "docker", _logs, output_dir=False),
    _Verb("use-cases", "docker", _use_cases, output_dir=False, dry_run_preview=True),
    _Verb("build", "compose", _compose_build, dry_run_preview=True),
    _Verb("up", "compose", _compose_up, dry_run_preview=True),
    _Verb("down", "compose", _compose_down, dry_run_preview=True),
    _Verb("ps", "compose", _compose_ps, output_dir=False),
    _Verb("logs", "compose", _compose_logs, output_dir=False),
]
_GROUPS: "dict[str, typer.Typer]" = {"docker": docker_app, "compose": compose_app}

for _verb in _VERBS:
    _verb.leaf.__cli_output_dir__ = _verb.output_dir  # ty: ignore[unresolved-attribute]
    _verb.leaf.__cli_dry_run_preview__ = _verb.dry_run_preview  # ty: ignore[unresolved-attribute]
    _GROUPS[_verb.group].command(name=_verb.name)(_verb.leaf)

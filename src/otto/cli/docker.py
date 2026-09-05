"""
otto docker — build images and deploy use-case stacks on lab hosts.

Subcommands::

    otto docker use-cases [USE_CASE]
    otto docker up    [USE_CASE [SERVICE...]] [--on H] [--no-build]
                      [--provide CAP=REPO] [--env K=V] [--env-file FILE]
    otto docker down  [USE_CASE [SERVICE...]] [--on H] [--provide CAP=REPO]
    otto docker build [USE_CASE [IMAGE...]] [--repo NAME] [--on H] [--rebuild]
    otto docker ps    [--on H]

``up``/``down`` speak USE-CASES (spec §10): one named, cross-repo deployment
resolved by the provider competition (§4) and placed by role (§5), not a
per-repo loop over ``[[docker.composes]]``. ``build`` still has its bare
per-repo mode and additionally accepts a use-case to narrow to the winners;
``ps`` is unchanged.

Every leaf is a thin wrapper around the library API in :mod:`otto.docker`,
which is also what instructions and suites import directly.

IMPORT BUDGET: ``otto.docker.resolve`` and ``otto.docker.deployment`` are
imported FUNCTION-SCOPE throughout. A module-scope import would put both (and
their transitive config/lab imports) on the ``otto docker --help`` path, which
``tests/unit/import_budget`` gates — the eager names at the top of this module
are exactly the ones that surface was measured with.
"""

from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, TypeVar

import typer
from rich import print as rprint
from rich.markup import escape
from rich.table import Table

from ..config import Repo, get_lab, get_repos
from ..config.lab import Lab
from ..docker import build_images, compose_ps
from ..host.unix_host import UnixHost
from ..utils import Status
from .completers import completion_source
from .invoke import fail, print_error

if TYPE_CHECKING:
    from collections.abc import Coroutine

    from ..config.repo import DockerUseCase
    from ..docker.deployment import UseCaseStack
    from ..docker.resolve import Displacement

_T = TypeVar("_T")

docker_app = typer.Typer(
    name="docker",
    help="Build images and deploy use-case stacks on docker-capable lab hosts.",
    no_args_is_help=True,
    context_settings={
        "help_option_names": ["-h", "--help"],
    },
)


# Read-only docker subcommands that produce no artifacts → no output dir.
_NO_OUTPUT_DIR_SUBCOMMANDS = frozenset({"ps", "use-cases"})

# Subcommands that own their own ``--dry-run`` preview and so opt OUT of the
# seam default (``otto.cli.invoke.stop_at_dry_run_seam``, which otherwise
# prints a generic block and exits 0 ABOVE the leaf body). ``deploy`` and
# ``teardown`` resolve the whole pure half of the pipeline under a dry run and
# decline with spec §12's plan — the exact compose command included — so
# stopping at the seam would delete the preview this workstream exists to
# ship. ``build``/``ps`` keep the safe default; ``use-cases`` is read-only and
# behaves identically either way, so it needs no opt-in.
_DRY_RUN_PREVIEW_SUBCOMMANDS = frozenset({"up", "down"})


@docker_app.callback()
def docker_callback(ctx: typer.Context) -> None:
    """Build images and deploy use-case stacks on docker-capable lab hosts.

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
    the same loudness contract ``_select_repos`` carries one layer down.
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
            f"name the one you mean, e.g. `otto docker up {names[0]}`."
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

    Renders each record AS IT IS and calls NEITHER priority the higher one, for
    ``deployment._log_displacements``' reason: ``--provide`` narrows the field
    to one repo before ranking, so the winner can legitimately carry a LOWER
    priority than what it displaced, and the loser can be the winner's own repo
    (two fragments of one repo). "Lower priority lost" would be false in both
    cases; naming who won, at what, and what stood down is true in all of them.
    """
    for d in displaced:
        line = (
            f"docker: {d.capability} goes to {d.winner_repo} (priority "
            f"{d.winner_priority}); {d.loser_repo} (priority {d.loser_priority}) "
            f"stands down"
        )
        rprint(f"[yellow]{escape(line)}")


def _print_stack_report(stack: "UseCaseStack") -> None:
    """Report what :func:`~otto.docker.deployment.deploy` registered, per host."""
    _print_displacements(stack.selection.displaced)
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


async def _run_use_case(action: "Coroutine[Any, Any, _T]") -> "_T | _Declined":
    """Await a use-case library call, rendering its two refusal shapes.

    ``deploy``/``teardown`` refuse in exactly two ways, and they mean opposite
    things to the process:

    * :class:`~otto.result.CommandNotRunError` — a dry run's decline. It is the
      ANSWER the user asked for (it carries spec §12's resolved plan and, for
      ``deploy``, the exact compose command), so it prints and exits 0. Caught
      here rather than left to the boundary frame, which would print it as
      ``error:`` and exit 1 — turning a successful preview into a failure.
    * :class:`~otto.docker.resolve.UseCaseResolutionError` — a configuration
      refusal (an ``--on`` naming no lab host, a provider tie, an unresolvable
      role). The LIBRARY's phrase is what reaches the user, verbatim: this
      layer keeps no second copy that could drift from it.

    Returns the call's value, or :data:`_DECLINED` when it declined. A
    sentinel and not ``None``, because ``None`` is ``teardown``'s own successful
    return value — reading it as "declined" would make every real teardown
    print nothing.
    """
    from ..docker.resolve import UseCaseResolutionError
    from ..host.host import is_dry_run
    from ..result import CommandNotRunError

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


def _select_repos(repo_name: str | None, on: str | None = None) -> list[Repo]:
    """Filter loaded repos by name AND by lab applicability.

    A repo is "applicable" if either:
      - none of its [[docker.use_cases]] fragments commit a placement pin
        (there is nothing to check against the active lab — a later step
        surfaces its own clear error if that turns out to matter), or
      - at least one pinned host names a host in the active lab.

    Only committed pins are consulted here, never full placement resolution
    (role matching, scope, or the config-debris checks `_place_fragment`
    performs): this filter is a coarse, non-authoritative pre-check, and the
    LOUD authority over whether a fragment's placement is even well-formed is
    `_resolve_parent`/`resolve_placement`, run later at actual resolve time
    for the repos that survive here. Running full resolution here too would
    double-refuse — a mis-keyed `placement` table (e.g. a role that names a
    key the fragment doesn't carry) is `_place_fragment`'s specific, actionable
    refusal, and catching it here would either swallow it into this filter's
    generic "not in active lab" exclusion or race it against a second,
    differently-worded refusal depending on which check runs first. A repo
    whose fragments carry only a role (no pin) has nothing concrete for this
    coarse filter to check, so it is kept — same as a repo with no
    candidates at all — and the resolve-time step still catches every real
    problem, once, in one voice.

    --on is a runtime override of *where* to deploy, never a signal of
    *which* repos belong to the active lab, so it plays no part in this
    filter (see the loop below) — only in the earlier "does --on itself
    name a host in this lab" check.

    A multi-repo workspace can declare docker stacks on hosts that belong to
    different labs (e.g. repo1 → unix/test3, repo2 → unix_alt/alt3).
    Only one lab is active per `otto` invocation, so iterating over a repo
    whose target host isn't loaded would yield a confusing "not in lab"
    error. Every excluded repo is printed (yellow) with its reason instead.

    If *on* is explicitly provided but does not name a host in the active
    lab, that's a user error — fail fast rather than silently skipping
    every repo and exiting 0. Likewise, an empty selection after filtering —
    whether because no active repo declares a [docker] section at all, or
    every candidate was excluded above — is a hard error (exit 1), never a
    silent no-op.
    """
    lab = get_lab()

    if on is not None and on not in lab.hosts:
        fail(
            f"--on {on!r} is not a host in the active lab {lab.name!r}. "
            f"Available hosts: {sorted(lab.hosts)}"
        )

    docker_repos = [
        r for r in get_repos() if r.docker_settings.composes or r.docker_settings.images
    ]
    if not docker_repos:
        fail(
            "otto docker: no active repo declares a [docker] section — nothing to act on. "
            "Add [docker] to a repo's .otto/settings.toml (see the settings guide)."
        )
    if repo_name is not None:
        matches = [r for r in docker_repos if r.name == repo_name]
        if not matches:
            fail(f"No loaded repo named {repo_name!r} with a [docker] section.")
        docker_repos = matches

    applicable: list[Repo] = []
    excluded: list[tuple[str, str]] = []  # (repo name, reason)
    for r in docker_repos:
        # Lab applicability is determined by the repo's committed placement
        # pins, not by --on. --on is a runtime override of where to deploy, not
        # a signal of which repos belong to the active lab — using [on] here
        # would incorrectly keep every repo whenever the override is in lab.
        # Pins only, never resolve_placement: this is a coarse pre-check, and
        # _resolve_parent is the loud authority that actually validates a
        # fragment's placement later — running full resolution here would
        # double-refuse the same config debris in two different voices.
        # A lab-qualified pin ("unix_alt:alt3") is stripped to its host id —
        # this filter only asks "is the host reachable from here", the same
        # question a bare pin answers by direct membership.
        candidates: list[str] = [
            pin.rpartition(":")[2]
            for uc in r.docker_settings.use_cases
            for pin in uc.placement.values()
        ]
        # A repo with no pinned candidates at all (role-only, or no
        # use-cases) is kept — _resolve_parent will surface a clear error of
        # its own.
        if not candidates or any(c in lab.hosts for c in candidates):
            applicable.append(r)
        else:
            excluded.append(
                (r.name, f"its docker hosts {candidates} are not in active lab {lab.name!r}")
            )
    for name, reason in excluded:
        # escape()d for the same reason the use-cases table's fragment cell is:
        # `reason` embeds a Python list ("its docker hosts ['test3'] are not
        # ..."), and rich reads `['test3']` as a style tag and deletes it —
        # from the one message whose entire job is naming WHICH hosts were
        # unreachable.
        rprint(f"[yellow]{escape(f'docker: skipping repo {name!r} — {reason}')}")
    if not applicable:
        fail(
            "otto docker: every candidate repo was excluded (see the reasons above). "
            "Load the lab the repo targets, or pass --on with a host in this lab "
            "after fixing the repo's [docker] declaration."
        )
    return applicable


def _resolve_parent_for_repo(repo: Repo, lab: Lab, on: str | None) -> UnixHost:
    """Reuse compose._resolve_parent — public via private import to avoid duplicate logic."""
    from ..docker.compose import _resolve_parent

    return _resolve_parent(repo, lab, on)


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
        fail(
            f"--on {on!r} is not a host in the active lab {lab.name!r}. "
            f"Available hosts: {sorted(lab.hosts)}"
        )
    return host.id


def _narrow_to_use_case(repos: list[Repo], use_case: str, provide: "dict[str, str]") -> list[Repo]:
    """Keep only the repos whose fragments WON the competition for *use_case*.

    ``build <USE_CASE>`` exists so a deploy's image work can be done ahead of
    time without also building the images of every repo the provider
    competition (spec §4) just excluded. The competition runs over every active
    repo — that is what makes it the same competition ``up`` runs — and the
    result is intersected with the lab-applicable selection ``_select_repos``
    already made.
    """
    from ..docker.resolve import UseCaseResolutionError, select_fragments

    try:
        selection = select_fragments(use_case, get_repos(), provide=provide)
    except UseCaseResolutionError as e:
        fail(e)
    _print_displacements(selection.displaced)
    winners = {sf.repo.name for sf in selection.fragments}
    narrowed = [r for r in repos if r.name in winners]
    if not narrowed:
        fail(
            f"otto docker build {use_case}: the repos participating in use-case "
            f"{use_case!r} ({sorted(winners)}) are not in this lab's selection "
            f"({sorted(r.name for r in repos)}) — there is nothing to build for it."
        )
    return narrowed


async def _build(
    use_case: Annotated[
        str | None,
        typer.Argument(
            help="Build only the repos participating in this use-case (default: all selected).",
            autocompletion=_use_case_completer,
        ),
    ] = None,
    image: Annotated[
        list[str] | None, typer.Argument(help="Image names to build (default: all).")
    ] = None,
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
    provide: Annotated[
        list[str] | None,
        typer.Option("--provide", help="Break a provider tie: CAPABILITY=REPO. Repeatable."),
    ] = None,
) -> None:
    """Build docker images declared in selected repos.

    With a USE_CASE, only the repos taking part in it are built — the same
    provider competition `otto docker up` runs, so the images that get built
    are the ones that deployment would actually use.
    """
    provide_map = _parse_provide(provide)
    lab = get_lab()
    on = _canonicalize_on(lab, on)
    selected_repos = _select_repos(repo, on=on)
    if use_case is not None:
        selected_repos = _narrow_to_use_case(selected_repos, use_case, provide_map)
    any_failed = False
    acted = False
    for r in selected_repos:
        if not r.docker_settings.images:
            msg = escape(f"docker: {r.name} declares no [[docker.images]] — nothing to build")
            rprint(f"[yellow]{msg}")
            continue
        acted = True
        parent = _resolve_parent_for_repo(r, lab, on)
        results = await build_images(r, parent, image_names=image, rebuild=rebuild)
        for name, res in results.items():
            # `value` on every branch: the tag on ok, the captured build output
            # on failure. Never `msg` — an exec-produced CommandResult leaves
            # it empty, so reading it here would print nothing at all.
            if res.status is Status.Skipped:
                rprint(f"[dim]{r.name}/{name}: cached → {res.value}")
            elif res.status is Status.Success:
                rprint(f"[green]{r.name}/{name}: built → {res.value}")
            else:
                any_failed = True
                print_error(f"{r.name}/{name}: FAILED\n{res.value}")
    if not acted:
        fail(
            "otto docker build: no selected repo declares [[docker.images]] — nothing to "
            "build (see the notices above). Add [[docker.images]] to a repo's "
            ".otto/settings.toml (see the settings guide)."
        )
    if any_failed:
        raise typer.Exit(1)


async def _up(
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
            help="Collapse every fragment onto this lab host (spec §5 knob 1).",
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
    # --on is deliberately NOT canonicalized here. `deploy` resolves the handle
    # itself (it shares one pure prefix with `teardown`, so the two verbs
    # cannot disagree about where a deployment lives), and it owns the refusal
    # — so a host this lab does not have is named once, in one sentence,
    # however the deployment was reached (T7 review I3).
    from ..docker.deployment import deploy

    provide_map = _parse_provide(provide)
    env_map = _parse_env(env)
    name = _default_use_case(use_case)
    stack = await _run_use_case(
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


async def _down(
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
            help="Collapse every fragment onto this lab host (spec §5 knob 1).",
            autocompletion=_docker_host_completer,
        ),
    ] = None,
    provide: Annotated[
        list[str] | None,
        typer.Option("--provide", help="Break a provider tie: CAPABILITY=REPO. Repeatable."),
    ] = None,
) -> None:
    """Tear a use-case's stacks down and unregister their container hosts.

    --on and --provide are resolved exactly as `otto docker up` resolves them,
    so a teardown can never address a different project than the deployment it
    is undoing. Naming SERVICEs stops and removes just those, leaving the rest
    of the stack and its network standing.
    """
    from ..docker.deployment import teardown

    provide_map = _parse_provide(provide)
    name = _default_use_case(use_case)
    outcome = await _run_use_case(
        teardown(name, services=service or None, on=on, provide=provide_map)
    )
    if isinstance(outcome, _Declined):
        return
    scope = f" ({', '.join(service)})" if service else ""
    rprint(f"[green]{escape(f'{name}{scope}: torn down.')}")


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
    the answer is the same with or without --dry-run. Values are never printed,
    only the env KEY names. Name a USE_CASE to see just that one.
    """
    # Selection (§4) and placement (§5) are pure, which is what lets this verb
    # exist at all. A refusal from either is REPORTED, not raised: this is an
    # inventory, and "one of your six use-cases cannot place its edge fragment"
    # is exactly the answer the user came for — not a reason to hide the other
    # five, and not a reason to exit 1.
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
    lab = get_lab()
    parents: list[UnixHost] = []
    if on:
        # --on is a CLI host-id input: accept a canonical id or a positional
        # handle (e.g. dut1), same as `otto host`.
        host = lab.resolve_handle(on)
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


# Read-only docker subcommands (`ps`, `use-cases`) produce no artifacts → opt them out of
# the per-command output dir. The leaf-invoke preamble reads `__cli_output_dir__`
# off the command callback (default True); typer's own callback shim
# functools-wraps the registered function, carrying the marker through. This
# keeps `_NO_OUTPUT_DIR_SUBCOMMANDS` the single source of truth for the policy.
# The async bodies run under the command lifecycle via the leaf-invoke
# wrapper's coroutine bridge (cli/invoke._wrap_invoke) at dispatch.
_DOCKER_SUBCOMMANDS: dict[str, Any] = {
    "build": _build,
    "up": _up,
    "down": _down,
    "ps": _ps,
    "use-cases": _use_cases,
}
for _sub_name, _sub_fn in _DOCKER_SUBCOMMANDS.items():
    if _sub_name in _NO_OUTPUT_DIR_SUBCOMMANDS:
        _sub_fn.__cli_output_dir__ = False
    if _sub_name in _DRY_RUN_PREVIEW_SUBCOMMANDS:
        # Read by `otto.cli.invoke._leaf_declares_preview` off the RESOLVED
        # command's callback — the same mechanism `@cli_exposed(
        # dry_run_preview=True)` stamps for host verbs, reached here without a
        # decorator because these leaves are registered from a table.
        _sub_fn.__cli_dry_run_preview__ = True
    docker_app.command(name=_sub_name)(_sub_fn)

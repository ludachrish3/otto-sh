r"""otto's composition root: project discovery + contained user-code registration.

Replaces ``config``'s import-time side effects. Phase 1 (*discovery*)
parses the environment and project ``settings.toml`` files — no user code runs.
Between phase 1 and phase 2 the *dependency pass* (``config.dependencies``)
validates each project's declared dependencies, skips projects whose required deps
are unsatisfied (framed ``DependencyError``\ s), and orders phase-2 registration
topologically (stable — project-root order when no deps are declared). Phase 2
(*registration*) imports each project's init modules, wrapping every user-module
exec so one broken file becomes a framed :class:`BootstrapError` instead of
bricking the process. Project test files are NOT part of phase 2: no otto code
executes a test file outside a pytest session, and a test file may register
nothing (:func:`otto.registry.loading_test_files`). After phase 2 — and only
after, because the registries it reads are populated BY those imports — one
check runs that does NOT get contained: a project that
registered product or dev-tool providers, OR that declares
``[[products]]``/``[[dev_tools]]`` entries, must have declared the labs it
applies to (:class:`ProjectScopeError`, spec §D2). Lab loading is
deliberately NOT part of bootstrap — it happens lazily at first access.

``bootstrap()`` is idempotent: the CLI entrypoint calls it before argv
parsing, ``open_context()`` reaches it only through ``declared.compose()`` and
the initiator the root installed, and repeated calls return the same
:class:`BootstrapResult`.

What it composes, a :class:`BootstrapResult`, is published to the
composed-projects catalog in :mod:`otto.declared`, the one home of the composed
state, where the layers below this root read it: the in-progress result
before the projects' init modules run, then the complete one; a composition that
fails, and :func:`invalidate`, withdraw both. This module keeps no copy of
its own. The last statement of this module installs the catalog's initiator,
so importing the root lets a reader below it start composition, and composes
nothing itself.

Its error types, :class:`BootstrapError`, :class:`DependencyError`,
:class:`ProjectScopeError` and :class:`BootstrapWarning`, are defined in
:mod:`otto.config.scope`, below every layer that raises or catches them, and
re-exported here under their public names: each is the same object as its
owner's. Every other module that raises or catches :class:`ProjectScopeError`
imports it from the owner when called, so a test's patch of
``otto.config.scope.ProjectScopeError`` reaches it there; this module's own
D2 check raises through the binding it took at import, which that patch does
not reach.

The composed state's public names are defined in :mod:`otto.declared` and
re-exported here, each the same object as its owner's:
:class:`BootstrapResult`, and the accessors :func:`get_projects`,
:func:`get_ordered_projects` and :func:`is_bootstrapped`. The first two compose
on first use, never at import, through the catalog's initiator, which looks
:func:`bootstrap` up in this module when called, so a test's patch of
``otto.bootstrap.bootstrap`` reaches them; :func:`is_bootstrapped` never
composes. Discovery, phase 1, is defined in :mod:`otto.config.project` with its
cache, and this module re-exports its public :func:`get_env` from there
lazily, the same object as its owner's: importing the root loads no project
loader. Code that runs discovery calls ``otto.config.project.discover``, and
:func:`bootstrap` reads it off that module when called, so a test's patch of
``otto.config.project.discover`` reaches it.
:func:`get_completion_names` is defined in :mod:`otto.registry`, which holds
the CLI's completion-cache snapshot, and re-exported here: its readers
import that module, not this one, so resolving a command for ``--help`` or a
TAB loads no composition root.
"""

import importlib
from typing import TYPE_CHECKING

from .config.scope import BootstrapError, BootstrapWarning, DependencyError, ProjectScopeError
from .declared import (
    BootstrapResult,
    clear_composed,
    get_ordered_projects,
    get_projects,
    install_initiator,
    is_bootstrapped,
    publish_composed,
)
from .errors import is_containable
from .registry import clear_completion_names, get_completion_names, registering_project

if TYPE_CHECKING:
    from .config.project import Project, get_env

__all__ = [
    "BootstrapError",
    "BootstrapResult",
    "BootstrapWarning",
    "DependencyError",
    "ProjectScopeError",
    "bootstrap",
    "get_completion_names",
    "get_env",
    "get_ordered_projects",
    "get_projects",
    "invalidate",
    "is_bootstrapped",
]


# name -> the module that defines it, imported on first access by __getattr__: the
# discovery name this root re-exports, its public get_env. Lazy, so importing the root
# loads no project loader.
_LAZY_ATTRS: dict[str, str] = {
    "get_env": "otto.config.project",
}


def __getattr__(name: str) -> object:
    """PEP 562 resolver for the discovery name re-exported from otto.config.project."""
    if name in _LAZY_ATTRS:
        return getattr(importlib.import_module(_LAZY_ATTRS[name]), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    """Include the lazy re-export in dir(); the module dict does not hold it."""
    return sorted(set(globals()) | set(_LAZY_ATTRS))


_NO_LAB_PATTERNS = """\
project '{name}' registers product/dev-tool providers or declares
[[products]]/[[dev_tools]] entries but declares no [project] lab_patterns in
.otto/settings.toml. A providing project must say which labs it applies to. Add:

    [project]
    lab_patterns = [".*"]   # every lab — make the reach explicit
    #host_patterns = [".*"]

and narrow the patterns to the labs this project actually targets.\
"""
"""D2's refusal. An empty ``lab_patterns = []`` gets this same text: it is not
a narrower declaration, it is the same "no lab" the missing key compiles to."""

_NO_HOST_PATTERNS = """\
project '{name}' registers product/dev-tool providers or declares
[[products]]/[[dev_tools]] entries but declares an empty [project]
host_patterns in .otto/settings.toml, which admits no host in any lab — so
every provider/entry it registers is dead code. Either drop the key (it
defaults to every host) or name them:

    [project]
    host_patterns = [".*"]   # every host in those labs\
"""
"""The other axis. ``host_patterns = []`` is reachable only by writing it out —
the field defaults to ``[".*"]`` — so it is always a deliberate keystroke, and
always the wrong one on a project that registers providers or declares entries."""


def _check_providing_projects_declare_scope(
    all_projects: "list[Project]", ordered_projects: "list[Project]"
) -> None:
    """Refuse a project that provides without declaring its fleet (D2).

    "Provides" means either half of the design: a project that registered a
    product/dev-tool *provider* (the code-first seam) or one whose
    ``.otto/settings.toml`` carries a non-empty ``[[products]]``/
    ``[[dev_tools]]`` array (the declarative seam) — both reach hosts the
    exact same way once built, so both owe the same ``[project]`` scope.

    Both halves are judged over *ordered_projects* (the dependency pass's
    survivors). For providers that is structural: a project whose required deps
    went unsatisfied never had its init module run, so it cannot have
    registered anything — checking it would be vacuous. For declared entries
    it is parity with :func:`otto.declared.declared_for_host`, which filters
    a dependency-skipped project's entries out at collection (Chris,
    2026-09-02): entries that can never apply owe no scope, and refusing the
    whole bootstrap over a project that is already skipped (and already surfaced
    as a dependency finding) would bury the real problem under a second one.

    Runs after phase 2, which is the earliest moment the provider half of the
    question can be answered: the provider registries are populated BY the
    init imports, so a check that ran any sooner would see an empty registry
    and pass every project. (The declared-entry half is already final after
    phase 1, but is judged here too, in the same pass, for one refusal path.)

    Both provider subscriptions are read, ``PRODUCT_PROVIDERS`` and
    ``DEV_TOOL_PROVIDERS``, because they are separate (a provider can only ever
    land in one of them). Providers with no owner —
    registered outside any project's init import, which in practice means test
    code calling the register function directly — are skipped: there is no
    project to name and no ``settings.toml`` to point at. So is an owner naming a
    project that is not among *all_projects*, which is the same "nothing to point
    at" case reached from the other side.

    Args:
        all_projects: Every project phase 1 discovered, dependency-skipped ones
            included — used only to resolve owner names back to projects.
        ordered_projects: The projects whose init modules actually ran (the
            dependency pass's order) — the list both halves are judged over.

    Raises:
        ProjectScopeError: On the first offending project, sorted by name so a
            fleet with two of them always reports the same one.
    """
    from .host.dev_tool import registered_dev_tool_providers
    from .host.product import registered_product_providers

    # Structural guarantee, made explicit rather than assumed: `owner` is only
    # ever stamped from the `registering_project` marker bootstrap sets around an
    # init import (see the module docstring), so a provider's owner can never
    # name a project outside `ordered_projects` in the first place. Intersecting
    # anyway keeps this set's provenance honest against *this* call's inputs
    # instead of a fact about a different function far away.
    ordered_names = {project.name for project in ordered_projects}
    provider_owners = {
        owner
        for _, owner in [*registered_product_providers(), *registered_dev_tool_providers()]
        if owner
    } & ordered_names
    declared_owners = {
        project.name
        for project in ordered_projects
        if project.declared_products or project.declared_dev_tools
    }
    owners = provider_owners | declared_owners
    by_name = {project.name: project for project in all_projects}
    for name in sorted(owners):
        project = by_name.get(name)
        if project is None:
            continue
        scope = project.project_scope
        if scope is None or not scope.lab_patterns:
            raise ProjectScopeError(project.root, _NO_LAB_PATTERNS.format(name=name))
        if not scope.host_patterns:
            raise ProjectScopeError(project.root, _NO_HOST_PATTERNS.format(name=name))


def _contain_data_profile_errors(projects: "list[Project]", errors: list[BootstrapError]) -> None:
    """Check each loaded project's ``[os_profiles]``; contain a bad table as that project's error.

    *projects* are the projects in the dependency order, whose ``init`` modules
    ran. A dependency-skipped project is not passed: what its tables mean
    depends on host classes its own ``init`` would have registered, so a
    verdict here would blame it for a cause that is not its own and turn its
    skip into a load error. The settings parse at discovery still checked
    its tables' shape.

    A bad table is contained exactly as a failed ``init`` module is, because
    that is what it is: a load error found after the project's init modules ran (the
    check needs the host classes they register). It is recorded as a framed
    :class:`BootstrapError` naming the project and the table, attributed to the
    project by ``root``, and the project stays in ``ordered_projects``: its init
    ran, so the provider-scope check still judges what it registered, and its
    dependents behave as they do when a dependency's init fails. Other projects
    and commands proceed; a run the project is part of, or that an active
    project requiring it is part of, fails loudly, and any other run demotes
    the error to a warning.
    """
    if not any(project.os_profiles for project in projects):
        # Nothing to check: the module that checks stays unimported.
        return
    from .config.scope import TOML_SETTINGS_PATH
    from .host.os_profile import check_data_profiles

    for project in projects:
        try:
            check_data_profiles([project])
        except ValueError as e:  # noqa: PERF203 — containment seam: one project's bad table
            errors.append(BootstrapError(project.root, str(TOML_SETTINGS_PATH), e))


def bootstrap() -> BootstrapResult:
    """Run the composition root (idempotent): discovery, dependency pass, registration.

    The catalog in :mod:`otto.declared` holds what it composed: a published
    result is returned as it is, the complete one, or, re-entered from this
    call's own import phase, the in-progress one.
    """
    # Read when called, from its defining module, so a patch of it there reaches here.
    from .declared import published_result

    published = published_result()
    if published is not None:
        # Complete, or re-entered from our own import phase (see the catalog's
        # in-progress result). Answer with what is already settled rather than
        # composing the root a second time: `projects`/`ordered_projects` are final
        # by then, so a project init module asking `get_projects()` gets the same
        # list the outer call will publish. `errors` is the SAME list object
        # the outer call keeps appending to, so this view is live rather than
        # a stale copy — but it is necessarily partial: inits that have not
        # run yet cannot have failed yet.
        return published
    # Read when called, from its defining module, so a patch of it there reaches here.
    from .config.project import discover

    discovered = discover()
    env, projects = discovered.env, discovered.projects
    errors: list[BootstrapError] = list(discovered.errors)
    from .config.dependencies import resolve_dependencies

    resolution = resolve_dependencies(projects)
    errors.extend(resolution.errors)
    # otto's six project instructions (install/uninstall/cleanup/get-logs/
    # install-tools/status) are declared by `otto.project.actions` into the
    # instruction tables' first-party slot (FIRST_PARTY_ACTIONS), which the
    # tables read when they check a registration or derive a view. Declared
    # here, before any project's init runs, so every project's actions class is
    # checked against otto's own; `declare()` rather than the import alone, so
    # a slot emptied since the module was first imported is filled again. The
    # merged flags are checked after the loop instead (see below), because they
    # are only known once every project has declared its own bodies.
    #
    # Not contained like the per-project imports below: this is otto's own module,
    # so a failure here is a bug in otto, not a project's, and framing it as one
    # project's containable error would hide it.
    publish_composed(
        BootstrapResult(
            env=env,
            projects=projects,
            errors=errors,
            warnings=resolution.warnings,
            ordered_projects=resolution.ordered,
        ),
        complete=False,
    )
    try:
        # The project layer sits below the root. Each function is read off its
        # module when called, so a caller may substitute it.
        from .project import actions, commands

        actions.declare()
        for project in resolution.ordered:
            project.add_libs_to_pythonpath()
            with registering_project(project.name):
                for mod in project.init:
                    try:
                        importlib.import_module(mod)
                    except BaseException as e:  # noqa: PERF203 — containment seam: per-item resilience, ANY user-code failure becomes a framed error
                        if not is_containable(e):
                            raise
                        errors.append(BootstrapError(project.root, mod, e))
        # Every project has spoken: check each project instruction's merged
        # options. Not contained -- a cross-project options collision is a
        # declared conflict the user must resolve, not one project's breakage.
        commands.check_project_instruction_options()
        # Every loaded project's host classes are registered now, so its
        # [os_profiles] tables can be checked: a table may name a class an init
        # module registers. A skipped project's init never ran, so it is not judged.
        _contain_data_profile_errors(resolution.ordered, errors)
        # Only now are the provider registries populated, so only now can D2 ask
        # what each project registered. It raises rather than joining `errors`: the
        # contained failures above are "one project's file is broken, the rest still
        # work", while an unscoped providing project would silently apply its products
        # to every host in every lab — there is no degraded mode to offer.
        _check_providing_projects_declare_scope(projects, resolution.ordered)
        result = BootstrapResult(
            env=env,
            projects=projects,
            errors=errors,
            warnings=resolution.warnings,
            ordered_projects=resolution.ordered,
        )
    except BaseException:
        # A bootstrap that RAISED (D2's refusal, an uncontainable init
        # failure) must not leave a partial view standing as though it were
        # the composition root: the next call has to re-run and fail the same
        # way, not read a half-built answer. The failure withdraws both
        # published results.
        clear_composed()
        raise
    # The complete result replaces the in-progress one.
    publish_composed(result, complete=True)
    return result


def invalidate() -> None:
    """Drop every cached bootstrap result so the next call recomputes.

    The supported recovery path for long-lived embedders: fix the project or the
    environment, call ``invalidate()``, and re-run :func:`bootstrap` — the
    prior discovery's errors are discarded together with the discovery that
    produced them. The results published in :mod:`otto.declared` are
    withdrawn too; the initiator stays installed, so the next initiating read
    composes again. The CLI's completion-cache snapshot
    (:func:`otto.registry.get_completion_names`) is cleared as well, and so
    are the cached root keys (:func:`otto.utils.root_key`).
    """
    from .config.project import clear_discovery
    from .utils import root_key

    clear_discovery()
    root_key.cache_clear()
    clear_completion_names()
    # Both published results, the in-progress one too. `bootstrap()` replaces
    # that when it succeeds and withdraws it when it fails, so it is already
    # gone whenever anyone can call this — but "drop every cached bootstrap
    # result" must mean every one, or the day that invariant breaks this
    # function silently stops being the recovery path it advertises.
    clear_composed()


def _reset() -> None:
    """Clear all bootstrap state (test hook; alias of :func:`invalidate`)."""
    invalidate()


def _initiate() -> BootstrapResult:
    """Start composition for a reader below the root: run :func:`bootstrap`.

    The initiator :mod:`otto.declared` holds. ``bootstrap`` is looked up in
    this module when the initiator is called, so a replaced or patched
    ``otto.bootstrap.bootstrap`` is the one that runs.
    """
    return bootstrap()


# Last: installing the initiator runs nothing, so importing this module
# composes nothing (spec 2026-10-11 placement-and-phases §3.5).
install_initiator(_initiate)

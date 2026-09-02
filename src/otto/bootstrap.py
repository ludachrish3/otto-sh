r"""otto's composition root: repo discovery + contained user-code registration.

Replaces ``config``'s import-time side effects. Phase 1 (*discovery*)
parses the environment and repo ``settings.toml`` files — no user code runs.
Between phase 1 and phase 2 the *dependency pass* (``config.dependencies``)
validates each repo's declared dependencies, skips repos whose required deps
are unsatisfied (framed ``DependencyError``\ s), and orders phase-2 registration
topologically (stable — sut-dir order when no deps are declared). Phase 2
(*registration*) imports each repo's init modules and test files, wrapping
every user-module exec so one broken file becomes a framed
:class:`BootstrapError` instead of bricking the process. After phase 2 — and
only after, because the registries it reads are populated BY those imports —
one check runs that does NOT get contained: a repo that registered product or
dev-tool providers, OR that declares ``[[products]]``/``[[dev_tools]]``
entries, must have declared the labs it applies to (:class:`ProjectScopeError`,
spec §D2). Lab loading is deliberately NOT part of bootstrap — it happens
lazily at first access.

``bootstrap()`` is idempotent: the CLI entrypoint calls it before argv
parsing, ``open_context()`` calls it lazily, and repeated calls return the
same :class:`BootstrapResult`.
"""

import importlib
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .errors import OttoError, is_containable
from .registry import registering_repo

if TYPE_CHECKING:
    from .config.repo import Repo
    from .models.settings import OttoEnvSettings


class BootstrapError(OttoError):
    """One user file failed to load during bootstrap registration."""

    def __init__(self, sut_dir: Any, source: str, cause: BaseException) -> None:
        """Frame *cause* as ``repo <sut_dir>: failed to load <source>``."""
        super().__init__(f"repo {sut_dir}: failed to load {source}: {cause!r}")
        self.sut_dir = sut_dir
        self.source = source
        self.__cause__ = cause


class DependencyError(BootstrapError):
    """A repo's declared dependencies cannot be satisfied (or a dependency was skipped)."""

    def __init__(self, sut_dir: Any, message: str) -> None:
        """Frame *message* as ``repo <sut_dir>: <message>``."""
        Exception.__init__(self, f"repo {sut_dir}: {message}")
        self.sut_dir = sut_dir
        self.source = "dependencies"


class ProjectScopeError(BootstrapError):
    """A repo registered providers without declaring the labs it applies to (D2).

    Unlike its siblings this one is NOT contained: a repo whose providers can
    never reach a host is a configuration mistake with no degraded mode worth
    offering, and the whole point of the check is that it is impossible to
    ignore. *message* is used verbatim — it already opens with the repo's name,
    so the ``repo <sut_dir>:`` prefix its siblings add would say it twice.
    """

    def __init__(self, sut_dir: Any, message: str) -> None:
        """Carry *message* unframed; keep *sut_dir* on the exception for callers."""
        Exception.__init__(self, message)
        self.sut_dir = sut_dir
        self.source = "project_scope"


@dataclass(frozen=True)
class BootstrapWarning:
    """A non-fatal dependency finding: rendered at startup, never gates dispatch."""

    sut_dir: Any
    message: str


@dataclass(frozen=True)
class DiscoveryResult:
    """Everything phase 1 produced: environment, repos, contained errors.

    Separate from :class:`BootstrapResult` rather than reused: ``warnings``
    comes from the dependency pass, which runs inside :func:`bootstrap` after
    discovery has returned, so a shared type would carry a field that is
    structurally always empty on this path.
    """

    env: "OttoEnvSettings"
    repos: list["Repo"]
    errors: list[BootstrapError] = field(default_factory=list)


@dataclass(frozen=True)
class BootstrapResult:
    """Everything bootstrap produced: environment, repos, contained errors."""

    env: "OttoEnvSettings"
    repos: list["Repo"]
    errors: list[BootstrapError] = field(default_factory=list)
    warnings: list[BootstrapWarning] = field(default_factory=list)
    ordered_repos: list["Repo"] = field(default_factory=list)
    """Repos in the dependency pass's topological order (skipped repos excluded)."""


_discovered: "DiscoveryResult | None" = None
_result: BootstrapResult | None = None
_in_progress: BootstrapResult | None = None
"""What :func:`bootstrap` already knows while its import phase is still running.

Set once discovery and the dependency pass are done — which is when ``repos``
and ``ordered_repos`` are final — and cleared when ``bootstrap()`` leaves. It
exists so a RE-ENTRANT ``bootstrap()`` has something true to answer with, and
the reentrance is real: the import phase runs repo ``init`` modules and test
files, i.e. user code, and anything there that reaches ``config.get_repos()``
— directly, or by way of a stamped host whose product providers consult
:func:`~otto.config.scope.scope_for_repo` — lands back here with ``_result``
still unset. Without this, that call composed a SECOND, nested root.
"""
_completion_names: dict[str, Any] | None = None


_NO_LAB_PATTERNS = """\
repo '{name}' registers product/dev-tool providers or declares
[[products]]/[[dev_tools]] entries but declares no [project] lab_patterns in
.otto/settings.toml. A providing repo must say which labs it applies to. Add:

    [project]
    lab_patterns = [".*"]   # every lab — make the reach explicit
    #host_patterns = [".*"]

and narrow the patterns to the labs this project actually targets.\
"""
"""D2's refusal. An empty ``lab_patterns = []`` gets this same text: it is not
a narrower declaration, it is the same "no lab" the missing key compiles to."""

_NO_HOST_PATTERNS = """\
repo '{name}' registers product/dev-tool providers or declares
[[products]]/[[dev_tools]] entries but declares an empty [project]
host_patterns in .otto/settings.toml, which admits no host in any lab — so
every provider/entry it registers is dead code. Either drop the key (it
defaults to every host) or name them:

    [project]
    host_patterns = [".*"]   # every host in those labs\
"""
"""The other axis. ``host_patterns = []`` is reachable only by writing it out —
the field defaults to ``[".*"]`` — so it is always a deliberate keystroke, and
always the wrong one on a repo that registers providers or declares entries."""


def _check_providing_repos_declare_scope(
    all_repos: "list[Repo]", ordered_repos: "list[Repo]"
) -> None:
    """Refuse a repo that provides without declaring its fleet (D2).

    "Provides" means either half of the design: a repo that registered a
    product/dev-tool *provider* (the code-first seam) or one whose
    ``.otto/settings.toml`` carries a non-empty ``[[products]]``/
    ``[[dev_tools]]`` array (the declarative seam) — both reach hosts the
    exact same way once built, so both owe the same ``[project]`` scope.

    Both halves are judged over *ordered_repos* (the dependency pass's
    survivors). For providers that is structural: a repo whose required deps
    went unsatisfied never had its init module run, so it cannot have
    registered anything — checking it would be vacuous. For declared entries
    it is parity with :func:`otto.declared.declared_for_host`, which filters
    a dependency-skipped repo's entries out at collection (Chris,
    2026-09-02): entries that can never apply owe no scope, and refusing the
    whole bootstrap over a repo that is already skipped (and already surfaced
    as a dependency finding) would bury the real problem under a second one.

    Runs after phase 2, which is the earliest moment the provider half of the
    question can be answered: the provider registries are populated BY the
    init imports, so a check that ran any sooner would see an empty registry
    and pass every repo. (The declared-entry half is already final after
    phase 1, but is judged here too, in the same pass, for one refusal path.)

    Both provider registries are read because they are separate lists (a
    provider can only ever land in one of them). Providers with no owner —
    registered outside any repo's init import, which in practice means test
    code calling the register function directly — are skipped: there is no
    repo to name and no ``settings.toml`` to point at. So is an owner naming a
    repo that is not among *all_repos*, which is the same "nothing to point
    at" case reached from the other side.

    Args:
        all_repos: Every repo phase 1 discovered, dependency-skipped ones
            included — used only to resolve owner names back to repos.
        ordered_repos: The repos whose init modules actually ran (the
            dependency pass's order) — the list both halves are judged over.

    Raises:
        ProjectScopeError: On the first offending repo, sorted by name so a
            fleet with two of them always reports the same one.
    """
    from .host.dev_tool import _DEV_TOOL_PROVIDERS
    from .host.product import _PRODUCT_PROVIDERS

    # Structural guarantee, made explicit rather than assumed: `owner` is only
    # ever stamped from the `registering_repo` marker bootstrap sets around an
    # init import (see the module docstring), so a provider's owner can never
    # name a repo outside `ordered_repos` in the first place. Intersecting
    # anyway keeps this set's provenance honest against *this* call's inputs
    # instead of a fact about a different function far away.
    ordered_names = {repo.name for repo in ordered_repos}
    provider_owners = {
        owner for _, owner in [*_PRODUCT_PROVIDERS, *_DEV_TOOL_PROVIDERS] if owner
    } & ordered_names
    declared_owners = {
        repo.name for repo in ordered_repos if repo.declared_products or repo.declared_dev_tools
    }
    owners = provider_owners | declared_owners
    by_name = {repo.name: repo for repo in all_repos}
    for name in sorted(owners):
        repo = by_name.get(name)
        if repo is None:
            continue
        scope = repo.project_scope
        if scope is None or not scope.lab_patterns:
            raise ProjectScopeError(repo.sut_dir, _NO_LAB_PATTERNS.format(name=name))
        if not scope.host_patterns:
            raise ProjectScopeError(repo.sut_dir, _NO_HOST_PATTERNS.format(name=name))


def discover() -> DiscoveryResult:
    """Phase 1: env + repo discovery (settings parse only — no user code). Cached.

    Per-repo config-data failures (unreadable or malformed ``settings.toml``)
    get the same containment as phase-2 user-code failures: the repo is
    skipped and a framed :class:`BootstrapError` is recorded, surfacing via
    ``bootstrap().errors`` (help degrades, real dispatch fails loud).
    Env-level failures (bad ``OTTO_SUT_DIRS`` / OTTO_* values) still raise —
    with no environment there is nothing to degrade to.

    The errors ride the cached result itself: recomputing discovery (after
    :func:`invalidate`) necessarily recomputes them, so a stale error cannot
    outlive the discovery that produced it.
    """
    global _discovered  # noqa: PLW0603 — module-level singleton/cache
    if _discovered is None:
        from .config.env import load_otto_env
        from .config.repo import TOML_SETTINGS_PATH, Repo

        env = load_otto_env()
        repos: list[Repo] = []
        errors: list[BootstrapError] = []
        for sut_dir in env.sut_dirs:
            try:
                repos.append(Repo(sut_dir=sut_dir))
            except BaseException as e:  # noqa: PERF203 — containment seam: per-item resilience, ANY config-data failure becomes a framed error
                if not is_containable(e):
                    raise
                errors.append(BootstrapError(sut_dir, str(TOML_SETTINGS_PATH), e))
        _discovered = DiscoveryResult(env=env, repos=repos, errors=errors)
    return _discovered


def bootstrap() -> BootstrapResult:
    """Run the composition root (idempotent): discovery, dependency pass, registration."""
    global _result, _in_progress  # noqa: PLW0603 — module-level singleton/cache
    if _result is not None:
        return _result
    if _in_progress is not None:
        # Re-entered from our own import phase (see _in_progress). Answer with
        # what is already settled rather than composing the root a second time:
        # `repos`/`ordered_repos` are final by then, so a repo init module
        # asking `get_repos()` gets the same list the outer call will publish.
        # `errors` is the SAME list object the outer call keeps appending to,
        # so this view is live rather than a stale copy — but it is necessarily
        # partial: inits that have not run yet cannot have failed yet.
        return _in_progress
    discovered = discover()
    env, repos = discovered.env, discovered.repos
    errors: list[BootstrapError] = list(discovered.errors)
    from .config.dependencies import resolve_dependencies

    resolution = resolve_dependencies(repos)
    errors.extend(resolution.errors)
    # First-party default instructions (install/uninstall/cleanup/get-logs/
    # install-tools/status) register before any repo's init runs. Not contained
    # like the per-repo imports below: this is otto's own module, so a failure
    # here is a bug in otto, not a repo's, and framing it as one repo's
    # containable error would hide it.
    #
    # The decorator's collision guard keys on the registering-repo marker, so
    # for a repo using `@instruction` this ordering is belt-and-braces: the
    # guard fires whichever import ran first. It is the MECHANISM for the
    # routes the decorator never sees -- a repo that registers an
    # InstructionEntry with INSTRUCTIONS.register() directly is refused here
    # only because the first-party names are already taken by the line below,
    # and then by the registry's generic "already registered" rather than the
    # guard's "register a ProjectActions subclass instead". A repo init module
    # that reads INSTRUCTIONS should also see the full first-party set.
    _in_progress = BootstrapResult(
        env=env,
        repos=repos,
        errors=errors,
        warnings=resolution.warnings,
        ordered_repos=resolution.ordered,
    )
    try:
        importlib.import_module("otto.project.instructions")
        for repo in resolution.ordered:
            repo.add_libs_to_pythonpath()
            with registering_repo(repo.name):
                for mod in repo.init:
                    try:
                        importlib.import_module(mod)
                    except BaseException as e:  # noqa: PERF203 — containment seam: per-item resilience, ANY user-code failure becomes a framed error
                        if not is_containable(e):
                            raise
                        errors.append(BootstrapError(repo.sut_dir, mod, e))
                for test_file in repo.iter_test_files():
                    try:
                        repo.import_test_file(test_file)
                    except BaseException as e:  # noqa: PERF203 — containment seam: per-item resilience, ANY user-code failure becomes a framed error
                        if not is_containable(e):
                            raise
                        errors.append(BootstrapError(repo.sut_dir, test_file.name, e))
        # Only now are the provider registries populated, so only now can D2 ask
        # what each repo registered. It raises rather than joining `errors`: the
        # contained failures above are "one repo's file is broken, the rest still
        # work", while an unscoped providing repo would silently apply its products
        # to every host in every lab — there is no degraded mode to offer.
        _check_providing_repos_declare_scope(repos, resolution.ordered)
        _result = BootstrapResult(
            env=env,
            repos=repos,
            errors=errors,
            warnings=resolution.warnings,
            ordered_repos=resolution.ordered,
        )
    finally:
        # Cleared whichever way this leaves. A bootstrap that RAISED (D2's
        # refusal, an uncontainable init failure) must not leave a partial view
        # standing as though it were the composition root: the next call has to
        # re-run and fail the same way, not read a half-built answer.
        _in_progress = None
    return _result


def is_bootstrapped() -> bool:
    """Report whether bootstrap has already started — never forces it.

    True once either ``_result`` (a completed, successful ``bootstrap()``) or
    ``_in_progress`` (the phase-2 import pass is running, ``_result`` not yet
    set) is non-None; false only before the first call, and after
    :func:`invalidate`. Mid-bootstrap counts as bootstrapped ON PURPOSE: an
    init module that builds a host — directly, or by way of a stamped host
    whose product/dev-tool providers apply — is running INSIDE that window,
    and :func:`~otto.config.get_repos` already answers correctly and for free
    there (the re-entrant branch in :func:`bootstrap` returns
    ``_in_progress``, whose ``repos``/``ordered_repos`` are final by then).
    Treating that window as "not bootstrapped" would make a host built mid-
    bootstrap silently drop its declared entries while its providers still
    applied. The non-forcing probe: a caller that must not TRIGGER discovery
    or repo init imports as a side effect of merely asking reads this instead
    of calling :func:`bootstrap` or :func:`~otto.config.get_repos` — only a
    process that has not started bootstrap at all collects nothing.
    """
    return _result is not None or _in_progress is not None


def set_completion_names(names: "dict[str, Any] | None") -> None:
    """Install the completion-cache snapshot (fast path; set by the CLI entry)."""
    global _completion_names  # noqa: PLW0603 — module-level singleton/cache
    _completion_names = names


def get_completion_names() -> "dict[str, Any] | None":
    """Return the completion-cache snapshot, or None outside the fast path."""
    return _completion_names


def invalidate() -> None:
    """Drop every cached bootstrap result so the next call recomputes.

    The supported recovery path for long-lived embedders: fix the repo or the
    environment, call ``invalidate()``, and re-run :func:`bootstrap` — the
    prior discovery's errors are discarded together with the discovery that
    produced them.
    """
    global _discovered, _result, _in_progress, _completion_names  # noqa: PLW0603 — module-level singleton/cache
    _discovered = None
    _result = None
    # Defence in depth. `bootstrap()` clears this in a `finally`, so it is
    # already None whenever anyone can call this — but "drop every cached
    # bootstrap result" must mean every one, or the day that invariant breaks
    # this function silently stops being the recovery path it advertises.
    _in_progress = None
    _completion_names = None


def _reset() -> None:
    """Clear all bootstrap state (test hook; alias of :func:`invalidate`)."""
    invalidate()

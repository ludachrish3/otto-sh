"""Cross-repo composition: ordered walks, one debug sweep, and the converge layer.

Where :mod:`otto.project.actions` is what ONE repo does to the fleet, this is
what the LAB does: the same verbs, composed across every configured repo in
dependency order, plus the host-global steps that belong to no repo at all.

Module-level async functions over the ambient context (the
``otto.config.all_hosts`` idiom), so an instruction, a suite, or a pytest
fixture calls them with zero arguments.

THREE RULES DECIDE EVERY WALK BELOW.

* **Direction.** Build-up walks dependencies first
  (:func:`~otto.config.get_ordered_repos`'s own order); teardown walks it
  reversed, because a dependent must come down before the thing it depends on.
  The order is READ, never rewritten -- ``get_ordered_repos()`` hands back
  bootstrap's own list, so ``reversed()`` is safe where an in-place
  ``.reverse()`` would leave every later caller walking backwards.
* **Failure.** Building is fail-fast: installing a dependent on top of a
  dependency that is known to be missing produces a lab nobody can reason
  about. Tearing down and gathering logs are best-effort: every repo is
  attempted, and the first failure is what returns. A repo that will not go
  must not strand the ones after it.
* **Host-global steps are the orchestrator's alone.** Debug logs and toolchain
  tools belong to a host, not to a repo (spec section 5), so the per-repo
  actions refuse to touch them and this layer performs each ONCE across the
  fleet: the debug sweep AFTER teardown -- teardown-time activity is what those
  logs exist to capture -- and the toolchain steps at the far end, where one
  toolchain shared by every owner can be placed or removed without a repo
  taking its neighbours' tooling with it.

The orchestrator itself is not overrideable in v1 (recorded decision): a repo
customizes by registering its own :class:`~otto.project.actions.ProjectActions`.
"""

from typing import TYPE_CHECKING

from ..result import Result
from ..utils import Status
from .actions import PROJECT_ACTIONS, ProjectActions, _reduce_results, actions_for
from .state import InstallState, ProjectStatus

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable

    from ..config.repo import Repo
    from ..context import OttoContext
    from ..host.host import Host


def _lab() -> "tuple[OttoContext, list[Repo]]":
    """Return the active context and the repos to walk, dependencies first.

    Both lookups are imported HERE rather than at module scope, matching the
    package's circular-import idiom (:mod:`otto.config` and :mod:`otto.context`
    reach back into hosts): ``import otto.project`` must stay cheap, because
    the default instructions import it at CLI startup.
    """
    from ..config import get_ordered_repos
    from ..context import get_context

    return get_context(), get_ordered_repos()


def _first(*results: Result) -> Result:
    """Return the first non-ok result, else Success -- "first failure wins" as a value.

    Every argument has already been awaited by the time this is called, so it
    chooses what to REPORT, not what to attempt.
    """
    for result in results:
        if not result.is_ok:
            return result
    return Result(Status.Success)


async def _walk(
    verb: str,
    call: "Callable[[ProjectActions], Awaitable[Result]]",
    ctx: "OttoContext",
    repos: "Iterable[Repo]",
    *,
    best_effort: bool,
) -> Result:
    """Run *call* against each repo's actions, naming the repo in any failure.

    *best_effort* is the whole difference between a build-up and a teardown:
    False stops at the first failure, True attempts every repo and reports the
    first failure seen. The repo name is baked into the message because the
    :class:`~otto.result.Result` cannot carry it, and "install failed" without
    a repo name is not actionable in a multi-repo lab.
    """
    first_failure: "Result | None" = None
    for repo in repos:
        result = await call(actions_for(repo, ctx))
        if result.is_ok:
            continue
        failure = Result(result.status, msg=f"{verb} failed in repo {repo.name!r}: {result.msg}")
        if not best_effort:
            return failure
        if first_failure is None:
            first_failure = failure
    return first_failure if first_failure is not None else Result(Status.Success)


####################
#  Fleet dispatch
####################

# The host-global sweeps below dispatch through the HOST INSTANCE, never through
# an unbound ``BaseHost`` attribute -- see the note above the twin helpers in
# :mod:`otto.project.actions` for why (``do_for_all_hosts`` calls the function
# object it is handed, so a class attribute freezes out every host-class
# override, ``get_debug_logs`` and ``install_toolchain_tools`` above all).


async def _dispatch_get_debug_logs(host: "Host") -> Result:
    """Fetch *host*'s debug logs through the host's OWN method."""
    return await host.get_debug_logs()


async def _dispatch_install_toolchain_tools(host: "Host") -> Result:
    """Install *host*'s toolchain tools through the host's OWN method."""
    return await host.install_toolchain_tools()


async def _dispatch_remove_toolchain_tools(host: "Host") -> Result:
    """Remove *host*'s toolchain tools through the host's OWN method."""
    return await host.remove_toolchain_tools()


async def _dispatch_toolchain_tools_absent(host: "Host") -> bool:
    """Ask *host* whether its toolchain tools are gone, through its OWN method."""
    return await host.toolchain_tools_absent()


async def _sweep_debug_logs(ctx: "OttoContext") -> Result:
    """Gather every fleet host's debug logs -- the one sweep per operation."""
    return _reduce_results(await ctx.do_for_all_hosts(_dispatch_get_debug_logs))


####################
#  Lifecycle
####################


async def install(ensure: bool = False, recover_partial: bool = True) -> Result:
    """Install every repo's products, dependencies first, stopping at the first failure.

    With *ensure*, this is :func:`ensure_installed` instead: the lab's current
    state is consulted and only the missing work is done (that is what the
    CLI's ``install --ensure`` passes). *recover_partial* is forwarded there
    and means nothing without it -- a plain install has no state to recover
    from, it just installs.
    """
    if ensure:
        return await ensure_installed(recover_partial=recover_partial)
    ctx, repos = _lab()
    return await _walk("install", lambda a: a.install(), ctx, repos, best_effort=False)


async def uninstall(get_product_logs: bool = True, get_debug_logs: bool = True) -> Result:
    """Uninstall every repo in reverse order (best-effort), then sweep debug logs once.

    Product logs come off inside each repo's own uninstall, before that repo's
    teardown. The host-level debug sweep is this layer's, and runs after EVERY
    repo has torn down: those logs belong to no repo, teardown-time activity is
    what they exist to capture, and N repos each sweeping the same host would
    mean N transfers with each overwriting the last.
    """
    ctx, repos = _lab()
    torn = await _walk(
        "uninstall",
        lambda a: a.uninstall(get_product_logs=get_product_logs),
        ctx,
        reversed(repos),
        best_effort=True,
    )
    if not get_debug_logs:
        return torn
    return _first(torn, await _sweep_debug_logs(ctx))


async def cleanup(get_product_logs: bool = True, get_debug_logs: bool = True) -> Result:
    """Clean up every repo in reverse order, sweep debug logs, then remove the toolchain.

    Strictly more than :func:`uninstall`: each repo also removes its own dev
    tools, and the host-global toolchain tools come off at the end. That last
    step is here and nowhere else -- one toolchain serves every owner on a
    host, so a repo removing it would take its neighbours' tooling with it.

    Ordering, twice over: the debug sweep is taken before the toolchain goes,
    so no log retrieval depends on tooling this step is deleting; and the
    toolchain removal runs even when the sweep or a repo failed, because a
    best-effort teardown that abandons the last step leaves exactly the
    remnants ``cleanup`` exists to remove.
    """
    ctx, repos = _lab()
    cleaned = await _walk(
        "cleanup",
        lambda a: a.cleanup(get_product_logs=get_product_logs),
        ctx,
        reversed(repos),
        best_effort=True,
    )
    swept = await _sweep_debug_logs(ctx) if get_debug_logs else Result(Status.Success)
    removed = _reduce_results(await ctx.do_for_all_hosts(_dispatch_remove_toolchain_tools))
    return _first(cleaned, swept, removed)


####################
#  Logs
####################


async def get_logs(
    product: bool = True, debug: bool = True, require_product_logs: bool = False
) -> Result:
    """Gather every repo's product logs (best-effort), then sweep debug logs once.

    Walk order is immaterial here, so it is the natural one. Each repo hauls
    its own products' logs -- those are owner-scoped -- while the single debug
    sweep is host-level and this layer's, exactly as in :func:`uninstall`.

    *require_product_logs* with ``product=False`` is a contradiction and is
    refused up front. The per-repo actions refuse it too, but that refusal
    never fires in a lab with no repos, and a requirement that is parsed but
    unenforceable would report success having promised logs nobody went looking
    for.
    """
    if require_product_logs and not product:
        return Result(
            Status.Error,
            msg=(
                "require_product_logs cannot be satisfied with product=False: "
                "the product-log haul it requires is the step being skipped. "
                "Gather product logs, or drop the requirement."
            ),
        )
    ctx, repos = _lab()
    hauled = await _walk(
        "get_logs",
        lambda a: a.get_logs(product=product, require_product_logs=require_product_logs),
        ctx,
        repos,
        best_effort=True,
    )
    if not debug:
        return hauled
    return _first(hauled, await _sweep_debug_logs(ctx))


####################
#  Tools
####################


async def install_tools(dev: bool = True, toolchain: bool = False) -> Result:
    """Install every repo's dev tools, then (when asked) the host toolchains.

    Fail-fast like :func:`install`, and in the same order as the host verb it
    composes: dev tools first, then the toolchain sweep, which is skipped
    entirely when the dev walk failed rather than layering a toolchain on top
    of tooling that is known to be missing.

    THE TOOLCHAIN HALF IS ONLY HERE. A host has one toolchain shared by every
    owner, so ``ProjectActions.install_tools(toolchain=True)`` is a declared
    no-op; if this sweep were dropped, asking for a toolchain would be a silent
    no-op end to end.
    """
    ctx, repos = _lab()
    installed = await _walk(
        "install_tools",
        lambda a: a.install_tools(dev=dev),
        ctx,
        repos,
        best_effort=False,
    )
    if not installed.is_ok or not toolchain:
        return installed
    return _reduce_results(await ctx.do_for_all_hosts(_dispatch_install_toolchain_tools))


####################
#  Questions
####################


def _counts(actions: ProjectActions) -> bool:
    """Whether *actions*' repo contributes to the lab-level install state.

    A repo that owns no products anywhere AND registered no actions of its own
    (a docs-only repo, say) has no install state to contribute; counting it
    would drag every aggregate to PARTIAL forever. A repo with a registered
    subclass is always counted -- it opted into having an opinion, and its
    ``status()`` may well be computed from something otto cannot see.
    """
    return actions.owns_products or actions.repo.name in PROJECT_ACTIONS


def _aggregate(states: "Iterable[InstallState]") -> InstallState:
    """Reduce counted repos' states to the lab's answer.

    THE UNINSTALLED TEST COMES FIRST so that zero counted repos aggregate to
    UNINSTALLED rather than to a vacuous INSTALLED -- the same rule as
    :meth:`otto.host.host.BaseHost.is_installed`'s empty-products case:
    nothing that could be installed is not "installed".
    """
    states = list(states)
    if all(state is InstallState.UNINSTALLED for state in states):
        return InstallState.UNINSTALLED
    if all(state is InstallState.INSTALLED for state in states):
        return InstallState.INSTALLED
    return InstallState.PARTIAL


async def status() -> ProjectStatus:
    """Report each counted repo's install state and the lab-level aggregate.

    A repo the counted-repo rule excludes is absent from
    :attr:`~otto.project.state.ProjectStatus.repos` entirely rather than
    present with a made-up state.
    """
    ctx, repos = _lab()
    states: "dict[str, InstallState]" = {}
    for repo in repos:
        actions = actions_for(repo, ctx)
        if _counts(actions):
            states[repo.name] = await actions.status()
    return ProjectStatus(overall=_aggregate(states.values()), repos=states)


async def is_clean() -> bool:
    """Whether no repo's products or dev tools, and no host's toolchain tools, remain.

    EVERY repo is asked, not only the counted ones. The counted-repo rule
    exists to keep an opinionless repo from dragging an AGGREGATE STATE, and a
    repo with nothing installed answers True here for free -- while skipping it
    would miss the dev tools of a tooling repo that owns no products at all
    (``owns_products`` cannot see tools).

    A host that could not answer RAISES rather than counting as unclean.
    ``do_for_all_hosts`` captures exceptions as values, and reading a dry run's
    refusal -- or a dead transport -- as "not clean" would send a converge into
    a cleanup on a fact nobody established.
    """
    ctx, repos = _lab()
    for repo in repos:
        if not await actions_for(repo, ctx).is_clean():
            return False
    for outcome in (await ctx.do_for_all_hosts(_dispatch_toolchain_tools_absent)).values():
        if isinstance(outcome, BaseException):
            raise outcome
        if not outcome:
            return False
    return True


####################
#  Converge
####################


async def ensure_installed(recover_partial: bool = True) -> Result:
    """Bring the lab to INSTALLED, doing only the work its current state needs.

    INSTALLED is a skip. UNINSTALLED installs. PARTIAL is the state this exists
    for: half-installed remnants are torn down first and the install runs
    fresh, because installing over remnants is how a lab gets into this state
    in the first place. ``recover_partial=False`` proceeds straight to the
    install for callers who know their remnants are harmless.

    A failed recovery teardown STOPS the converge and returns that failure.
    Installing on top of remnants known to be stranded would reproduce the very
    PARTIAL state this was called to fix, and report success doing it.
    """
    state = (await status()).overall
    if state is InstallState.INSTALLED:
        return Result(Status.Skipped, msg="already installed")
    if state is InstallState.PARTIAL and recover_partial:
        torn = await uninstall()
        if not torn.is_ok:
            refusal = "partial-install recovery: teardown failed, not installing over it"
            return Result(torn.status, msg=f"{refusal}: {torn.msg}")
    return await install()


async def ensure_uninstalled() -> Result:
    """Bring the lab to UNINSTALLED; a fully uninstalled lab is a skip.

    PARTIAL runs the uninstall -- that is the case a boolean ``is_installed``
    could not see, and leaving half a lab installed is what it would do.
    """
    if (await status()).overall is InstallState.UNINSTALLED:
        return Result(Status.Skipped, msg="already uninstalled")
    return await uninstall()


async def ensure_clean() -> Result:
    """Run :func:`cleanup` unless the lab is already clean.

    Asks :func:`is_clean` rather than :func:`status`: clean is a stronger
    condition than uninstalled (dev tools and toolchain tools are not
    products), so an uninstalled-but-tooled lab still gets cleaned.
    """
    if await is_clean():
        return Result(Status.Skipped, msg="already clean")
    return await cleanup()

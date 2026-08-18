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
  taking its neighbours' tooling with it. Netem impairments and otto tunnels
  join them in :func:`cleanup` for the same reason, one step further out: they
  belong to the LAB rather than to any host, and no repo's products or dev
  tools put them there.

The orchestrator itself is not overrideable in v1 (recorded decision): a repo
customizes by registering its own :class:`~otto.project.actions.ProjectActions`.
"""

from typing import TYPE_CHECKING

from ..result import Result
from ..utils import Status
from .actions import (
    _REQUIRE_PRODUCT_LOGS_CONTRADICTION,
    PROJECT_ACTIONS,
    ProjectActions,
    _reduce_results,
    actions_for,
)
from .state import InstallState, ProjectStatus

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable

    from ..config.lab import Lab
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


def _reported(*results: Result) -> Result:
    """Return the first failure, else the first DECLINE, else Success.

    :func:`_first` with one more rung, and the rung exists for exactly one
    caller: :func:`cleanup`'s impairment reset, which answers
    ``Status.Skipped`` when :func:`otto.link.manage.repair_all` DECLINED a link
    it might otherwise have repaired (a foreign qdisc, a management-interface
    refusal) rather than failing on it. A decline is ``is_ok`` -- it must not
    abort a best-effort teardown and it is not a failure, because otto never
    applied what it is refusing to remove -- but it is not Success either:
    something is still on the wire that ``cleanup`` did not take off, and
    returning a bare ``Result(Success)`` over it would say the opposite in the
    one field a caller reads.

    A FAILURE OUTRANKS A DECLINE IN EITHER ARGUMENT ORDER, which is why the
    failure pass runs over all of them BEFORE the decline pass starts rather
    than one loop returning the first non-Success it meets. That one loop
    passes every test written in the order the steps run -- and is wrong in
    exactly the case a real lab produces: the impairment reset declines first
    (any lab with an unreadable or foreign-qdisc link), the tunnel reap fails
    after it, and "first non-Success" hands back the DECLINE. ``is_ok`` is
    ``True`` on it, so ``otto run cleanup`` would exit 0 with tunnel processes
    still running and ``ensure_clean`` would call the lab converged.
    """
    failure = _first(*results)
    if not failure.is_ok:
        return failure
    for result in results:
        if result.status is not Status.Success:
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
#  Lab infrastructure
####################

# NEITHER OF THE TWO STEPS BELOW IS A FLEET WALK, and that is why they do not
# dispatch through ``do_for_all_hosts`` like everything above. A netem qdisc
# and a tunnel are LAB objects: an impairment is placed per LINK (whose two
# ends are two different hosts), and a tunnel is a chain of processes across
# every hop it crosses. ``otto.link`` and ``otto.tunnel`` already own the
# lab-wide sweeps -- with the refusals, the post-mutation verifies and the
# dry-run planning that go with them -- so this layer hands each the lab and
# reports what comes back. Nothing here builds a `tc` or a `kill`.
#
# Both are imported inside the call, like everything else this module looks up:
# ``import otto.project`` must stay cheap, because the default instructions
# import it at CLI startup, and ``otto.link.manage`` alone drags in the daemon
# and sentinel modules that ``otto.link``'s own package __getattr__ exists to
# keep off cheap importers.


def _live_refusals(lab: "Lab", skipped: "list[str]") -> "list[str]":
    """Return the ``repair_all`` skips worth reporting, dropping the structural ones.

    ``repair_all`` files every refusal in one bucket, and two very different
    facts land there. A link that could NEVER carry otto's impairment -- every
    implicit hop edge is one, because
    :func:`~otto.link.derive.implicit_links` builds endpoints with no named
    interface -- was refused before any device was asked, and there is nothing
    for a cleanup to say about it: the lab is not dirtier for having hop edges.
    A link that COULD have been impaired and was declined anyway (a foreign
    qdisc, a management-interface or hop-transit refusal, a host with no
    impairer) is the opposite: something may well be on that netdev, and otto
    has just declined to take it off.

    Reporting both would make ``cleanup`` unable to return Success on any real
    lab -- an N-host lab resolves at least N implicit ids -- which would drain
    the decline of its meaning exactly when it carries the most: a genuine
    foreign-qdisc refusal would be status-indistinguishable from the standing
    noise, and every message would carry N lines nobody can act on.

    The split is asked, not parsed:
    :func:`~otto.link.placement.impairment_refusal` is the same pure predicate
    ``otto link list`` prints its refusals from, so this cannot drift from the
    placement layer's own answer. Only the ids it names are dropped, matched on
    ``repair_all``'s own ``"<link id>: <why>"`` prefix, so a live refusal
    against one of those links -- there is none today, since the structural
    check fires first -- would still be reported.
    """
    from ..link.placement import BOTH_DIRECTIONS, impairment_refusal

    structural = tuple(
        f"{link.id}: "
        for link in lab.static_links()
        if impairment_refusal(link, BOTH_DIRECTIONS) is not None
    )
    if not structural:
        return list(skipped)
    return [entry for entry in skipped if not entry.startswith(structural)]


async def _reset_impairments(ctx: "OttoContext") -> Result:
    """Repair every lab link that can carry otto's netem, refusals and all.

    Goes through :func:`otto.link.manage.repair_all`, which walks the lab's own
    static links -- refusing the ones it could never have impaired -- and keeps
    the ``_ensure_not_foreign`` rail: a root qdisc otto did not create is never
    cleared. That rail is the reason this step is not a device-side "delete
    every qdisc" enumeration -- such a sweep would clobber the ``tc``
    configuration a human put on a shared host, and a cleanup that does that
    once is a cleanup nobody runs again.

    ``repair_all`` never raises. Its outcomes become three different answers,
    because they are three different facts:

    * :attr:`~otto.link.manage.RepairAllReport.failures` -- a link otto tried
      and could not repair (host down, a clear that did not take). A failure.
    * :attr:`~otto.link.manage.RepairAllReport.skipped` -- a link otto DECLINED
      to touch, once :func:`_live_refusals` has dropped the links that were
      never impairable in the first place. Not a failure, and not a success
      either: see :func:`_reported`. The reasons are named rather than counted,
      matching what ``otto link repair --all`` prints from the same bucket.
    * :attr:`~otto.link.manage.RepairAllReport.dry_run` -- nothing was read and
      nothing was reset. ``Status.NotRun``, the dry-run contract's own answer
      for an acting verb, rather than the Success an empty report would
      otherwise be indistinguishable from.
    """
    from ..link.manage import repair_all

    report = await repair_all(ctx.lab)
    if report.dry_run:
        return Result(
            Status.NotRun,
            msg="dry run: no link was read and no impairment was reset",
        )
    declined = "; ".join(_live_refusals(ctx.lab, report.skipped))
    if report.failures:
        also = f" (declined: {declined})" if declined else ""
        failed = "; ".join(report.failures)
        return Result(Status.Failed, msg=f"impairment reset failed: {failed}{also}")
    if declined:
        return Result(Status.Skipped, msg=f"impairment reset declined: {declined}")
    return Result(Status.Success)


async def _remove_tunnels(ctx: "OttoContext") -> Result:
    """Reap every otto tunnel in the lab, and report anything that survived.

    Goes through :func:`otto.tunnel.manage.remove_all_tunnels`, which is
    already discovery-driven, lab-wide and owner-agnostic: it reaps every
    sentinel-tagged process on every ``has_bash`` host, from the processes
    themselves rather than from lab data, so a tunnel added by a colleague or
    by a crashed run comes down with the rest.

    :attr:`~otto.tunnel.manage.RemovedReport.survivors` is a FAILURE, and it is
    the whole reason that report re-scans after killing: a process still
    present after the kill is a tunnel still carrying traffic, and reporting
    the reap on the strength of ``removed_ids`` alone would call that clean.
    An unreachable host is a failure for the sibling reason -- the scan never
    saw it, so a tunnel outlives the reap on exactly those hosts, which is why
    ``otto tunnel remove --all`` exits 1 on them too.
    """
    from ..tunnel.manage import remove_all_tunnels

    report = await remove_all_tunnels(ctx.lab)
    if report.plan is not None:
        return Result(
            Status.NotRun,
            msg="dry run: no host was scanned for tunnel processes and none was reaped",
        )
    if report.survivors:
        still = ", ".join(f"{host_id}/{pid}" for host_id, pid in report.survivors)
        return Result(Status.Failed, msg=f"tunnel processes survived the kill: {still}")
    if report.unreachable:
        return Result(
            Status.Failed,
            msg=f"tunnel reap could not reach: {', '.join(report.unreachable)}",
        )
    return Result(Status.Success)


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


async def cleanup(
    get_product_logs: bool = True,
    get_debug_logs: bool = True,
    reset_impairments: bool = True,
    remove_tunnels: bool = True,
) -> Result:
    """Clean the lab: every repo, the debug logs, the toolchain, impairments, tunnels.

    Strictly more than :func:`uninstall`: each repo also removes its own dev
    tools, the host-global toolchain tools come off, and the lab's own
    infrastructure -- netem impairments and otto tunnels -- comes down with
    them. The last three steps are here and nowhere else: one toolchain serves
    every owner on a host, and an impairment or a tunnel belongs to no repo at
    all, so a repo performing any of them would act for its neighbours.

    ORDERING, AND THE LAST STEP IS THE ONE THAT MATTERS. The debug sweep is
    taken before the toolchain goes, so no log retrieval depends on tooling
    this step is deleting. **The tunnel reap is last of all, because a tunnel
    can BE the access path to a host**: reaping it earlier severs the
    connection the repo walk, the log sweep and the toolchain removal still
    need. Resetting impairments sits immediately before it for the mirrored
    reason -- clearing delay and loss off a link only improves the path
    everything above ran over, so it can afford to wait until they are done.

    Every step runs even when an earlier one failed, and the first failure is
    what returns: a best-effort teardown that abandons the steps after a
    failure leaves exactly the remnants ``cleanup`` exists to remove. A link
    otto DECLINED to repair -- a foreign qdisc, a management-interface refusal
    -- is reported WITHOUT failing the run: the result comes back
    ``Status.Skipped`` naming each one, which is ``is_ok`` and is deliberately
    not ``Status.Success``, because something otto did not take off is still
    there. A link that could never have been impaired at all -- every implicit
    hop edge -- is dropped from that report instead, because a lab is not
    dirtier for having hop edges and reporting them would leave ``cleanup``
    unable to answer Success on any real lab.
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
    repaired = await _reset_impairments(ctx) if reset_impairments else Result(Status.Success)
    reaped = await _remove_tunnels(ctx) if remove_tunnels else Result(Status.Success)
    return _reported(cleaned, swept, removed, repaired, reaped)


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
        return Result(Status.Error, msg=_REQUIRE_PRODUCT_LOGS_CONTRADICTION)
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


async def _impairment_present(ctx: "OttoContext") -> bool:
    """Whether any lab link carries an impairment :func:`cleanup` would reset.

    Reads through :func:`otto.link.manage.read_link_states` -- the read-side
    twin of the ``repair_all`` this answers for -- so asking the question
    changes nothing.

    THE SCOPE IS THE ONE ``repair_all`` ACTS ON, deliberately, and the foreign
    qdisc is where that bites: :func:`otto.link.manage._ensure_not_foreign`
    refuses to clear a root qdisc otto did not create, so a link carrying one
    is a link ``cleanup`` provably cannot change. Counting it as unclean would
    make every ``ensure_clean`` run a cleanup that cannot move the answer,
    forever. A link that is structurally unimpairable (every implicit hop edge)
    has no state to read and none to remove.

    A link whose state could not be READ raises instead of answering.
    ``read_link_states`` promises never to raise per link -- it reports the
    three ways a read can fail as flags -- so this is the layer that has to
    refuse: "clean" would be a fabrication (the dry-run case is exactly a link
    nobody looked at), and "not clean" would send a converge into a cleanup on
    a fact nobody established. That is the same rule the toolchain probe above
    follows, in the same words.
    """
    from ..link.manage import (
        LinkCommandFailedError,
        LinkHostUnreachableError,
        LinkNotMeasuredError,
        read_link_states,
    )

    for state in await read_link_states(ctx.lab):
        if state.not_measured:
            raise LinkNotMeasuredError(
                f"link {state.link.id!r}: nothing was read, so whether it carries an "
                f"impairment is unknown"
            )
        if state.unreachable:
            raise LinkHostUnreachableError(
                f"link {state.link.id!r}: a placement host could not be reached, so "
                f"whether it carries an impairment is unknown"
            )
        if state.read_errors:
            raise LinkCommandFailedError(
                f"link {state.link.id!r}: the impairment read failed: "
                f"{'; '.join(sorted(state.read_errors.values()))}"
            )
        for direction in state.by_direction.values():
            if direction is not None and (direction.whole is not None or direction.scoped):
                return True
    return False


async def _tunnel_present(ctx: "OttoContext") -> bool:
    """Whether any otto tunnel is running in the lab.

    Reads through :func:`otto.tunnel.discovery.discover_tunnels` -- the read
    side of the ``remove_all_tunnels`` this answers for, and the same scan the
    reap itself starts from, so the two cannot disagree about what a tunnel is.

    A scan that measured nothing raises, for the reason the link read above
    does: an empty tunnel list is precisely what a clean lab returns, so a dry
    run's declined scan and a host that never answered would both come back as
    "clean" from a check that never looked.

    A TUNNEL ALREADY IN HAND ANSWERS FIRST, and the order of the two checks
    below is that decision. An incomplete scan matters only while the answer is
    still open: once ANY tunnel has been seen, the lab is dirty and no host
    that failed to answer can make it clean again, so raising there would
    refuse a question otto has already answered -- and would strand
    ``ensure_clean`` on a lab it can see needs cleaning. It applies to
    ``unreachable`` alone: ``not_measured`` means nothing was asked at all, so
    the tunnel list is empty by construction and the raise below is the only
    outcome available.
    """
    from ..host.errors import HostUnreachableError
    from ..tunnel.discovery import TunnelNotMeasuredError, discover_tunnels

    discovery = await discover_tunnels(ctx.lab)
    if discovery.tunnels:
        return True
    if discovery.not_measured:
        raise TunnelNotMeasuredError(
            "no host was scanned for tunnel processes, so whether the lab carries a "
            "tunnel is unknown"
        )
    if discovery.unreachable:
        raise HostUnreachableError(
            f"tunnel scan could not reach {', '.join(discovery.unreachable)}, so whether "
            f"the lab carries a tunnel is unknown"
        )
    return False


async def is_clean() -> bool:
    """Whether nothing :func:`cleanup` removes is left in the lab.

    EVERY repo is asked, not only the counted ones. The counted-repo rule
    exists to keep an opinionless repo from dragging an AGGREGATE STATE, and a
    repo with nothing installed answers True here for free -- while skipping it
    would miss the dev tools of a tooling repo that owns no products at all
    (``owns_products`` cannot see tools).

    THE QUESTION MATCHES THE VERB, step for step: products and dev tools, the
    hosts' toolchain tools, then the lab's own impairments and tunnels, in
    ``cleanup``'s own order. That agreement is the whole contract -- without
    the last two, ``ensure_clean`` would report a lab dirty only in tunnels as
    already clean while ``otto run cleanup`` visibly reaped them, which is the
    surface split-brain this package exists to prevent.

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
    if await _impairment_present(ctx):
        return False
    return not await _tunnel_present(ctx)


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

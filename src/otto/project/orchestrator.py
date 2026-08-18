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
from .state import (
    Cleanliness,
    CleanlinessItem,
    CleanlinessKind,
    CleanlinessReport,
    InstallState,
    ProjectStatus,
)

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
    facts land there. A link ``repair_all`` COULD NEVER HAVE REPAIRED -- every
    implicit hop edge is one, because
    :func:`~otto.link.derive.implicit_links` builds endpoints with no named
    interface -- was refused before any device was asked, and there is nothing
    for a cleanup to say about it: the lab is not dirtier for having hop edges.
    A link that COULD have been repaired and was declined anyway (a foreign
    qdisc, a management-interface or hop-transit refusal, a host with no
    impairer) is the opposite: something may well be on that netdev, and otto
    has just declined to take it off.

    "COULD NEVER HAVE REPAIRED" IS NOT "COULD NEVER HAVE BEEN IMPAIRED", and
    the difference is one real shape: a link with a named interface on ONE end
    is refused by ``impairment_refusal(link, BOTH_DIRECTIONS)`` while ``otto
    link impair --from <that end>`` places netem on it happily. The filter is
    right anyway, because it is asked in the directions the sweep it filters
    actually works in -- ``repair_all`` goes through ``repair_link``, which
    takes ``_directions(link, None)``, both of them -- so a half-named link is
    one the sweep can never clear either. Only the CLAIM has to stay the
    narrower one: this drops links a cleanup could not have acted on, not links
    that could not be impaired.

    Reporting both would make ``cleanup`` unable to return Success on any real
    lab -- an N-host lab resolves at least N implicit ids -- which would drain
    the decline of its meaning exactly when it carries the most: a genuine
    foreign-qdisc refusal would be status-indistinguishable from the standing
    noise, and every message would carry N lines nobody can act on.

    The split is asked, not parsed:
    :func:`~otto.link.placement.impairment_refusal` is the same pure predicate
    ``otto link list`` prints its refusals from, so this cannot drift from the
    placement layer's own answer. THE DROP IS BY LINK ID, NOT BY REASON: the
    match is on ``repair_all``'s own ``"<link id>: <why>"`` prefix, so every
    skip filed against a structurally-refused link goes, whatever text it
    carries. That discards nothing today, because such a link cannot also
    produce a live refusal: both structural causes -- the local-host rule and
    an endpoint with no named interface -- are decided from lab data before any
    device is contacted (``ensure_not_local_link`` then ``endpoint_placements``,
    on the dry-run path as well as the real one), so the link's one skip entry
    IS the structural one, and the live refusals a scan finds
    (management interface, hop transit) are only reachable past a check it
    never passes.
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


async def is_uninstalled() -> bool:
    """Whether the lab aggregate is UNINSTALLED -- no counted repo has a product on.

    DELIBERATELY UNPAIRED: there is no lab-level ``is_installed()``, and adding
    one is the change to refuse. False on such a boolean would mean PARTIAL and
    UNINSTALLED alike -- the exact conflation
    :class:`~otto.project.state.InstallState`'s third
    member exists to resolve, and the callers most likely to ask are the
    converges, which must tell those two apart to choose between installing and
    tearing the remnants down first. Everything except "is there nothing
    installed at all" reads :func:`status`.
    """
    return (await status()).overall is InstallState.UNINSTALLED


####################
#  Cleanliness probes
####################

# ONE PROBE PER AXIS, TWO CONSUMERS, AND THEY DIFFER ONLY IN WHAT THEY DO WITH
# A NON-FACT. :func:`is_clean` must refuse to answer when something would not
# say -- a converge that cleans on an unread state acts on nothing anybody
# established -- while :func:`cleanliness` must print the rows it did get and
# MARK the rest, because a display that dies on one unreachable host shows
# nothing about the twelve that answered. Both duties are real, and neither may
# get its own copy of "what does a leftover look like": that mirrored logic is
# precisely what drifts, and a cleanliness check that disagreed with `cleanup`
# is the split-brain this package exists to prevent.
#
# So every probe below answers in :class:`~otto.project.state.CleanlinessItem`
# rows that CARRY the refusal instead of raising it, and each consumer decides
# what to do with one.


def _row(kind: "CleanlinessKind", name: str, clean: bool, detail: str = "") -> CleanlinessItem:
    """One row whose answer was actually obtained."""
    return CleanlinessItem(
        kind=kind,
        name=name,
        state=Cleanliness.CLEAN if clean else Cleanliness.DIRTY,
        detail=detail,
    )


def _unreadable(
    kind: "CleanlinessKind", name: str, error: BaseException, detail: str
) -> CleanlinessItem:
    """One row nobody could read: the refusal held, plus a phrase to print.

    *detail* is deliberately not ``str(error)``. The exception is raised on its
    own by :func:`is_clean` and has to name its subject to be readable there;
    the row already carries that name in a column of its own, so repeating it
    in the cell beside it is noise in the only surface that prints both.
    """
    return CleanlinessItem(
        kind=kind, name=name, state=Cleanliness.UNKNOWN, detail=detail, error=error
    )


def _verdict(items: "Iterable[CleanlinessItem]") -> bool:
    """Reduce one axis' rows to ``is_clean``'s boolean, raising what nobody read.

    A DEFINITIVE DIRTY ANSWER OUTRANKS AN UNREADABLE ROW IN EITHER ORDER, which
    is why nothing is raised until the whole axis has been walked. An
    incomplete scan matters only while the answer is still open: once one link
    has been seen carrying netem -- or one tunnel found running -- the lab needs
    cleaning, and a host that failed to answer cannot make it clean again.
    Raising there would refuse a question otto has already answered, and strand
    :func:`ensure_clean` on a lab it can see needs cleaning.

    Otherwise the first unreadable row's own exception is raised UNWRAPPED: an
    unreachable host, a ``tc`` that is not installed and a dry run's declined
    read are three different classes, and the callers that handle them are
    written against those types. ``error`` is non-None on exactly the UNKNOWN
    rows -- :class:`~otto.project.state.CleanlinessItem` pins the two to each
    other in both directions -- which is what makes the single assignment below
    total, with no arm for a row that is unreadable and yet has nothing to
    raise.
    """
    unreadable: "BaseException | None" = None
    for item in items:
        if item.state is Cleanliness.DIRTY:
            return False
        if unreadable is None:
            unreadable = item.error
    if unreadable is not None:
        raise unreadable
    return True


async def _repo_items(ctx: "OttoContext", repos: "Iterable[Repo]") -> "list[CleanlinessItem]":
    """One row per repo, from that repo's OWN ``is_clean`` -- products and dev tools.

    EVERY repo is asked, not only the counted ones. The counted-repo rule
    exists to keep an opinionless repo from dragging an install AGGREGATE, and
    a repo with nothing installed answers clean here for free -- while skipping
    it would miss the dev tools of a tooling repo that owns no products at all
    (``owns_products`` cannot see tools).

    A repo whose probe RAISES becomes an unreadable row rather than taking the
    whole report down with it. :func:`is_clean` keeps a loop of its own over
    the same method instead of reusing this one, and that is not a second copy
    of anything: both call :meth:`~otto.project.actions.ProjectActions.is_clean`,
    the single authority on what one repo's leftovers are. What differs is only
    the walk -- the boolean stops at the first dirty repo, because the repos
    after it cannot change the answer and probing them is device work for
    nothing, while a report has to ask them all to have anything to show.
    """
    items: "list[CleanlinessItem]" = []
    for repo in repos:
        try:
            clean = await actions_for(repo, ctx).is_clean()
        except Exception as exc:  # noqa: BLE001,PERF203 — a display marks what it could not learn; is_clean is the surface that refuses to answer
            items.append(
                _unreadable(CleanlinessKind.REPO, repo.name, exc, f"the repo probe failed: {exc!r}")
            )
        else:
            items.append(_row(CleanlinessKind.REPO, repo.name, clean))
    return items


async def _toolchain_items(ctx: "OttoContext") -> "list[CleanlinessItem]":
    """One row per fleet host, on whether the toolchain tools :func:`cleanup` removes are gone.

    Host-global, which is why this is the orchestrator's question and no
    repo's: one toolchain serves every owner on a host.
    :meth:`~otto.context.OttoContext.do_for_all_hosts` captures a host's
    refusal as a VALUE, so a dry run's decline or a dead transport arrives here
    as an entry to file rather than as a raise to catch.
    """
    outcomes = await ctx.do_for_all_hosts(_dispatch_toolchain_tools_absent)
    items: "list[CleanlinessItem]" = []
    for host_id, outcome in outcomes.items():
        if isinstance(outcome, BaseException):
            items.append(
                _unreadable(
                    CleanlinessKind.TOOLCHAIN,
                    host_id,
                    outcome,
                    f"the toolchain probe did not answer: {outcome!r}",
                )
            )
        else:
            items.append(_row(CleanlinessKind.TOOLCHAIN, host_id, outcome))
    return items


async def _impairment_items(ctx: "OttoContext") -> "list[CleanlinessItem]":
    """One row per lab link that could carry an impairment :func:`cleanup` would reset.

    Reads through :func:`otto.link.manage.read_link_states` -- the read-side
    twin of the ``repair_all`` this answers for -- so asking the question
    changes nothing.

    THE SCOPE IS THE ONE ``repair_all`` ACTS ON, deliberately, and the foreign
    qdisc is where that bites: :func:`otto.link.manage._ensure_not_foreign`
    refuses to clear a root qdisc otto did not create, so a link carrying one
    is a link ``cleanup`` provably cannot change. Counting it as unclean would
    make every :func:`ensure_clean` run a cleanup that cannot move the answer,
    forever -- so a foreign tree leaves the row CLEAN.

    ANY LINK OTTO REFUSED TO IMPAIR GETS NO ROW AT ALL, which is WIDER than
    the structural refusals :func:`_live_refusals` drops from ``cleanup``'s own
    report. ``read_link_states`` clears ``impairable`` for both kinds: the
    structural ones, decided from lab data (every implicit hop edge -- no named
    interface -- and any link with the local host as an endpoint), and the LIVE
    ones only a scan can find (a management interface, a hop-transit netdev, an
    in-path placement that would not resolve).

    Skipping both is deliberate, and the first reason applies to every refused
    link: a refusal is answered BEFORE the read, so ``by_direction`` comes back
    empty and there is nothing to show. Rendering that as a clean row would
    claim a link is clear that nobody looked at -- the fabrication this whole
    axis is built to avoid -- and it is not the kind of unknown an operator can
    act on either, since otto refusing to touch a link is a fact about the
    lab's shape rather than about what is left on it.

    Beyond that they are dropped for different reasons. The structural ones are
    standing noise: an N-host lab resolves at least N implicit hop edges, none
    with any state to read or to remove, and N rows saying so would bury the
    handful that mean something. The live ones have a better place to be said,
    and ``cleanup`` says it: they are exactly the bucket ``_live_refusals``
    KEEPS, so a declined management interface comes back named in cleanup's own
    ``Skipped`` message, from the verb that declined it, rather than as a status
    row that could only repeat the refusal without the action behind it.

    A link whose state could not be READ becomes an unreadable row.
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

    kind = CleanlinessKind.IMPAIRMENT
    items: "list[CleanlinessItem]" = []
    for state in await read_link_states(ctx.lab):
        link_id = state.link.id
        if not state.impairable:
            continue
        if state.not_measured:
            items.append(
                _unreadable(
                    kind,
                    link_id,
                    LinkNotMeasuredError(
                        f"link {link_id!r}: nothing was read, so whether it carries an "
                        f"impairment is unknown"
                    ),
                    "nothing was read",
                )
            )
        elif state.unreachable:
            items.append(
                _unreadable(
                    kind,
                    link_id,
                    LinkHostUnreachableError(
                        f"link {link_id!r}: a placement host could not be reached, so "
                        f"whether it carries an impairment is unknown"
                    ),
                    "a placement host could not be reached",
                )
            )
        elif state.read_errors:
            why = "; ".join(sorted(state.read_errors.values()))
            items.append(
                _unreadable(
                    kind,
                    link_id,
                    LinkCommandFailedError(f"link {link_id!r}: the impairment read failed: {why}"),
                    f"the read failed: {why}",
                )
            )
        else:
            impaired = sorted(
                direction.value
                for direction, shape in state.by_direction.items()
                if shape is not None and (shape.whole is not None or shape.scoped)
            )
            items.append(_row(kind, link_id, not impaired, detail=", ".join(impaired)))
    return items


async def _tunnel_items(ctx: "OttoContext") -> "list[CleanlinessItem]":
    """One row per otto tunnel running in the lab, plus what the scan could not see.

    Reads through :func:`otto.tunnel.discovery.discover_tunnels` -- the read
    side of the ``remove_all_tunnels`` this answers for, and the same scan the
    reap itself starts from, so the two cannot disagree about what a tunnel is.

    A scan that measured nothing is unreadable, for the reason the link read
    above is: an empty tunnel list is precisely what a clean lab returns, so a
    dry run's declined scan and a host that never answered would both come back
    as "clean" from a check that never looked. Those two rows belong to the
    SCAN rather than to any tunnel, so they are filed against the lab -- there
    is no tunnel id to name when the whole point is that nobody found out.

    A tunnel already in hand still outranks an incomplete scan, but that rule
    lives in :func:`_verdict` now rather than in an early return here: it is
    the same rule on every axis, and a report wants BOTH the tunnel that was
    found and the host that was missed.
    """
    from ..host.errors import HostUnreachableError
    from ..tunnel.discovery import TunnelNotMeasuredError, discover_tunnels

    kind = CleanlinessKind.TUNNEL
    discovery = await discover_tunnels(ctx.lab)
    items = [
        _row(kind, found.tunnel.id, clean=False, detail=found.status) for found in discovery.tunnels
    ]
    if discovery.not_measured:
        items.append(
            _unreadable(
                kind,
                "lab",
                TunnelNotMeasuredError(
                    "no host was scanned for tunnel processes, so whether the lab carries a "
                    "tunnel is unknown"
                ),
                "no host was scanned for tunnel processes",
            )
        )
    if discovery.unreachable:
        missed = ", ".join(discovery.unreachable)
        items.append(
            _unreadable(
                kind,
                "lab",
                HostUnreachableError(
                    f"tunnel scan could not reach {missed}, so whether the lab carries a "
                    f"tunnel is unknown"
                ),
                f"the scan could not reach {missed}",
            )
        )
    if not items:
        items.append(_row(kind, "lab", clean=True, detail="no otto tunnel is running"))
    return items


####################
#  Cleanliness
####################


async def cleanliness() -> CleanlinessReport:
    """Report every leftover :func:`cleanup` would find, and mark what could not be read.

    THE READ-ONLY TWIN OF :func:`is_clean`, over the same probes, differing in
    exactly one thing: a state nobody could read is a ROW here and a raise
    there. That is not a softer rule, it is the other half of the same one --
    a converge must never act on a non-fact, and a display must never hide the
    twelve facts it does have behind the one it does not.

    Every row is something ``cleanup`` acts on, in ``cleanup``'s own order:
    each repo's products and dev tools, each host's toolchain tools, each
    impairable link's netem, then the tunnels. Nothing short-circuits -- a
    dirty first repo does not stop the scan, because "which of them" is the
    whole question a report is asked.

    No converge consults this, and neither does ``otto run status`` unless
    ``--full`` is passed: it is device work (a link read per link, a process
    scan per host) that the install answer does not need.
    """
    ctx, repos = _lab()
    return CleanlinessReport(
        items=[
            *await _repo_items(ctx, repos),
            *await _toolchain_items(ctx),
            *await _impairment_items(ctx),
            *await _tunnel_items(ctx),
        ]
    )


async def is_clean() -> bool:
    """Whether nothing :func:`cleanup` removes is left in the lab.

    EVERY repo is asked, not only the counted ones -- see ``_repo_items``
    for why, and for why the repo walk here stops at the first dirty repo where
    the report's does not.

    THE QUESTION MATCHES THE VERB, step for step: products and dev tools, the
    hosts' toolchain tools, then the lab's own impairments and tunnels, in
    ``cleanup``'s own order. That agreement is the whole contract -- without
    the last two, :func:`ensure_clean` would report a lab dirty only in tunnels
    as already clean while ``otto run cleanup`` visibly reaped them, which is
    the surface split-brain this package exists to prevent.

    A state that could not be read RAISES rather than counting as unclean.
    ``do_for_all_hosts`` captures exceptions as values, and reading a dry run's
    refusal -- or a dead transport -- as "not clean" would send a converge into
    a cleanup on a fact nobody established. ``_verdict`` is where that
    happens, so this refusal and the report's "unknown" cell are one decision
    seen from two sides.

    AN AXIS THAT CANNOT ANSWER STOPS THE WALK rather than letting the later
    axes run to look for a dirty row that would outrank it. Reading them is
    device work whose only purpose would be to strengthen a verdict already
    unavailable, and this is the cheap path a converge takes before every test.
    """
    ctx, repos = _lab()
    for repo in repos:
        if not await actions_for(repo, ctx).is_clean():
            return False
    return (
        _verdict(await _toolchain_items(ctx))
        and _verdict(await _impairment_items(ctx))
        and _verdict(await _tunnel_items(ctx))
    )


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
    if await is_uninstalled():
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

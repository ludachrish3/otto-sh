"""Per-repo project actions -- one repo's lifecycle, and the seam to override it.

A repo gets ONE :class:`ProjectActions` -- otto's default, or the subclass it
registered from its init module -- constructed per command with that repo's
:class:`~otto.config.repo.Repo` and the live
:class:`~otto.context.OttoContext`. The defaults drive the fleet's hosts through
the host verbs, scoped to ``owner == repo.name``, so one repo's install can
never touch another repo's products.

THE OWNER SCOPE IS THE WHOLE POINT of this layer. The host verbs already know
how to install products; what they cannot know is which of the products a host
carries belong to the repo whose action is running. Every default below passes
``owner=self.repo.name``, and the tests mutate that argument away to prove it.

Two things are deliberately ABSENT from these signatures, both for the same
reason -- they are host-global, so they belong to no repo and are the
orchestrator's to perform once (spec section 5):

* **debug logs** -- ``uninstall`` hard-wires ``get_debug_logs=False`` rather
  than exposing a flag. N repos each sweeping the same host's debug logs means
  N transfers, each overwriting the last.
* **toolchain tools** -- one toolchain per host, shared by every owner;
  ``install_tools(toolchain=True)`` is a no-op at this layer.
"""

from typing import TYPE_CHECKING, TypeVar

from ..registry import Registry, get_registering_repo
from ..result import Result
from ..utils import Status
from .state import InstallState

if TYPE_CHECKING:
    from ..config.repo import Repo
    from ..context import OttoContext
    from ..host.dev_tool import DevTool
    from ..host.host import Host
    from ..host.product import Product

_OwnedT = TypeVar("_OwnedT", "Product", "DevTool")
"""Either owner-stamped host attachment; the two seams share this filter."""


def _owned(items: "list[_OwnedT]", owner: str) -> "list[_OwnedT]":
    """Return the *items* stamped with *owner*.

    The dev-tool twin of :meth:`otto.host.host.BaseHost._owned_products`, which
    filters the product list the same way for the host verbs that take an
    ``owner=`` argument. Nothing here filters on ``None``: an unowned
    attachment belongs to no repo's actions, so no repo's default touches it.
    """
    return [item for item in items if item.owner == owner]


def _reduce_results(results: "dict[str, Result | BaseException]") -> Result:
    """Reduce a ``do_for_all_hosts`` mapping to one Result, naming the host.

    The first non-ok entry wins -- every host has already run by the time this
    is called (``do_for_all_hosts`` gathers them), so this chooses what to
    REPORT, not what to attempt. An exception is a value in that mapping, not a
    raise, and a reduction that only understood Results would read a crashed
    host as a pass.

    Only ``status`` and ``msg`` are lifted out of the failing entry -- never
    ``value``, which RAISES on a dry run's
    :class:`~otto.result.NotRunResult`. The host id has to come from the
    mapping's key because the Result itself does not carry it, which is why
    this repacks rather than returning the entry whole.
    """
    for host_id, outcome in results.items():
        if isinstance(outcome, BaseException):
            return Result(Status.Error, msg=f"host {host_id!r}: {outcome!r}")
        if not outcome.is_ok:
            return Result(outcome.status, msg=f"host {host_id!r}: {outcome.msg}")
    return Result(Status.Success)


####################
#  Fleet dispatch
####################

# EVERY FLEET WALK IN THIS PACKAGE GOES THROUGH ONE OF THESE, so that a host
# CLASS may override the verb. :meth:`otto.context.OttoContext.do_for_all_hosts`
# calls the function object it is handed -- ``method(host, *args, **kwargs)``,
# with no attribute lookup on the host -- so handing it ``BaseHost.install``
# freezes the walk to ``BaseHost``'s body: a class registered through
# :func:`~otto.host.os_profile.register_host_class` that overrides the verb (the
# design's override point #1, and the one the guides name -- "a host family
# whose debug logs come out of journald overrides ``get_debug_logs``") would run
# under ``otto host <id> <verb>`` and be SILENTLY BYPASSED by ``otto run <verb>``.
# The two surfaces would then disagree while both reported success, which is the
# split-brain this package exists to prevent.
#
# Dispatching through the instance puts the attribute lookup back on the object
# at call time. It is the same idiom the coverage fetchers use
# (:mod:`otto.coverage.fetcher.remote`), and the one the owned-dev-tool walkers
# below already had for free by taking ``host`` as a parameter.


async def _dispatch_install(host: "Host", owner: "str | None" = None) -> Result:
    """Install *host*'s ``owner``-scoped products through the host's OWN method."""
    return await host.install(owner=owner)


async def _dispatch_uninstall(
    host: "Host",
    get_product_logs: bool = True,
    get_debug_logs: bool = True,
    owner: "str | None" = None,
) -> Result:
    """Uninstall *host*'s ``owner``-scoped products through the host's OWN method."""
    return await host.uninstall(
        get_product_logs=get_product_logs,
        get_debug_logs=get_debug_logs,
        owner=owner,
    )


async def _dispatch_get_product_logs(host: "Host", owner: "str | None" = None) -> Result:
    """Haul *host*'s ``owner``-scoped product logs through the host's OWN method."""
    return await host.get_product_logs(owner=owner)


async def _install_owned_dev_tools(host: "Host", owner: str) -> Result:
    """Stage then install each of *owner*'s dev tools on *host* (first failure wins).

    The owner-scoped twin of
    :meth:`otto.host.host.BaseHost.install_dev_tools`, which is host-global:
    tool installation has no ``owner=`` argument at the host layer, so the
    per-repo walk lives here. Each tool is carried through both phases before
    the next one starts, matching the host verb -- a tool that stages but
    cannot install stops the walk rather than leaving later tools installed on
    top of a half-placed prerequisite.
    """
    for tool in _owned(host.dev_tools, owner):
        result = await tool.stage(host)
        if not result.is_ok:
            return result
        result = await tool.install(host)
        if not result.is_ok:
            return result
    return Result(Status.Success)


async def _uninstall_owned_dev_tools(host: "Host", owner: str) -> Result:
    """Uninstall each of *owner*'s dev tools on *host* (best-effort).

    Every tool is attempted even after one fails -- a tool that refuses to go
    must not strand the rest of the repo's tooling on the board -- and the
    first failure seen is what returns, mirroring
    :meth:`otto.host.host.BaseHost.cleanup`'s tool loop.
    """
    first_failure: "Result | None" = None
    for tool in _owned(host.dev_tools, owner):
        result = await tool.uninstall(host)
        if not result.is_ok and first_failure is None:
            first_failure = result
    return first_failure if first_failure is not None else Result(Status.Success)


class ProjectActions:
    """One repo's lifecycle across the lab; subclass to override any part of it.

    Constructed per command with the repo it acts for and the live context.
    Every default fans out across ``ctx.all_hosts()`` -- the fleet, so the
    built-in ``local`` host and Docker containers are excluded as everywhere
    else -- and scopes each host verb to this repo's products and dev tools.

    Overriding is ordinary Python: subclass, register it with
    :func:`register_project_actions` from the repo's init module, and call
    ``super()`` for whatever should keep the default behaviour. A subclass may
    freely mix custom sequencing with the per-host verbs (``await
    host.install()`` for chosen hosts) -- nothing here is privileged.
    """

    def __init__(self, repo: "Repo", ctx: "OttoContext") -> None:
        self.repo = repo
        """The repo these actions act for; its ``name`` is the owner scope."""

        self.ctx = ctx
        """The live context, providing the fleet and its dispatch."""

    ####################
    #  Lifecycle
    ####################

    async def install(self) -> Result:
        """Install this repo's products on every fleet host.

        Hosts proceed in parallel; within a host, products install in
        declaration order -- which is already repo-dependency order, because
        bootstrap imports init modules topologically and providers append in
        registration order.
        """
        return _reduce_results(
            await self.ctx.do_for_all_hosts(_dispatch_install, owner=self.repo.name)
        )

    async def uninstall(self, get_product_logs: bool = True) -> Result:
        """Uninstall this repo's products on every fleet host.

        Product logs come off each host BEFORE its teardown, which is the host
        verb's own contract. Debug logs are NOT gathered here and there is no
        flag to ask for them: they are host-level, belong to no repo, and the
        orchestrator sweeps them once after every repo has torn down.
        """
        return _reduce_results(
            await self.ctx.do_for_all_hosts(
                _dispatch_uninstall,
                get_product_logs=get_product_logs,
                get_debug_logs=False,
                owner=self.repo.name,
            )
        )

    async def cleanup(self, get_product_logs: bool = True) -> Result:
        """Uninstall this repo's products, then remove its dev tools.

        Strictly more than :meth:`uninstall`, in that order: products first (a
        dev tool may be what a product's uninstall needs), then the tooling.
        Best-effort -- the tools are removed even when the product teardown
        failed, and the first failure seen is what returns.

        Toolchain tools are NOT removed here. One toolchain serves every owner
        on a host, so removing it is the orchestrator's final host-global step;
        a repo tearing it down would take its neighbours' tooling with it.
        """
        uninstalled = await self.uninstall(get_product_logs=get_product_logs)
        tools = _reduce_results(
            await self.ctx.do_for_all_hosts(_uninstall_owned_dev_tools, owner=self.repo.name)
        )
        return uninstalled if not uninstalled.is_ok else tools

    ####################
    #  Logs
    ####################

    async def get_logs(self, product: bool = True, require_product_logs: bool = False) -> Result:
        """Retrieve this repo's product logs from every fleet host.

        There is no debug half, deliberately -- see this module's docstring.
        Zero retrieved logs is success; *require_product_logs* turns an empty
        haul into a failure that names the host that delivered nothing.

        THE REQUIREMENT IS ASKED ONLY OF HOSTS THIS REPO HAS PRODUCTS ON. A
        repo rarely owns something on every host in the lab -- firmware lives
        on the embedded target, a service on the servers -- and a bare host
        cannot produce logs for products it does not carry. Demanding a haul
        from the whole fleet would fail a repo that retrieved everything it
        owns, named after a host it never deploys to.

        *require_product_logs* with ``product=False`` is a contradiction and is
        refused up front rather than ignored: the haul it requires is the step
        being skipped, and a requirement that is parsed but unenforceable would
        report success having promised logs nobody went looking for.
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
        if not product:
            return Result(Status.Success)
        haul = _reduce_results(
            await self.ctx.do_for_all_hosts(_dispatch_get_product_logs, owner=self.repo.name)
        )
        if not haul.is_ok or not require_product_logs:
            # The haul's own failure returns unchanged: it says WHY nothing
            # arrived, which a derived "no logs" verdict would replace with a
            # symptom.
            return haul
        for host in self.ctx.all_hosts():
            if not _owned(host.products, self.repo.name):
                continue
            product_dir = host.log_dest() / "product"
            if not product_dir.exists() or not any(product_dir.iterdir()):
                return Result(
                    Status.Error,
                    msg=f"require_product_logs: no product logs retrieved from {host.id}",
                )
        return Result(Status.Success)

    ####################
    #  Tools
    ####################

    async def install_tools(self, dev: bool = True, toolchain: bool = False) -> Result:  # noqa: ARG002 — toolchain is host-global (see below); the parameter mirrors the host and orchestrator verbs so an override can honour it
        """Install this repo's dev tools on every fleet host.

        *toolchain* is accepted and does nothing at this layer, which is the
        declared contract rather than an oversight: a host has ONE toolchain,
        shared by every owner, so placing it is the orchestrator's host-global
        step -- the same reasoning that keeps debug logs out of
        :meth:`uninstall`. The parameter stays in the signature so a subclass
        with toolchain work of its own has somewhere to hang it, and so
        ``super().install_tools(...)`` can be called with the caller's flags
        unchanged.
        """
        if not dev:
            return Result(Status.Success)
        return _reduce_results(
            await self.ctx.do_for_all_hosts(_install_owned_dev_tools, owner=self.repo.name)
        )

    ####################
    #  Questions
    ####################

    @property
    def owns_products(self) -> bool:
        """Whether any fleet host carries a product owned by this repo.

        The orchestrator's counted-repo rule reads this: a repo that owns no
        products anywhere and registered no actions of its own (a docs-only
        repo, say) has no install state to contribute and must not drag the
        lab-level aggregate to PARTIAL.
        """
        return any(_owned(host.products, self.repo.name) for host in self.ctx.all_hosts())

    async def status(self) -> InstallState:
        """Report this repo's install state across the fleet.

        COUNTS PRODUCTS, NOT HOSTS. A host-level ``is_installed()`` answers
        False for a host that is clean and for a host that is half-installed
        alike, so a reduction over host booleans cannot see the state that
        matters most. Counting the owned products directly can: all installed
        is INSTALLED, none is UNINSTALLED, anything between is PARTIAL.

        Owning no products anywhere is UNINSTALLED, mirroring
        :meth:`otto.host.host.BaseHost.is_installed`'s empty-products rule --
        nothing that could be installed is not vacuously "installed". Callers
        that need to tell "nothing to install" from "nothing installed" ask
        :attr:`owns_products`.
        """
        installed = 0
        total = 0
        for host in self.ctx.all_hosts():
            for product in _owned(host.products, self.repo.name):
                total += 1
                if await product.is_installed(host):
                    installed += 1
        if total == 0 or installed == 0:
            return InstallState.UNINSTALLED
        return InstallState.INSTALLED if installed == total else InstallState.PARTIAL

    async def is_clean(self) -> bool:
        """Whether none of this repo's products or dev tools remain installed.

        The matching question to :meth:`cleanup`, asked with the same scope:
        another repo's leftovers are not this repo's uncleanliness. Toolchain
        tools are host-global and are checked by the orchestrator, not here.
        """
        for host in self.ctx.all_hosts():
            for product in _owned(host.products, self.repo.name):
                if await product.is_installed(host):
                    return False
            for tool in _owned(host.dev_tools, self.repo.name):
                if await tool.is_installed(host):
                    return False
        return True


PROJECT_ACTIONS: "Registry[type[ProjectActions]]" = Registry(
    "project actions",
    register_hint="otto.register_project_actions()",
    collision_hint=(
        "A repo registers at most one ProjectActions; consolidate into a single subclass."
    ),
)
"""Registered :class:`ProjectActions` subclasses, keyed by repo name."""


def register_project_actions(cls: "type[ProjectActions]") -> "type[ProjectActions]":
    """Register *cls* as the calling repo's actions; usable as a decorator.

    Call from an init module listed in ``.otto/settings.toml`` ``[init]`` --
    that import is what attributes the class to its repo, via the marker
    ``bootstrap()`` sets around it. A second registration from the SAME repo
    fails loud; different repos each registering their own class is the
    intended composition, not a collision.

    Returns *cls* unchanged so it can be used as a decorator.

    Raises:
        ValueError: If called outside a repo's init import (nothing to
            attribute the class to), or if this repo already registered one.
    """
    repo_name = get_registering_repo()
    if repo_name is None:
        raise ValueError(
            "register_project_actions() must be called from a repo init module "
            "(listed in .otto/settings.toml [init]) — that import is what "
            "attributes the class to its repo."
        )
    PROJECT_ACTIONS.register(repo_name, cls, origin=cls.__module__)
    return cls


def actions_for(repo: "Repo", ctx: "OttoContext") -> ProjectActions:
    """Build *repo*'s actions: its registered subclass, else otto's default.

    A repo that registers nothing gets :class:`ProjectActions` itself, so the
    zero-effort case is a working lifecycle rather than an error.
    """
    cls = PROJECT_ACTIONS.get(repo.name) if repo.name in PROJECT_ACTIONS else ProjectActions
    return cls(repo=repo, ctx=ctx)

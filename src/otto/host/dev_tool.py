"""
Development-tool lifecycle strategy for hosts.

A :class:`DevTool` is a unit of repo-internal tooling deployed to a host —
debug probes, trace helpers, board-side scratch utilities. It is the
:class:`~otto.host.product.Product` shape *deliberately*: same ``ABC``, same
``name``/``owner``, same ``stage``/``install``/``uninstall``/``is_installed``,
same provider registry. Authoring knowledge transfers in both directions; a
project that can write a product can write a dev tool without learning a second
model.

What differs is the **lifecycle**, which is why this is a separate seam and a
separate list rather than a flag on :class:`~otto.host.product.Product`. Dev
tools are installed by ``install_tools(dev=True)`` and removed by ``cleanup()``,
and they are never part of ``is_installed()``'s answer — that question is about
the software under test. Sharing one list would make cleanup uninstall dev tools
as products and make a bare debug probe read as an installed product.

Dev tools are customized in repo config or code, never lab data: a
``[[dev_tools]]`` entry in ``.otto/settings.toml`` declares the common cases
(see :mod:`otto.declared` and :func:`register_dev_tool_kind`), and a
:func:`register_dev_tool_provider` callback from a ``.otto`` init module
remains the code fallback for whatever the match table cannot express —
declared entries apply first at ingest, so a provider dev tool whose name a
declared entry claimed stands down. Lab data stays tooling-agnostic and
evolves independently of dev-tool code; declaring dev tools *in* lab data is
deliberately **not** supported.
"""

import logging
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING

from ..declared import KindRegistry, declared_for_host
from ..registry import get_registering_repo
from ..result import Result

if TYPE_CHECKING:
    from ..declared import DeclaredEntry
    from .host import Host

logger = logging.getLogger(__name__)


class DevTool(ABC):
    """A repo-defined development tool deployed to a host (behavior contract).

    The :class:`~otto.host.product.Product` shape, deliberately: ``name``,
    ``owner``, ``stage``/``install``/``uninstall``/``is_installed``. Dev
    tools are internal per-board tooling (probes, helpers), installed by
    ``install_tools(dev=True)`` and removed by ``cleanup()`` — never part
    of ``is_installed()``'s product answer.
    """

    name: str
    """Logical identity — used for logging, ``is_installed`` lookups, and dedup.
    Not a file path: a tool may be multi-file or installed from a package."""

    owner: str | None = None
    """Owning repo's name, stamped at lab ingest from the registering-repo
    marker (see :func:`otto.registry.registering_repo`). ``None`` = attached
    outside any repo's init import. Default per-repo actions filter on this."""

    @abstractmethod
    async def stage(self, host: "Host") -> Result:
        """Transfer/place this tool's artifacts onto *host* (no install).

        Return any :class:`~otto.result.Result` — a bare one, or the
        :class:`~otto.result.CommandResult` of the command that did the work,
        whose retcode and output then reach the CLI's exit code untouched.
        """
        ...

    @abstractmethod
    async def install(self, host: "Host") -> Result:
        """Install this tool's already-staged artifacts on *host*."""
        ...

    @abstractmethod
    async def uninstall(self, host: "Host") -> Result:
        """Remove this tool from *host*."""
        ...

    @abstractmethod
    async def is_installed(self, host: "Host") -> bool:
        """Return True when this tool is currently installed on *host*."""
        ...


DevToolProvider = Callable[["Host"], Iterable[DevTool] | None]
"""A function that, given a host, returns the dev tools it should carry.

Registered from a ``.otto`` init module via :func:`register_dev_tool_provider`
and run once per lab-ingested host. All tooling knowledge stays in repo code;
lab data never names a dev tool."""

_DEV_TOOL_PROVIDERS: list[tuple[DevToolProvider, str | None]] = []
"""Registered providers paired with the repo that registered each one.

Separate from ``product._PRODUCT_PROVIDERS`` on purpose — each seam owns its
registry, so a provider can never attach to the other seam's list."""


def register_dev_tool_provider(provider: DevToolProvider) -> None:
    """Register a function that decides which dev tools a host carries.

    Call from an init module listed in ``.otto/settings.toml`` — the same
    extension hook the other host strategies use. The provider runs once per
    lab-ingested host; inspect the host's tooling-agnostic attributes
    (``element``, ``element_id``, ``os_type``, ``id``, ``ip``, ``source_lab``,
    ``metadata``, ``element_metadata``)
    and return the dev tools that host should carry (or ``None``/``[]`` for
    none). Behavior lives in code; lab data stays tooling-agnostic.

    The registering repo is captured **here**, not at ingest: this call runs
    inside that repo's init import, whereas the provider runs long after,
    when the marker is gone.
    """
    _DEV_TOOL_PROVIDERS.append((provider, get_registering_repo()))


DEV_TOOL_KINDS: KindRegistry["DevTool"] = KindRegistry(
    "dev tool kind", register_hint="otto.host.dev_tool.register_dev_tool_kind()"
)
"""Named factories for settings-declared dev tools (spec 2026-09-01 §5-§6).

Separate from :data:`otto.host.product.PRODUCT_KINDS` on purpose — each
seam owns its registry, the same two-list reasoning as the providers."""


def register_dev_tool_kind(
    name: str,
    factory: "Callable[[DeclaredEntry, Host], DevTool]",
    *,
    overwrite: bool = False,
) -> None:
    """Register a dev tool *kind* — the code a ``[[dev_tools]]`` entry binds to.

    Call from an init module listed in ``.otto/settings.toml``, the same
    extension hook :func:`register_dev_tool_provider` uses. The factory
    receives the parsed :class:`~otto.declared.DeclaredEntry` (whose
    ``params`` carry every non-reserved TOML key) and the matched host, and
    returns the :class:`DevTool` to attach; it should validate its params and
    raise ``ValueError`` naming the entry on a bad one — a misdeclared entry
    fails ingest loudly, exactly as a misconfigured provider does.
    """
    DEV_TOOL_KINDS.register(name, factory, overwrite=overwrite)


def apply_declared_dev_tools(host: "Host") -> None:
    """Attach the settings-declared dev tools admitted for *host*.

    Called at the ingest chokepoint BEFORE :func:`apply_dev_tool_providers`:
    running first is the fallback contract — the provider loop's name-dedup
    then skips any code dev tool whose name a declared entry already claimed,
    so config wins and code fills the gaps. Entry collection and the §5
    ``[project]`` gate live in :func:`otto.declared.declared_for_host`;
    matching, first-match-wins and owner stamping in
    :meth:`~otto.declared.KindRegistry.build`. A dev tool whose name the host
    already carries is skipped, the provider loop's identical guard.

    The §5 gate itself is not separate per seam here: both declared loops
    share the one implementation in :func:`otto.declared.declared_for_host`.
    What stays separate is :data:`DEV_TOOL_KINDS` and its target list — a
    declared entry built through this registry only ever lands on
    ``host.dev_tools``, never on ``host.products``, the same two-list
    reasoning as the provider registries.
    """
    seen = {t.name for t in host.dev_tools}
    for tool in DEV_TOOL_KINDS.build(declared_for_host(host, "declared_dev_tools"), host):
        if tool.name in seen:
            logger.debug("declared dev tool: skipping duplicate %r on host %s", tool.name, host.id)
            continue
        host.dev_tools.append(tool)
        seen.add(tool.name)


def apply_dev_tool_providers(host: "Host") -> None:
    """Run every registered provider against *host*, attaching their dev tools.

    Called at the single lab-ingest chokepoint
    (:func:`otto.host.factory.create_host_from_dict`). Providers run in
    registration order and their results are concatenated onto
    ``host.dev_tools`` — never onto ``host.products``, so the tool lifecycle
    stays out of the product lifecycle's answers. A dev tool whose
    :attr:`DevTool.name` already appears on the host is skipped (deduplication
    guards two overlapping providers). A provider that raises propagates — a
    misconfigured provider fails ingest loudly.

    Each attached dev tool is stamped with :attr:`DevTool.owner` — the repo that
    registered the provider — unless the tool already names an owner, which lets
    one repo hand a tool to another's ownership deliberately.

    A provider is SKIPPED — not called — when its registering repo's
    ``[project]`` declaration does not target ``(host.source_lab, host.id)``
    (spec §5), under the identical rule and the identical carve-outs
    :func:`otto.host.product.apply_product_providers` documents. Gated here in
    its own right, not inherited from that seam: the two registries are
    deliberately separate, so a gate present in one loop and absent from the
    other would leave a repo free to hang debug tooling on any host it liked.
    """
    from ..config.scope import repo_targets, scope_for_repo  # function-scope: import-light seam

    seen = {t.name for t in host.dev_tools}
    for provider, provider_owner in _DEV_TOOL_PROVIDERS:
        if host.source_lab and not repo_targets(
            scope_for_repo(provider_owner), host.source_lab, host.id
        ):
            logger.debug(
                "dev tool provider: repo %r does not target host %s of lab %r — not run",
                provider_owner,
                host.id,
                host.source_lab,
            )
            continue
        for tool in provider(host) or ():
            if tool.name in seen:
                logger.debug(
                    "dev tool provider: skipping duplicate %r on host %s",
                    tool.name,
                    host.id,
                )
                continue
            if tool.owner is None:
                tool.owner = provider_owner
            host.dev_tools.append(tool)
            seen.add(tool.name)

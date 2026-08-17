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

Dev tools are **behavior**, so they are customized in code, not lab data: a repo
registers a :func:`register_dev_tool_provider` callback from a ``.otto`` init
module, and otto applies it to each host as it is ingested (see
:func:`apply_dev_tool_providers`). Lab data stays tooling-agnostic; declaring
dev tools *in* lab data is deliberately **not** supported.
"""

import logging
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING

from ..registry import get_registering_repo
from ..result import Result

if TYPE_CHECKING:
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
    (``element``, ``element_id``, ``os_type``, ``id``, ``ip``, ``resources``)
    and return the dev tools that host should carry (or ``None``/``[]`` for
    none). Behavior lives in code; lab data stays tooling-agnostic.

    The registering repo is captured **here**, not at ingest: this call runs
    inside that repo's init import, whereas the provider runs long after,
    when the marker is gone.
    """
    _DEV_TOOL_PROVIDERS.append((provider, get_registering_repo()))


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
    """
    seen = {t.name for t in host.dev_tools}
    for provider, provider_owner in _DEV_TOOL_PROVIDERS:
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

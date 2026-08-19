"""
Product lifecycle strategy for hosts.

A :class:`Product` is a unit of software-under-test deployed to a host — the
lifecycle analog of :class:`~otto.host.binary_loader.BinaryLoader`. It is a
**behavior contract** (an ``ABC``): projects subclass it and inject instances
via :attr:`~otto.host.host.BaseHost.products`. The host orchestrates; the
product knows how to stage/install/uninstall/check itself.

It is intentionally **not** a pydantic model — that would force every project
product into pydantic and diverge from the sibling host strategies
(:class:`~otto.host.command_frame.CommandFrame`,
:class:`~otto.host.binary_loader.BinaryLoader`,
:class:`~otto.host.embedded_filesystem.EmbeddedFileSystem`).
Concrete subclasses pick their own data representation (``@dataclass`` or an
``OttoModel``).

Products are **behavior**, so they are customized in code, not lab data: a
product repo registers a :func:`register_product_provider` callback from a
``.otto`` init module, and otto applies it to each host as it is ingested (see
:func:`apply_product_providers`). Lab data stays product-agnostic and evolves
independently of product code; declaring products *in* lab data is deliberately
**not** supported.
"""

import logging
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from typing_extensions import override

from ..registry import get_registering_repo
from ..result import Result
from ..utils import Status

if TYPE_CHECKING:
    from .host import Host

logger = logging.getLogger(__name__)


class Product(ABC):
    """A unit of software-under-test deployed to a host (behavior contract)."""

    name: str
    """Logical identity — used for logging, ``is_installed`` lookups, and dedup.
    Not a file path: a product may be multi-file or installed from a repo."""

    owner: str | None = None
    """Owning repo's name, stamped at lab ingest from the registering-repo
    marker (see :func:`otto.registry.registering_repo`). ``None`` = attached
    outside any repo's init import. Default per-repo actions filter on this."""

    @abstractmethod
    async def stage(self, host: "Host") -> Result:
        """Transfer/place this product's artifacts onto *host* (no install).

        Return any :class:`~otto.result.Result` — a bare one, or the
        :class:`~otto.result.CommandResult` of the command that did the work,
        whose retcode and output then reach the CLI's exit code untouched.
        """
        ...

    @abstractmethod
    async def install(self, host: "Host") -> Result:
        """Install this product's already-staged artifacts on *host*."""
        ...

    @abstractmethod
    async def uninstall(self, host: "Host") -> Result:
        """Remove this product from *host*."""
        ...

    @abstractmethod
    async def is_installed(self, host: "Host") -> bool:
        """Return True when this product is currently installed on *host*."""
        ...

    async def get_logs(self, host: "Host", dest: Path) -> Result:  # noqa: ARG002 — required by the Product.get_logs hook signature; overrides use host/dest, this retrieves-nothing default does not
        """Retrieve this product's log files into local directory *dest*.

        Default: retrieves nothing, successfully — zero logs is not a
        failure. Override when the product produces logs; retrieval need
        not run on *host* (an external mechanism is fine). Write files
        under *dest*; the caller owns the directory layout above it.
        """
        return Result(Status.Success)


@dataclass(slots=True)
class FileProduct(Product):
    """Convenience base for a product that *is* a single artifact file.

    ``stage()`` transfers the artifact via :meth:`~otto.host.host.Host.put`. ``name`` defaults to
    the artifact's basename. ``install``/``uninstall``/``is_installed`` remain
    abstract — they are inherently project-specific. Once the remote file-ops
    phase lands, the natural ``is_installed`` is
    ``await host.exists(self.dest_dir / self.artifact.name)``.
    """

    artifact: Path
    """Local path to the artifact file to stage onto the host."""

    name: str = ""
    """Logical name; defaults to ``artifact.name`` when left empty."""

    dest_dir: Path = field(default_factory=Path)
    """Destination directory on the host; resolved against the host's
    ``default_dest_dir`` by :meth:`~otto.host.host.Host.put`."""

    def __post_init__(self) -> None:
        if not self.name:
            self.name = self.artifact.name

    @override
    async def stage(self, host: "Host") -> Result:
        """Transfer the artifact, returning ``host.put``'s result unchanged."""
        return await host.put(self.artifact, self.dest_dir)


ProductProvider = Callable[["Host"], Iterable[Product] | None]
"""A function that, given a host, returns the products it should carry.

Registered from a ``.otto`` init module via :func:`register_product_provider`
and run once per lab-ingested host. All product knowledge stays in product-repo
code; lab data never names a product."""

_PRODUCT_PROVIDERS: list[tuple[ProductProvider, str | None]] = []
"""Registered providers paired with the repo that registered each one."""


def register_product_provider(provider: ProductProvider) -> None:
    """Register a function that decides which products a host carries.

    Call from an init module listed in ``.otto/settings.toml`` — the same
    extension hook the other host strategies use. The provider runs once per
    lab-ingested host; inspect the host's product-agnostic attributes
    (``element``, ``element_id``, ``os_type``, ``id``, ``ip``, ``resources``)
    and return the products that host should carry (or ``None``/``[]`` for
    none). Behavior lives in code; lab data stays product-agnostic.

    The registering repo is captured **here**, not at ingest: this call runs
    inside that repo's init import, whereas the provider runs long after,
    when the marker is gone.
    """
    _PRODUCT_PROVIDERS.append((provider, get_registering_repo()))


def apply_product_providers(host: "Host") -> None:
    """Run every registered provider against *host*, attaching their products.

    Called at the single lab-ingest chokepoint
    (:func:`otto.host.factory.create_host_from_dict`). Providers run in
    registration order and their results are concatenated onto
    ``host.products``. A product whose :attr:`Product.name` already appears on
    the host is skipped (deduplication guards two overlapping providers). A
    provider that raises propagates — a misconfigured provider fails ingest
    loudly.

    Each attached product is stamped with :attr:`Product.owner` — the repo that
    registered the provider — unless the product already names an owner, which
    lets one repo hand a product to another's ownership deliberately.

    A provider is SKIPPED — not called — when its registering repo's
    ``[project]`` declaration does not target ``(host.source_lab, host.id)``
    (spec §5). This is the admission half of scoping: the fleet walks bound
    which hosts a repo may reach, and without this a repo declared for one lab
    still hangs its products on every host of every other lab the run happens
    to load. Skipping before the call rather than filtering the return is the
    point — a provider that ran has already been handed a machine its repo
    never declared, and providers inspect hosts and keep their own state.

    Two carve-outs admit, both because a gate that cannot compute a narrowing
    must narrow nothing. An UNSTAMPED host (``source_lab == ""``) is not
    judged at all: hosts built outside the loader — direct
    :func:`~otto.host.factory.create_host_from_dict` use, container hosts, the
    built-in ``local`` — predate scoping and behave exactly as before. And an
    owner whose declaration cannot be resolved admits, which
    :func:`~otto.config.scope.scope_for_repo` decides and documents.
    """
    from ..config.scope import repo_targets, scope_for_repo  # function-scope: import-light seam

    seen = {p.name for p in host.products}
    for provider, provider_owner in _PRODUCT_PROVIDERS:
        if host.source_lab and not repo_targets(
            scope_for_repo(provider_owner), host.source_lab, host.id
        ):
            logger.debug(
                "product provider: repo %r does not target host %s of lab %r — not run",
                provider_owner,
                host.id,
                host.source_lab,
            )
            continue
        for product in provider(host) or ():
            if product.name in seen:
                logger.debug(
                    "product provider: skipping duplicate %r on host %s",
                    product.name,
                    host.id,
                )
                continue
            if product.owner is None:
                product.owner = provider_owner
            host.products.append(product)
            seen.add(product.name)

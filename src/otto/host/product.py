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

from ..utils import Status

if TYPE_CHECKING:
    from .host import Host

logger = logging.getLogger(__name__)


class Product(ABC):
    """A unit of software-under-test deployed to a host (behavior contract)."""

    name: str
    """Logical identity — used for logging, ``is_installed`` lookups, and dedup.
    Not a file path: a product may be multi-file or installed from a repo."""

    @abstractmethod
    async def stage(self, host: "Host") -> tuple[Status, str]:
        """Transfer/place this product's artifacts onto *host* (no install)."""
        ...

    @abstractmethod
    async def install(self, host: "Host") -> tuple[Status, str]:
        """Install this product's already-staged artifacts on *host*."""
        ...

    @abstractmethod
    async def uninstall(self, host: "Host") -> tuple[Status, str]:
        """Remove this product from *host*."""
        ...

    @abstractmethod
    async def is_installed(self, host: "Host") -> bool:
        """Return True when this product is currently installed on *host*."""
        ...


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
    async def stage(self, host: "Host") -> tuple[Status, str]:
        # host.put now returns a per-file transfer Result; collapse it to this
        # method's (Status, str) contract until the product lifecycle is
        # converted to the Result family in a later pass.
        result = await host.put(self.artifact, self.dest_dir)
        return result.status, result.msg


ProductProvider = Callable[["Host"], Iterable[Product] | None]
"""A function that, given a host, returns the products it should carry.

Registered from a ``.otto`` init module via :func:`register_product_provider`
and run once per lab-ingested host. All product knowledge stays in product-repo
code; lab data never names a product."""

_PRODUCT_PROVIDERS: list[ProductProvider] = []


def register_product_provider(provider: ProductProvider) -> None:
    """Register a function that decides which products a host carries.

    Call from an init module listed in ``.otto/settings.toml`` — the same
    extension hook the other host strategies use. The provider runs once per
    lab-ingested host; inspect the host's product-agnostic attributes
    (``element``, ``element_id``, ``os_type``, ``id``, ``ip``, ``resources``)
    and return the products that host should carry (or ``None``/``[]`` for
    none). Behavior lives in code; lab data stays product-agnostic.
    """
    _PRODUCT_PROVIDERS.append(provider)


def apply_product_providers(host: "Host") -> None:
    """Run every registered provider against *host*, attaching their products.

    Called at the single lab-ingest chokepoint
    (:func:`otto.host.factory.create_host_from_dict`). Providers run in
    registration order and their results are concatenated onto
    ``host.products``. A product whose :attr:`Product.name` already appears on
    the host is skipped (deduplication guards two overlapping providers). A
    provider that raises propagates — a misconfigured provider fails ingest
    loudly.
    """
    seen = {p.name for p in host.products}
    for provider in _PRODUCT_PROVIDERS:
        for product in provider(host) or ():
            if product.name in seen:
                logger.debug(
                    "product provider: skipping duplicate %r on host %s",
                    product.name,
                    host.id,
                )
                continue
            host.products.append(product)
            seen.add(product.name)

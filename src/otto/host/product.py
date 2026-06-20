"""
Product lifecycle strategy for hosts.

A :class:`Product` is a unit of software-under-test deployed to a host — the
lifecycle analog of :class:`~otto.host.binary_loader.BinaryLoader`. It is a
**behavior contract** (an ``ABC``): projects subclass it and inject instances
via :attr:`~otto.host.host.BaseHost.products`. The host orchestrates; the
product knows how to stage/install/uninstall/check itself.

It is intentionally **not** a pydantic model — that would force every project
product into pydantic and diverge from the sibling host strategies
(:class:`CommandFrame`, :class:`BinaryLoader`, :class:`EmbeddedFileSystem`).
Concrete subclasses pick their own data representation (``@dataclass`` or an
``OttoModel``). A lab-data declaration path (a ``ProductSpec`` boundary model +
``register_product`` registry) is a documented future follow-on.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from ..utils import Status

if TYPE_CHECKING:
    from .host import Host


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

    ``stage()`` transfers the artifact via :meth:`Host.put`. ``name`` defaults to
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
    ``default_dest_dir`` by :meth:`Host.put`."""

    def __post_init__(self) -> None:
        if not self.name:
            self.name = self.artifact.name

    async def stage(self, host: "Host") -> tuple[Status, str]:
        return await host.put(self.artifact, self.dest_dir)

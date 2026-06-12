"""
Pluggable *binary load* strategy for embedded hosts.

Loading a binary into a device's executable runtime — Zephyr's LLEXT
``llext load_hex`` is the first example — is **not** a file transfer: there is
no destination file or filesystem, the binary goes straight into the kernel's
loader. A :class:`BinaryLoader` is a small **stateless value object** (mirroring
:class:`~otto.host.command_frame.CommandFrame`) that formats the device's
load/unload commands and reads their output. The host executes; the loader never
touches the session.

A project can register additional loaders via :func:`register_binary_loader`
from a ``.otto`` init module — the same extension hook
:func:`otto.host.command_frame.register_command_frame` follows.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import ClassVar


class BinaryLoader(ABC):
    """How to load/unload a binary into an embedded target's runtime."""

    type_name: ClassVar[str]
    """Lab-data string for this loader (e.g. ``'llext-hex'``); unique across loaders."""

    max_unload_rounds: ClassVar[int] = 16
    """Cap on the unload-to-eviction loop the host drives (see
    :meth:`otto.host.embeddedHost.EmbeddedHost.unload`). Some loaders (LLEXT)
    refcount a resident binary, so one unload may decrement without evicting."""

    @abstractmethod
    def load_command(self, name: str, payload: bytes) -> str:
        """Return the device command that loads *payload* under *name*."""
        ...

    @abstractmethod
    def check_loaded(self, output: str) -> tuple[bool, str]:
        """Return ``(ok, reason)`` from a load command's output — ``reason`` is
        the failure text when ``ok`` is False, ``""`` otherwise.
        """
        ...

    @abstractmethod
    def unload_command(self, name: str) -> str:
        """Return the device command that unloads (one round of) *name*."""
        ...

    @abstractmethod
    def is_fully_unloaded(self, output: str) -> bool:
        """Return True when an unload round's output shows *name* no longer resident."""
        ...


class LlextHexLoader(BinaryLoader):
    """Zephyr LLEXT shell loader: ``llext load_hex`` / ``llext unload``.

    ``load_hex`` takes the hex-encoded ELF inline as one shell-command argument.
    LLEXT refcounts a resident extension, so a full eviction may need several
    ``unload`` rounds — :meth:`is_fully_unloaded` is True only once the shell
    reports ``No such extension``.
    """

    type_name = "llext-hex"

    def load_command(self, name: str, payload: bytes) -> str:
        return f"llext load_hex {name} {payload.hex()}"

    def check_loaded(self, output: str) -> tuple[bool, str]:
        ok = "Successfully loaded extension" in output
        return (True, "") if ok else (False, output.strip())

    def unload_command(self, name: str) -> str:
        return f"llext unload {name}"

    def is_fully_unloaded(self, output: str) -> bool:
        return "No such extension" in output


_LOADER_CLASSES: dict[str, type[BinaryLoader]] = {
    LlextHexLoader.type_name: LlextHexLoader,
}


def register_binary_loader(type_name: str, cls: type[BinaryLoader]) -> None:
    """Make a custom :class:`BinaryLoader` subclass available to lab data.

    Call from an init module listed in ``.otto/settings.toml`` — the same
    pattern :func:`otto.host.command_frame.register_command_frame` follows.
    """
    if cls.type_name != type_name:
        raise ValueError(
            f"register_binary_loader: type_name {type_name!r} doesn't match "
            f"{cls.__name__}.type_name = {cls.type_name!r}"
        )
    _LOADER_CLASSES[type_name] = cls


def build_binary_loader(type_name: str) -> BinaryLoader:
    """Construct the :class:`BinaryLoader` registered under *type_name*."""
    try:
        cls = _LOADER_CLASSES[type_name]
    except KeyError:
        known = ", ".join(sorted(_LOADER_CLASSES))
        raise ValueError(
            f"Unknown binary loader {type_name!r}. Registered loaders: {known}. "
            f"Custom loaders can be added via register_binary_loader()."
        ) from None
    return cls()

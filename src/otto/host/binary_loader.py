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

from abc import ABC, abstractmethod
from typing import ClassVar

from typing_extensions import override

from ..registry import Registry, caller_module


class BinaryLoader(ABC):
    """How to load/unload a binary into an embedded target's runtime."""

    type_name: ClassVar[str]
    """Lab-data string for this loader (e.g. ``'llext-hex'``); unique across loaders."""

    max_unload_rounds: ClassVar[int] = 16
    """Cap on the unload-to-eviction loop the host drives (see
    :meth:`otto.host.embedded_host.EmbeddedHost.unload`). Some loaders (LLEXT)
    refcount a resident binary, so one unload may decrement without evicting."""

    @abstractmethod
    def load_command(self, name: str, payload: bytes) -> str:
        """Return the device command that loads *payload* under *name*."""
        ...

    @abstractmethod
    def check_loaded(self, output: str) -> tuple[bool, str]:
        """Return ``(ok, reason)`` from a load command's output.

        ``reason`` is the failure text when ``ok`` is False, ``""`` otherwise.
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

    @override
    def load_command(self, name: str, payload: bytes) -> str:
        return f"llext load_hex {name} {payload.hex()}"

    @override
    def check_loaded(self, output: str) -> tuple[bool, str]:
        ok = "Successfully loaded extension" in output
        return (True, "") if ok else (False, output.strip())

    @override
    def unload_command(self, name: str) -> str:
        return f"llext unload {name}"

    @override
    def is_fully_unloaded(self, output: str) -> bool:
        return "No such extension" in output


# Seeded empty here and populated by ``_register_builtin_loaders()`` at module
# end, so otto's own built-ins travel the same ``register_binary_loader`` path
# third parties use.
LOADER_CLASSES: Registry[type[BinaryLoader]] = Registry(
    "binary loader", register_hint="otto.host.binary_loader.register_binary_loader()"
)


def register_binary_loader(
    type_name: str, cls: type[BinaryLoader], *, overwrite: bool = False
) -> None:
    """Make a custom :class:`BinaryLoader` subclass available to lab data.

    Call from an init module listed in ``.otto/settings.toml`` — the same
    pattern :func:`otto.host.command_frame.register_command_frame` follows.

    *overwrite* replaces an existing registration under *type_name*
    deliberately (e.g. a built-in); by default a duplicate name raises.
    """
    if cls.type_name != type_name:
        raise ValueError(
            f"register_binary_loader: type_name {type_name!r} doesn't match "
            f"{cls.__name__}.type_name = {cls.type_name!r}"
        )
    LOADER_CLASSES.register(type_name, cls, overwrite=overwrite, origin=caller_module())


def build_binary_loader(type_name: str) -> BinaryLoader:
    """Construct the :class:`BinaryLoader` registered under *type_name*."""
    return LOADER_CLASSES.get(type_name)()


def _register_builtin_loaders() -> None:
    register_binary_loader(LlextHexLoader.type_name, LlextHexLoader)


_register_builtin_loaders()

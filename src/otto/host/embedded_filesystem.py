"""
On-device filesystem abstraction for :class:`~otto.host.embedded_host.EmbeddedHost`.

Embedded targets are not homogeneous: a Zephyr build may expose FAT on a RAM
disk, LittleFS on simulated flash, or no filesystem at all. Console file
transfer, the disk-equivalent monitor parser, and lab-data validation all
need to know *which* of those a host has — not just "embedded yes/no". This
module is otto's typed representation of that fact.

Each subclass is a small, stateless value object: it carries the mount path,
the optional ``fs mount …`` command needed to bring the FS up, and the
command-formation hooks (``read_command``, ``write_command``, etc.) that
:class:`~otto.host.transfer.EmbeddedFileTransfer` and the embedded
monitor's disk parser call when they need to drive the device shell. The
defaults assume the stock Zephyr ``fs`` shell; a custom filesystem can
override any subset of the hooks.

Lab data declares the variant by string — ``"filesystem": "fat-ram"`` etc. —
and :func:`otto.host.factory.create_host_from_dict` instantiates the
right class. A project can register additional types via
:func:`register_filesystem`; see :doc:`/guide/hosts/extending-embedded`
for the extension walkthrough.

Built-in variants
-----------------

- :class:`NoFileSystem` (``"none"``) — the host has no on-device FS. Console
  transfer fails fast with a clear error; the disk parser yields nothing.
- :class:`FatRamFileSystem` (``"fat-ram"``) — FAT on a RAM disk, mounted at
  ``/RAM:``. Used by the ``sprout`` test target. Requires an explicit
  ``fs mount fat /RAM:`` because Zephyr 3.7's ``zephyr,fstab`` does not bind
  to FAT.
- :class:`LittleFsFileSystem` (``"littlefs"``) — LittleFS on simulated
  flash, mounted at ``/lfs``. Auto-mounted via ``zephyr,fstab``, so no
  ``mount_cmd``.
"""

from abc import ABC
from typing import ClassVar

from ..registry import Registry, caller_module


class EmbeddedFileSystem(ABC):
    """Abstract base for on-device filesystem variants.

    Concrete subclasses set the class-level constants (``type_name``,
    ``mount``, ``mount_cmd``) and may override the command-formation methods
    if their target shell uses a different syntax than stock Zephyr ``fs``.

    Default command hooks
    ---------------------
    The default ``*_command`` methods assume the Zephyr ``fs`` shell. A
    project introducing a vendor filesystem with different syntax (e.g.
    ``myfs read`` instead of ``fs read``) should override only the relevant
    methods; the transfer code and monitor parser call these methods
    rather than hardcoding the literal command strings, so the seam is real.
    """

    type_name: ClassVar[str]
    """Lab-data string for this variant (e.g. ``'fat-ram'``).

    Looked up against ``FILESYSTEM_CLASSES`` by the host factory.
    Must be unique across all registered subclasses.
    """

    mount: ClassVar[str | None]
    """Mount path of this filesystem on the device, or ``None`` when the
    target has no FS. Also used as the default ``default_dest_dir`` for an
    :class:`~otto.host.embedded_host.EmbeddedHost` that doesn't override it
    explicitly."""

    mount_cmd: ClassVar[str | None] = None
    """Optional one-shot ``fs mount …`` command. Needed for filesystems
    Zephyr cannot auto-mount via ``zephyr,fstab`` (notably FAT, in 3.7 LTS).
    ``None`` for auto-mounted filesystems and for :class:`NoFileSystem`."""

    @property
    def supports_transfer(self) -> bool:
        """True when console file transfer can target this filesystem.

        Equivalent to ``mount is not None`` — every FS with a mount path
        supports ``fs read`` / ``fs write``; a :class:`NoFileSystem` host
        does not.
        """
        return self.mount is not None

    @property
    def supports_disk_metric(self) -> bool:
        """True when ``fs statvfs`` is reachable on this filesystem.

        Defaults to ``supports_transfer`` because every Zephyr ``fs`` shell
        with a mount also exposes ``fs statvfs``. Override to ``False`` on
        a custom filesystem that lacks the statvfs subcommand.
        """
        return self.supports_transfer

    # -- Command-formation hooks -------------------------------------------------

    def read_command(self, path: str) -> str:
        """Render the shell command that reads *path* as a hexdump."""
        return f"fs read {path}"

    def write_command(self, path: str, offset: int, hexbytes: str) -> str:
        """Render the chunked-write command for *path*.

        *hexbytes* is the already-space-separated lower-case hex of the
        chunk (e.g. ``"41 42 43"``). Zephyr 3.7's ``fs write`` requires the
        ``-o <offset>`` flag for positional offset (live-verified, see
        :mod:`otto.host.transfer`); a vendor FS that uses a
        positional offset can override this method.
        """
        return f"fs write {path} -o {offset} {hexbytes}"

    def rm_command(self, path: str) -> str:
        """Render the command that removes *path*."""
        return f"fs rm {path}"

    def trunc_command(self, path: str, length: int) -> str:
        """Render the command that truncates *path* to *length* bytes."""
        return f"fs trunc {path} {length}"

    def ls_command(self, path: str) -> str:
        """Render the command that lists *path*."""
        return f"fs ls {path}"

    def statvfs_command(self) -> str | None:
        """Render the filesystem usage stats command, or ``None`` when unavailable.

        No FS or no statvfs builtin → ``None``. Default: ``None`` when no mount,
        ``fs statvfs <mount>`` otherwise.
        """
        if not self.supports_disk_metric or self.mount is None:
            return None
        return f"fs statvfs {self.mount}"


class NoFileSystem(EmbeddedFileSystem):
    """The target has no on-device filesystem.

    Set as the default for an :class:`~otto.host.embedded_host.EmbeddedHost`
    when ``"filesystem"`` is absent from lab data. Console transfer and the
    disk parser both short-circuit to a clear no-op / clear error — never
    a hang or a garbled response from running ``fs`` against a target that
    doesn't have it.
    """

    type_name = "none"
    mount = None


class FatRamFileSystem(EmbeddedFileSystem):
    """FAT on a RAM disk, mounted at ``/RAM:``.

    Used by the ``sprout`` test target on Zephyr 3.7. The ``mount_cmd`` is
    required because the ``zephyr,fstab`` binding in 3.7 LTS does not
    handle FAT — otto issues ``fs mount fat /RAM:`` once on first transfer.
    """

    type_name = "fat-ram"
    mount = "/RAM:"
    mount_cmd = "fs mount fat /RAM:"


class LittleFsFileSystem(EmbeddedFileSystem):
    """LittleFS on simulated flash, mounted at ``/lfs``.

    Used by the ``sprout_lfs`` test target. Auto-mounted via
    ``zephyr,fstab`` at boot, so no ``mount_cmd`` is needed.
    """

    type_name = "littlefs"
    mount = "/lfs"


# Seeded empty here and populated by ``_register_builtin_filesystems()`` at
# module end, so otto's own built-ins travel the same ``register_filesystem``
# path third parties use.
FILESYSTEM_CLASSES: Registry[type[EmbeddedFileSystem]] = Registry(
    "embedded filesystem type", register_hint="otto.host.embedded_filesystem.register_filesystem()"
)


def register_filesystem(
    type_name: str, cls: type[EmbeddedFileSystem], *, overwrite: bool = False
) -> None:
    """Make a custom :class:`EmbeddedFileSystem` subclass available to lab data.

    Call from an init module listed in ``.otto/settings.toml`` — the same
    pattern :func:`otto.monitor.parsers.register_host_parsers` follows.
    Once registered, lab-data entries can reference the subclass by
    *type_name* in the ``filesystem`` field, and
    :func:`otto.host.factory.create_host_from_dict` will instantiate it.

    *overwrite* replaces an existing registration under *type_name*
    deliberately (e.g. a built-in); by default a duplicate name raises.

    Raises
    ------
    ValueError
        If *type_name* doesn't match ``cls.type_name`` (a likely-bug
        mismatch — the registry key and the class constant should agree).
    """
    if cls.type_name != type_name:
        raise ValueError(
            f"register_filesystem: type_name {type_name!r} doesn't match "
            f"{cls.__name__}.type_name = {cls.type_name!r}"
        )
    FILESYSTEM_CLASSES.register(type_name, cls, overwrite=overwrite, origin=caller_module())


def build_filesystem(type_name: str) -> EmbeddedFileSystem:
    """Construct the :class:`EmbeddedFileSystem` registered under *type_name*.

    Used by :func:`otto.host.factory.create_host_from_dict` to resolve
    the lab-data ``filesystem`` string into a typed instance.

    Raises
    ------
    ValueError
        If *type_name* is not registered. The error lists the currently
        registered types so a typo (``'fatram'`` vs ``'fat-ram'``) is
        diagnosable from the message alone.
    """
    return FILESYSTEM_CLASSES.get(type_name)()


def _register_builtin_filesystems() -> None:
    register_filesystem(NoFileSystem.type_name, NoFileSystem)
    register_filesystem(FatRamFileSystem.type_name, FatRamFileSystem)
    register_filesystem(LittleFsFileSystem.type_name, LittleFsFileSystem)


_register_builtin_filesystems()

Adding a custom embedded filesystem
===================================

An :class:`~otto.host.embeddedHost.EmbeddedHost`'s on-device filesystem is a
typed object on the host —
:class:`~otto.host.embedded_filesystem.EmbeddedFileSystem`. It is the source
of truth for:

- the mount path (``/RAM:``, ``/lfs``, ...) — used as the default
  ``default_dest_dir`` and the target of ``fs statvfs`` in the disk metric;
- the optional ``mount_cmd`` that must run once before the first transfer
  (needed for filesystems Zephyr cannot auto-mount via ``zephyr,fstab``);
- the **command-formation hooks** (``read_command``, ``write_command``,
  ``rm_command``, ``trunc_command``, ``ls_command``, ``statvfs_command``)
  that :class:`~otto.host.embedded_transfer.EmbeddedFileTransfer` and the
  embedded monitor's disk parser drive when they talk to the device.

The three built-in variants — :class:`NoFileSystem`, :class:`FatRamFileSystem`,
:class:`LittleFsFileSystem` — assume the stock Zephyr ``fs`` shell. A
project introducing a vendor filesystem or a non-Zephyr embedded OS extends
otto by **subclassing and registering** a new variant. The seam is real:
the transfer code and disk parser never hardcode the literal ``fs ...``
strings, so a custom subclass that overrides one hook composes cleanly with
the inherited defaults for the rest.

Two extension paths, in increasing depth:

1. :ref:`Shallow path <shallow-filesystem>` — different mount path or
   ``mount_cmd``, identical ``fs`` shell syntax. One subclass, one
   ``register_filesystem`` call.
2. :ref:`Deep path <deep-filesystem>` — different command syntax (e.g. a
   vendor shell that uses ``myfs read`` instead of ``fs read``). Override
   one or more command-formation hooks; everything else still inherits.

If your *embedded OS* is also new — not just the filesystem — see
:doc:`adding_an_embedded_os` for the orthogonal session/framing seam. The
two extension stories often pair: a new OS typically also has a new FS.

.. _shallow-filesystem:

Shallow path — new mount / mount_cmd, same shell syntax
-------------------------------------------------------

The common case for a Zephyr-based project: a custom build with a
filesystem otto doesn't ship a class for (e.g. NFFS, FAT mounted at a
non-default path), but using the same Zephyr ``fs`` shell. Subclass
:class:`~otto.host.embedded_filesystem.EmbeddedFileSystem`, set the class
constants, and register the type.

.. code-block:: python

   # myproject/otto_filesystems.py
   from otto.host.embedded_filesystem import EmbeddedFileSystem, register_filesystem

   class NffsFileSystem(EmbeddedFileSystem):
       """NewtNFFS on simulated flash, mounted at ``/nffs``."""
       type_name = 'nffs'
       mount = '/nffs'
       # NFFS auto-mounts via zephyr,fstab — no mount_cmd needed.

   register_filesystem('nffs', NffsFileSystem)

Register from an init module listed in ``.otto/settings.toml`` — the same
location :func:`otto.monitor.parsers.register_host_parsers` is called from
— so the registration runs before any lab data is loaded.

Then declare the variant in lab data:

.. code-block:: json

   {
       "ne": "mote_nffs", "osType": "embedded", "ip": "192.0.2.7",
       "filesystem": "nffs"
   }

That's the whole change. :func:`otto.storage.factory.create_host_from_dict`
resolves ``"nffs"`` through the registry to an :class:`NffsFileSystem`
instance on ``host.filesystem``. Transfers go through ``fs read``/``fs
write`` at the new mount path; the disk metric reports ``fs statvfs /nffs``.

.. _deep-filesystem:

Deep path — different on-device command syntax
----------------------------------------------

Sometimes the device-side tool is not the stock Zephyr ``fs`` shell — a
vendor build might use a name-shifted variant like ``myfs``, or a different
embedded OS might use entirely different commands. Override only the
command-formation hooks that differ:

.. code-block:: python

   class MyFsFileSystem(EmbeddedFileSystem):
       type_name = 'myfs'
       mount = '/data'

       # Overridden — vendor command name.
       def read_command(self, path):
           return f'myfs read {path}'

       def write_command(self, path, offset, hexbytes):
           return f'myfs write {path} {offset} {hexbytes}'

       def rm_command(self, path):
           return f'myfs rm {path}'

       # `trunc_command`, `ls_command`, `statvfs_command` inherit the
       # stock `fs <verb> ...` defaults — fine if the vendor shell also
       # exposes those subcommands, or override them too.

   register_filesystem('myfs', MyFsFileSystem)

The base class's ``supports_transfer`` and ``supports_disk_metric`` derive
from ``mount`` and ``supports_disk_metric`` defaults to ``supports_transfer``;
override either if your filesystem can transfer but lacks ``statvfs`` (or
vice versa).

Hook reference
--------------

The seam — every method on :class:`EmbeddedFileSystem` that the transfer
code and disk parser call — is in source order in the module itself; see
:doc:`embedded_filesystem`.

Common ones:

.. list-table::
   :header-rows: 1
   :widths: 25 35 40

   * - Hook
     - Stock Zephyr default
     - When to override
   * - ``mount`` (class attr)
     - ``'/RAM:'`` etc.
     - Always — declares where on the device this FS lives.
   * - ``mount_cmd`` (class attr)
     - ``None`` (auto-mount)
     - When the FS needs an ``fs mount …`` before the first access.
   * - ``read_command(path)``
     - ``fs read <path>``
     - Vendor shell uses a different verb / argument shape.
   * - ``write_command(path, offset, hexbytes)``
     - ``fs write <path> -o <offset> <hexbytes>``
     - Different offset flag or chunk syntax.
   * - ``statvfs_command()``
     - ``fs statvfs <mount>`` (when ``supports_disk_metric``)
     - FS lacks ``statvfs``; return ``None`` to skip the disk metric.
   * - ``supports_disk_metric``
     - Same as ``supports_transfer``
     - FS supports transfer but not capacity stats.

Validation and errors
---------------------

:func:`otto.storage.factory.validate_host_dict` rejects an unknown
``filesystem`` value before the host is constructed. The error message
lists every currently-registered type, so a typo
(``"fatram"`` vs ``"fat-ram"``) is diagnosable from the message alone:

.. code-block:: text

   ValueError: Field 'filesystem' must be one of: fat-ram, littlefs, none
   (host 'mote' declared 'fatram')

If a host's ``filesystem`` resolves to :class:`NoFileSystem`, the transfer
code short-circuits with a clear, FS-aware error before sending any shell
command — no hang, no garbled response. The embedded monitor's disk parser
likewise yields nothing for that host. Both behaviors are static-declared
in lab data, not runtime-detected.

See also
--------

- :doc:`embedded_filesystem` — the module-level API reference.
- :doc:`embedded_transfer` — how :class:`EmbeddedFileTransfer` consumes the
  filesystem hooks.
- :doc:`adding_an_embedded_os` — the orthogonal session/framing seam, for
  when the *shell* is also new.

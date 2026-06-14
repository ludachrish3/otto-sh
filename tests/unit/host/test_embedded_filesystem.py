"""
Unit tests for :mod:`otto.host.embedded_filesystem`.

The module is small and stateless — these tests pin down the contract that
the storage factory, :class:`~otto.host.embedded_transfer.EmbeddedFileTransfer`,
and the embedded monitor's disk parser all rely on:

- Each registered subclass exposes the right ``type_name`` and ``mount``.
- ``supports_transfer`` / ``supports_disk_metric`` split correctly: the
  no-FS variant blocks both; FAT and LittleFS allow both.
- The command-formation hooks render the stock Zephyr ``fs`` syntax.
- The string-keyed registry is open: a project can register a custom
  subclass and the factory will resolve it.
"""

import pytest

from otto.host.embedded_filesystem import (
    EmbeddedFileSystem,
    FatRamFileSystem,
    LittleFsFileSystem,
    NoFileSystem,
    _FILESYSTEM_CLASSES,
    build_filesystem,
    register_filesystem,
)


# ---------------------------------------------------------------------------
# Built-in variants
# ---------------------------------------------------------------------------

class TestBuiltinVariants:

    def test_no_filesystem_has_no_mount(self):
        fs = NoFileSystem()
        assert fs.type_name == 'none'
        assert fs.mount is None
        assert fs.mount_cmd is None
        assert fs.supports_transfer is False
        assert fs.supports_disk_metric is False
        assert fs.statvfs_command() is None

    def test_fat_ram_mounts_at_ram_with_explicit_mount_cmd(self):
        """FAT on 3.7 LTS does not auto-mount via ``zephyr,fstab``; otto
        issues ``fs mount fat /RAM:`` before the first transfer."""
        fs = FatRamFileSystem()
        assert fs.type_name == 'fat-ram'
        assert fs.mount == '/RAM:'
        assert fs.mount_cmd == 'fs mount fat /RAM:'
        assert fs.supports_transfer is True
        assert fs.supports_disk_metric is True

    def test_littlefs_mounts_at_lfs_no_mount_cmd(self):
        """LittleFS auto-mounts via ``zephyr,fstab``; no ``fs mount`` needed."""
        fs = LittleFsFileSystem()
        assert fs.type_name == 'littlefs'
        assert fs.mount == '/lfs'
        assert fs.mount_cmd is None
        assert fs.supports_transfer is True


# ---------------------------------------------------------------------------
# Command-formation hooks
# ---------------------------------------------------------------------------

class TestCommandHooks:
    """The hooks below are the seam projects override to support a vendor FS
    with different command syntax. Pinning the stock-Zephyr default values
    here is what makes the seam a real contract rather than an internal
    convention."""

    def test_read_command_renders_fs_read(self):
        assert FatRamFileSystem().read_command('/RAM:/x.bin') == 'fs read /RAM:/x.bin'

    def test_write_command_uses_dash_o_offset(self):
        """Zephyr 3.7's ``fs write`` requires ``-o <offset>`` for a positional
        offset — see :mod:`otto.host.embedded_transfer` for the live-shell
        gotcha. The default hook must emit that form."""
        cmd = FatRamFileSystem().write_command('/RAM:/x.bin', 32, '41 42 43')
        assert cmd == 'fs write /RAM:/x.bin -o 32 41 42 43'

    def test_rm_command(self):
        assert LittleFsFileSystem().rm_command('/lfs/x') == 'fs rm /lfs/x'

    def test_trunc_command(self):
        assert LittleFsFileSystem().trunc_command('/lfs/x', 0) == 'fs trunc /lfs/x 0'

    def test_ls_command(self):
        assert FatRamFileSystem().ls_command('/RAM:') == 'fs ls /RAM:'

    def test_statvfs_command_targets_the_mount(self):
        assert FatRamFileSystem().statvfs_command() == 'fs statvfs /RAM:'
        assert LittleFsFileSystem().statvfs_command() == 'fs statvfs /lfs'

    def test_statvfs_command_is_none_when_disk_metric_unsupported(self):
        """``NoFileSystem`` cannot serve a disk metric, so the embedded
        monitor's disk parser uses ``None`` to skip the host cleanly."""
        assert NoFileSystem().statvfs_command() is None


# ---------------------------------------------------------------------------
# Registry + extensibility
# ---------------------------------------------------------------------------

class TestRegistry:

    def test_builtin_types_are_registered(self):
        assert set(_FILESYSTEM_CLASSES) >= {'none', 'fat-ram', 'littlefs'}

    def test_build_filesystem_returns_the_right_class(self):
        assert isinstance(build_filesystem('none'), NoFileSystem)
        assert isinstance(build_filesystem('fat-ram'), FatRamFileSystem)
        assert isinstance(build_filesystem('littlefs'), LittleFsFileSystem)

    def test_build_filesystem_unknown_type_lists_registered_types(self):
        """An unknown type_name surfaces the registered set in the error
        message — a typo like ``'fatram'`` should be obvious from the diff."""
        with pytest.raises(ValueError) as excinfo:
            build_filesystem('fatram')
        msg = str(excinfo.value)
        assert 'fatram' in msg
        assert 'fat-ram' in msg  # Registered types listed for diagnosis.

    def test_register_filesystem_adds_a_custom_subclass(self):
        """A project can register a subclass and have lab data instantiate
        it via :func:`build_filesystem` — the same path the storage factory
        uses."""
        class VendorFs(EmbeddedFileSystem):
            type_name = 'vendor-fs-roundtrip-test'
            mount = '/vfs'

        register_filesystem('vendor-fs-roundtrip-test', VendorFs)
        try:
            instance = build_filesystem('vendor-fs-roundtrip-test')
            assert isinstance(instance, VendorFs)
            assert instance.mount == '/vfs'
            assert instance.supports_transfer is True
        finally:
            _FILESYSTEM_CLASSES.pop('vendor-fs-roundtrip-test', None)

    def test_register_filesystem_rejects_type_name_mismatch(self):
        """Mismatched key and ``cls.type_name`` is a likely bug; surface it
        rather than letting the host load with the wrong identifier."""
        class MismatchedFs(EmbeddedFileSystem):
            type_name = 'declared-name'
            mount = '/m'

        with pytest.raises(ValueError):
            register_filesystem('wrong-name', MismatchedFs)


# ---------------------------------------------------------------------------
# Custom subclass — the "deep" extension path
# ---------------------------------------------------------------------------

class TestCustomCommandSyntax:
    """A project subclass can override individual command-formation hooks
    to target a non-stock Zephyr shell. Show that the seam composes — only
    the overridden method changes; the others inherit the stock defaults."""

    def test_subclass_can_override_one_command_in_isolation(self):
        class VendorFs(EmbeddedFileSystem):
            type_name = 'vendor-test'
            mount = '/vfs'

            def read_command(self, path: str) -> str:
                return f'myfs read {path}'

        fs = VendorFs()
        # Overridden:
        assert fs.read_command('/vfs/x') == 'myfs read /vfs/x'
        # Inherited (stock):
        assert fs.write_command('/vfs/x', 0, '41') == 'fs write /vfs/x -o 0 41'
        assert fs.statvfs_command() == 'fs statvfs /vfs'

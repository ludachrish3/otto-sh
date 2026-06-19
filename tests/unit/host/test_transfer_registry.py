"""Unified transfer backend registry + create seam + applicability (WS#4)."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from otto.host import transfer as xfer_mod
from otto.host.options import NcOptions, ScpOptions
from otto.host.transfer import (
    BaseFileTransfer,
    FtpFileTransfer,
    NcFileTransfer,
    ScpFileTransfer,
    SftpFileTransfer,
    TransferContext,
    build_transfer_backend,
    register_transfer_backend,
)


@pytest.fixture(autouse=True)
def _isolate_transfer_registry():
    saved = dict(xfer_mod._TRANSFER_BACKENDS)
    try:
        yield
    finally:
        xfer_mod._TRANSFER_BACKENDS.clear()
        xfer_mod._TRANSFER_BACKENDS.update(saved)


class TestBuiltins:
    def test_nc_registered_to_ncfiletransfer(self):
        cls = build_transfer_backend("nc")
        assert cls is NcFileTransfer
        assert cls.host_families == frozenset({"unix"})

    def test_ftp_registered_to_ftpfiletransfer(self):
        cls = build_transfer_backend("ftp")
        assert cls is FtpFileTransfer
        assert cls.host_families == frozenset({"unix"})

    def test_scp_registered_to_scpfiletransfer(self):
        cls = build_transfer_backend("scp")
        assert cls is ScpFileTransfer
        assert cls.host_families == frozenset({"unix"})

    def test_sftp_registered_to_sftpfiletransfer(self):
        cls = build_transfer_backend("sftp")
        assert cls is SftpFileTransfer
        assert cls.host_families == frozenset({"unix"})


class TestRegistry:
    def test_unknown_raises_with_known_list(self):
        with pytest.raises(ValueError, match="Unknown transfer backend"):
            build_transfer_backend("nope")

    def test_register_rejects_empty_host_families(self):
        class NoFamilies(BaseFileTransfer):
            host_families = frozenset()

            async def _run_put(self, *a):  # pragma: no cover - not invoked
                ...

            async def _run_get(self, *a):  # pragma: no cover - not invoked
                ...

        with pytest.raises(ValueError, match="host_families is empty"):
            register_transfer_backend("bad", NoFamilies)

    def test_register_and_build_custom(self):
        class XmodemTransfer(NcFileTransfer):
            host_families = frozenset({"unix"})

        register_transfer_backend("xmodem", XmodemTransfer)
        assert build_transfer_backend("xmodem") is XmodemTransfer


class TestCreate:
    def test_create_constructs_ncfiletransfer(self):
        ctx = TransferContext(
            transfer="nc",
            host_name="h1",
            connections=MagicMock(),
            nc_options=NcOptions(),
            scp_options=ScpOptions(),
            get_local_ip=lambda: "1.2.3.4",
            exec_cmd=AsyncMock(),
            max_filename_len=255,
        )
        ft = NcFileTransfer.create(ctx)
        assert isinstance(ft, NcFileTransfer)
        assert ft.transfer == "nc"


def test_public_reexports_available():
    import otto.host as host_pkg

    assert hasattr(host_pkg, "register_term_backend")
    assert hasattr(host_pkg, "register_transfer_backend")
    assert hasattr(host_pkg, "build_transfer_backend")


def test_each_selector_resolves_to_its_own_backend_class():
    from otto.host.transfer import (
        ConsoleFileTransfer,
        FtpFileTransfer,
        NcFileTransfer,
        ScpFileTransfer,
        SftpFileTransfer,
        TftpFileTransfer,
        build_transfer_backend,
    )
    assert build_transfer_backend("scp") is ScpFileTransfer
    assert build_transfer_backend("sftp") is SftpFileTransfer
    assert build_transfer_backend("ftp") is FtpFileTransfer
    assert build_transfer_backend("nc") is NcFileTransfer
    assert build_transfer_backend("console") is ConsoleFileTransfer
    assert build_transfer_backend("tftp") is TftpFileTransfer


def test_public_import_surface_preserved():
    # Names previously importable from otto.host.transfer still import (sans FileTransfer).
    import otto.host as host_pkg
    from otto.host.transfer import (  # noqa: F401
        _TRANSFER_BACKENDS,
        BaseFileTransfer,
        EmbeddedFileTransfer,
        NcListenerCheck,
        NcPortStrategy,
        TransferContext,
        TransferProgressFactory,
        TransferProgressHandler,
        build_transfer_backend,
        make_rich_progress_handler,
        make_transfer_progress,
        register_transfer_backend,
        validate_filename_lengths,
    )
    for name in ("register_transfer_backend", "build_transfer_backend",
                 "make_rich_progress_handler", "make_transfer_progress",
                 "TransferProgressHandler", "NcListenerCheck", "NcPortStrategy",
                 "EmbeddedFileTransfer"):
        assert hasattr(host_pkg, name), name


class TestEmbeddedTransferRegistration:
    def test_console_registered_embedded_only(self):
        from otto.host.transfer import ConsoleFileTransfer, EmbeddedFileTransfer

        cls = build_transfer_backend("console")
        assert cls is ConsoleFileTransfer
        assert issubclass(cls, EmbeddedFileTransfer)
        assert cls.host_families == frozenset({"embedded"})

    def test_tftp_registered_embedded_only(self):
        from otto.host.transfer import EmbeddedFileTransfer, TftpFileTransfer

        cls = build_transfer_backend("tftp")
        assert cls is TftpFileTransfer
        assert issubclass(cls, EmbeddedFileTransfer)
        assert cls.host_families == frozenset({"embedded"})

    def test_embedded_create_constructs(self):
        from unittest.mock import AsyncMock

        from otto.host.transfer import ConsoleFileTransfer, EmbeddedFileTransfer

        ctx = TransferContext(
            transfer="console",
            host_name="dut",
            exec_cmd=AsyncMock(),
            filesystem=None,
            max_filename_len=255,
        )
        ft = ConsoleFileTransfer.create(ctx)
        assert isinstance(ft, EmbeddedFileTransfer)
        assert isinstance(ft, ConsoleFileTransfer)

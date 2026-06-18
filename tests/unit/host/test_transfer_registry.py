"""Unified transfer backend registry + create seam + applicability (WS#4)."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from otto.host import transfer as xfer_mod
from otto.host.options import NcOptions, ScpOptions
from otto.host.transfer import (
    BaseFileTransfer,
    FileTransfer,
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
    @pytest.mark.parametrize("name", ["scp", "sftp", "ftp", "nc"])
    def test_unix_protocols_registered_to_filetransfer(self, name):
        cls = build_transfer_backend(name)
        assert cls is FileTransfer
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
        class XmodemTransfer(FileTransfer):
            host_families = frozenset({"unix"})

        register_transfer_backend("xmodem", XmodemTransfer)
        assert build_transfer_backend("xmodem") is XmodemTransfer


class TestCreate:
    def test_create_constructs_filetransfer(self):
        ctx = TransferContext(
            transfer="scp",
            host_name="h1",
            connections=MagicMock(),
            nc_options=NcOptions(),
            scp_options=ScpOptions(),
            get_local_ip=lambda: "1.2.3.4",
            exec_cmd=AsyncMock(),
            max_filename_len=255,
        )
        ft = FileTransfer.create(ctx)
        assert isinstance(ft, FileTransfer)
        assert ft.transfer == "scp"


def test_public_reexports_available():
    import otto.host as host_pkg

    assert hasattr(host_pkg, "register_term_backend")
    assert hasattr(host_pkg, "register_transfer_backend")
    assert hasattr(host_pkg, "build_transfer_backend")


class TestEmbeddedTransferRegistration:
    def test_console_registered_embedded_only(self):
        from otto.host.embedded_transfer import EmbeddedFileTransfer

        cls = build_transfer_backend("console")
        assert cls is EmbeddedFileTransfer
        assert cls.host_families == frozenset({"embedded"})

    def test_tftp_registered_embedded_only(self):
        cls = build_transfer_backend("tftp")
        assert cls.host_families == frozenset({"embedded"})

    def test_embedded_create_constructs(self):
        from unittest.mock import AsyncMock

        from otto.host.embedded_transfer import EmbeddedFileTransfer

        ctx = TransferContext(
            transfer="console",
            host_name="dut",
            exec_cmd=AsyncMock(),
            filesystem=None,
            max_filename_len=255,
        )
        ft = EmbeddedFileTransfer.create(ctx)
        assert isinstance(ft, EmbeddedFileTransfer)
        assert ft.transfer == "console"

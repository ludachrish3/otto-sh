"""Unified transfer backend registry + create seam + applicability (WS#4)."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from otto.host import transfer as xfer_mod
from otto.host.options import NcOptions, ScpOptions, UserlandOptions
from otto.host.transfer import (
    TRANSFER_BACKENDS,
    BaseFileTransfer,
    FtpFileTransfer,
    NcFileTransfer,
    ProgressGranularity,
    ScpFileTransfer,
    SftpFileTransfer,
    TransferContext,
    build_transfer_backend,
    register_transfer_backend,
)
from otto.host.userland import Userland


def _ctx(**overrides: object) -> TransferContext:
    """A :class:`TransferContext` carrying what any built-in backend needs.

    The union of the unix fields the built-ins read, so one helper builds a ctx
    that ``NcFileTransfer.create``, ``ScpFileTransfer.create`` and
    ``SftpFileTransfer.create`` all accept. *overrides* replace individual
    fields (e.g. a non-default :class:`~otto.host.options.ScpOptions`).
    """
    fields: dict[str, object] = {
        "transfer": "nc",
        "host_name": "h1",
        "connections": MagicMock(),
        "nc_options": NcOptions(),
        "scp_options": ScpOptions(),
        "get_local_ip": lambda: "1.2.3.4",
        "exec_cmd": AsyncMock(),
        "max_filename_len": 255,
        "userland": Userland(UserlandOptions(), AsyncMock()),
    }
    fields.update(overrides)
    return TransferContext(**fields)  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
def _isolate_transfer_registry():
    """Unregister any test-added transfer backend after each test."""
    before = set(xfer_mod.TRANSFER_BACKENDS.names())
    try:
        yield
    finally:
        for name in set(xfer_mod.TRANSFER_BACKENDS.names()) - before:
            xfer_mod.TRANSFER_BACKENDS.unregister(name)


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

    def test_register_rejects_a_backend_without_a_progress_granularity(self):
        class NoPromise(BaseFileTransfer):
            host_families = frozenset({"unix"})

            async def _run_put(self, *a):  # pragma: no cover - not invoked
                ...

            async def _run_get(self, *a):  # pragma: no cover - not invoked
                ...

        with pytest.raises(ValueError, match="progress_granularity is missing"):
            register_transfer_backend("nopromise", NoPromise)

    def test_register_rejects_a_progress_granularity_that_is_not_one(self):
        """A bare int is not a promise -- the refusal checks the TYPE, not the name."""

        class BareInt(BaseFileTransfer):
            host_families = frozenset({"unix"})
            progress_granularity = 8192  # not a ProgressGranularity

            async def _run_put(self, *a):  # pragma: no cover - not invoked
                ...

            async def _run_get(self, *a):  # pragma: no cover - not invoked
                ...

        with pytest.raises(ValueError, match="progress_granularity is missing"):
            register_transfer_backend("bareint", BareInt)

    def test_a_none_arm_without_a_note_cannot_be_declared(self):
        with pytest.raises(ValueError, match="must explain itself in `note`"):
            ProgressGranularity(put=32, get=None)

    def test_a_non_positive_stride_cannot_be_declared(self):
        with pytest.raises(ValueError, match="positive byte count or None"):
            ProgressGranularity(put=0, get=8192)

    def test_every_registered_backend_declares_its_promise(self):
        for name in TRANSFER_BACKENDS.names():
            cls = build_transfer_backend(name)
            declared = cls.progress_granularity
            assert isinstance(declared, ProgressGranularity), name
            for arm in ("put", "get"):
                if getattr(declared, arm) is None:
                    assert declared.note.strip(), f"{name}.{arm} is None with no note"


class TestCreate:
    def test_create_constructs_ncfiletransfer(self):
        """``create`` builds the backend and carries the ctx's userland into it.

        The userland is the seam the listener's hard cap hangs off, and it is
        the only construction input that is silently survivable: drop it and
        the backend still builds, still transfers, and simply stops capping
        its listeners. So the identity is asserted here rather than left to
        the behaviour tests, which all construct the backend directly and
        never exercise this function.
        """
        ctx = _ctx()
        ft = NcFileTransfer.create(ctx)
        assert isinstance(ft, NcFileTransfer)
        assert ft.transfer == "nc"
        assert ft._userland is ctx.userland


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
        TRANSFER_BACKENDS,
        BaseFileTransfer,
        EmbeddedFileTransfer,
        NcListenerCheck,
        NcPortStrategy,
        ProgressGranularity,
        TransferContext,
        TransferProgressFactory,
        TransferProgressHandler,
        build_transfer_backend,
        make_rich_progress_handler,
        make_transfer_progress,
        register_transfer_backend,
        validate_filename_lengths,
    )

    for name in (
        "register_transfer_backend",
        "build_transfer_backend",
        "make_rich_progress_handler",
        "make_transfer_progress",
        "TransferProgressHandler",
        "NcListenerCheck",
        "NcPortStrategy",
        "EmbeddedFileTransfer",
    ):
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


@pytest.mark.asyncio
async def test_sftp_hands_its_declared_stride_to_asyncssh(tmp_path: Path, monkeypatch):
    """The declaration is true by construction: asyncssh reads in blocks of exactly it."""
    from otto.host.transfer import sftp as sftp_mod
    from otto.host.transfer.sftp import SftpFileTransfer

    conn = MagicMock()
    conn.get = AsyncMock(return_value=None)
    conn.put = AsyncMock(return_value=None)

    async def fake_open(*a, **kw):
        return conn

    monkeypatch.setattr(sftp_mod, "open_sftp_or_attribute", fake_open)
    backend = SftpFileTransfer.create(_ctx())
    src = tmp_path / "f.bin"
    src.write_bytes(b"z" * 10)
    await backend._run_put([src], Path("/remote"), None)
    await backend._run_get([Path("/remote/f.bin")], tmp_path, None)
    g = SftpFileTransfer.progress_granularity
    assert conn.put.await_args.kwargs["block_size"] == g.put == 16384
    assert conn.get.await_args.kwargs["block_size"] == g.get == 16384


def test_scp_effective_granularity_is_its_configured_block_size():
    from otto.host.options import ScpOptions
    from otto.host.transfer.scp import ScpFileTransfer

    backend = ScpFileTransfer.create(_ctx(scp_options=ScpOptions(block_size=65536)))
    assert backend.effective_progress_granularity().put == 65536
    assert backend.effective_progress_granularity().get == 65536
    assert ScpFileTransfer.progress_granularity.put == ScpOptions().block_size == 16384


def test_scp_extra_block_size_wins_the_promise_because_it_wins_for_asyncssh():
    """`extra` is applied LAST into the kwargs asyncssh gets, so it decides the stride.

    `ScpOptions._kwargs()` ends `kw.update(self.extra)`, and `extra` is
    user-settable from `[scp_options]` in lab data and documented as "Extra
    kwargs forwarded to ``asyncssh.scp()``". A promise read from the dedicated
    field alone would answer 16384 while asyncssh strode 65536 -- and the
    conformance surface sizes its payload from the promise, so the mismatch
    would surface as a backend failure rather than as the config choice it is.
    """
    from otto.host.options import ScpOptions
    from otto.host.transfer.scp import ScpFileTransfer

    opts = ScpOptions(extra={"block_size": 65536})
    # The premise: this is what actually reaches asyncssh.scp.
    assert opts._kwargs()["block_size"] == 65536
    backend = ScpFileTransfer.create(_ctx(scp_options=opts))
    assert backend.effective_progress_granularity().put == 65536
    assert backend.effective_progress_granularity().get == 65536


def test_a_non_positive_scp_block_size_answers_the_default_instead_of_raising():
    """Reporting the promise must not be the thing that raises on a bad config.

    `ScpOptions.block_size` carries no positivity constraint at either boundary,
    so `block_size = 0` is reachable from lab data. `ProgressGranularity`
    refuses a non-positive stride by construction, which would turn a method
    whose only job is to ANSWER A QUESTION into the place the config error
    surfaces. (asyncssh wedges on 0 independently -- `min(size - offset,
    self._block_size)` never advances -- so the transfer still fails; it just
    does not fail from here.)
    """
    from otto.host.options import ScpOptions
    from otto.host.transfer.scp import ScpFileTransfer

    for hostile in (ScpOptions(block_size=0), ScpOptions(extra={"block_size": -1})):
        backend = ScpFileTransfer.create(_ctx(scp_options=hostile))
        answered = backend.effective_progress_granularity()
        assert answered == ScpFileTransfer.progress_granularity
        assert answered.put == 16384


def test_the_ftp_stride_is_aioftps_own_block_size():
    """``_FTP_BLOCK_SIZE`` mirrors a constant ftp.py cannot read at class-body time.

    ``aioftp`` is imported lazily inside the transfer methods and is absent from
    the ``host`` import-budget snapshot, so reading ``DEFAULT_BLOCK_SIZE`` where
    the declaration is written would pull the package into every ``otto host``
    invocation. ftp.py therefore holds its own copy, and this pin is what keeps
    the copy honest across an aioftp release.
    """
    import aioftp

    from otto.host.transfer.ftp import _FTP_BLOCK_SIZE, FtpFileTransfer

    assert _FTP_BLOCK_SIZE == aioftp.DEFAULT_BLOCK_SIZE
    assert FtpFileTransfer.progress_granularity.put == aioftp.DEFAULT_BLOCK_SIZE
    assert FtpFileTransfer.progress_granularity.get == aioftp.DEFAULT_BLOCK_SIZE

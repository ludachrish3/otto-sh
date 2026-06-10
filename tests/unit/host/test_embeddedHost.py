"""
Tests for EmbeddedHost (Phase 3 skeleton).

These cover construction, the OS-family schema fields, host naming, file
transfer wiring, and the not-yet-implemented interactive bridge. Zephyr command
framing is exercised in ``test_zephyr.py``; the console transfer backend in
``test_embedded_transfer.py``.
"""

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from otto.host import EmbeddedHost, RemoteHost, ZephyrHost
from otto.host.command_frame import ZephyrFrame
from otto.host.host import setDryRun
from otto.host.options import TelnetOptions
from otto.utils import CommandStatus, Status


@pytest.fixture
def host():
    """Bare ZephyrHost, no connections established."""
    h = ZephyrHost(ip='192.0.2.1', ne='sprout', log=False)
    yield h
    # Several tests swap internals for AsyncMocks. A mocked ``_connections``
    # makes ``__del__``'s ``connected`` check truthy, so at GC it would churn
    # an event loop. Drop the reference so ``__del__`` early-returns.
    h._connections = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Generic embedded host: fail-loud without a command_frame
# ---------------------------------------------------------------------------

class TestGenericEmbeddedFailsLoud:
    def test_no_command_frame_raises(self):
        with pytest.raises(ValueError, match='command_frame'):
            EmbeddedHost(ip='192.0.2.1', ne='sprout', log=False)

    def test_explicit_frame_builds_generic_embedded(self):
        h = EmbeddedHost(
            ip='192.0.2.1', ne='sprout', log=False,
            command_frame=ZephyrFrame(),
        )
        h._connections = None  # type: ignore[assignment]
        assert h.osName is None          # generic: no implicit OS name
        assert h.osType == 'embedded'
        assert isinstance(h.command_frame, ZephyrFrame)


# ---------------------------------------------------------------------------
# Initialization
# ---------------------------------------------------------------------------

class TestInit:

    def test_default_values(self, host: EmbeddedHost):
        assert host.ip == '192.0.2.1'
        assert host.ne == 'sprout'
        assert host.creds == {}
        assert host.hop is None
        assert host.resources == set()
        assert host.is_virtual is False

    def test_is_a_remote_host(self, host: EmbeddedHost):
        assert isinstance(host, RemoteHost)

    def test_os_schema_defaults(self, host: EmbeddedHost):
        assert host.osType == 'zephyr'
        assert host.osName == 'Zephyr'
        assert host.osVersion is None

    def test_os_schema_overrides(self):
        host = ZephyrHost(
            ip='192.0.2.1', ne='sprout', log=False,
            osName='Zephyr', osVersion='3.7.0',
        )
        assert host.osName == 'Zephyr'
        assert host.osVersion == '3.7.0'

    def test_telnet_connection_manager(self, host: EmbeddedHost):
        """An embedded host always uses a telnet transport."""
        assert host._connections.term == 'telnet'
        assert host._connections._telnet_conn is None

    def test_custom_telnet_options(self):
        host = ZephyrHost(
            ip='192.0.2.1', ne='sprout', log=False,
            telnet_options=TelnetOptions(port=2323),
        )
        assert host.telnet_options.port == 2323


# ---------------------------------------------------------------------------
# ID and name generation (inherited from RemoteHost)
# ---------------------------------------------------------------------------

class TestIdAndNameGeneration:

    def test_id_no_board(self):
        host = ZephyrHost(ip='192.0.2.1', ne='Sprout', log=False)
        assert host.id == 'sprout'
        assert host.name == 'Sprout'

    def test_id_with_board(self):
        host = ZephyrHost(ip='192.0.2.1', ne='Sprout', board='Mote', log=False)
        assert host.id == 'sprout_mote'
        assert host.name == 'Sprout Mote'

    def test_custom_name_preserved(self):
        host = ZephyrHost(ip='192.0.2.1', ne='sprout', name='custom', log=False)
        assert host.name == 'custom'


# ---------------------------------------------------------------------------
# Hop configuration
# ---------------------------------------------------------------------------

class TestHop:

    def test_no_hop_means_no_transport(self, host: EmbeddedHost):
        assert host._connections._hop is None

    def test_hop_builds_transport(self):
        """A configured hop produces an SshHopTransport on the ConnectionManager."""
        host = ZephyrHost(ip='192.0.2.1', ne='sprout', hop='basil_seed', log=False)
        assert host.hop == 'basil_seed'
        assert host._connections._hop is not None


# ---------------------------------------------------------------------------
# Dry-run command execution
# ---------------------------------------------------------------------------

class TestDryRun:

    @pytest.mark.asyncio
    async def test_run_in_dry_run_skips(self, host: EmbeddedHost):
        setDryRun(True)
        try:
            result = await host.run('kernel version')
        finally:
            setDryRun(False)
        assert result.only.status == Status.Skipped

    @pytest.mark.asyncio
    async def test_oneshot_in_dry_run_skips(self, host: EmbeddedHost):
        setDryRun(True)
        try:
            result = await host.oneshot('kernel uptime')
        finally:
            setDryRun(False)
        assert result.status == Status.Skipped


# ---------------------------------------------------------------------------
# Not-yet-implemented surfaces
# ---------------------------------------------------------------------------

class TestNotImplemented:

    @pytest.mark.asyncio
    async def test_interact_raises(self, host: EmbeddedHost):
        with pytest.raises(NotImplementedError):
            await host.interact()


# ---------------------------------------------------------------------------
# File transfer
# ---------------------------------------------------------------------------

class TestFileTransfer:

    def test_console_backend_by_default(self, host: EmbeddedHost):
        assert host.transfer == 'console'
        assert host._file_transfer.transfer == 'console'

    def test_transfer_backend_is_configurable(self):
        host = ZephyrHost(ip='192.0.2.1', ne='sprout', log=False, transfer='tftp')
        host._connections = None  # type: ignore[assignment]  # avoid __del__ churn
        assert host.transfer == 'tftp'
        assert host._file_transfer.transfer == 'tftp'

    @pytest.mark.asyncio
    async def test_get_dry_run_skips(self, host: EmbeddedHost, tmp_path):
        setDryRun(True)
        try:
            status, _ = await host.get(tmp_path / 'f', tmp_path)
        finally:
            setDryRun(False)
        assert status == Status.Skipped

    @pytest.mark.asyncio
    async def test_put_dry_run_skips(self, host: EmbeddedHost, tmp_path):
        setDryRun(True)
        try:
            status, _ = await host.put(tmp_path / 'f', tmp_path)
        finally:
            setDryRun(False)
        assert status == Status.Skipped


# ---------------------------------------------------------------------------
# default_dest_dir resolution
# ---------------------------------------------------------------------------

class TestDefaultDestDir:
    """``default_dest_dir`` lets a fan-out caller pass a generic
    ``Path()`` and still have each host land transfers on its own mounted
    filesystem — the fix for ``otto -l embedded run test-instruction``
    failing on Zephyr targets where the bare ``/`` has no FS."""

    def test_default_is_empty_path(self, host: EmbeddedHost):
        """When unset, ``default_dest_dir`` is ``Path()`` — preserves the
        ``put(..., dest_dir=Path())`` semantics for hosts whose firmware
        accepts relative paths natively (none in practice for Zephyr, but
        the contract is symmetric)."""
        assert host.default_dest_dir == Path()

    def test_string_in_lab_data_is_coerced_to_path(self):
        """Lab JSON stores ``default_dest_dir`` as a string; ``__post_init__``
        must coerce it so ``_resolve_dest`` can use Path arithmetic."""
        h = ZephyrHost(
            ip='192.0.2.1', ne='sprout', log=False,
            default_dest_dir='/RAM:',  # type: ignore[arg-type]
        )
        h._connections = None  # type: ignore[assignment]  # avoid __del__ churn
        assert h.default_dest_dir == Path('/RAM:')

    def test_resolve_empty_returns_default(self):
        h = ZephyrHost(
            ip='192.0.2.1', ne='sprout', log=False,
            default_dest_dir=Path('/RAM:'),
        )
        h._connections = None  # type: ignore[assignment]
        assert h._resolve_dest(Path()) == Path('/RAM:')

    def test_resolve_absolute_passes_through(self):
        h = ZephyrHost(
            ip='192.0.2.1', ne='sprout', log=False,
            default_dest_dir=Path('/RAM:'),
        )
        h._connections = None  # type: ignore[assignment]
        # Explicit absolute path overrides the default — caller knows where
        # they want it.
        assert h._resolve_dest(Path('/lfs/elsewhere')) == Path('/lfs/elsewhere')

    def test_resolve_relative_joins_under_default(self):
        h = ZephyrHost(
            ip='192.0.2.1', ne='sprout', log=False,
            default_dest_dir=Path('/RAM:'),
        )
        h._connections = None  # type: ignore[assignment]
        assert h._resolve_dest(Path('subdir')) == Path('/RAM:/subdir')

    @pytest.mark.asyncio
    async def test_put_resolves_empty_dest_before_delegating(
        self, host: EmbeddedHost, tmp_path,
    ):
        """End-to-end: ``put(..., dest_dir=Path())`` on a host configured
        with ``default_dest_dir=/RAM:`` must hand ``Path('/RAM:')`` to the
        file-transfer layer, not ``Path()``. This is the failing case from
        ``otto -l embedded run test-instruction``."""
        host.default_dest_dir = Path('/RAM:')
        host._file_transfer = AsyncMock()
        host._file_transfer.put_files.return_value = (Status.Success, '')
        src = tmp_path / 'output1.bin'
        src.write_bytes(b'x')
        await host.put(src, Path())
        passed_dest = host._file_transfer.put_files.call_args.args[1]
        assert passed_dest == Path('/RAM:')


# ---------------------------------------------------------------------------
# Delegation to the session manager
# ---------------------------------------------------------------------------

class TestDelegation:
    """The Host API surface delegates to the SessionManager / ConnectionManager."""

    @pytest.mark.asyncio
    async def test_run_delegates_to_session_manager(self, host: EmbeddedHost):
        host._session_mgr = AsyncMock()
        host._session_mgr.run_cmd.return_value = CommandStatus(
            command='kernel version', output='3.7.0', status=Status.Success, retcode=0,
        )
        result = await host.run('kernel version')
        assert result.only.output == '3.7.0'
        host._session_mgr.run_cmd.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_oneshot_runs_on_persistent_session(self, host: EmbeddedHost):
        """oneshot shares the single console — it goes through run_cmd, not a pool."""
        host._session_mgr = AsyncMock()
        host._session_mgr.run_cmd.return_value = CommandStatus(
            command='kernel uptime', output='42', status=Status.Success, retcode=0,
        )
        result = await host.oneshot('kernel uptime')
        assert result.output == '42'
        host._session_mgr.run_cmd.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_send_delegates(self, host: EmbeddedHost):
        host._session_mgr = AsyncMock()
        await host.send('help\r')
        host._session_mgr.send.assert_awaited_once_with('help\r')

    @pytest.mark.asyncio
    async def test_expect_delegates(self, host: EmbeddedHost):
        host._session_mgr = AsyncMock()
        host._session_mgr.expect.return_value = 'uart:~$'
        out = await host.expect('uart')
        assert out == 'uart:~$'
        host._session_mgr.expect.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_open_session_delegates(self, host: EmbeddedHost):
        host._session_mgr = AsyncMock()
        sentinel = object()
        host._session_mgr.open_session.return_value = sentinel
        result = await host.open_session('monitor')
        assert result is sentinel
        host._session_mgr.open_session.assert_awaited_once_with('monitor')

    @pytest.mark.asyncio
    async def test_close_tears_down_repeater_sessions_connections(self, host: EmbeddedHost):
        host._repeater = AsyncMock()
        host._session_mgr = AsyncMock()
        host._connections = AsyncMock()
        await host.close()
        host._repeater.stop_all.assert_awaited_once()
        host._session_mgr.close_all.assert_awaited_once()
        host._connections.close.assert_awaited_once()


# ---------------------------------------------------------------------------
# verify_connection (dry-run connectivity check)
# ---------------------------------------------------------------------------

class TestVerifyConnection:

    @pytest.mark.asyncio
    async def test_success(self, host: EmbeddedHost):
        host._connections = AsyncMock()
        result = await host.verify_connection()
        assert result.status == Status.Success
        host._connections.telnet.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_failure_is_reported(self, host: EmbeddedHost):
        host._connections = AsyncMock()
        host._connections.telnet.side_effect = ConnectionError('no route to host')
        result = await host.verify_connection()
        assert result.status == Status.Error
        assert 'no route to host' in result.output

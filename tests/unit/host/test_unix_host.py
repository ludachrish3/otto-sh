"""
Unit tests for UnixHost — pure, no-VM coverage of the class internals
(initialization, run/exec dispatch, mocked file-transfer paths, session
creation and the HostSession proxy/lifecycle).

The behavior that needs a live Vagrant bed lives in
:mod:`tests.integration.host.test_unix_host_integration` (parametrized over
ssh / telnet / local).
"""

import asyncio
import contextlib
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from otto.host import HostSession, UnixHost
from otto.host.host import DEFAULT_COMMAND_TIMEOUT
from otto.host.login_proxy import Cred
from otto.host.options import NcOptions, SshOptions, UserlandOptions
from otto.host.session import ShellSession
from otto.host.transfer.nc import _TIMEOUT_STYLE_PREFIXES
from otto.host.userland import Userland
from otto.logger.mode import LogMode
from otto.result import CommandResult, Result
from otto.utils import Status
from tests._fixtures.chaos import ChaosPoints, Surface, sweep_cancellation


def _cs(
    *, command: str = "", output: str = "", status: Status = Status.Success, retcode: int = 0
) -> CommandResult:
    """Build a :class:`~otto.result.CommandResult` for the netcat-transfer fakes.

    The nc backend reads command output from ``.value``; this keeps the old
    ``command=/output=/status=/retcode=`` keyword call shape, mapping ``output``
    onto ``value``.
    """
    return CommandResult(command=command, value=output, status=status, retcode=retcode)


def _sm(result) -> tuple[Status, str]:
    """Unwrap ``(status, msg)`` from a transfer aggregate :class:`~otto.result.Result`."""
    return result.status, result.msg


@pytest.fixture
def host() -> UnixHost:
    """Bare UnixHost, no connections established."""
    return UnixHost(
        ip="10.0.0.1", element="box", creds=[Cred(login="user", password="pass")], log=LogMode.QUIET
    )


# ---------------------------------------------------------------------------
# Initialization
# ---------------------------------------------------------------------------


class TestInit:
    def test_default_values(self, host: UnixHost):
        assert host.ip == "10.0.0.1"
        assert host.element == "box"
        assert host.creds == [Cred(login="user", password="pass")]
        assert host.term == "ssh"
        assert host.transfer == "scp"
        assert host.nc_options.exec_name == "nc"
        assert host.nc_options.port == 9000
        assert host.is_virtual is False
        assert host.hop is None
        assert host._connections._ssh_conn is None
        assert host._connections._sftp_conn is None
        assert host._connections._ftp_conn is None
        assert host._connections._telnet_conn is None


# ---------------------------------------------------------------------------
# ID and name generation
# ---------------------------------------------------------------------------


class TestIdAndNameGeneration:
    @pytest.mark.asyncio
    async def test_id_no_board(self):
        h = UnixHost(
            ip="10.0.0.1",
            element="Alt1",
            creds=[Cred(login="u", password="p")],
            log=LogMode.QUIET,
        )
        assert h.id == "alt1"
        await h.close()

    @pytest.mark.asyncio
    async def test_id_with_board(self):
        h = UnixHost(
            ip="10.0.0.1",
            element="Alt1",
            board="Qemu",
            creds=[Cred(login="u", password="p")],
            log=LogMode.QUIET,
        )
        assert h.id == "alt1_qemu"
        await h.close()

    @pytest.mark.asyncio
    async def test_id_with_board_and_slot(self):
        h = UnixHost(
            ip="10.0.0.1",
            element="Alt1",
            board="Qemu",
            slot=0,
            creds=[Cred(login="u", password="p")],
            log=LogMode.QUIET,
        )
        assert h.id == "alt1_qemu0"
        await h.close()

    @pytest.mark.asyncio
    async def test_name_no_board(self):
        h = UnixHost(
            ip="10.0.0.1",
            element="alt1",
            creds=[Cred(login="u", password="p")],
            log=LogMode.QUIET,
        )
        assert h.name == "alt1"
        await h.close()

    @pytest.mark.asyncio
    async def test_name_with_board(self):
        h = UnixHost(
            ip="10.0.0.1",
            element="alt1",
            board="qemu",
            creds=[Cred(login="u", password="p")],
            log=LogMode.QUIET,
        )
        assert h.name == "alt1 qemu"
        await h.close()

    @pytest.mark.asyncio
    async def test_name_override(self):
        h = UnixHost(
            ip="10.0.0.1",
            element="alt1",
            creds=[Cred(login="u", password="p")],
            name="custom",
            log=LogMode.QUIET,
        )
        assert h.name == "custom"
        await h.close()


# ---------------------------------------------------------------------------
# _creds
# ---------------------------------------------------------------------------


class TestCreds:
    def test_returns_first_pair(self, host: UnixHost):
        user, password = host._creds
        assert user == "user"
        assert password == "pass"

    @pytest.mark.asyncio
    async def test_returns_first_pair_from_multiple_creds(self):
        h = UnixHost(
            ip="10.0.0.1",
            element="box",
            creds=[
                Cred(login="vagrant", password="vagrant"),
                Cred(login="test", password="Password1"),
            ],
            log=LogMode.QUIET,
        )
        user, password = h._creds
        assert user == "vagrant"
        assert password == "vagrant"
        await h.close()


# ---------------------------------------------------------------------------
# userland
# ---------------------------------------------------------------------------


class TestUserland:
    """The host end of the capability layer: one resolver, shared by its consumers.

    ``UserlandOptions`` is what the lab DECLARED; :class:`~otto.host.userland.Userland`
    is what the host RESOLVED. The host owns exactly one of the latter and
    hands the same instance to everything that reads a capability, so a
    resolution paid for by one consumer is not re-paid by the next — against
    a server that refuses excess channels rather than queueing them.
    """

    def _host(self, **extra) -> UnixHost:
        """A host whose real transport, if anything reaches for it, is REFUSED fast.

        Loopback on port 1 rather than the usual unroutable lab address, and
        the difference is the difference between a red and a hang. These tests
        stub ``exec``; the mistakes they exist to catch — a resolver that bound
        ``self.exec`` eagerly, or one built for some other host — are exactly
        the mistakes that send a probe to the REAL transport instead. Against
        an address that silently drops, that is a 180s per-test alarm whose
        message says "deadlock" and names nothing. Against a closed port it is
        ``ConnectionRefusedError`` in milliseconds, the probe reports "could
        not ask", and the assertion below fails saying which answer was wrong.
        """
        extra.setdefault("ssh_options", SshOptions(port=1))
        return UnixHost(
            ip="127.0.0.1",
            element="box",
            creds=[Cred(login="user", password="pass")],
            log=LogMode.QUIET,
            **extra,
        )

    def test_the_host_hands_one_userland_to_every_consumer(self):
        """Built once per host and shared, not rebuilt per reader.

        Three distinct failures, each with its own assertion: no override at
        all (the mixin's ``None`` survives), a fresh resolver per call (every
        reader pays its own probe round and none of them see another's
        answers), and a transfer backend left holding nothing.
        """
        h = self._host(transfer="nc")
        userland = h._userland()
        assert isinstance(userland, Userland)
        assert h._userland() is userland
        assert h._file_transfer._userland is userland

    @pytest.mark.parametrize("style", ["dash-t", "coreutils"])
    @pytest.mark.asyncio
    async def test_a_declared_timeout_style_reaches_the_nc_listener_cap(
        self, style: str, monkeypatch
    ):
        """End to end: a declared answer selects the listener's hard-cap spelling.

        Compared against the backend's own mapping rather than a copied
        string: which spelling each style maps to is pinned in
        ``tests/unit/host/test_transfer_nc_listener_reap.py``, and what THIS
        test adds is that the host's declaration is what picks the row. Both
        styles, because one alone is passed by a backend that ignores the
        userland and hard-codes a prefix.

        ``exec`` is stubbed to fail rather than left alone, and that choice is
        about the shape of the failure. Every capability is declared, so a
        correct host issues no probe at all; a host that dropped the
        declaration on the way to the resolver would probe, and the probe
        would reach for a connection to an address that does not answer. The
        stub converts that into ``timeout_style`` resolving to ``absent`` and
        an empty prefix — a red that names the property — instead of a test
        that hangs until the suite's per-test alarm, which reads the same as a
        genuine deadlock in a summary line.
        """

        async def _refuses(cmd: str, *_a, **_kw) -> CommandResult:
            raise AssertionError(f"a fully declared userland must not probe, but ran {cmd!r}")

        h = self._host(
            transfer="nc",
            userland_options=UserlandOptions(
                shell_dialect="ash",
                elevation="su",
                base64_flag="-d",
                stat_size="stat",
                timeout_style=style,
            ),
        )
        monkeypatch.setattr(h, "exec", _refuses)
        await h._userland().resolve()
        assert h._file_transfer._nc_listener_prefix == _TIMEOUT_STYLE_PREFIXES[style]

    @pytest.mark.asyncio
    async def test_the_userland_probes_this_host_and_not_something_else(self, monkeypatch):
        """The resolver's ``run`` is this host's own ``exec``.

        Nothing is declared, so every capability is probed — and the probes
        have to arrive at THIS host. A resolver handed some other callable
        (or a bare stub) would resolve without the host ever being asked, and
        every answer would then describe a machine that is not the one otto
        is about to run commands on.

        The fake answers ``su`` and refuses ``sudo``, which is the BusyBox
        shape the layer exists for; a resolver reading the fake's replies
        cannot land on the ``sudo`` default by accident. Answering yes to
        everything instead would be satisfied by an ``AsyncMock`` that had
        never been called, so the discrimination is worth the one thing it
        costs: this echoes a probe spelling that
        ``tests/unit/host/test_userland.py`` owns. If that list is reworded,
        the authoritative test reds first and this one follows.
        """
        h = self._host()
        seen: list[str] = []

        async def fake_exec(cmd: str, *_a, **_kw) -> CommandResult:
            seen.append(cmd)
            rc = 0 if cmd == "command -v su" else 1
            return CommandResult(command=cmd, value="", status=Status.Success, retcode=rc)

        monkeypatch.setattr(h, "exec", fake_exec)
        await h._userland().resolve()

        assert h._userland().elevation == "su", (
            f"the resolver answered {h._userland().elevation!r} from somewhere other than "
            f"this host's exec, which saw {seen}"
        )

    @pytest.mark.asyncio
    async def test_an_override_copy_probes_itself_not_the_host_it_was_copied_from(
        self, monkeypatch
    ):
        """A ``dataclasses.replace`` copy gets its own resolver, bound to its own exec.

        This is the property ``_userland_cache``'s ``init=False`` buys, and it
        is not hypothetical: ``otto.config.fleet._apply_option_overrides``
        builds override copies exactly this way for every ``get_host(...)`` /
        ``all_hosts(...)`` call that passes an option table, and the copy opens
        its OWN connection. A copied-over resolver would send that copy's
        probes down the original's transport and answer for the wrong machine
        — silently, because both are the same host at the same address and the
        answers would usually agree.

        Drop the ``init=False`` and ``replace`` carries the cache across:
        every assertion below still reads plausibly, and the original's
        recorder is the only thing that notices.
        """
        original = self._host()
        copy = replace(original, nc_options=NcOptions(port=9999))

        original_saw: list[str] = []
        copy_saw: list[str] = []

        def _recorder(into: list[str]):
            async def _exec(cmd: str, *_a, **_kw) -> CommandResult:
                into.append(cmd)
                return CommandResult(command=cmd, value="", status=Status.Success, retcode=1)

            return _exec

        monkeypatch.setattr(original, "exec", _recorder(original_saw))
        monkeypatch.setattr(copy, "exec", _recorder(copy_saw))

        await copy._userland().resolve()

        assert copy._userland() is not original._userland()
        assert copy_saw, "the copy's resolver never reached the copy"
        assert original_saw == [], (
            f"the copy's resolver probed the host it was copied from: {original_saw}"
        )


# ---------------------------------------------------------------------------
# close()  # noqa: ERA001 — section divider comment
# ---------------------------------------------------------------------------


class TestClose:
    @pytest.mark.asyncio
    async def test_close_when_not_connected_is_safe(self):
        h = UnixHost(
            ip="10.0.0.1", element="box", creds=[Cred(login="u", password="p")], log=LogMode.QUIET
        )
        await h.close()

    @pytest.mark.asyncio
    async def test_close_disconnects_ssh(self):
        h = UnixHost(
            ip="10.0.0.1", element="box", creds=[Cred(login="u", password="p")], log=LogMode.QUIET
        )
        mock_conn = MagicMock()
        mock_conn.wait_closed = AsyncMock()
        h._connections._ssh_conn = mock_conn
        await h.close()
        mock_conn.close.assert_called_once()
        mock_conn.wait_closed.assert_called_once()
        assert h._connections._ssh_conn is None

    @pytest.mark.asyncio
    async def test_close_does_not_run_process_wide_gc(self):
        """close() must not trigger a process-wide gc.collect().

        A process-wide collection sweeps up objects leaked by *other* tests
        and fires their ``__del__``; pytest's ``[unraisable]`` plugin then
        escalates those ResourceWarnings into a flake on whichever test
        happened to call ``close()``.
        """
        import gc

        collected: list[bool] = []

        class _LeakSentinel:
            def __del__(self):
                collected.append(True)

        h = UnixHost(
            ip="10.0.0.1", element="box", creds=[Cred(login="u", password="p")], log=LogMode.QUIET
        )

        # Disable automatic generational gc *before* building the cycle, so
        # only an explicit gc.collect() — not incidental allocation pressure —
        # can reclaim the sentinel.
        gc.disable()
        try:
            # Unreachable reference cycle holding the sentinel; reclaimable
            # only by gc.collect(), not by refcounting.
            cycle: dict = {}
            cycle["self"] = cycle
            cycle["sentinel"] = _LeakSentinel()
            del cycle

            await h.close()
            assert not collected, "close() ran a process-wide gc.collect()"
        finally:
            gc.enable()
            gc.collect()  # clean up our own cycle

    @pytest.mark.asyncio
    async def test_close_closes_transports_when_session_close_raises(self):
        """A wedged session must not leak the raw transports behind it: the
        failure propagates, but _connections.close() still runs (chaos spec:
        teardown chain robustness)."""
        h = UnixHost(
            ip="10.0.0.1", element="box", creds=[Cred(login="u", password="p")], log=LogMode.QUIET
        )
        h._session_mgr.close_all = AsyncMock(side_effect=RuntimeError("session wedged"))
        conn_close = AsyncMock()
        h._connections.close = conn_close
        with pytest.raises(RuntimeError, match="session wedged"):
            await h.close()
        conn_close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_close_chain_sweep(self):
        """Tier-1 sweep: whichever step dies (drop OR cancel), the other still
        runs and the failure propagates — try/finally, not log-and-continue."""
        steps = ["sessions", "connections"]

        async def scenario(points: ChaosPoints) -> None:
            h = UnixHost(
                ip="10.0.0.1",
                element="box",
                creds=[Cred(login="u", password="p")],
                log=LogMode.QUIET,
            )

            async def close_all() -> None:
                await points.point("sessions", surface=Surface.NETWORK)

            async def conn_close() -> None:
                await points.point("connections", surface=Surface.NETWORK)

            h._session_mgr.close_all = close_all
            h._connections.close = conn_close
            await h.close()

        def oracle(points, outcome, exc_type, k) -> None:
            expected = [s for i, s in enumerate(steps) if i != k - 1]
            assert points.executed == expected, f"step behind {steps[k - 1]!r} was skipped"
            assert isinstance(outcome, exc_type), "the failure must stay loud"

        report = await sweep_cancellation(scenario, oracle)
        assert report.points == len(steps)
        # Both steps are transport teardowns: a command-failure cannot arise at
        # either, and saying so keeps the narrowing visible.
        assert report.injected["command-failure"] == 0
        assert report.skipped["command-failure"] == len(steps)
        for name in ("cancellation", "connection-dropped", "connection-reset", "timeout"):
            assert report.injected[name] == len(steps), name


# ---------------------------------------------------------------------------
# run() — list form
# ---------------------------------------------------------------------------


class TestRunList:
    @pytest.mark.asyncio
    async def test_single_element_list(self, host: UnixHost):
        ok = CommandResult(command="echo hi", value="hi", status=Status.Success, retcode=0)
        with patch.object(host, "_run_one", new_callable=AsyncMock, return_value=ok):
            result = await host.run(["echo hi"])
        assert len(result) == 1
        assert result[0] == ok
        assert result.status == Status.Success

    @pytest.mark.asyncio
    async def test_accepts_list_of_commands(self, host: UnixHost):
        r1 = CommandResult(command="ls", value="", status=Status.Success, retcode=0)
        r2 = CommandResult(command="pwd", value="/home", status=Status.Success, retcode=0)
        with patch.object(host, "_run_one", new_callable=AsyncMock, side_effect=[r1, r2]):
            result = await host.run(["ls", "pwd"])
        assert len(result) == 2

    @pytest.mark.asyncio
    async def test_overall_success_when_all_pass(self, host: UnixHost):
        r1 = CommandResult(command="ls", value="", status=Status.Success, retcode=0)
        r2 = CommandResult(command="pwd", value="", status=Status.Success, retcode=0)
        with patch.object(host, "_run_one", new_callable=AsyncMock, side_effect=[r1, r2]):
            result = await host.run(["ls", "pwd"])
        assert result.status == Status.Success

    @pytest.mark.asyncio
    async def test_overall_failed_when_any_fails(self, host: UnixHost):
        r1 = CommandResult(command="ls", value="", status=Status.Success, retcode=0)
        r2 = CommandResult(command="badcmd", value="", status=Status.Failed, retcode=127)
        with patch.object(host, "_run_one", new_callable=AsyncMock, side_effect=[r1, r2]):
            result = await host.run(["ls", "badcmd"])
        assert result.status == Status.Failed


# ---------------------------------------------------------------------------
# Command execution (via session)
# ---------------------------------------------------------------------------


class TestCommandExecution:
    def _mock_session(self, result: CommandResult) -> MagicMock:
        """Create a mock ShellSession that returns a fixed CommandResult."""
        session = MagicMock(spec=ShellSession)
        session.alive = True
        session.run_cmd = AsyncMock(return_value=result)
        session.close = AsyncMock()
        return session

    @pytest.mark.asyncio
    async def test_success(self, host: UnixHost):
        ok = CommandResult(command="echo hello", value="hello", status=Status.Success, retcode=0)
        host._session_mgr._session = self._mock_session(ok)
        result = (await host.run("echo hello")).only
        assert result.status == Status.Success
        assert result.retcode == 0
        assert result.value == "hello"

    @pytest.mark.asyncio
    async def test_failure(self, host: UnixHost):
        fail = CommandResult(
            command="badcmd", value="command not found", status=Status.Failed, retcode=127
        )
        host._session_mgr._session = self._mock_session(fail)
        result = (await host.run("badcmd")).only
        assert result.status == Status.Failed
        assert result.retcode == 127

    @pytest.mark.asyncio
    async def test_connection_failure_propagates(self, host: UnixHost):
        with (
            patch.object(
                host._connections,
                "ssh",
                new_callable=AsyncMock,
                side_effect=ConnectionError("refused"),
            ),
            pytest.raises(ConnectionError),
        ):
            await host.run("echo hi")

    @pytest.mark.asyncio
    async def test_command_recorded(self, host: UnixHost):
        ok = CommandResult(command="echo out", value="out", status=Status.Success, retcode=0)
        host._session_mgr._session = self._mock_session(ok)
        result = (await host.run("echo out")).only
        assert result.command == "echo out"

    @pytest.mark.asyncio
    async def test_expects_forwarded_to_session(self, host: UnixHost):
        ok = CommandResult(command="sudo ls", value="", status=Status.Success, retcode=0)
        host._session_mgr._session = self._mock_session(ok)
        expects = [(r"Password:", "secret\n")]
        await host.run("sudo ls", expects=expects)
        host._session_mgr._session.run_cmd.assert_called_once()
        args, kwargs = host._session_mgr._session.run_cmd.call_args
        assert args == ("sudo ls",)
        assert kwargs["expects"] == expects
        # Single-command form: the default passes straight through unchanged.
        assert kwargs["timeout"] == DEFAULT_COMMAND_TIMEOUT
        assert kwargs["write_progress"] is None
        assert kwargs["redact"] is False
        assert callable(kwargs["on_output"])  # NORMAL-mode tagging sink, not None

    @pytest.mark.asyncio
    async def test_timeout_forwarded_to_session(self, host: UnixHost):
        ok = CommandResult(command="sleep 1", value="", status=Status.Success, retcode=0)
        host._session_mgr._session = self._mock_session(ok)
        await host.run("sleep 1", timeout=30.0)
        host._session_mgr._session.run_cmd.assert_called_once()
        args, kwargs = host._session_mgr._session.run_cmd.call_args
        assert args == ("sleep 1",)
        assert kwargs["expects"] is None
        assert kwargs["timeout"] == 30.0
        assert kwargs["write_progress"] is None
        assert kwargs["redact"] is False
        assert callable(kwargs["on_output"])  # NORMAL-mode tagging sink, not None

    @pytest.mark.asyncio
    async def test_telnet_connection_failure_propagates(self):
        h = UnixHost(
            ip="10.0.0.1",
            element="box",
            creds=[Cred(login="u", password="p")],
            term="telnet",
            log=LogMode.QUIET,
        )
        with (
            patch.object(
                h._connections,
                "telnet",
                new_callable=AsyncMock,
                side_effect=ConnectionError("refused"),
            ),
            pytest.raises(ConnectionError),
        ):
            await h.run("echo hi")
        await h.close()


# ---------------------------------------------------------------------------
# exec() — concurrent-safe command execution
# ---------------------------------------------------------------------------


class TestExec:
    def _mock_ssh_conn(self) -> MagicMock:
        conn = MagicMock()
        conn.wait_closed = AsyncMock()
        return conn

    def _mock_ssh_process(self, lines: list[str], exit_status: int = 0) -> MagicMock:
        """Create a mock SSH process with async-iterable stdout."""
        process = MagicMock()

        class AsyncLineIter:
            def __init__(self, data: list[str]):
                self._data = iter(data)

            def __aiter__(self):
                return self

            async def __anext__(self):
                try:
                    return next(self._data)
                except StopIteration:
                    raise StopAsyncIteration from None

        process.stdout = AsyncLineIter(lines)
        mock_wait_result = MagicMock()
        mock_wait_result.exit_status = exit_status
        process.wait = AsyncMock(return_value=mock_wait_result)
        process.terminate = MagicMock()
        return process

    @pytest.mark.asyncio
    async def test_exec_ssh_success(self, host: UnixHost):
        process = self._mock_ssh_process(["hello\n"])
        host._connections._ssh_conn = self._mock_ssh_conn()
        host._connections._ssh_conn.create_process = AsyncMock(return_value=process)

        result = await host.exec("echo hello")

        assert result.status == Status.Success
        assert result.retcode == 0
        assert result.value == "hello"

    @pytest.mark.asyncio
    async def test_exec_ssh_nonzero_exit(self, host: UnixHost):
        process = self._mock_ssh_process(["not found\n"], exit_status=1)
        host._connections._ssh_conn = self._mock_ssh_conn()
        host._connections._ssh_conn.create_process = AsyncMock(return_value=process)

        result = await host.exec("badcmd")

        assert result.status == Status.Failed
        assert result.retcode == 1

    @pytest.mark.asyncio
    async def test_exec_user_refused_on_unix(self, host: UnixHost):
        """Call `_exec_one` directly — `exec()`'s dry-run/timeout validation
        must not be able to short-circuit the branch under test."""
        with pytest.raises(
            NotImplementedError, match=r"exec\(user=\.\.\.\) is not supported on UnixHost"
        ):
            await host._exec_one("id", timeout=5.0, user="root")

    @pytest.mark.asyncio
    async def test_run_user_refused_on_unix(self, host: UnixHost):
        """Call `_run_one` directly — `run()`'s dry-run/timeout layers must
        not be able to short-circuit the branch under test."""
        with pytest.raises(
            NotImplementedError, match=r"run\(user=\.\.\.\) is not supported on UnixHost"
        ):
            await host._run_one("id", timeout=5.0, user="root")

    @pytest.mark.asyncio
    async def test_run_user_refusal_names_the_shell_reason(self, host: UnixHost):
        with pytest.raises(
            NotImplementedError, match="the persistent shell has no user-switching semantics"
        ):
            await host._run_one("id", timeout=5.0, user="root")

    @pytest.mark.asyncio
    async def test_exec_telnet_success(self):
        h = UnixHost(
            ip="10.0.0.1",
            element="box",
            creds=[Cred(login="u", password="p")],
            term="telnet",
            log=LogMode.QUIET,
        )
        expected = CommandResult(
            command="echo hello", value="hello", status=Status.Success, retcode=0
        )

        mock_client = MagicMock()
        mock_client.connect = AsyncMock()
        mock_client.reader = MagicMock()
        mock_client.writer = MagicMock()
        mock_client.close = AsyncMock()

        mock_session = MagicMock()
        mock_session.run_cmd = AsyncMock(return_value=expected)
        mock_session.close = AsyncMock()
        mock_session._ensure_initialized = AsyncMock()

        with (
            patch("otto.host.session.TelnetClient", return_value=mock_client),
            patch("otto.host.session.TelnetSession", return_value=mock_session),
        ):
            result = await h.exec("echo hello")

        assert result.status == Status.Success
        assert result.value == "hello"
        mock_client.connect.assert_called_once()
        mock_session.run_cmd.assert_called_once()
        args, kwargs = mock_session.run_cmd.call_args
        assert args == ("echo hello",)
        assert kwargs["expects"] is None
        assert kwargs["timeout"] == DEFAULT_COMMAND_TIMEOUT
        assert kwargs["redact"] is False
        assert callable(kwargs["on_output"])  # NORMAL-mode tagging sink, not None
        await h.close()

    @pytest.mark.concurrency
    @pytest.mark.asyncio
    async def test_exec_telnet_concurrent_does_not_deadlock(self):
        """Regression: concurrent telnet ``exec()`` calls must not serialize.

        ``_put_files_nc`` launches ``nc -l <port>`` via ``exec(timeout=float("inf"))``
        to start a listener, then — when multiple files are transferred in
        parallel via ``asyncio.gather`` — other concurrent ``exec()`` calls
        run alongside it (port discovery for the next file, additional
        listeners, etc.).  The documented contract of ``exec()`` is that
        concurrent calls run independently.  When the telnet cache serializes
        all calls through a single session, the second call blocks waiting
        for the first to finish; the paired ``_connect_with_retry`` on the
        caller side then times out with "Remote nc listener on <ip>:<port>
        not ready".
        """
        h = UnixHost(
            ip="10.0.0.1",
            element="test2",
            creds=[Cred(login="u", password="p")],
            term="telnet",
            log=LogMode.QUIET,
        )

        listener_running = asyncio.Event()
        release_listener = asyncio.Event()

        async def _fake_run_cmd(
            cmd, expects=None, timeout=None, on_output=None, redact=False, write_progress=None
        ):
            if "nc -l" in cmd:
                listener_running.set()
                await release_listener.wait()
            return CommandResult(command=cmd, value="", status=Status.Success, retcode=0)

        def _new_client(*args, **kwargs):
            c = MagicMock()
            c.connect = AsyncMock()
            c.reader = MagicMock()
            c.writer = MagicMock()
            c.close = AsyncMock()
            return c

        def _new_session(*args, **kwargs):
            s = MagicMock()
            s.run_cmd = AsyncMock(side_effect=_fake_run_cmd)
            s.close = AsyncMock()
            s._ensure_initialized = AsyncMock()
            s.alive = True
            s._on_output = None
            return s

        with (
            patch("otto.host.session.TelnetClient", side_effect=_new_client),
            patch("otto.host.session.TelnetSession", side_effect=_new_session),
        ):
            listener_task = asyncio.create_task(
                h.exec("nc -l 45681 < /dev/null > /tmp/x 2>/dev/null", timeout=float("inf")),
            )
            # Wait until the listener is actually running inside its
            # session, so we know it's holding whatever resource the
            # cache uses.
            await asyncio.wait_for(listener_running.wait(), timeout=1.0)

            # A concurrent exec() call must NOT block on the listener.
            # Under the bug this deadlocks and wait_for raises TimeoutError.
            try:
                await asyncio.wait_for(h.exec("echo concurrent"), timeout=1.0)
            except asyncio.TimeoutError:
                pytest.fail(
                    "h.exec() deadlocked waiting for a concurrent long-"
                    "running telnet exec — reproduces the "
                    "'Remote nc listener on <ip>:<port> not ready' "
                    "failure in _put_files_nc on telnet hosts",
                )
            finally:
                release_listener.set()
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(listener_task, timeout=1.0)

        await h.close()

    @pytest.mark.asyncio
    async def test_exec_timeout_forwarded(self, host: UnixHost):
        process = self._mock_ssh_process([])
        host._connections._ssh_conn = self._mock_ssh_conn()
        host._connections._ssh_conn.create_process = AsyncMock(return_value=process)

        await host.exec("sleep 5", timeout=30.0)

        host._connections._ssh_conn.create_process.assert_called_once()

    @pytest.mark.asyncio
    async def test_exec_forwards_log_false(self):
        from unittest.mock import AsyncMock

        from otto.host.unix_host import UnixHost
        from otto.result import CommandResult
        from otto.utils import Status

        h = UnixHost(
            ip="10.0.0.1",
            element="box",
            creds=[Cred(login="user", password="pass")],
            log=LogMode.QUIET,
        )
        h._session_mgr = AsyncMock()
        h._session_mgr.exec.return_value = CommandResult(
            command="c",
            value="",
            status=Status.Success,
            retcode=0,
        )
        # QUIET command composes with the host's standing mode (also QUIET) → QUIET.
        await h.exec("base64 /bin/ls", log=LogMode.QUIET)
        h._session_mgr.exec.assert_awaited_once_with(
            "base64 /bin/ls",
            timeout=DEFAULT_COMMAND_TIMEOUT,
            log=LogMode.QUIET,
        )


# ---------------------------------------------------------------------------
# File transfer: put/get user= refusal
# ---------------------------------------------------------------------------


class TestPutGetUserRefusal:
    """`user` is a container-only concept on the transfer path; UnixHost
    refuses it loudly, above the dry-run arm, rather than silently ignoring
    it. Entering at the public method (not `_exec_one`-style private helper)
    because the refusal itself lives directly in `put`/`get`.
    """

    @pytest.mark.asyncio
    async def test_put_user_refused_on_unix(self, host: UnixHost):
        with pytest.raises(
            NotImplementedError, match=r"put\(user=\.\.\.\) is not supported on UnixHost"
        ):
            await host.put(Path("a"), Path("/tmp"), user="root")

    @pytest.mark.asyncio
    async def test_get_user_refused_on_unix(self, host: UnixHost):
        with pytest.raises(
            NotImplementedError, match=r"get\(user=\.\.\.\) is not supported on UnixHost"
        ):
            await host.get(Path("a"), Path("/tmp"), user="root")


# ---------------------------------------------------------------------------
# File transfer: not-connected errors
# ---------------------------------------------------------------------------


class TestNotConnectedFileTransfer:
    @pytest.mark.asyncio
    async def test_scp_get_raises(self, host: UnixHost):
        with (
            patch.object(
                host._connections,
                "ssh",
                new_callable=AsyncMock,
                side_effect=RuntimeError("not connected"),
            ),
            pytest.raises(RuntimeError, match="not connected"),
        ):
            await host.get([Path("/remote/file.txt")], Path("/tmp"))

    @pytest.mark.asyncio
    async def test_scp_put_raises(self, host: UnixHost):
        with (
            patch.object(
                host._connections,
                "ssh",
                new_callable=AsyncMock,
                side_effect=RuntimeError("not connected"),
            ),
            pytest.raises(RuntimeError, match="not connected"),
        ):
            await host.put([Path("/tmp/file.txt")], Path("/tmp"))

    @pytest.mark.asyncio
    async def test_sftp_get_raises(self):
        h = UnixHost(
            ip="10.0.0.1",
            element="box",
            creds=[Cred(login="u", password="p")],
            transfer="sftp",
            log=LogMode.QUIET,
        )
        with (
            patch.object(
                h._connections,
                "ssh",
                new_callable=AsyncMock,
                side_effect=RuntimeError("not connected"),
            ),
            pytest.raises(RuntimeError, match="not connected"),
        ):
            await h.get([Path("/remote/file.txt")], Path("/tmp"))
        await h.close()

    @pytest.mark.asyncio
    async def test_sftp_put_raises(self):
        h = UnixHost(
            ip="10.0.0.1",
            element="box",
            creds=[Cred(login="u", password="p")],
            transfer="sftp",
            log=LogMode.QUIET,
        )
        with (
            patch.object(
                h._connections,
                "ssh",
                new_callable=AsyncMock,
                side_effect=RuntimeError("not connected"),
            ),
            pytest.raises(RuntimeError, match="not connected"),
        ):
            await h.put([Path("/tmp/file.txt")], Path("/tmp"))
        await h.close()

    @pytest.mark.asyncio
    async def test_ftp_get_raises(self):
        h = UnixHost(
            ip="10.0.0.1",
            element="box",
            creds=[Cred(login="u", password="p")],
            transfer="ftp",
            log=LogMode.QUIET,
        )
        with (
            patch.object(
                h._connections,
                "ftp",
                new_callable=AsyncMock,
                side_effect=RuntimeError("not connected"),
            ),
            pytest.raises(RuntimeError, match="not connected"),
        ):
            await h.get([Path("/remote/file.txt")], Path("/tmp"))
        await h.close()

    @pytest.mark.asyncio
    async def test_ftp_put_raises(self):
        h = UnixHost(
            ip="10.0.0.1",
            element="box",
            creds=[Cred(login="u", password="p")],
            transfer="ftp",
            log=LogMode.QUIET,
        )
        with (
            patch.object(
                h._connections,
                "ftp",
                new_callable=AsyncMock,
                side_effect=RuntimeError("not connected"),
            ),
            pytest.raises(RuntimeError, match="not connected"),
        ):
            await h.put([Path("/tmp/file.txt")], Path("/tmp"))
        await h.close()

    @pytest.mark.asyncio
    async def test_nc_get_raises(self):
        h = UnixHost(
            ip="10.0.0.1",
            element="box",
            creds=[Cred(login="u", password="p")],
            transfer="nc",
            log=LogMode.QUIET,
        )

        # The file-size stat succeeds; the nc send exec fails (not
        # connected) — get must surface that as an error, not raise.
        async def mock_exec(cmd: str, **kw) -> CommandResult:
            if cmd.startswith("stat"):
                return _cs(
                    command=cmd, output="0 regular empty file", status=Status.Success, retcode=0
                )
            raise RuntimeError("not connected")

        async def fake_start_server(cb, host, port):
            mock_server = AsyncMock()
            mock_server.close = MagicMock()
            mock_server.wait_closed = AsyncMock()
            mock_server.sockets = [MagicMock()]
            mock_server.sockets[0].getsockname.return_value = ("0.0.0.0", 9999)
            return mock_server

        with (
            patch.object(h, "_get_local_ip", return_value="127.0.0.1"),
            patch.object(h, "exec", new_callable=AsyncMock, side_effect=mock_exec),
            patch("otto.host.transfer.nc.asyncio.start_server", side_effect=fake_start_server),
        ):
            status, _ = _sm(await h.get([Path("/remote/file.txt")], Path("/tmp")))
        assert status == Status.Error
        await h.close()

    @pytest.mark.asyncio
    async def test_nc_put_raises(self, tmp_path: Path):
        h = UnixHost(
            ip="10.0.0.1",
            element="box",
            creds=[Cred(login="u", password="p")],
            transfer="nc",
            log=LogMode.QUIET,
        )
        src = tmp_path / "file.txt"
        src.write_bytes(b"data")
        with (
            patch.object(
                h, "exec", new_callable=AsyncMock, side_effect=RuntimeError("not connected")
            ),
            patch(
                "otto.host.transfer.nc._connect_with_retry",
                AsyncMock(side_effect=ConnectionError("nc listener not ready")),
            ),
        ):
            status, _ = _sm(await h.put([src], Path("/tmp")))
        assert status == Status.Error
        await h.close()


# ---------------------------------------------------------------------------
# File transfer: mocked success paths
# ---------------------------------------------------------------------------


class TestSshFileTransfer:
    def _mock_ssh_conn(self) -> MagicMock:
        conn = MagicMock()
        conn.wait_closed = AsyncMock()
        return conn

    @pytest.mark.asyncio
    async def test_scp_get_success(self, host: UnixHost):
        host._connections._ssh_conn = self._mock_ssh_conn()
        with patch("asyncssh.scp", new_callable=AsyncMock) as mock_scp:
            status, msg = _sm(
                await host.get([Path("/etc/hostname")], Path("/tmp"), show_progress=False)
            )
        assert status == Status.Success
        assert msg == ""
        mock_scp.assert_called_once()

    @pytest.mark.asyncio
    async def test_scp_put_success(self, host: UnixHost, tmp_path: Path):
        src = tmp_path / "upload.txt"
        src.write_text("hello")
        host._connections._ssh_conn = self._mock_ssh_conn()
        with patch("asyncssh.scp", new_callable=AsyncMock) as mock_scp:
            status, _msg = _sm(await host.put([src], Path("/tmp"), show_progress=False))
        assert status == Status.Success
        mock_scp.assert_called_once()

    @pytest.mark.asyncio
    async def test_sftp_get_success(self):
        h = UnixHost(
            ip="10.0.0.1",
            element="box",
            creds=[Cred(login="u", password="p")],
            transfer="sftp",
            log=LogMode.QUIET,
        )
        mock_sftp = MagicMock()
        mock_sftp.get = AsyncMock()
        h._connections._sftp_conn = mock_sftp

        status, _msg = _sm(await h.get([Path("/etc/hostname")], Path("/tmp"), show_progress=False))
        assert status == Status.Success
        mock_sftp.get.assert_called_once()
        await h.close()

    @pytest.mark.asyncio
    async def test_sftp_put_success(self, tmp_path: Path):
        h = UnixHost(
            ip="10.0.0.1",
            element="box",
            creds=[Cred(login="u", password="p")],
            transfer="sftp",
            log=LogMode.QUIET,
        )
        src = tmp_path / "upload.txt"
        src.write_text("hello")
        mock_sftp = MagicMock()
        mock_sftp.put = AsyncMock()
        h._connections._sftp_conn = mock_sftp

        status, _msg = _sm(await h.put([src], Path("/tmp"), show_progress=False))
        assert status == Status.Success
        mock_sftp.put.assert_called_once()
        await h.close()

    @pytest.mark.asyncio
    async def test_ftp_get_success(self):
        h = UnixHost(
            ip="10.0.0.1",
            element="box",
            creds=[Cred(login="u", password="p")],
            transfer="ftp",
            log=LogMode.QUIET,
        )
        mock_ftp = MagicMock()
        mock_ftp.download = AsyncMock()
        mock_ftp.quit = AsyncMock()  # called by close()
        h._connections._ftp_conn = mock_ftp

        status, _msg = _sm(
            await h.get([Path("/home/vagrant/test.txt")], Path("/tmp"), show_progress=False)
        )
        assert status == Status.Success
        mock_ftp.download.assert_called_once()
        await h.close()

    @pytest.mark.asyncio
    async def test_ftp_put_success(self, tmp_path: Path):
        h = UnixHost(
            ip="10.0.0.1",
            element="box",
            creds=[Cred(login="u", password="p")],
            transfer="ftp",
            log=LogMode.QUIET,
        )
        src = tmp_path / "upload.txt"
        src.write_text("hello")
        mock_ftp = MagicMock()
        mock_ftp.upload = AsyncMock()
        mock_ftp.quit = AsyncMock()  # called by close()
        h._connections._ftp_conn = mock_ftp

        status, _msg = _sm(await h.put([src], Path("/tmp"), show_progress=False))
        assert status == Status.Success
        mock_ftp.upload.assert_called_once()
        await h.close()


# ---------------------------------------------------------------------------
# File transfer: netcat mocked unit tests
# ---------------------------------------------------------------------------


class TestNcFileTransfer:
    @pytest.mark.asyncio
    async def test_nc_get_success(self, tmp_path: Path):
        h = UnixHost(
            ip="10.0.0.1",
            element="box",
            creds=[Cred(login="u", password="p")],
            transfer="nc",
            log=LogMode.QUIET,
        )

        send_cs = _cs(command="nc ...", output="", status=Status.Success, retcode=0)

        # Control-plane ops (the file-size stat) and the nc send all route
        # through `exec` now — no dedicated monitor session.
        async def mock_exec(cmd: str, **kw) -> CommandResult:
            if cmd.startswith("stat"):
                # The payload's real size, because the read loop terminates on
                # it: an overstated stat is a short read now, not a cosmetic
                # progress mismatch. The type rides along in the same answer --
                # `stat -L -c '%s %F'` -- because only a regular file delivers
                # `st_size` bytes down the pipe.
                return _cs(
                    command=cmd,
                    output=f"{len(file_data)} regular file",
                    status=Status.Success,
                    retcode=0,
                )
            return send_cs

        dest = tmp_path / "out"
        dest.mkdir()

        file_data = b"hello world"

        async def fake_start_server(cb, host, port):
            """Simulate asyncio.start_server: invoke the callback with a reader that yields file_data."""  # noqa: E501 — descriptive docstring
            mock_server = AsyncMock()
            mock_server.close = MagicMock()
            mock_server.wait_closed = AsyncMock()

            reader = AsyncMock(spec=asyncio.StreamReader)
            reader.read = AsyncMock(side_effect=[file_data, b""])
            writer = MagicMock()
            writer.close = MagicMock()

            # Fire the connection handler so it writes the file
            asyncio.get_running_loop().call_soon(lambda: asyncio.ensure_future(cb(reader, writer)))
            return mock_server

        with (
            patch.object(h, "exec", AsyncMock(side_effect=mock_exec)) as mock_os,
            patch.object(h, "_get_local_ip", return_value="127.0.0.1"),
            patch("otto.host.transfer.nc.asyncio.start_server", side_effect=fake_start_server),
        ):
            status, msg = _sm(await h.get([Path("/remote/file.txt")], dest, show_progress=False))

        assert status == Status.Success
        assert msg == ""
        assert (dest / "file.txt").read_bytes() == file_data
        # The file-size stat ran as a control-plane exec.
        assert any(
            c.args and c.args[0] == "stat -L -c '%s %F' /remote/file.txt"
            for c in mock_os.await_args_list
        )
        await h.close()

    @pytest.mark.asyncio
    async def test_nc_put_success(self, tmp_path: Path):
        h = UnixHost(
            ip="10.0.0.1",
            element="box",
            creds=[Cred(login="u", password="p")],
            transfer="nc",
            log=LogMode.QUIET,
        )

        src = tmp_path / "upload.txt"
        src.write_bytes(b"test content")

        # Command-dispatched responses instead of a positional side_effect list:
        # _put_files_nc now also runs `_wait_for_remote_listener`, which
        # probes for `ss`/`netstat` and then polls the listener — several
        # extra exec calls whose order a positional list can't capture.
        async def mock_exec(cmd: str, **kw) -> CommandResult:
            if "nc -l" in cmd:
                return _cs(command=cmd, output="", status=Status.Success, retcode=0)
            if cmd.startswith("type "):
                return _cs(command=cmd, output="", status=Status.Success, retcode=0)
            if "ss -tln" in cmd or "netstat -tln" in cmd or "/proc/net/tcp" in cmd:
                return _cs(command=cmd, output="", status=Status.Success, retcode=0)
            if cmd.startswith("stat -c %s "):
                return _cs(
                    command=cmd, output=str(src.stat().st_size), status=Status.Success, retcode=0
                )
            # Port discovery (ss/netstat/python/proc) returns a port number.
            return _cs(command=cmd, output="44444", status=Status.Success, retcode=0)

        sent_data = bytearray()
        mock_writer = MagicMock()
        mock_writer.write = MagicMock(side_effect=sent_data.extend)
        mock_writer.drain = AsyncMock()
        mock_writer.close = MagicMock()
        mock_writer.wait_closed = AsyncMock()
        mock_reader = AsyncMock(spec=asyncio.StreamReader)

        with (
            patch.object(h, "exec", AsyncMock(side_effect=mock_exec)),
            patch(
                "otto.host.transfer.nc._connect_with_retry",
                AsyncMock(return_value=(mock_reader, mock_writer)),
            ),
        ):
            status, msg = _sm(await h.put([src], Path("/tmp"), show_progress=False))

        assert status == Status.Success
        assert msg == ""
        assert sent_data == b"test content"
        mock_writer.drain.assert_called()
        mock_writer.close.assert_called_once()
        await h.close()

    @pytest.mark.asyncio
    async def test_nc_put_suppresses_host_logging_during_transfer(self, tmp_path: Path):
        """During put, host.log must be QUIET so per-host records are
        dropped by HostFilter; it must be restored to its prior value after
        the transfer completes."""
        h = UnixHost(
            ip="10.0.0.1",
            element="box",
            creds=[Cred(login="u", password="p")],
            transfer="nc",
            log=LogMode.NORMAL,
        )

        src = tmp_path / "upload.txt"
        src.write_bytes(b"test content")

        log_states: list[object] = []

        async def exec_capturing_log(cmd: str, **_kw) -> CommandResult:
            log_states.append(h.log)
            # Compound strategy probe runs first (warm-up); return a valid
            # port+listener pair so the cascades don't fire.
            if cmd.startswith("port=proc; listener=proc"):
                return _cs(command=cmd, output="python proc", status=Status.Success, retcode=0)
            if "nc -l" in cmd:
                return _cs(command=cmd, output="", status=Status.Success, retcode=0)
            if "ss -tln" in cmd or "netstat -tln" in cmd or "/proc/net/tcp" in cmd:
                return _cs(command=cmd, output="", status=Status.Success, retcode=0)
            if cmd.startswith("stat -c %s "):
                return _cs(
                    command=cmd, output=str(src.stat().st_size), status=Status.Success, retcode=0
                )
            # Port discovery returns a port number.
            return _cs(command=cmd, output="44444", status=Status.Success, retcode=0)

        mock_writer = MagicMock()
        mock_writer.write = MagicMock()
        mock_writer.drain = AsyncMock()
        mock_writer.close = MagicMock()
        mock_writer.wait_closed = AsyncMock()
        mock_reader = AsyncMock(spec=asyncio.StreamReader)

        assert h.log is LogMode.NORMAL
        with (
            patch.object(h, "exec", AsyncMock(side_effect=exec_capturing_log)),
            patch(
                "otto.host.transfer.nc._connect_with_retry",
                AsyncMock(return_value=(mock_reader, mock_writer)),
            ),
        ):
            status, _ = _sm(await h.put([src], Path("/tmp"), show_progress=False))

        assert status == Status.Success
        assert log_states
        assert all(state is LogMode.QUIET for state in log_states)
        assert h.log is LogMode.NORMAL
        await h.close()

    @pytest.mark.asyncio
    async def test_nc_get_suppresses_host_logging_during_transfer(self, tmp_path: Path):
        """Symmetric check for get — the file-size stat and the send exec
        must both run with host.log == LogMode.QUIET."""
        h = UnixHost(
            ip="10.0.0.1",
            element="box",
            creds=[Cred(login="u", password="p")],
            transfer="nc",
            log=LogMode.NORMAL,
        )

        send_cs = _cs(command="nc ...", output="", status=Status.Success, retcode=0)

        log_states: list[object] = []

        dest = tmp_path / "out"
        dest.mkdir()

        file_data = b"hello world"

        async def fake_start_server(cb, host, port):
            mock_server = AsyncMock()
            mock_server.close = MagicMock()
            mock_server.wait_closed = AsyncMock()
            reader = AsyncMock(spec=asyncio.StreamReader)
            reader.read = AsyncMock(side_effect=[file_data, b""])
            writer = MagicMock()
            writer.close = MagicMock()
            asyncio.get_running_loop().call_soon(lambda: asyncio.ensure_future(cb(reader, writer)))
            return mock_server

        async def exec_capturing_log(cmd: str, *_a, **_kw) -> CommandResult:
            log_states.append(h.log)
            if cmd.startswith("stat"):
                return _cs(command=cmd, output="11 regular file", status=Status.Success, retcode=0)
            return send_cs

        assert h.log is LogMode.NORMAL
        with (
            patch.object(h, "exec", AsyncMock(side_effect=exec_capturing_log)),
            patch.object(h, "_get_local_ip", return_value="127.0.0.1"),
            patch("otto.host.transfer.nc.asyncio.start_server", side_effect=fake_start_server),
        ):
            status, _ = _sm(await h.get([Path("/remote/file.txt")], dest, show_progress=False))

        assert status == Status.Success
        assert log_states
        assert all(state is LogMode.QUIET for state in log_states)
        assert h.log is LogMode.NORMAL
        await h.close()


# ---------------------------------------------------------------------------
# open_session() — session creation (unit)
# ---------------------------------------------------------------------------


class TestOpenSession:
    """Unit tests for UnixHost.open_session() — session creation and registration."""

    def _mock_shell_session(self, alive: bool = True) -> MagicMock:
        ok = CommandResult(command="echo hi", value="hi", status=Status.Success, retcode=0)
        session = MagicMock(spec=ShellSession)
        session.alive = alive
        session.run_cmd = AsyncMock(return_value=ok)
        session.send = AsyncMock()
        session.expect = AsyncMock(return_value="output")
        session.close = AsyncMock()
        return session

    def _mock_telnet_client(self) -> MagicMock:
        client = MagicMock()
        client.connect = AsyncMock()
        client.reader = MagicMock()
        client.writer = MagicMock()
        client.close = AsyncMock()
        return client

    # --- SSH ---

    @pytest.mark.asyncio
    async def test_ssh_returns_remote_session(self, host: UnixHost):
        mock_shell = self._mock_shell_session()
        host._connections._ssh_conn = MagicMock()
        with patch("otto.host.session.SshSession", return_value=mock_shell):
            result = await host.open_session("monitor")
        assert isinstance(result, HostSession)
        assert result.alive is True

    @pytest.mark.asyncio
    async def test_ssh_session_registered_in_host(self, host: UnixHost):
        mock_shell = self._mock_shell_session()
        host._connections._ssh_conn = MagicMock()
        with patch("otto.host.session.SshSession", return_value=mock_shell):
            result = await host.open_session("monitor")
        assert host._session_mgr._named_sessions["monitor"] is result

    @pytest.mark.asyncio
    async def test_ssh_session_uses_existing_conn(self, host: UnixHost):
        mock_conn = MagicMock()
        mock_conn.wait_closed = AsyncMock()
        host._connections._ssh_conn = mock_conn
        mock_shell = self._mock_shell_session()
        with patch("otto.host.session.SshSession", return_value=mock_shell) as MockSshSession:  # noqa: N806 — CapWords for a class mock
            await host.open_session("monitor")
        MockSshSession.assert_called_once_with(
            mock_conn,
            command_frame=None,
            init_timeout=None,
            shell_history=False,
        )

    # --- Telnet ---

    @pytest.mark.asyncio
    async def test_telnet_returns_remote_session(self):
        h = UnixHost(
            ip="10.0.0.1",
            element="box",
            creds=[Cred(login="u", password="p")],
            term="telnet",
            log=LogMode.QUIET,
        )
        mock_shell = self._mock_shell_session()
        with (
            patch("otto.host.session.TelnetClient", return_value=self._mock_telnet_client()),
            patch("otto.host.session.TelnetSession", return_value=mock_shell),
        ):
            result = await h.open_session("monitor")
        assert isinstance(result, HostSession)
        assert result.alive is True
        await h.close()

    @pytest.mark.asyncio
    async def test_telnet_connects_new_client(self):
        h = UnixHost(
            ip="10.0.0.1",
            element="box",
            creds=[Cred(login="u", password="p")],
            term="telnet",
            log=LogMode.QUIET,
        )
        mock_client = self._mock_telnet_client()
        with (
            patch("otto.host.session.TelnetClient", return_value=mock_client),
            patch("otto.host.session.TelnetSession", return_value=self._mock_shell_session()),
        ):
            await h.open_session("monitor")
        mock_client.connect.assert_called_once()
        await h.close()

    @pytest.mark.asyncio
    async def test_telnet_session_owns_its_client(self):
        h = UnixHost(
            ip="10.0.0.1",
            element="box",
            creds=[Cred(login="u", password="p")],
            term="telnet",
            log=LogMode.QUIET,
        )
        mock_client = self._mock_telnet_client()
        with (
            patch("otto.host.session.TelnetClient", return_value=mock_client),
            patch(
                "otto.host.session.TelnetSession", return_value=self._mock_shell_session()
            ) as MockTelnetSession,  # noqa: N806 — CapWords for a class mock
        ):
            await h.open_session("monitor")
        MockTelnetSession.assert_called_once_with(
            mock_client.reader,
            mock_client.writer,
            _owned_client=mock_client,
            command_frame=None,
            init_timeout=None,
            write_chunk_size=mock_client.options.write_chunk_size,
            write_chunk_delay=mock_client.options.write_chunk_delay,
            shell_history=False,
        )
        await h.close()

    # --- Multiple SSH sessions ---

    @pytest.mark.asyncio
    async def test_multiple_ssh_sessions_are_distinct_objects(self, host: UnixHost):
        shell_a = self._mock_shell_session()
        shell_b = self._mock_shell_session()
        host._connections._ssh_conn = MagicMock()
        with patch("otto.host.session.SshSession", side_effect=[shell_a, shell_b]):
            session_a = await host.open_session("alpha")
            session_b = await host.open_session("beta")
        assert session_a is not session_b
        assert host._session_mgr._named_sessions["alpha"] is session_a
        assert host._session_mgr._named_sessions["beta"] is session_b

    @pytest.mark.asyncio
    async def test_multiple_ssh_sessions_both_alive(self, host: UnixHost):
        host._connections._ssh_conn = MagicMock()
        with patch(
            "otto.host.session.SshSession",
            side_effect=[
                self._mock_shell_session(),
                self._mock_shell_session(),
            ],
        ):
            s1 = await host.open_session("s1")
            s2 = await host.open_session("s2")
        assert s1.alive is True
        assert s2.alive is True

    # --- Multiple Telnet sessions ---

    @pytest.mark.asyncio
    async def test_multiple_telnet_sessions_each_create_own_client(self):
        h = UnixHost(
            ip="10.0.0.1",
            element="box",
            creds=[Cred(login="u", password="p")],
            term="telnet",
            log=LogMode.QUIET,
        )
        client_a = self._mock_telnet_client()
        client_b = self._mock_telnet_client()
        with (
            patch("otto.host.session.TelnetClient", side_effect=[client_a, client_b]),
            patch(
                "otto.host.session.TelnetSession",
                side_effect=[
                    self._mock_shell_session(),
                    self._mock_shell_session(),
                ],
            ),
        ):
            await h.open_session("alpha")
            await h.open_session("beta")
        client_a.connect.assert_called_once()
        client_b.connect.assert_called_once()
        await h.close()

    @pytest.mark.asyncio
    async def test_multiple_telnet_sessions_each_own_separate_client(self):
        h = UnixHost(
            ip="10.0.0.1",
            element="box",
            creds=[Cred(login="u", password="p")],
            term="telnet",
            log=LogMode.QUIET,
        )
        client_a = self._mock_telnet_client()
        client_b = self._mock_telnet_client()
        with (
            patch("otto.host.session.TelnetClient", side_effect=[client_a, client_b]),
            patch(
                "otto.host.session.TelnetSession",
                side_effect=[
                    self._mock_shell_session(),
                    self._mock_shell_session(),
                ],
            ) as MockTelnetSession,  # noqa: N806 — CapWords for a class mock
        ):
            await h.open_session("alpha")
            await h.open_session("beta")
        calls = MockTelnetSession.call_args_list
        assert calls[0].kwargs["_owned_client"] is client_a
        assert calls[1].kwargs["_owned_client"] is client_b
        await h.close()

    # --- Mix of SSH host and Telnet host ---

    @pytest.mark.asyncio
    async def test_ssh_host_and_telnet_host_each_hold_own_sessions(self):
        """An SSH host and a Telnet host can hold independent named sessions simultaneously."""
        ssh_host = UnixHost(
            ip="10.0.0.1",
            element="ssh-box",
            creds=[Cred(login="u", password="p")],
            term="ssh",
            log=LogMode.QUIET,
        )
        telnet_host = UnixHost(
            ip="10.0.0.2",
            element="tel-box",
            creds=[Cred(login="u", password="p")],
            term="telnet",
            log=LogMode.QUIET,
        )

        ssh_shell = self._mock_shell_session()
        telnet_shell = self._mock_shell_session()
        mock_client = self._mock_telnet_client()

        ssh_host._connections._ssh_conn = MagicMock()
        with patch("otto.host.session.SshSession", return_value=ssh_shell):
            ssh_session = await ssh_host.open_session("monitor")

        with (
            patch("otto.host.session.TelnetClient", return_value=mock_client),
            patch("otto.host.session.TelnetSession", return_value=telnet_shell),
        ):
            telnet_session = await telnet_host.open_session("monitor")

        assert isinstance(ssh_session, HostSession)
        assert isinstance(telnet_session, HostSession)
        assert ssh_session is not telnet_session
        assert ssh_host._session_mgr._named_sessions["monitor"] is ssh_session
        assert telnet_host._session_mgr._named_sessions["monitor"] is telnet_session
        await telnet_host.close()

    # --- Reuse and replacement ---

    @pytest.mark.asyncio
    async def test_reuse_live_session_returns_same_object(self, host: UnixHost):
        mock_shell = self._mock_shell_session(alive=True)
        host._connections._ssh_conn = MagicMock()
        with patch("otto.host.session.SshSession", return_value=mock_shell):
            first = await host.open_session("monitor")
            second = await host.open_session("monitor")
        assert first is second

    @pytest.mark.asyncio
    async def test_dead_session_is_replaced(self, host: UnixHost):
        shell_old = self._mock_shell_session(alive=True)
        shell_new = self._mock_shell_session(alive=True)
        host._connections._ssh_conn = MagicMock()
        with patch("otto.host.session.SshSession", side_effect=[shell_old, shell_new]):
            first = await host.open_session("monitor")
            first._session.alive = False
            second = await host.open_session("monitor")
        assert first is not second
        assert host._session_mgr._named_sessions["monitor"] is second

    # --- Error cases ---

    @pytest.mark.asyncio
    async def test_unknown_term_raises_value_error(self):
        h = UnixHost(
            ip="10.0.0.1", element="box", creds=[Cred(login="u", password="p")], log=LogMode.QUIET
        )
        h.term = "foobar"
        h._connections.term = "foobar"
        with pytest.raises(ValueError, match="foobar"):
            await h.open_session("monitor")


# ---------------------------------------------------------------------------
# HostSession proxy — delegation and lifecycle (unit)
# ---------------------------------------------------------------------------


class TestHostSessionProxy:
    """Unit tests for HostSession — argument forwarding, state, and cleanup."""

    def _make_remote_session(
        self,
        host: UnixHost,
        name: str = "monitor",
        alive: bool = True,
    ) -> tuple[HostSession, MagicMock]:
        ok = CommandResult(command="echo hi", value="hi", status=Status.Success, retcode=0)
        shell = MagicMock(spec=ShellSession)
        shell.alive = alive
        shell.run_cmd = AsyncMock(return_value=ok)
        shell.send = AsyncMock()
        shell.expect = AsyncMock(return_value="some output")
        shell.close = AsyncMock()
        remote = HostSession(
            name=name,
            session=shell,
            log_command=host._log_command,
            log_output=host._log_output,
            deregister=lambda n: host._session_mgr._named_sessions.pop(n, None),
        )
        host._session_mgr._named_sessions[name] = remote
        return remote, shell

    @pytest.mark.asyncio
    async def test_run_returns_command_status(self, host: UnixHost):
        session, _ = self._make_remote_session(host)
        result = (await session.run("echo hi")).only
        assert isinstance(result, CommandResult)
        assert result.status == Status.Success
        assert result.value == "hi"

    @pytest.mark.asyncio
    async def test_run_delegates_cmd_to_shell_session(self, host: UnixHost):
        session, shell = self._make_remote_session(host)
        # Explicit timeout: HostSession.run's default moved to
        # DEFAULT_COMMAND_TIMEOUT (30.0); this test only cares that the
        # session forwards whatever timeout it's given, so pin the old
        # value it used to receive implicitly.
        await session.run("ls /tmp", timeout=10.0)
        shell.run_cmd.assert_called_once()
        _, kwargs = shell.run_cmd.call_args
        assert kwargs["expects"] is None
        assert kwargs["timeout"] == 10.0
        assert callable(kwargs["on_output"])
        assert kwargs["redact"] is False

    @pytest.mark.asyncio
    async def test_run_forwards_expects(self, host: UnixHost):
        session, shell = self._make_remote_session(host)
        expects = [(r"Password:", "secret\n")]
        # Explicit timeout: see test_run_delegates_cmd_to_shell_session.
        await session.run("sudo ls", expects=expects, timeout=10.0)  # type: ignore[arg-type]
        shell.run_cmd.assert_called_once()
        _, kwargs = shell.run_cmd.call_args
        assert kwargs["expects"] == expects
        assert kwargs["timeout"] == 10.0
        assert callable(kwargs["on_output"])
        assert kwargs["redact"] is False

    @pytest.mark.asyncio
    async def test_run_forwards_timeout(self, host: UnixHost):
        session, shell = self._make_remote_session(host)
        await session.run("sleep 5", timeout=60.0)
        shell.run_cmd.assert_called_once()
        _, kwargs = shell.run_cmd.call_args
        assert kwargs["expects"] is None
        assert kwargs["timeout"] == 60.0
        assert callable(kwargs["on_output"])
        assert kwargs["redact"] is False

    @pytest.mark.asyncio
    async def test_send_delegates(self, host: UnixHost):
        session, shell = self._make_remote_session(host)
        await session.send("hello\n")
        shell.send.assert_called_once_with("hello\n")

    @pytest.mark.asyncio
    async def test_expect_delegates_and_returns_output(self, host: UnixHost):
        session, shell = self._make_remote_session(host)
        result = await session.expect(r"\$")
        shell.expect.assert_called_once_with(r"\$", DEFAULT_COMMAND_TIMEOUT)
        assert result == "some output"

    @pytest.mark.asyncio
    async def test_expect_forwards_timeout(self, host: UnixHost):
        session, shell = self._make_remote_session(host)
        await session.expect(r"\$", timeout=5.0)
        shell.expect.assert_called_once_with(r"\$", 5.0)

    def test_alive_true_when_session_alive(self, host: UnixHost):
        session, _ = self._make_remote_session(host, alive=True)
        assert session.alive is True

    def test_alive_false_when_session_dead(self, host: UnixHost):
        session, _ = self._make_remote_session(host, alive=False)
        assert session.alive is False

    @pytest.mark.asyncio
    async def test_close_calls_underlying_session_close(self, host: UnixHost):
        session, shell = self._make_remote_session(host)
        await session.close()
        shell.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_close_removes_from_host_registry(self, host: UnixHost):
        session, _ = self._make_remote_session(host, name="monitor")
        assert "monitor" in host._session_mgr._named_sessions
        await session.close()
        assert "monitor" not in host._session_mgr._named_sessions

    @pytest.mark.asyncio
    async def test_context_manager_closes_on_exit(self, host: UnixHost):
        session, shell = self._make_remote_session(host)
        async with session:
            shell.close.assert_not_called()
        shell.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_context_manager_removes_from_registry_on_exit(self, host: UnixHost):
        session, _ = self._make_remote_session(host, name="monitor")
        async with session:
            assert "monitor" in host._session_mgr._named_sessions
        assert "monitor" not in host._session_mgr._named_sessions

    @pytest.mark.asyncio
    async def test_context_manager_yields_self(self, host: UnixHost):
        session, _ = self._make_remote_session(host)
        async with session as ctx:
            assert ctx is session


# ---------------------------------------------------------------------------
# Host-level lifecycle with named sessions (unit)
# ---------------------------------------------------------------------------


class TestOpenSessionCleanup:
    """Unit tests for host.close() and _connected interactions with named sessions."""

    def _add_mock_session(self, host: UnixHost, name: str, alive: bool = True) -> MagicMock:
        """Register a HostSession backed by a mock ShellSession and return the shell mock."""
        shell = MagicMock(spec=ShellSession)
        shell.alive = alive
        shell.close = AsyncMock()
        host._session_mgr._named_sessions[name] = HostSession(
            name=name,
            session=shell,
            log_command=host._log_command,
            log_output=host._log_output,
            deregister=lambda n: host._session_mgr._named_sessions.pop(n, None),
        )
        return shell

    @pytest.mark.asyncio
    async def test_host_close_closes_all_named_sessions(self, host: UnixHost):
        shell_a = self._add_mock_session(host, "a")
        shell_b = self._add_mock_session(host, "b")
        await host.close()
        shell_a.close.assert_called_once()
        shell_b.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_host_close_clears_registry(self, host: UnixHost):
        self._add_mock_session(host, "monitor")
        await host.close()
        assert host._session_mgr._named_sessions == {}

    def test_connected_true_with_live_named_session(self, host: UnixHost):
        self._add_mock_session(host, "monitor", alive=True)
        assert host._connected is True

    def test_connected_false_when_named_session_dead(self, host: UnixHost):
        self._add_mock_session(host, "monitor", alive=False)
        assert host._connected is False

    def test_connected_false_with_no_sessions(self, host: UnixHost):
        assert host._connected is False

    def test_connected_true_with_multiple_sessions_one_alive(self, host: UnixHost):
        self._add_mock_session(host, "dead", alive=False)
        self._add_mock_session(host, "live", alive=True)
        assert host._connected is True


@pytest.mark.asyncio
async def test_host_current_user_reads_default_session():
    from unittest.mock import MagicMock

    from otto.host.session import ShellSession
    from otto.host.unix_host import UnixHost

    host = UnixHost(
        ip="10.0.0.1",
        element="box",
        creds=[Cred(login="admin", password="secret")],
        user="admin",
        log=LogMode.QUIET,
    )
    transport = MagicMock(spec=ShellSession)
    transport.current_user = "admin"
    host._session_mgr._session = transport
    assert host.current_user == "admin"


@pytest.mark.asyncio
async def test_unix_switch_user_updates_host_current_user():
    from unittest.mock import AsyncMock, MagicMock

    from otto.host.session import ShellSession
    from otto.host.unix_host import UnixHost

    host = UnixHost(
        ip="10.0.0.1",
        element="box",
        creds=[Cred(login="admin", password="secret"), Cred(login="root", password="rootpw")],
        user="admin",
        log=LogMode.QUIET,
    )
    transport = MagicMock(spec=ShellSession)
    transport.alive = True
    transport.send = AsyncMock()
    transport.expect = AsyncMock(return_value="Password:")
    transport.current_user = "admin"
    host._session_mgr._session = transport
    await host.switch_user("root")
    assert host.current_user == "root"


# ---------------------------------------------------------------------------
# Kernel modules
# ---------------------------------------------------------------------------


def _unix_host():
    from otto.host.unix_host import UnixHost

    return UnixHost(
        ip="10.0.0.1",
        element="box",
        creds=[Cred(login="admin", password="secret")],
        user="admin",
        log=LogMode.QUIET,
    )


@pytest.mark.asyncio
async def test_loaded_modules_parses_proc_modules_column_one():
    from unittest.mock import AsyncMock

    from otto.result import CommandResult
    from otto.utils import Status

    host = _unix_host()
    proc = "ext4 737280 2 - Live 0x0\nnvme 49152 3 nvme_core, Live 0x0\n"
    host.exec = AsyncMock(
        return_value=CommandResult(
            command="cat /proc/modules", value=proc, status=Status.Success, retcode=0
        )
    )
    mods = await host._loaded_modules()
    assert mods.is_ok
    assert mods.value == ["ext4", "nvme"]


@pytest.mark.asyncio
async def test_loaded_modules_empty_when_read_fails():
    from unittest.mock import AsyncMock

    from otto.result import CommandResult
    from otto.utils import Status

    host = _unix_host()
    host.exec = AsyncMock(
        return_value=CommandResult(
            command="cat /proc/modules", value="", status=Status.Error, retcode=1
        )
    )
    mods = await host._loaded_modules()
    assert not mods.is_ok
    assert mods.value == []


@pytest.mark.asyncio
async def test_lsmod_returns_loaded_module_names():
    from unittest.mock import AsyncMock

    host = _unix_host()
    host._loaded_modules = AsyncMock(return_value=Result(Status.Success, value=["ext4", "nvme"]))
    assert (await host.lsmod()).value == ["ext4", "nvme"]


def _run_result(cmd, output, status, retcode):
    from otto.result import CommandResult, Results

    return Results.collect(
        [CommandResult(command=cmd, value=output, status=status, retcode=retcode)]
    )


@pytest.mark.asyncio
async def test_load_stages_then_insmod_sudo_for_nonroot(tmp_path):
    from unittest.mock import AsyncMock, MagicMock

    from otto.utils import Status

    host = _unix_host()
    host._session_mgr = MagicMock()
    host._session_mgr.current_user = "admin"  # non-root
    ko = tmp_path / "my-mod.ko"
    ko.write_bytes(b"\x00")
    host.put = AsyncMock(return_value=Result(Status.Success, value={}))
    host.run = AsyncMock(return_value=_run_result("insmod /tmp/my-mod.ko", "", Status.Success, 0))
    host.rm = AsyncMock(return_value=Result(Status.Success))
    result = await host.load(ko)
    assert result.status is Status.Success
    assert result.msg == ""
    host.put.assert_awaited_once()
    assert host.run.await_args.args[0] == "insmod /tmp/my-mod.ko"
    assert host.run.await_args.kwargs["sudo"] is True
    host.rm.assert_awaited_once()  # staged file cleaned up


@pytest.mark.asyncio
async def test_load_no_sudo_when_current_user_root(tmp_path):
    from unittest.mock import AsyncMock, MagicMock

    from otto.utils import Status

    host = _unix_host()
    host._session_mgr = MagicMock()
    host._session_mgr.current_user = "root"
    ko = tmp_path / "m.ko"
    ko.write_bytes(b"\x00")
    host.put = AsyncMock(return_value=Result(Status.Success, value={}))
    host.run = AsyncMock(return_value=_run_result("insmod /tmp/m.ko", "", Status.Success, 0))
    host.rm = AsyncMock(return_value=Result(Status.Success))
    await host.load(ko)
    assert host.run.await_args.kwargs["sudo"] is False


@pytest.mark.asyncio
async def test_load_put_failure_short_circuits(tmp_path):
    from unittest.mock import AsyncMock, MagicMock

    from otto.utils import Status

    host = _unix_host()
    host._session_mgr = MagicMock()
    host._session_mgr.current_user = "admin"
    ko = tmp_path / "m.ko"
    ko.write_bytes(b"\x00")
    host.put = AsyncMock(return_value=Result(Status.Error, value={}, msg="scp failed"))
    host.run = AsyncMock()
    result = await host.load(ko)
    assert result.status is Status.Error
    assert "staging" in result.msg
    host.run.assert_not_awaited()


@pytest.mark.asyncio
async def test_load_error_message_uses_normalized_name(tmp_path):
    from unittest.mock import AsyncMock, MagicMock

    from otto.utils import Status

    host = _unix_host()
    host._session_mgr = MagicMock()
    host._session_mgr.current_user = "admin"
    ko = tmp_path / "foo-bar.ko"
    ko.write_bytes(b"\x00")
    host.put = AsyncMock(return_value=Result(Status.Success, value={}))
    host.run = AsyncMock(
        return_value=_run_result("insmod ...", "Invalid module format", Status.Error, 1)
    )
    host.rm = AsyncMock(return_value=Result(Status.Success))
    result = await host.load(ko)
    assert result.status is Status.Error
    assert "foo_bar" in result.msg
    assert "Invalid module format" in result.msg


@pytest.mark.asyncio
async def test_unload_idempotent_when_not_resident():
    from unittest.mock import AsyncMock

    from otto.utils import Status

    host = _unix_host()
    host._loaded_modules = AsyncMock(return_value=Result(Status.Success, value=["ext4"]))
    host.run = AsyncMock()
    result = await host.unload("my_mod")
    assert result.status is Status.Success
    assert result.msg == ""
    host.run.assert_not_awaited()  # not resident → no rmmod


@pytest.mark.asyncio
async def test_unload_rmmod_with_sudo_when_resident():
    from unittest.mock import AsyncMock, MagicMock

    from otto.utils import Status

    host = _unix_host()
    host._session_mgr = MagicMock()
    host._session_mgr.current_user = "admin"
    host._loaded_modules = AsyncMock(return_value=Result(Status.Success, value=["my_mod"]))
    host.run = AsyncMock(return_value=_run_result("rmmod my_mod", "", Status.Success, 0))
    result = await host.unload("my-mod")  # dash normalized to my_mod
    assert result.status is Status.Success
    assert host.run.await_args.args[0] == "rmmod my_mod"
    assert host.run.await_args.kwargs["sudo"] is True


@pytest.mark.asyncio
async def test_unload_error_maps_rmmod_failure():
    from unittest.mock import AsyncMock, MagicMock

    from otto.utils import Status

    host = _unix_host()
    host._session_mgr = MagicMock()
    host._session_mgr.current_user = "admin"
    host._loaded_modules = AsyncMock(return_value=Result(Status.Success, value=["my_mod"]))
    host.run = AsyncMock(
        return_value=_run_result("rmmod my_mod", "Module my_mod is in use", Status.Error, 1)
    )
    result = await host.unload("my_mod")
    assert result.status is Status.Error
    assert "in use" in result.msg


@pytest.mark.asyncio
async def test_lsmod_dry_run_declines_rather_than_reporting_no_modules():
    """Dry-run refuses to answer, instead of answering "nothing is loaded".

    It used to return ``Result(Status.Skipped, value=[])`` -- ok, and an empty
    module list, so a caller asking "is module X loaded?" was told **no** by a
    machine that was never contacted. See
    ``docs/superpowers/specs/2026-08-15-dry-run-contract-design.md`` section 4;
    the full guard (announcement, host naming, the genuine-read-failure
    control) is ``tests/unit/host/test_dry_run.py``.
    """
    from otto.result import CommandNotRunError
    from tests.conftest import active_context

    host = _unix_host()
    with active_context(dry_run=True):
        lsmod_result = await host.lsmod()
    assert lsmod_result.status is Status.NotRun
    assert lsmod_result.is_ok is False
    with pytest.raises(CommandNotRunError):
        _ = lsmod_result.value


@pytest.mark.asyncio
async def test_unload_dry_run_issues_rmmod_without_idempotency_check():
    """Under dry-run the idempotency check is skipped, so the would-be ``rmmod``
    is still the command that gets announced (symmetric with load's insmod).

    The mocked ``run`` returns the DECLINE the real primitive now produces, not
    the old ``Skipped``/``retcode 0`` synthetic -- a stale double here would
    keep this green against a product that had stopped declining at all.
    ``unload`` then reads that decline's value to build its failure message, so
    the contract fires; the point of the test is that ``_loaded_modules`` was
    never consulted, which the raise's own text proves.
    """
    from unittest.mock import AsyncMock, MagicMock

    from otto.result import CommandNotRunError, NotRunResult, Results
    from otto.utils import Status
    from tests.conftest import active_context

    host = _unix_host()
    host._session_mgr = MagicMock()
    host._session_mgr.current_user = "admin"
    host._loaded_modules = AsyncMock(
        return_value=Result(Status.Success, value=[])
    )  # would short-circuit if consulted
    declined = Results.collect(
        [NotRunResult(status=Status.NotRun, command="rmmod foo", retcode=-1, host_name=host.name)]
    )
    host.run = AsyncMock(return_value=declined)
    with active_context(dry_run=True), pytest.raises(CommandNotRunError) as exc:
        await host.unload("foo")
    host.run.assert_awaited_once()
    assert host.run.await_args.args[0] == "rmmod foo"
    host._loaded_modules.assert_not_awaited()  # idempotency check skipped in dry-run
    assert "rmmod foo" in str(exc.value)


class TestSshExecTimeout:
    """Regression: UnixHost.exec over SSH must honour its timeout.

    Before the fix the read loop was never wrapped in asyncio.wait_for, so the
    `except asyncio.TimeoutError` below it was dead code and this test hung.
    """

    @pytest.mark.asyncio
    async def test_ssh_exec_stalling_command_times_out(self):
        h = UnixHost(
            ip="10.0.0.1",
            element="stalled",
            creds=[Cred(login="u", password="p")],
            term="ssh",
            log=LogMode.QUIET,
        )

        class _StalledStdout:
            def __aiter__(self):
                return self

            async def __anext__(self):
                await asyncio.sleep(3600)  # never yields, never returns
                raise StopAsyncIteration

        class _StalledProcess:
            stdout = _StalledStdout()
            terminated = False

            def terminate(self):
                type(self).terminated = True

            async def wait(self):
                return SimpleNamespace(exit_status=-1)

        class _Conn:
            async def create_process(self, cmd, **kw):
                return _StalledProcess()

        h._session_mgr._connections = MagicMock()
        h._session_mgr._connections.term = "ssh"
        h._session_mgr._connections.proxy_hops = []
        h._session_mgr._connections.ssh = AsyncMock(return_value=_Conn())
        h._session_mgr._exec_factory = None

        result = await asyncio.wait_for(h.exec("sleep 3600", timeout=0.1), timeout=10.0)

        assert result.status == Status.Error
        assert result.timed_out is True
        assert "timed out" in result.value
        assert _StalledProcess.terminated is True


class TestSshExecKillEscalation:
    """Regression: when a remote command ignores terminate(), SSH exec must
    escalate to kill() rather than hang on the post-terminate reap.

    The fake process's ``wait()`` blocks on an ``asyncio.Event`` that only
    ``kill()`` sets; ``terminate()`` deliberately does nothing, mirroring a
    remote command that ignores its termination signal. If the escalation
    branch in ``SessionManager.exec`` were missing or broken, ``wait()``
    would never unblock and the outer ``asyncio.wait_for(..., timeout=5.0)``
    below would fail the test rather than let it hang the suite.
    """

    @pytest.mark.serial_timing
    @pytest.mark.asyncio
    async def test_ssh_exec_escalates_to_kill_when_terminate_is_ignored(self, monkeypatch):
        # Patched where the name is *used* -- session.py binds its own
        # module-local `_EXEC_REAP_TIMEOUT` via `from .host import
        # _EXEC_REAP_TIMEOUT`, so patching otto.host.host's copy would not
        # bite here.
        monkeypatch.setattr("otto.host.session._EXEC_REAP_TIMEOUT", 0.2)

        h = UnixHost(
            ip="10.0.0.1",
            element="stalled",
            creds=[Cred(login="u", password="p")],
            term="ssh",
            log=LogMode.QUIET,
        )

        class _StalledStdout:
            def __aiter__(self):
                return self

            async def __anext__(self):
                await asyncio.sleep(3600)  # never yields, never returns
                raise StopAsyncIteration

        class _IgnoresTerminateProcess:
            stdout = _StalledStdout()
            terminated = False
            killed = False

            def __init__(self):
                self._killed_event = asyncio.Event()

            def terminate(self):
                # Remote command ignores SIGTERM: recorded, but the wait()
                # below stays blocked until kill() is called.
                type(self).terminated = True

            def kill(self):
                type(self).killed = True
                self._killed_event.set()

            async def wait(self):
                await self._killed_event.wait()
                return SimpleNamespace(exit_status=-1)

        process = _IgnoresTerminateProcess()

        class _Conn:
            async def create_process(self, cmd, **kw):
                return process

        h._session_mgr._connections = MagicMock()
        h._session_mgr._connections.term = "ssh"
        h._session_mgr._connections.proxy_hops = []
        h._session_mgr._connections.ssh = AsyncMock(return_value=_Conn())
        h._session_mgr._exec_factory = None

        start = asyncio.get_running_loop().time()
        result = await asyncio.wait_for(h.exec("sleep 3600", timeout=0.1), timeout=5.0)
        elapsed = asyncio.get_running_loop().time() - start

        assert result.status == Status.Error
        assert result.timed_out is True
        assert _IgnoresTerminateProcess.terminated is True
        assert _IgnoresTerminateProcess.killed is True
        # Sanity check that the _EXEC_REAP_TIMEOUT monkeypatch actually took
        # effect: with the real 5.0s default this would take >5s per bounded
        # reap attempt and blow the outer 5.0s wait_for above instead of
        # completing. At the patched 0.2s it finishes in well under a second.
        assert elapsed < 1.0


# ---------------------------------------------------------------------------
# interactive() telnet teardown (G15: no awaited close in a bare finally)
# ---------------------------------------------------------------------------


class _FakeInteractiveTelnetClient:
    """Stands in for the dedicated interactive TelnetClient interactive() builds."""

    last: "_FakeInteractiveTelnetClient | None" = None
    connect_error: Exception | None = None
    close_error: Exception | None = None

    def __init__(self, **kwargs):
        self.close_attempted = False
        type(self).last = self

    async def connect(self, interactive: bool = False) -> None:
        if type(self).connect_error is not None:
            raise type(self).connect_error

    async def close(self) -> None:
        self.close_attempted = True
        if type(self).close_error is not None:
            raise type(self).close_error


@pytest.fixture
def telnet_host(monkeypatch) -> UnixHost:
    """Telnet-term host whose interactive client and login pump are test-local fakes."""
    _FakeInteractiveTelnetClient.last = None
    _FakeInteractiveTelnetClient.connect_error = None
    _FakeInteractiveTelnetClient.close_error = None
    monkeypatch.setattr("otto.host.unix_host.TelnetClient", _FakeInteractiveTelnetClient)
    monkeypatch.setattr("otto.host.unix_host.run_telnet_login", AsyncMock())
    return UnixHost(
        ip="10.0.0.1",
        element="box",
        creds=[Cred(login="user", password="pass")],
        term="telnet",
        log=LogMode.QUIET,
    )


class TestInteractiveTelnetTeardown:
    @pytest.mark.asyncio
    async def test_close_failure_does_not_mask_the_connect_error(self, telnet_host, caplog):
        """The login failure is the story; the dead client's close is a footnote."""
        _FakeInteractiveTelnetClient.connect_error = RuntimeError("telnet connect refused")
        _FakeInteractiveTelnetClient.close_error = OSError("close blew up")

        with (
            caplog.at_level("WARNING", logger="otto.host.connections"),
            pytest.raises(RuntimeError, match="telnet connect refused"),
        ):
            await telnet_host._login()

        client = _FakeInteractiveTelnetClient.last
        assert client is not None
        assert client.close_attempted
        assert any(
            "interactive telnet client close teardown failed" in r.message for r in caplog.records
        )

    @pytest.mark.asyncio
    async def test_close_failure_after_a_clean_session_is_warned_not_raised(
        self, telnet_host, caplog
    ):
        """A session the user finished normally must not fail on teardown noise."""
        _FakeInteractiveTelnetClient.close_error = OSError("close blew up")

        with caplog.at_level("WARNING", logger="otto.host.connections"):
            await telnet_host._login()

        assert any(
            "interactive telnet client close teardown failed" in r.message for r in caplog.records
        )

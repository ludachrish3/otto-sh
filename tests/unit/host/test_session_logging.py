"""
Diagnostic logging on the host connect/framing path (Phase 5.6).

These tests assert that DEBUG-level logging on the ``otto.host.*`` loggers
surfaces the buffer regions a new-embedded-OS bring-up would need to see:
the handshake command sent and matched, the framed command written for each
run_cmd, the begin-marker match, and the per-command summary (retcode +
output length + buffer). The instrumentation lives at the framing-seam call
sites in the base ``ShellSession``, so subclasses inherit visibility for
free; the tests drive the base class through the same ``MockSession`` used
by ``test_session.py``.

No env var, no custom filter — just standard Python logging at DEBUG.
"""

import asyncio
import logging
import re

import pytest
import pytest_asyncio

from otto.host.session import ShellSession


class MockSession(ShellSession):
    """In-memory ShellSession (mirrors the double in test_session.py)."""

    def __init__(self) -> None:
        super().__init__()
        self._out_reader: asyncio.StreamReader | None = None
        self.written: list[str] = []

    async def _open(self) -> None:
        self._out_reader = asyncio.StreamReader()

    async def _write(self, data: str) -> None:
        self.written.append(data)

    async def _read_until_pattern(self, pattern: re.Pattern[str]) -> str:
        assert self._out_reader is not None
        buf = ""
        while True:
            chunk = await self._out_reader.read(1)
            if not chunk:
                raise asyncio.IncompleteReadError(buf.encode(), None)
            buf += chunk.decode()
            if pattern.search(buf):
                return buf

    async def close(self) -> None:
        self._alive = False
        self._initialized = False

    def feed(self, data: str) -> None:
        assert self._out_reader is not None
        self._out_reader.feed_data(data.encode())


@pytest_asyncio.fixture
async def initialized_session(caplog: pytest.LogCaptureFixture) -> MockSession:
    """A MockSession past the readiness handshake, with caplog active."""
    caplog.set_level(logging.DEBUG, logger="otto")
    s = MockSession()
    await s._open()

    async def handshake():
        await s._ensure_initialized()

    task = asyncio.create_task(handshake())
    await asyncio.sleep(0.01)
    s.feed(s._ready_marker + "\n")
    await task
    return s


def _messages(caplog: pytest.LogCaptureFixture) -> str:
    """All captured log messages joined, for substring assertions."""
    return "\n".join(r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Handshake logging
# ---------------------------------------------------------------------------


class TestHandshakeLogging:
    @pytest.mark.asyncio
    async def test_handshake_start_and_match_logged_at_debug(
        self,
        caplog: pytest.LogCaptureFixture,
    ):
        """A successful handshake logs the start (cmd + marker + timeout)
        and the match.

        The handshake is driven by the shared ``confirm_live`` resend loop
        (see ``otto.host.shell_liveness``), which owns per-probe retry
        bookkeeping internally and doesn't expose an attempt count or the
        matched bytes to its caller — so, unlike the bespoke loop it
        replaced, the match log here is a plain confirmation rather than a
        timing/attempts/bytes breakdown.
        """
        caplog.set_level(logging.DEBUG, logger="otto")
        s = MockSession()
        await s._open()

        task = asyncio.create_task(s._ensure_initialized())
        await asyncio.sleep(0.01)
        s.feed(s._ready_marker + "\n")
        await task

        log = _messages(caplog)
        assert "handshake start" in log
        assert s._ready_marker in log  # marker echoed in the log
        assert "stty -echo" in log  # the bash handshake command
        assert "handshake matched" in log

    @pytest.mark.asyncio
    async def test_handshake_failure_logs_and_raises(
        self,
        caplog: pytest.LogCaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """A handshake that never sees the marker logs the FAILED line,
        then raises ConnectionError.

        ``confirm_live`` (see ``otto.host.shell_liveness``) swallows each
        per-probe timeout and resends silently rather than logging it, so
        the FAILED line from ``_fail_init`` is the observable failure
        signal here — not a per-probe timeout log.
        """
        caplog.set_level(logging.DEBUG, logger="otto")
        s = MockSession()
        # Shrink the timeout so the test doesn't sit on the default 3s.
        monkeypatch.setattr(s, "_init_timeout", 0.05)
        monkeypatch.setattr(s, "_init_probe_interval", 0.02)
        await s._open()

        with pytest.raises(ConnectionError):
            await s._ensure_initialized()

        log = _messages(caplog)
        assert "handshake start" in log
        assert "handshake FAILED" in log
        assert "marking session dead" in log


# ---------------------------------------------------------------------------
# run_cmd framing-seam logging
# ---------------------------------------------------------------------------


class TestRunCmdLogging:
    @pytest.mark.asyncio
    async def test_framed_write_and_summary_logged_at_debug(
        self,
        initialized_session: MockSession,
        caplog: pytest.LogCaptureFixture,
    ):
        """run_cmd logs the framed payload at write time and a per-command
        summary (cmd + retcode + output length + buffer preview) at the end."""
        s = initialized_session
        # Reassert the level — `caplog.clear()` resets it back to WARNING,
        # which would silently drop the run_cmd logs we want to capture.
        caplog.clear()
        caplog.set_level(logging.DEBUG, logger="otto")

        async def simulate():
            await asyncio.sleep(0.01)
            s.feed(f"{s._begin_marker}\nhello\n{s._end_marker_prefix}0__\n")

        feed_task = asyncio.create_task(simulate())
        result = await s.run_cmd("echo hello")
        await feed_task

        assert result.retcode == 0
        log = _messages(caplog)
        assert "framed write" in log
        assert "cmd='echo hello'" in log
        assert "begin marker matched" in log
        assert "run_cmd done" in log
        assert "retcode=0" in log
        # The buffer the parser saw is logged for forensic value.
        assert s._begin_marker in log
        assert s._end_marker_prefix in log

    @pytest.mark.asyncio
    async def test_never_redacts_session_diagnostics(
        self,
        initialized_session: MockSession,
        caplog: pytest.LogCaptureFixture,
    ):
        """When redact=True, the framed-write, begin-marker, and buffer-preview
        diagnostics must NOT emit the raw command text — they must emit
        ``<redacted`` placeholders instead.  The secret must never appear in the
        captured log even at DEBUG level."""
        s = initialized_session
        caplog.clear()
        caplog.set_level(logging.DEBUG, logger="otto")

        async def simulate():
            await asyncio.sleep(0.01)
            s.feed(f"{s._begin_marker}\nok\n{s._end_marker_prefix}0__\n")

        feed_task = asyncio.create_task(simulate())
        result = await s.run_cmd("SECRETPW", redact=True)
        await feed_task

        assert result.retcode == 0
        blob = "\n".join(r.getMessage() for r in caplog.records)
        assert "SECRETPW" not in blob, "secret command leaked into DEBUG log"
        assert "framed write cmd=<redacted>" in blob, "framed-write placeholder missing"
        assert "run_cmd done cmd=<redacted>" in blob, "run_cmd-done placeholder missing"
        assert "begin marker matched" not in blob, (
            "begin-marker diagnostic must be suppressed when redacting"
        )


# ---------------------------------------------------------------------------
# recover_session logging
# ---------------------------------------------------------------------------


class TestRecoverSessionLogging:
    @pytest.mark.asyncio
    async def test_recover_session_entry_and_outcome_logged(
        self,
        initialized_session: MockSession,
        caplog: pytest.LogCaptureFixture,
    ):
        """recover_session logs its marker + recover command on entry and the
        partial-output length on success."""
        s = initialized_session
        caplog.clear()
        caplog.set_level(logging.DEBUG, logger="otto")

        async def simulate():
            await asyncio.sleep(0.01)
            # RECOVER-marker digit form (the echo-proof recover probe's real reply
            # — see BashFrame.recover/recover_pattern), not the bare recover token.
            s.feed(f"interrupted stuff\n{s._recover_marker}0__\n")

        feed_task = asyncio.create_task(simulate())
        partial = await s._recover_session()
        await feed_task

        assert partial.endswith("interrupted stuff")
        log = _messages(caplog)
        assert "recover_session entry" in log
        assert s._recover_marker in log
        assert "recover_session ok" in log


# ---------------------------------------------------------------------------
# Log-tag stability
# ---------------------------------------------------------------------------


class TestLogTag:
    def test_log_tag_includes_class_and_session_id(self):
        """``_log_tag`` is ``<class>@<session_id>`` — both pieces are
        load-bearing for telling concurrent sessions apart in a single log."""
        s = MockSession()
        tag = s._log_tag
        assert tag.startswith("MockSession@")
        assert s._session_id in tag


# ---------------------------------------------------------------------------
# Per-command log suppression (log=LogMode.QUIET)
# ---------------------------------------------------------------------------

from types import SimpleNamespace
from typing import cast

from otto.host.connections import ConnectionManager
from otto.host.session import SessionManager
from otto.result import CommandResult
from otto.utils import Status


class _AliveStubSession(ShellSession):
    """A session that's already 'initialized' and echoes one output line
    through whichever sink run_cmd is given. No real transport/handshake."""

    async def _open(self) -> None: ...
    async def _write(self, data: str) -> None: ...
    async def _read_until_pattern(self, pattern):  # pragma: no cover - unused
        raise AssertionError("stub does not read")

    async def close(self) -> None:
        self._alive = False
        self._initialized = False

    async def _ensure_initialized(self) -> None:
        self._initialized = True
        self._alive = True

    async def run_cmd(
        self, cmd, expects=None, timeout=None, on_output=None, redact=False, write_progress=None
    ):
        sink = on_output if on_output is not None else self._on_output
        sink("OUT")
        return CommandResult(status=Status.Success, value="OUT", command=cmd, retcode=0)


def _logging_mgr():
    cmds: list[str] = []
    outs: list[str] = []
    mgr = SessionManager(
        connections=cast("ConnectionManager", SimpleNamespace(term="telnet")),
        session_factory=_AliveStubSession,
        log_command=lambda cmd, _mode: cmds.append(cmd),
        log_output=lambda out, _mode: outs.append(out),
    )
    return mgr, cmds, outs


class TestPerCommandLogSuppression:
    @pytest.mark.asyncio
    async def test_log_true_records_command_and_output(self):
        mgr, cmds, outs = _logging_mgr()
        result = await mgr.run_cmd("echo hi", log=LogMode.NORMAL)
        assert result.value == "OUT"
        assert cmds == ["echo hi"]
        assert outs == ["OUT"]

    @pytest.mark.asyncio
    async def test_never_suppresses_command_and_output(self):
        mgr, cmds, outs = _logging_mgr()
        result = await mgr.run_cmd("llext load_hex foo DEADBEEF", log=LogMode.NEVER)
        # Output still returned to the caller — only logging is suppressed.
        assert result.value == "OUT"
        assert cmds == []
        assert outs == []

    @pytest.mark.asyncio
    async def test_never_flag_does_not_leak_between_calls(self):
        # The argument-passed sink means a NEVER command leaves no lingering
        # suppression — the next NORMAL command logs normally. (A host.log
        # mutation that forgot to restore would suppress "b" too.) This is the
        # property the concurrent-netcat-shells requirement depends on.
        mgr, cmds, outs = _logging_mgr()
        await mgr.run_cmd("a", log=LogMode.NEVER)
        await mgr.run_cmd("b", log=LogMode.NORMAL)
        assert cmds == ["b"]
        assert outs == ["OUT"]


# ---------------------------------------------------------------------------
# Console-suppress filter (Task 2)
# ---------------------------------------------------------------------------

from otto.host.host import HostFilter
from otto.logger.mode import LogMode


def _record(mode):
    rec = logging.LogRecord("otto", logging.INFO, __file__, 0, "msg", None, None)
    rec.host = object()
    rec.log_mode = mode
    return rec


def test_console_suppress_filter_drops_quiet_and_never():
    f = HostFilter()
    assert f.filter(_record(LogMode.NORMAL)) is True
    assert f.filter(_record(LogMode.QUIET)) is False
    assert f.filter(_record(LogMode.NEVER)) is False


def test_console_suppress_filter_passes_non_command_records():
    f = HostFilter()
    rec = logging.LogRecord("otto", logging.WARNING, __file__, 0, "boom", None, None)
    assert f.filter(rec) is True  # no host tag → always passes (warnings/errors)


# ---------------------------------------------------------------------------
# Host + command mode composition (Task 4)
# ---------------------------------------------------------------------------

from otto.host.login_proxy import Cred
from otto.host.unix_host import UnixHost
from otto.logger.mode import effective_mode


def _make_unix_host(log=LogMode.NORMAL) -> UnixHost:
    return UnixHost(ip="10.0.0.1", element="box", creds=[Cred(login="u", password="p")], log=log)


def test_effective_log_composes_host_and_command():
    host = _make_unix_host()
    host.log = LogMode.QUIET
    # A NORMAL command on a QUIET host runs QUIET; a NEVER command stays NEVER.
    assert host._effective_log(LogMode.NORMAL) is LogMode.QUIET
    assert host._effective_log(LogMode.NEVER) is LogMode.NEVER
    # The composition is the most-restrictive of the two modes.
    assert effective_mode(LogMode.QUIET, LogMode.NORMAL) is LogMode.QUIET

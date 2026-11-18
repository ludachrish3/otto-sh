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
from pathlib import Path

import pytest
import pytest_asyncio

from otto.host.element import Element
from otto.host.session import ShellSession
from tests.unit.host._session_feed import FeedAfterWriteMixin, identity_handshake_reply
from tests.unit.host._sessions import host_session, proxy_io, session_manager


class MockSession(FeedAfterWriteMixin, ShellSession):
    """In-memory ShellSession (mirrors the double in test_session.py)."""

    def __init__(self) -> None:
        super().__init__()
        self._out_reader: asyncio.StreamReader | None = None
        self.written: list[str] = []

    async def open_transport(self) -> None:
        self._out_reader = asyncio.StreamReader()

    async def write_transport(self, data: str) -> None:
        self.written.append(data)

    async def _take_unread(self) -> str:
        return ""

    async def read_transport_until(self, pattern: re.Pattern[str]) -> str:
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
    await s.open_transport()

    async def handshake():
        await s._ensure_initialized()

    feed_task = asyncio.create_task(s.feed_after_write(identity_handshake_reply(s._ready_marker)))
    task = asyncio.create_task(handshake())
    await task
    await feed_task
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
        await s.open_transport()

        feed_task = asyncio.create_task(
            s.feed_after_write(identity_handshake_reply(s._ready_marker))
        )
        task = asyncio.create_task(s._ensure_initialized())
        await task
        await feed_task

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
        await s.open_transport()

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

        feed_task = asyncio.create_task(
            s.feed_after_write(f"{s._begin_marker}\nhello\n{s._end_marker_prefix}0__\n")
        )
        result = await s.run_cmd(cmd="echo hello")
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
    async def test_the_transport_debug_lines_relay_command_and_output_verbatim(
        self,
        initialized_session: MockSession,
        caplog: pytest.LogCaptureFixture,
    ):
        """The command and the device's bytes reach every sink as written (#697).

        The record does not opt in to Rich markup, so the console and both
        log files show its message as written.
        """
        from tests._fixtures.log_text import shown

        s = initialized_session
        caplog.clear()
        caplog.set_level(logging.DEBUG, logger="otto")

        feed_task = asyncio.create_task(
            s.feed_after_write(
                f"{s._begin_marker}\nimage:[sha256:abc] [/x]\n{s._end_marker_prefix}0__\n"
            )
        )
        result = await s.run_cmd(cmd="echo '[red]x[/red]'")
        await feed_task

        assert result.retcode == 0
        rendered = "\n".join(shown(r) for r in caplog.records)
        assert "framed write cmd=\"echo '[red]x[/red]'\"" in rendered, rendered
        assert "run_cmd done cmd=\"echo '[red]x[/red]'\"" in rendered, rendered
        assert "image:[sha256:abc] [/x]" in rendered, rendered

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

        feed_task = asyncio.create_task(
            s.feed_after_write(f"{s._begin_marker}\nok\n{s._end_marker_prefix}0__\n")
        )
        result = await s.run_cmd(cmd="SECRETPW", redact=True)
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
        length of the recovery traffic it consumed on success."""
        s = initialized_session
        caplog.clear()
        caplog.set_level(logging.DEBUG, logger="otto")

        # Ordered on recovery's own Ctrl+C entry write. RECOVER-marker digit
        # form (the echo-proof recover probe's real reply — see
        # BashFrame.recover/recover_pattern), not the bare recover token.
        feed_task = asyncio.create_task(
            s.feed_after_write(f"interrupted stuff\n{s._recover_marker}0__\n")
        )
        await s._recover_session()
        await feed_task

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

from collections.abc import Callable
from types import SimpleNamespace
from typing import cast

from otto.host.connections import ConnectionManager
from otto.host.session import HostSession, SessionManager, _SessionProxyIO
from otto.result import CommandResult
from otto.utils import Status


class _AliveStubSession(ShellSession):
    """A session that's already 'initialized' and echoes one output line
    through whichever sink run_cmd is given. No real transport/handshake."""

    async def open_transport(self) -> None: ...
    async def write_transport(self, data: str) -> None: ...
    async def _take_unread(self) -> str:
        return ""

    async def read_transport_until(self, pattern):  # pragma: no cover - unused
        raise AssertionError("stub does not read")

    async def close(self) -> None:
        self._alive = False
        self._initialized = False

    async def _ensure_initialized(self) -> None:
        self._initialized = True
        self._alive = True

    async def run_cmd(
        self,
        cmd,
        expects=None,
        timeout=None,
        on_output=None,
        redact=False,
        write_progress=None,
        exec_input=None,
    ):
        sink = on_output if on_output is not None else self._on_output
        sink("OUT")
        return CommandResult(status=Status.Success, value="OUT", command=cmd, retcode=0)


def _logging_mgr():
    cmds: list[str] = []
    outs: list[str] = []
    mgr = session_manager(
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
        result = await mgr.run_cmd(cmd="echo hi", log=LogMode.NORMAL)
        assert result.value == "OUT"
        assert cmds == ["echo hi"]
        assert outs == ["OUT"]

    @pytest.mark.asyncio
    async def test_never_suppresses_command_and_output(self):
        mgr, cmds, outs = _logging_mgr()
        result = await mgr.run_cmd(cmd="llext load_hex foo DEADBEEF", log=LogMode.NEVER)
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
        await mgr.run_cmd(cmd="a", log=LogMode.NEVER)
        await mgr.run_cmd(cmd="b", log=LogMode.NORMAL)
        assert cmds == ["b"]
        assert outs == ["OUT"]


# ---------------------------------------------------------------------------
# Console-suppress filter (Task 2)
# ---------------------------------------------------------------------------

from otto.host.host import _HostFilter
from otto.logger.mode import LogMode


def _record(mode):
    rec = logging.LogRecord("otto", logging.INFO, __file__, 0, "msg", None, None)
    rec.host = object()
    rec.log_mode = mode
    return rec


def test_console_suppress_filter_drops_quiet_and_never():
    f = _HostFilter()
    assert f.filter(_record(LogMode.NORMAL)) is True
    assert f.filter(_record(LogMode.QUIET)) is False
    assert f.filter(_record(LogMode.NEVER)) is False


def test_console_suppress_filter_passes_non_command_records():
    f = _HostFilter()
    rec = logging.LogRecord("otto", logging.WARNING, __file__, 0, "boom", None, None)
    assert f.filter(rec) is True  # no host tag → always passes (warnings/errors)


# ---------------------------------------------------------------------------
# Host + command mode composition (Task 4)
# ---------------------------------------------------------------------------

from otto.host.login_proxy import Cred
from otto.host.unix_host import UnixHost
from otto.logger.mode import effective_mode


def _make_unix_host(log=LogMode.NORMAL) -> UnixHost:
    return UnixHost(
        ip="10.0.0.1", element=Element(name="box"), creds=[Cred(login="u", password="p")], log=log
    )


def test_effective_log_composes_host_and_command():
    host = _make_unix_host()
    host.log = LogMode.QUIET
    # A NORMAL command on a QUIET host runs QUIET; a NEVER command stays NEVER.
    assert host._effective_log(LogMode.NORMAL) is LogMode.QUIET
    assert host._effective_log(LogMode.NEVER) is LogMode.NEVER
    # The composition is the most-restrictive of the two modes.
    assert effective_mode(LogMode.QUIET, LogMode.NORMAL) is LogMode.QUIET


# ---------------------------------------------------------------------------
# The host log receives the lines of a command's value
# ---------------------------------------------------------------------------

import codecs

from pydantic import SecretStr

from otto.host.host import CredentialExpect
from otto.host.terminal_mapping import IDENTITY, ONLCR, UNMEASURED
from otto.host.terminal_mapping import Measurement as _Measurement
from otto.logger.relay import log_lines
from otto.testing import EXACT_OUTPUT_CASES, OutputCase
from otto.utils import wait_for_async

_MAPPINGS = {
    "onlcr": _Measurement(ONLCR, calibrated=True),
    "unknown": _Measurement("unknown('X\\nY')", calibrated=True),
}


class _ScriptedSession(MockSession):
    """A MockSession that reads in whole chunks, and says when it waits for more."""

    def __init__(self) -> None:
        super().__init__()
        self._unread = ""
        self._decoder = codecs.getincrementaldecoder("utf-8")()
        self.starved = asyncio.Event()

    async def read_transport_until(self, pattern: re.Pattern[str]) -> str:
        assert self._out_reader is not None
        while (match := pattern.search(self._unread)) is None:
            self.starved.set()
            chunk = await self._out_reader.read(65536)
            if not chunk:
                raise asyncio.IncompleteReadError(self._unread.encode(), None)
            self._unread += self._decoder.decode(chunk)
        self.starved.clear()
        data, self._unread = self._unread[: match.end()], self._unread[match.end() :]
        return data


@pytest_asyncio.fixture
async def scripted_session() -> _ScriptedSession:
    s = _ScriptedSession()
    await s.open_transport()
    feed_task = asyncio.create_task(s.feed_after_write(identity_handshake_reply(s._ready_marker)))
    await s._ensure_initialized()
    await feed_task
    return s


def _pieces(text: str, split: str) -> list[str]:
    """*text* as the reads deliver it: whole, one line per read, or every line cut in two."""
    if split == "whole":
        return [text]
    lines = text.splitlines(keepends=True)
    if split == "line":
        return lines
    return [half for line in lines for half in (line[: len(line) // 2], line[len(line) // 2 :])]


def _transcript(s: ShellSession, case: OutputCase) -> str:
    """What a terminal that adds a CR before every newline sends back for *case*."""
    body = f"{s._begin_marker}\n{case.text}{s._end_marker_prefix}{case.retcode}__\n"
    return body.replace("\n", "\r\n")


@pytest.mark.asyncio
@pytest.mark.parametrize("split", ["whole", "line", "mid-line"])
@pytest.mark.parametrize("mapping", sorted(_MAPPINGS))
@pytest.mark.parametrize("case", EXACT_OUTPUT_CASES, ids=lambda c: c.id)
async def test_a_shell_command_logs_the_lines_of_its_value(
    scripted_session: _ScriptedSession, case: OutputCase, mapping: str, split: str
):
    s = scripted_session
    s._terminal = _MAPPINGS[mapping]
    logged: list[str] = []
    feed_task = asyncio.create_task(s.feed_after_write(*_pieces(_transcript(s, case), split)))
    result = await s.run_cmd(cmd=case.command, on_output=logged.append)
    await feed_task
    assert result.retcode == case.retcode
    assert logged == log_lines(result.value), f"value={result.value!r}"


async def _logged_after(s: _ScriptedSession, text: str, event: asyncio.Event) -> None:
    """Feed *text*, then wait until *event* is set and the session waits for more."""
    s.starved.clear()
    s.feed(text)
    await asyncio.wait_for(event.wait(), 5)
    await asyncio.wait_for(s.starved.wait(), 5)


@pytest.mark.asyncio
async def test_under_onlcr_a_line_is_logged_before_the_next_one_arrives(
    scripted_session: _ScriptedSession,
):
    s = scripted_session
    s._terminal = _MAPPINGS["onlcr"]
    logged: list[str] = []
    first = asyncio.Event()

    def sink(line: str) -> None:
        logged.append(line)
        first.set()

    run = asyncio.create_task(s.run_cmd(cmd="two lines", on_output=sink))
    await wait_for_async(
        lambda: len(s.written) > 0, 5, interval=0.005, on_timeout="the command was never written"
    )
    await _logged_after(s, f"{s._begin_marker}\r\none\r\n", first)
    assert logged == ["one"]
    s.feed(f"two\r\n{s._end_marker_prefix}0__\r\n")
    result = await run
    assert logged == log_lines(result.value) == ["one", "two"]


@pytest.mark.asyncio
async def test_under_an_unknown_mapping_a_line_is_logged_once_the_next_one_arrives(
    scripted_session: _ScriptedSession,
):
    s = scripted_session
    s._terminal = _MAPPINGS["unknown"]
    logged: list[str] = []
    first = asyncio.Event()

    def sink(line: str) -> None:
        logged.append(line)
        first.set()

    run = asyncio.create_task(s.run_cmd(cmd="two lines", on_output=sink))
    await wait_for_async(
        lambda: len(s.written) > 0, 5, interval=0.005, on_timeout="the command was never written"
    )
    s.starved.clear()
    s.feed(f"{s._begin_marker}\r\none\r\n")
    await asyncio.wait_for(s.starved.wait(), 5)
    assert logged == []
    await _logged_after(s, "two\r\n", first)
    assert logged == ["one\r"]
    s.feed(f"{s._end_marker_prefix}0__\r\n")
    result = await run
    assert logged == log_lines(result.value) == ["one\r", "two"]


@pytest.mark.asyncio
async def test_a_prompt_answered_by_an_expect_is_logged_inside_its_line(
    scripted_session: _ScriptedSession,
):
    s = scripted_session
    s._terminal = _MAPPINGS["onlcr"]
    logged: list[str] = []
    feed_task = asyncio.create_task(
        s.feed_after_write(
            f"{s._begin_marker}\r\nContinue? y\r\nok\r\n{s._end_marker_prefix}0__\r\n"
        )
    )
    result = await s.run_cmd(cmd="ask", expects=[(r"Continue\? ", "y\n")], on_output=logged.append)
    await feed_task
    assert result.value == "Continue? y\nok"
    assert logged == ["Continue? y", "ok"]


@pytest.mark.asyncio
async def test_a_line_answered_with_a_credential_logs_what_it_always_has(
    scripted_session: _ScriptedSession,
):
    s = scripted_session
    s._terminal = _MAPPINGS["onlcr"]
    logged: list[str] = []
    cred = CredentialExpect(pattern=r"Password: ", response=SecretStr("pw"), user="u", host="h")
    feed_task = asyncio.create_task(
        s.feed_after_write(
            f"{s._begin_marker}\r\nPassword: \r\nok\r\n{s._end_marker_prefix}0__\r\n"
        )
    )
    await s.run_cmd(cmd="su u", expects=[cred], on_output=logged.append)
    await feed_task
    assert logged == ["ok"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "pattern"),
    [
        pytest.param("out\r\ndone", r"done.*\n", id="match-spans-the-end-marker"),
        pytest.param("out\r\ndone", r"out\r?\ndone.*\n", id="read-holds-a-line-the-end-and-more"),
    ],
)
async def test_nothing_at_or_after_the_end_marker_is_logged(
    scripted_session: _ScriptedSession, body: str, pattern: str
):
    """A read an expect match carries past the END record logs only what precedes it."""
    s = scripted_session
    s._terminal = _MAPPINGS["onlcr"]
    logged: list[str] = []
    transcript = f"{s._begin_marker}\r\n{body}{s._end_marker_prefix}0__\r\n"
    feed_task = asyncio.create_task(s.feed_after_write(transcript))
    result = await s.run_cmd(cmd="printf", expects=[(pattern, "")], on_output=logged.append)
    await feed_task
    assert result.value == "out\ndone"
    assert logged == ["out", "done"]


_PASSWORD = CredentialExpect(pattern=r"Password: ", response=SecretStr("pw"), user="u", host="h")
_TOKEN = CredentialExpect(pattern=r"Token: ", response=SecretStr("tk"), user="u", host="h")
_SPLIT = CredentialExpect(pattern=r"Pass\r?\nword: ", response=SecretStr("pw"), user="u", host="h")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "creds", "logged_before_this_branch"),
    [
        pytest.param("Password: \nok\n", [_PASSWORD], ["ok"], id="prompt-then-newline"),
        pytest.param("Password: ok\nend\n", [_PASSWORD], ["ok", "end"], id="text-after-prompt"),
        pytest.param(
            "first\nPass\nword: \nok\n", [_SPLIT], ["first", "ok"], id="read-spans-newline"
        ),
        pytest.param("ok\nPassword: ", [_PASSWORD], ["ok"], id="prompt-on-end-line"),
        pytest.param("ok\nPassword: done", [_PASSWORD], ["ok", "done"], id="text-on-end-line"),
        pytest.param(
            "Password: \nToken: \nok\n", [_PASSWORD, _TOKEN], ["ok"], id="two-credential-prompts"
        ),
    ],
)
async def test_a_credential_exchange_logs_what_it_logged_before(
    scripted_session: _ScriptedSession,
    body: str,
    creds: "list[CredentialExpect]",
    logged_before_this_branch: list[str],
):
    """The matched read is not logged; text after it on the same line is (main's behaviour)."""
    s = scripted_session
    s._terminal = _MAPPINGS["onlcr"]
    logged: list[str] = []
    transcript = f"{s._begin_marker}\n{body}{s._end_marker_prefix}0__\n".replace("\n", "\r\n")
    feed_task = asyncio.create_task(s.feed_after_write(transcript))
    await s.run_cmd(cmd="su u", expects=list(creds), on_output=logged.append)
    await feed_task
    assert logged == logged_before_this_branch


class _FlushError(Exception):
    """The host log's sink failed while a held line was being logged."""


def _failing_sink(line: str) -> None:
    raise _FlushError(line)


class _WarningSinkError(Exception):
    """The handler the flush failure's WARNING went to failed as well."""


class _FailingWarnings(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        raise _WarningSinkError(record.getMessage())


@pytest.fixture(params=["warning-logged", "warning-fails"])
def warning_sink(request: pytest.FixtureRequest):
    """The session logger's WARNING path, working or broken as the sink is.

    A broken handler fails both the output line and the report of that
    failure; neither may replace the exception on its way out.
    """
    if request.param == "warning-logged":
        yield
        return
    session_logger = logging.getLogger("otto.host.session")
    handler = _FailingWarnings(level=logging.WARNING)
    session_logger.addHandler(handler)
    try:
        yield
    finally:
        session_logger.removeHandler(handler)


@pytest.mark.asyncio
@pytest.mark.usefixtures("warning_sink")
@pytest.mark.parametrize("how", ["timeout", "cancel"])
async def test_a_sink_failing_on_the_held_line_never_replaces_a_shell_command_s_stop(
    scripted_session: _ScriptedSession, monkeypatch: pytest.MonkeyPatch, how: str
):
    # On Python 3.10 an outer cancel of `asyncio.wait_for` masks whatever the
    # inner task raised, so the cancel row cannot be shown red there; the
    # timeout rows can, and the row is kept for the versions that surface it.
    s = scripted_session
    s._terminal = _MAPPINGS["onlcr"]
    stopped: list[str] = []

    async def stop(*, restore=None, held=None) -> None:
        stopped.append(how)

    monkeypatch.setattr(s, "_stop_under_bound", stop)
    # A blank first line is held: it may be the whole of an empty value.
    s.starved.clear()
    run = asyncio.create_task(
        s.run_cmd(cmd="hang", timeout=0.2 if how == "timeout" else 60, on_output=_failing_sink)
    )
    await wait_for_async(
        lambda: len(s.written) > 0, 5, interval=0.005, on_timeout="the command was never written"
    )
    s.starved.clear()
    s.feed(f"{s._begin_marker}\r\n\r\n")
    await asyncio.wait_for(s.starved.wait(), 5)
    if how == "timeout":
        result = await run
        assert result.timed_out
    else:
        run.cancel()
        with pytest.raises(asyncio.CancelledError):
            await run
    assert stopped == [how]


@pytest.mark.asyncio
@pytest.mark.usefixtures("warning_sink")
@pytest.mark.parametrize("how", ["timeout", "cancel"])
async def test_a_sink_failing_on_the_held_line_never_replaces_a_drain_s_exception(how: str):
    from otto.host.session import ExecOutput, drain_exec_output

    reached = asyncio.Event()

    async def chunks():
        yield "\n"
        reached.set()
        await asyncio.Event().wait()

    drain = drain_exec_output(
        chunks(), ExecOutput(), lambda line, _m: _failing_sink(line), LogMode.NORMAL
    )
    if how == "timeout":
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(drain, 0.2)
        assert reached.is_set()
        return
    task = asyncio.create_task(drain)
    await asyncio.wait_for(reached.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


_LF_MAPPINGS = {
    "identity": _Measurement(IDENTITY, calibrated=True),
}
"""Mappings whose terminal adds no CR: the transcript carries the program's bytes as written."""

_CRLF_MAPPINGS = {
    **_MAPPINGS,
    "unmeasured": _Measurement(UNMEASURED, calibrated=False),
}
"""Mappings under which the terminal sends a CR before every newline."""


@pytest.mark.asyncio
@pytest.mark.parametrize("mapping", sorted(_LF_MAPPINGS))
@pytest.mark.parametrize("case", EXACT_OUTPUT_CASES, ids=lambda c: c.id)
async def test_a_shell_command_on_a_terminal_that_adds_no_cr_logs_the_lines_of_its_value(
    scripted_session: _ScriptedSession, case: OutputCase, mapping: str
):
    s = scripted_session
    s._terminal = _LF_MAPPINGS[mapping]
    logged: list[str] = []
    transcript = f"{s._begin_marker}\n{case.text}{s._end_marker_prefix}{case.retcode}__\n"
    feed_task = asyncio.create_task(s.feed_after_write(*_pieces(transcript, "line")))
    result = await s.run_cmd(cmd=case.command, on_output=logged.append)
    await feed_task
    assert logged == log_lines(result.value) == log_lines(case.value)


@pytest.mark.asyncio
async def test_under_identity_a_line_is_logged_before_the_next_one_arrives(
    scripted_session: _ScriptedSession,
):
    s = scripted_session
    s._terminal = _LF_MAPPINGS["identity"]
    logged: list[str] = []
    first = asyncio.Event()

    def sink(line: str) -> None:
        logged.append(line)
        first.set()

    run = asyncio.create_task(s.run_cmd(cmd="two lines", on_output=sink))
    await wait_for_async(
        lambda: len(s.written) > 0, 5, interval=0.005, on_timeout="the command was never written"
    )
    await _logged_after(s, f"{s._begin_marker}\none\r\n", first)
    assert logged == ["one\r"]
    s.feed(f"two\n{s._end_marker_prefix}0__\n")
    result = await run
    assert logged == log_lines(result.value) == ["one\r", "two"]


@pytest.mark.asyncio
async def test_unmeasured_on_a_crlf_terminal_holds_the_newest_line(
    scripted_session: _ScriptedSession,
):
    s = scripted_session
    s._terminal = _CRLF_MAPPINGS["unmeasured"]
    logged: list[str] = []
    run = asyncio.create_task(s.run_cmd(cmd="two lines", on_output=logged.append))
    await wait_for_async(
        lambda: len(s.written) > 0, 5, interval=0.005, on_timeout="the command was never written"
    )
    s.starved.clear()
    s.feed(f"{s._begin_marker}\r\none\r\n")
    await asyncio.wait_for(s.starved.wait(), 5)
    assert logged == []
    s.feed(f"two\r\n{s._end_marker_prefix}0__\r\n")
    result = await run
    assert logged == log_lines(result.value) == ["one\r", "two"]


@pytest.mark.asyncio
async def test_a_timed_out_shell_command_logs_the_complete_lines_it_held(
    scripted_session: _ScriptedSession, monkeypatch: pytest.MonkeyPatch
):
    import otto.host.session as session_mod

    monkeypatch.setattr(session_mod, "_RECOVERY_TIMEOUT", 0.05)
    s = scripted_session
    s._terminal = _MAPPINGS["unknown"]
    logged: list[str] = []
    feed_task = asyncio.create_task(
        s.feed_after_write(f"{s._begin_marker}\r\none\r\ntwo\r\nunfini")
    )
    result = await s.run_cmd(cmd="long", timeout=0.1, on_output=logged.append)
    await feed_task
    assert result.timed_out
    assert logged == ["one\r", "two\r"]


@pytest.mark.asyncio
async def test_a_stopped_drain_logs_the_blank_first_line_it_held():
    from otto.host.session import ExecOutput, drain_exec_output

    logged: list[str] = []
    reached = asyncio.Event()

    async def chunks():
        yield "\n"
        yield "unfini"
        reached.set()
        await asyncio.Event().wait()

    drain = asyncio.create_task(
        drain_exec_output(
            chunks(), ExecOutput(), lambda line, _m: logged.append(line), LogMode.NORMAL
        )
    )
    await asyncio.wait_for(reached.wait(), 5)
    drain.cancel()
    with pytest.raises(asyncio.CancelledError):
        await drain
    assert logged == [""]


@pytest.mark.asyncio
async def test_a_credential_prompt_the_command_ends_on_stays_out_of_the_log(
    scripted_session: _ScriptedSession,
):
    s = scripted_session
    s._terminal = _MAPPINGS["onlcr"]
    logged: list[str] = []
    cred = CredentialExpect(pattern=r"Password: ", response=SecretStr("pw"), user="u", host="h")
    feed_task = asyncio.create_task(
        s.feed_after_write(f"{s._begin_marker}\r\nok\r\nPassword: {s._end_marker_prefix}1__\r\n")
    )
    await s.run_cmd(cmd="su u", expects=[cred], on_output=logged.append)
    await feed_task
    assert logged == ["ok"]


class _ExpectStubSession(_AliveStubSession):
    """An initialized session under ONLCR whose expect() answers a scripted text."""

    answer = ""

    async def _ensure_initialized(self) -> None:
        await super()._ensure_initialized()
        self._terminal = _Measurement(ONLCR, calibrated=True)

    async def expect(self, pattern, *, timeout=None):
        await self._ensure_initialized()
        return self.answer


async def _expect_through(site: str, answer_sink: "Callable[[str, LogMode], None]") -> str:
    """``expect("x")`` through *site*, one of the three places that log what expect() read."""
    mgr = session_manager(
        connections=cast("ConnectionManager", SimpleNamespace(term="telnet")),
        session_factory=_ExpectStubSession,
        log_command=lambda *_: None,
        log_output=answer_sink if site == "manager" else lambda *_: None,
    )
    if site == "manager":
        return await mgr.expect("x")
    await mgr._ensure_session()
    session = mgr._session
    assert session is not None
    if site == "host-session":
        handle = host_session(
            "s",
            session,
            lambda *_: None,
            answer_sink,
            lambda _name: None,
        )
        return await handle.expect("x")
    return await proxy_io(session, lambda *_: None, answer_sink).expect("x")


@pytest.mark.asyncio
@pytest.mark.parametrize("site", ["manager", "host-session", "session-proxy"])
@pytest.mark.parametrize(
    ("answer", "lines"),
    [
        ("a\r\nb\r\n", ["a", "b"]),
        ("x\ry\r\n$ ", ["x\ry", "$ "]),
        ("", []),
    ],
)
async def test_what_expect_read_is_logged_by_the_command_rule(
    monkeypatch: pytest.MonkeyPatch, site: str, answer: str, lines: list[str]
):
    monkeypatch.setattr(_ExpectStubSession, "answer", answer)
    outs: list[str] = []
    assert await _expect_through(site, lambda out, _mode: outs.append(out)) == answer
    assert outs == lines


class _EchoingStubSession(_AliveStubSession):
    """An initialized session under ONLCR whose terminal echoes the line last sent."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.echo = ""

    async def _ensure_initialized(self) -> None:
        await super()._ensure_initialized()
        self._terminal = _Measurement(ONLCR, calibrated=True)

    async def write_transport(self, data) -> None:
        self.echo = str(data).replace("\n", "\r\n")

    async def read_transport_until(self, pattern):
        return self.echo


@pytest.mark.asyncio
@pytest.mark.parametrize("site", ["manager", "host-session", "session-proxy"])
@pytest.mark.parametrize("standing", [LogMode.NORMAL, LogMode.QUIET, LogMode.NEVER])
async def test_what_expect_read_folds_the_standing_mode_at_every_relay(
    caplog: pytest.LogCaptureFixture, site: str, standing: LogMode
):
    from otto.logger.mode import effective_mode

    relay = logging.getLogger("otto.test.relay")
    relayed: list[LogMode] = []

    def log_output(text: str, mode: LogMode) -> None:
        relayed.append(mode)
        relay.info(text, extra={"log_mode": mode})

    def log_command(text: str, mode: LogMode) -> None:
        relay.info(text, extra={"log_mode": mode})

    def fold(mode: LogMode) -> LogMode:
        return effective_mode(standing, mode)

    mgr = SessionManager(
        connections=cast("ConnectionManager", SimpleNamespace(term="telnet")),
        session_factory=_EchoingStubSession,
        log_command=log_command,
        log_output=log_output,
        fold_log=fold,
    )
    caplog.set_level(logging.DEBUG)
    with caplog.at_level(logging.DEBUG):
        if site == "manager":
            # The host folds the mode of its default session's send and
            # expect alike before the manager logs either.
            await mgr.send("mysql -psecret\n", log=fold(LogMode.NORMAL))
            read = await mgr.expect("secret", log=fold(LogMode.NORMAL))
        else:
            await mgr._ensure_session()
            session = mgr._session
            assert session is not None
            if site == "host-session":
                handle = HostSession(
                    "s", session, log_command, log_output, lambda _name: None, fold_log=fold
                )
            else:
                handle = _SessionProxyIO(session, log_command, log_output, fold_log=fold)
            await handle.send("mysql -psecret\n")
            read = await handle.expect("secret")
    assert read == "mysql -psecret\r\n"
    if standing is LogMode.NEVER:
        assert relayed == []
        assert [r.getMessage() for r in caplog.records if "secret" in r.getMessage()] == []
    else:
        assert relayed == [standing]


def _host_output_records(text: str) -> list[logging.LogRecord]:
    """The records the local host logs for the output *text*, as every route logs them."""
    from otto.host.local_host import LocalHost

    records: list[logging.LogRecord] = []

    class _Keep(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    keep = _Keep(level=logging.DEBUG)
    host_logger = logging.getLogger("otto.host.host")
    host_logger.addHandler(keep)
    saved = host_logger.level
    host_logger.setLevel(logging.DEBUG)
    try:
        LocalHost()._log_output(text)
    finally:
        host_logger.removeHandler(keep)
        host_logger.setLevel(saved)
    return records


@pytest.mark.parametrize(
    ("text", "shown"),
    [
        ("a\rb", ["@localhost > | a", "@localhost > | b"]),
        ("10%\r20%\r", ["@localhost > | 10%", "@localhost > | 20%"]),
        ("", ["@localhost > | "]),
        ("\r10%\r20%", ["@localhost > | 10%", "@localhost > | 20%"]),
        ("line\r\r", ["@localhost > | line"]),
        ("a\r\rb", ["@localhost > | a", "@localhost > | b"]),
        ("\r", ["@localhost > | "]),
        ("\r\r", ["@localhost > | "]),
        ("tab\tx", ["@localhost > | tab      x"]),
        ("[red]x[/red]\ry", ["@localhost > | [red]x[/red]", "@localhost > | y"]),
    ],
)
def test_a_sink_shows_each_carriage_return_segment_of_a_host_line_as_a_prefixed_line(
    tmp_path: Path, text: str, shown: list[str]
):
    from otto.logger.management import _make_file_handler

    (record,) = _host_output_records(text)
    assert record.relayed == text  # the record keeps the program's line as it was
    handler = _make_file_handler(tmp_path / "verbose.log", logging.DEBUG, rich=False)
    try:
        handler.emit(record)
    finally:
        handler.close()
    written = (tmp_path / "verbose.log").read_text(encoding="utf-8").split("\n")
    assert written.pop() == ""  # the file ends on a newline
    for line, want in zip(written, shown, strict=True):
        # Every line the file holds carries the record's prefix.
        assert "INFO" in line
        assert line.endswith(want)


@pytest.mark.parametrize("msg", ["", "\r"])
def test_a_file_sink_shows_an_empty_plain_record_as_one_prefixed_blank_line(
    tmp_path: Path, msg: str
):
    from otto.logger.management import _make_file_handler

    record = logging.LogRecord("otto.t", logging.INFO, __file__, 1, msg, None, None)
    handler = _make_file_handler(tmp_path / "verbose.log", logging.DEBUG, rich=False)
    try:
        handler.emit(record)
    finally:
        handler.close()
    (line, end) = (tmp_path / "verbose.log").read_text(encoding="utf-8").split("\n")
    assert end == ""
    assert "[INFO" in line


def test_a_sink_shows_each_carriage_return_segment_of_a_plain_record_as_a_line():
    from otto.logger.formatters import RichFormatter

    record = logging.LogRecord(
        "otto.t", logging.INFO, __file__, 1, "git said:\n10%\r20%\r\ndone", None, None
    )
    assert RichFormatter(fmt="{message}").format(record).split("\n") == [
        "git said:",
        "10%",
        "20%",
        "done",
    ]


@pytest.mark.parametrize(
    ("text", "shown"),
    [
        ("10%\r20%\r", ["@localhost > | 10%", "@localhost > | 20%"]),
        ("\r10%\r20%", ["@localhost > | 10%", "@localhost > | 20%"]),
        ("line\r\r", ["@localhost > | line"]),
        ("", ["@localhost > | "]),
        ("\r", ["@localhost > | "]),
    ],
)
def test_the_console_shows_each_carriage_return_segment_of_a_host_line_as_a_prefixed_line(
    text: str, shown: list[str]
):
    import io

    from rich.console import Console

    from otto.logger.management import _ConsoleHandler

    (record,) = _host_output_records(text)
    buf = io.StringIO()
    handler = _ConsoleHandler(
        console=Console(file=buf, width=200, color_system=None),
        markup=False,
        show_time=False,
        show_path=False,
    )
    handler.emit(record)
    printed = [line.rstrip() for line in buf.getvalue().split("\n")]
    assert printed.pop() == ""
    assert [line.removeprefix("INFO").strip() for line in printed] == [w.rstrip() for w in shown]
    assert printed[0].startswith("INFO")  # one row, under the record's level


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [LogMode.NORMAL, LogMode.NEVER], ids=lambda m: m.name)
@pytest.mark.parametrize("printed", ["a\\nb\\n", "a\\nb", ""])
async def test_a_process_exec_builds_its_value_once(
    monkeypatch: pytest.MonkeyPatch, mode: LogMode, printed: str
):
    from otto.host.local_host import LocalHost
    from otto.host.session import ExecOutput

    built: list[str] = []
    real = ExecOutput.value

    def counted(self: ExecOutput) -> str:
        built.append(self.raw())
        return real(self)

    monkeypatch.setattr(ExecOutput, "value", counted)
    host = LocalHost()
    try:
        result = await host.exec(f"printf '{printed}'", log=mode)
    finally:
        await host.close()
    assert result.route == "local-subprocess"
    assert len(built) == 1, built

"""Unit tests for :class:`otto.host.console.ConsoleClient` against scripted streams.

Each test scripts what the line shows in reply to what otto writes, so every
branch of the login state machine and of ``reset()`` is pinned without a
serial port. The fake reader hands out chunks on demand and blocks (until
the client's own deadline) when the script runs dry.
"""

import asyncio
import contextlib
import logging
import re
from typing import Any

import pytest

from otto.host.console import ConsoleClient, ConsoleState, classify_tail, strip_ansi
from otto.host.errors import ConsoleError
from otto.host.options import ConsoleOptions
from otto.host.telnet import ReadGateClosed, _live_console_transports, _register_console_transport
from otto.utils import wait_for_async

LOGIN = r"login: ?$"
PASSWORD = r"[Pp]assword: ?$"


class ScriptedReader:
    """Chunks are dicts ``{written_so_far_substring: reply}`` consumed in order.

    ``replies`` is a list of ``(trigger, chunk)`` or ``(trigger, chunk,
    delay)``. A chunk is handed out only once every byte in ``trigger`` (or
    ``b""`` for unconditional) has been written, and *delay* seconds after
    that first became true. When nothing is due, ``read`` waits until the
    next write (or the next delay runs out).
    """

    def __init__(self, replies, eof_after=False, isig_flush=False, login_flush=False):
        self.replies = [r if len(r) == 3 else (*r, 0.0) for r in replies]
        self.written = b""
        self.eof_after = eof_after
        self.isig_flush = isig_flush
        self.login_flush = login_flush
        self.interrupt_pending = False
        self.read_calls = 0
        self._due: dict[int, float] = {}
        self._wake = asyncio.Event()

    def note_write(self, data: bytes) -> None:
        # ``login_flush`` models login(1) flushing the tty before it execs
        # the shell: while the shell's first reply (the one triggered by the
        # password) is still pending, everything typed is discarded.
        if (
            self.login_flush
            and b"Password1\r" in self.written
            and any(b"Password1\r" in t for t, _c, _d in self.replies)
        ):
            return
        # ``isig_flush`` models a tty with ISIG and no NOFLSH: the Ctrl-C
        # interrupt flushes the input queue, so whatever arrives with it, or
        # after it but before the interrupt has been handled (its reply read
        # back), never reaches the shell.
        if self.isig_flush:
            if self.interrupt_pending:
                data = b""
            elif b"\x03" in data:
                data = data[: data.index(b"\x03") + 1]
                self.interrupt_pending = True
        self.written += data
        self._wake.set()

    async def read(self, n: int = 4096) -> bytes:
        self.read_calls += 1
        loop = asyncio.get_running_loop()
        while True:
            now = loop.time()
            waits = []
            for i, (trigger, chunk, delay) in enumerate(self.replies):
                if trigger not in self.written:
                    continue
                due = self._due.setdefault(id(self.replies[i]), now + delay)
                if due <= now:
                    del self.replies[i]
                    if b"\x03" in trigger:
                        self.interrupt_pending = False
                    return chunk
                waits.append(due - now)
            if self.eof_after and not self.replies:
                return b""
            self._wake.clear()
            if waits:
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self._wake.wait(), min(waits))
            else:
                await self._wake.wait()


class ScriptedWriter:
    def __init__(self, reader: ScriptedReader):
        self._reader = reader
        self.transport = None
        self.closed = False

    def write(self, data: bytes) -> None:
        self._reader.note_write(data)

    def iac(self, *args) -> None:  # DONT ECHO: ignored by the fake
        pass

    async def wait_for(self, **kw):
        return None

    def is_closing(self) -> bool:
        return self.closed

    def close(self) -> None:
        self.closed = True


class FakeTransport:
    """A socketless telnetlib3-transport stand-in: released once aborted."""

    def __init__(self):
        self.aborted = False

    def abort(self) -> None:
        self.aborted = True

    def get_write_buffer_size(self) -> int:
        return 0

    def is_closing(self) -> bool:
        return self.aborted

    def get_extra_info(self, name, default=None):
        return default


def _client(
    replies, *, login=True, eof_after=False, isig_flush=False, login_flush=False, **opt
) -> ConsoleClient:
    reader = ScriptedReader(
        replies, eof_after=eof_after, isig_flush=isig_flush, login_flush=login_flush
    )
    opts = ConsoleOptions(
        server="test1",
        port=4001,
        login=login,
        login_prompt=LOGIN,
        password_prompt=PASSWORD,
        login_timeout=0.3,
        settle=0.05,
        **opt,
    )
    # The dial port (54321) deliberately differs from options.port (4001):
    # messages must name the lab's own words (server id + options.port),
    # never the local/forwarded port otto actually connects to.
    c = ConsoleClient(
        host="localhost",
        port=54321,
        user="test",
        password="Password1",
        options=opts,
        name="test2",
        server="test1",
    )
    c._install(reader, ScriptedWriter(reader))  # as open() installs a stream
    return c


class TestClassify:
    def test_ansi_and_line_ends_are_stripped_before_matching(self):
        # Review Focus 1: agetty clears the screen and ends with CRLF+prompt.
        text = "\x1b[H\x1b[J\r\nUbuntu 24.04 LTS test2 ttyS0\r\n\r\ntest2 login: "
        assert classify_tail(text, re.compile(LOGIN), re.compile(PASSWORD)) is ConsoleState.AT_LOGIN

    def test_hostname_prefixed_prompts_match_and_a_motd_last_login_does_not(self):
        # Review Focus 3.
        lr, pr = re.compile(LOGIN), re.compile(PASSWORD)
        assert classify_tail("bb1350 login:", lr, pr) is ConsoleState.AT_LOGIN
        motd = "Last login: Thu Sep 24 10:00:00 2026\r\ntest@test2:~$ "
        assert classify_tail(motd, lr, pr) is None

    def test_password_prompt(self):
        state = classify_tail("Password: ", re.compile(LOGIN), re.compile(PASSWORD))
        assert state is ConsoleState.AT_PASSWORD

    def test_strip_ansi(self):
        assert strip_ansi("\x1b[1;32mhi\x1b[0m\x1b[K") == "hi"

    def test_strip_ansi_also_drops_osc_titles_and_charset_selects(self):
        # M9: an OSC window-title sequence and a charset-select left ANSI
        # debris behind before this fix.
        assert strip_ansi("\x1b]0;title\x07\x1b(Bhi") == "hi"


@pytest.mark.asyncio
class TestLoginSequence:
    async def test_happy_path_writes_user_then_password(self):
        c = _client(
            [
                (b"\r", b"\r\ntest2 login: "),
                (b"test\r", b"Password: "),
            ]
        )
        await c.login_sequence()
        assert c.logged_in is True
        assert c.reader.written == b"\rtest\rPassword1\r"

    async def test_returns_immediately_after_the_password_without_reading_again(self):
        # M3: spec §4 step 6 says write the password and return — a login
        # prompt returning after the password (a refusal) is NOT inspected
        # here; that surfaces later, through the marker handshake's own
        # timeout. read_calls pins that no read happens after the password.
        c = _client(
            [
                (b"\r", b"login: "),
                (b"test\r", b"Password: "),
            ]
        )
        await c.login_sequence()
        assert c.logged_in is True
        assert c.reader.written == b"\rtest\rPassword1\r"
        assert c.reader.read_calls == 2  # nudge, after-username — nothing after the password

    async def test_login_and_password_prompts_in_one_chunk(self):
        # Review Focus 2: BusyBox answers the username with the password prompt
        # in the same read as the echoed newline.
        c = _client([(b"\r", b"bb1350 login: "), (b"test\r", b"test\r\nPassword: ")])
        await c.login_sequence()
        assert c.logged_in

    async def test_already_logged_in_fails_loud_quoting_the_prompt(self):
        c = _client([(b"\r", b"\r\ntest@test2:~$ ")])
        with pytest.raises(ConsoleError, match=r"already logged in.*test@test2:~\$") as ei:
            await c.login_sequence()
        # I2: password=field(repr=False) — repr(c) must never print it.
        assert "Password1" not in repr(c)
        assert str(ei.value).startswith("test2: console test1:4001 (dial=ssh)")

    async def test_stuck_at_a_password_prompt_fails_loud(self):
        c = _client([(b"\r", b"Password: ")])
        with pytest.raises(ConsoleError, match="stuck at a password prompt"):
            await c.login_sequence()

    async def test_silent_line_fails_loud(self):
        c = _client([])
        with pytest.raises(ConsoleError, match="silent"):
            await c.login_sequence()

    async def test_busy_when_the_server_closes_before_any_prompt(self):
        c = _client([], eof_after=True)
        with pytest.raises(ConsoleError, match="busy"):
            await c.login_sequence()

    async def test_busy_quotes_bytes_seen_before_eof(self):
        # M5: a ser2net-style "Port already in use" line printed before the
        # server hangs up is the best evidence available — quote it.
        c = _client([(b"", b"Port already in use\r\n")], eof_after=True)
        with pytest.raises(ConsoleError, match=r"busy.*Port already in use") as ei:
            await c.login_sequence()
        assert "Password1" not in str(ei.value)

    async def test_a_reset_connection_is_wrapped_with_the_console_prefix(self):
        # M5: an OSError from the stream (e.g. an RST) must read like every
        # other failure this client raises, not a raw ConnectionResetError.
        class RaisingReader:
            async def read(self, n: int = 4096) -> bytes:
                raise ConnectionResetError("connection reset by peer")

        c = _client([])
        c.reader = RaisingReader()
        with pytest.raises(ConsoleError, match="lost the connection") as ei:
            await c.login_sequence()
        assert str(ei.value).startswith("test2: console test1:4001 (dial=ssh)")

    async def test_no_password_prompt_after_username_fails_loud(self):
        c = _client([(b"\r", b"login: ")])
        with pytest.raises(ConsoleError, match="no password prompt") as ei:
            await c.login_sequence()
        assert "Password1" not in str(ei.value)

    @pytest.mark.parametrize(
        ("login_prompt", "password_prompt", "message"),
        [
            (
                None,
                None,
                (
                    "no login prompt or password prompt pattern: set "
                    "console_options.login_prompt / console_options.password_prompt"
                ),
            ),
            (
                None,
                "Password: ",
                "no login prompt pattern: set console_options.login_prompt,",
            ),
            (
                "login: ",
                None,
                "no password prompt pattern: set console_options.password_prompt,",
            ),
        ],
    )
    async def test_no_login_prompt_pattern_configured_fails_loud(
        self, login_prompt, password_prompt, message
    ):
        # M7: the sixth message an operator can hit — an OS profile (or the
        # host record) with a login/password pattern missing. The message
        # names the field that is actually missing.
        reader = ScriptedReader([])
        opts = ConsoleOptions(
            server="test1",
            port=4001,
            login_timeout=0.3,
            settle=0.05,
            login_prompt=login_prompt,
            password_prompt=password_prompt,
        )
        c = ConsoleClient(
            host="localhost",
            port=54321,
            user="test",
            password="Password1",
            options=opts,
            name="test2",
            server="test1",
        )
        c.reader = reader
        c.writer = ScriptedWriter(reader)
        with pytest.raises(ConsoleError, match=re.escape(message)):
            await c.login_sequence()

    async def test_a_second_call_is_refused_without_writing_or_waiting(self):
        # M2: console_login() runs once — a second call (or a hook calling
        # it on a `login: true` console that already logged in) must not
        # retype the password, and must not wait login_timeout to say so.
        c = _client([])
        c._logged_in = True
        with pytest.raises(ConsoleError, match="already logged in by this connection"):
            await c.login_sequence()
        assert c.reader.written == b""
        assert c.reader.read_calls == 0

    async def test_prefix_names_options_port_not_the_dialled_port(self):
        # I3: the dial port (54321, a localhost forward) must never leak into
        # a message — only options.port (the lab's own port number) does.
        c = _client([(b"\r", b"Password: ")])
        with pytest.raises(ConsoleError, match=r"console test1:4001") as ei:
            await c.login_sequence()
        assert "54321" not in str(ei.value)


@pytest.mark.asyncio
class TestConnectAndClose:
    async def test_connect_skips_login_when_disabled(self, monkeypatch):
        c = _client([], login=False)
        opened = []

        async def fake_open(host, **kw):
            opened.append((host, kw["port"]))
            return c.reader, c.writer

        monkeypatch.setattr("otto.host.console.open_telnet_connection", fake_open)
        await c.connect()
        assert opened == [("localhost", 54321)]
        assert c.reader.written == b""  # no nudge, no login

    async def test_settle_drains_a_stale_prompt_before_classifying(self, monkeypatch):
        # I3: without a working settle(), a stale prompt already on the line
        # (left by whoever was there before otto) gets classified as fresh
        # and otto types a username into someone else's shell.
        c = _client(
            [
                (b"", b"stale\r\ntest2 login: "),
                (b"\r", b"\r\n$ "),
            ]
        )

        async def fake_open(host, **kw):
            return c.reader, c.writer

        monkeypatch.setattr("otto.host.console.open_telnet_connection", fake_open)
        with pytest.raises(ConsoleError, match="already logged in"):
            await c.connect()

    async def test_connect_classifies_a_crlf_only_echo_as_already_logged_in(self, monkeypatch):
        # N1: spec §4 step 4 — SILENT is "timeout, zero bytes"; a foreground
        # program's "\r\n" echo is bytes, just not a prompt, so it must
        # classify as already logged in, never as silent.
        c = _client([(b"\r", b"\r\n")])

        async def fake_open(host, **kw):
            return c.reader, c.writer

        monkeypatch.setattr("otto.host.console.open_telnet_connection", fake_open)
        with pytest.raises(ConsoleError, match="already logged in") as ei:
            await c.connect()
        assert "silent" not in str(ei.value)

    async def test_logout_after_connect_with_login_disabled_writes_nothing(self, monkeypatch):
        # M7: connect(login=False) never logs in, so close() must not send
        # EOF looking for a login prompt that logout() has no business at.
        c = _client([], login=False)

        async def fake_open(host, **kw):
            return c.reader, c.writer

        monkeypatch.setattr("otto.host.console.open_telnet_connection", fake_open)
        await c.connect()
        assert c.logged_in is False
        reader = c.reader
        await c.close()
        assert reader.written == b""

    async def test_open_bounds_the_tcp_connect_by_login_timeout(self, monkeypatch):
        c = _client([], login=False)
        seen = {}

        async def fake_open(host, **kw):
            seen.update(kw)
            return c.reader, c.writer

        monkeypatch.setattr("otto.host.console.open_telnet_connection", fake_open)
        await c.open()
        assert seen["connect_timeout"] == c.options.login_timeout

    async def test_extra_may_override_the_connect_timeout(self, monkeypatch):
        c = _client([], login=False, extra={"connect_timeout": 7.0})
        seen = {}

        async def fake_open(host, **kw):
            seen.update(kw)
            return c.reader, c.writer

        monkeypatch.setattr("otto.host.console.open_telnet_connection", fake_open)
        await c.open()
        assert seen["connect_timeout"] == 7.0

    async def test_a_timed_out_dial_is_a_console_error_with_the_prefix(self, monkeypatch):
        # telnetlib3's connect_timeout raises ConnectionError when it fires.
        c = _client([], login=False)

        async def timed_out(host, **kw):
            raise ConnectionError(f"TCP connection to {host}:{kw['port']} timed out after 0.3s")

        monkeypatch.setattr("otto.host.console.open_telnet_connection", timed_out)
        with pytest.raises(
            ConsoleError,
            match=r"^test2: console test1:4001 \(dial=ssh\) could not be dialled: .*timed out",
        ):
            await c.open()

    async def test_a_refused_dial_is_a_console_error_with_the_prefix(self):
        # A real telnetlib3 dial at a port nothing listens on.
        import socket

        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        c = ConsoleClient(
            host="127.0.0.1",
            port=port,
            user="test",
            password="Password1",
            options=ConsoleOptions(server="test1", port=4001, login_timeout=2.0),
            name="test2",
            server="test1",
        )
        with pytest.raises(
            ConsoleError, match=r"^test2: console test1:4001 \(dial=ssh\) could not be dialled"
        ) as excinfo:
            await c.open()
        assert isinstance(excinfo.value.__cause__, OSError)
        assert not c.alive

    async def test_close_before_open_does_not_raise_or_log_out(self):
        # Review Focus 4.
        c = ConsoleClient(host="h", port=1, user="u", password="p", options=ConsoleOptions())
        await c.close()
        assert not c.alive
        assert not c.logged_in

    async def test_logout_right_after_login_waits_for_the_shell_before_eof(self):
        # Measured on the bed: login(1) flushes the tty before it execs the
        # shell, so an EOF typed straight after the password is discarded,
        # the shell stays up, and logout() waits out its whole budget. The
        # fake drops every write until the shell's first reply (delayed) has
        # been handed out; logout() must not type into that window.
        c = _client(
            [
                (b"\r", b"login: "),
                (b"test\r", b"Password: "),
                (b"Password1\r", b"Last login: today\r\n$ ", 0.05),
                (b"\x04", b"\r\nlogout\r\n\r\ntest2 login: "),
            ],
            login_flush=True,
        )
        await c.login_sequence()
        reader = c.reader
        await c.logout(c.reader, c.writer)
        assert reader.written.endswith(b"Password1\r\x04"), (
            "the EOF must be typed only once the shell has spoken"
        )
        assert not reader.replies, "the login prompt came back: the EOF reached the shell"

    async def test_close_after_login_sends_eof_and_waits_for_the_prompt(self):
        c = _client(
            [
                (b"\r", b"login: "),
                (b"test\r", b"Password: "),
                (b"Password1\r", b"$ "),
                (b"\x04", b"\r\nlogout\r\n\r\ntest2 login: "),
            ]
        )
        await c.login_sequence()
        # M1: capture the fakes before close() nulls c.reader/c.writer.
        reader = c.reader
        writer = c.writer
        await c.close()
        assert reader.written.endswith(b"\x04")
        assert writer.closed
        assert not c.logged_in, "the release's end forgets its stream's login"

    async def test_close_with_logout_disabled_sends_nothing(self):
        c = _client(
            [(b"\r", b"login: "), (b"test\r", b"Password: "), (b"Password1\r", b"$ ")],
            logout=False,
        )
        await c.login_sequence()
        reader = c.reader
        await c.close()
        assert reader.written == b"\rtest\rPassword1\r"

    async def test_close_cancelled_mid_logout_still_releases_the_transport(self):
        # I1: close() must release the transport even when cancelled mid-
        # logout (e.g. a caller-side timeout) — otherwise the console
        # server's single-client slot stays held and the NEXT connect fails
        # as busy, the exact stuck-console outcome this term exists to
        # prevent. The script deliberately has no reply for the EOF byte, so
        # logout()'s read blocks until we cancel it.
        c = _client(
            [
                (b"\r", b"login: "),
                (b"test\r", b"Password: "),
            ]
        )
        await c.login_sequence()
        # N3: raise login_timeout well past the 0.01s cancel delay below, so
        # a loaded runner can never let logout()'s wait finish first and
        # turn this into a spurious failure (close() would then return
        # normally and `await task` would not raise CancelledError at all).
        c.options.login_timeout = 30
        transport = FakeTransport()
        c.writer.transport = transport
        _register_console_transport(transport)

        task = asyncio.ensure_future(c.close())
        await asyncio.sleep(0.01)  # let close()/logout() reach the blocked read
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert transport.aborted
        assert transport not in _live_console_transports

    async def test_two_concurrent_close_calls_do_not_raise(self):
        # N2: a second, concurrent close() (e.g. a ConnectionManager
        # teardown racing a session teardown) must not hit
        # AttributeError: 'NoneType' object has no attribute 'close'.
        c = _client(
            [
                (b"\r", b"login: "),
                (b"test\r", b"Password: "),
            ]
        )
        await c.login_sequence()
        results = await asyncio.gather(c.close(), c.close(), return_exceptions=True)
        assert results == [None, None]

    async def test_abort_releases_the_line_at_once_without_logging_out(self):
        # A replaced connection manager (reboot) cannot await: abort() frees
        # the single-client slot synchronously and types nothing — the device
        # it would log out of has rebooted.
        c = _client([(b"\r", b"login: "), (b"test\r", b"Password: "), (b"Password1\r", b"$ ")])
        await c.login_sequence()
        reader, writer = c.reader, c.writer
        transport = FakeTransport()
        writer.transport = transport
        _register_console_transport(transport)

        c.abort()

        assert reader.written == b"\rtest\rPassword1\r", "abort typed on the line"
        assert writer.closed
        assert transport.aborted
        await asyncio.sleep(0)  # the release is observed on the loop's next turn
        assert transport not in _live_console_transports
        assert not c.alive
        assert not c.logged_in
        c.abort()  # idempotent

    async def test_abort_never_raises_and_still_aborts_the_transport(self):
        c = _client([(b"\r", b"login: ")])
        transport = FakeTransport()
        c.writer.transport = transport
        _register_console_transport(transport)

        def closed_loop() -> None:
            raise RuntimeError("Event loop is closed")

        c.writer.close = closed_loop
        c.abort()

        assert transport.aborted, "a close() that raises must not skip the abort"
        await asyncio.sleep(0)
        assert transport not in _live_console_transports
        assert not c.alive


@pytest.mark.asyncio
class TestReset:
    async def test_already_at_login_is_a_no_op(self):
        c = _client([(b"\r", b"test2 login: "), (b"\r\r", b"\r\ntest2 login: ")])
        assert await c.reset() is ConsoleState.AT_LOGIN
        assert c.reader.written == b"\r\r", (
            "two nudges (the second confirms getty) and nothing else"
        )

    async def test_login_1s_retry_prompt_is_told_apart_by_a_second_nudge(self):
        """Measured on the bed: a CR nudge at login(1)'s Password: fails back, after
        the failure delay, to login(1)'s own retry prompt, which looks like
        getty's. A second CR there is an empty username: Password: again.
        The reset must see that and end login(1) with Ctrl-C."""
        c = _client(
            [
                (b"\r", b"\r\nLogin incorrect\r\ntest2 login: "),
                (b"\r\r", b"\r\nPassword: "),
                (b"\r\r\x03", b"\r\r\nUbuntu test2 ttyV0\r\n\r\ntest2 login: "),
            ]
        )
        assert await c.reset() is ConsoleState.AT_PASSWORD
        assert c.reader.written == b"\r\r\x03"

    async def test_a_password_prompt_is_cleared_with_ctrl_c(self):
        """Ctrl-C ends login(1): getty respawns a clean prompt. Never a credential."""
        c = _client(
            [(b"\r", b"Password: "), (b"\r\x03", b"\r\r\nUbuntu test2 ttyV0\r\n\r\nlogin: ")]
        )
        # The return names what the nudge FOUND: the reset had to act.
        assert await c.reset() is ConsoleState.AT_PASSWORD
        assert c.reader.written == b"\r\x03"

    async def test_login_1s_retry_prompt_is_not_where_the_reset_leaves_the_line(self):
        """Measured on the bed: a CR at login(1)'s Password: fails back to
        login(1)'s OWN retry prompt, which answers the next CR with Password:
        again. Ctrl-C ends login(1) instead, so getty's prompt comes back."""
        c = _client(
            [
                (b"\r", b"Password: "),
                # A CR answer would be taken to login(1)'s retry prompt ...
                (b"\r\r", b"\r\nLogin incorrect\r\ntest2 login: "),
                # ... whose next CR nudge yields Password: again.
                (b"\r\r\r", b"\r\nPassword: "),
                (b"\r\x03", b"\r\r\nUbuntu test2 ttyV0\r\n\r\ntest2 login: "),
            ]
        )
        assert await c.reset() is ConsoleState.AT_PASSWORD
        assert c.reader.written == b"\r\x03", "the password prompt got Ctrl-C, not CR"
        assert c.reader.replies[0][0] == b"\r\r", "the CR path to login(1)'s retry was never taken"

    async def test_a_shell_is_ended_with_ctrl_c_ctrl_d(self):
        c = _client([(b"\r", b"$ "), (b"\x03\x04\r", b"\r\nlogout\r\n\r\ntest2 login: ")])
        assert await c.reset() is ConsoleState.LOGGED_IN
        assert b"\x03\x04\r" in c.reader.written

    async def test_ctrl_d_survives_the_flush_ctrl_c_causes(self):
        # Measured on the bed (bash on a serial getty): a tty's ISIG handling
        # of Ctrl-C flushes its input queue, so a Ctrl-D written in the same
        # burst was discarded and every round just redrew the prompt. The
        # Ctrl-D must go in its own write, after the interrupt has landed.
        c = _client(
            [
                (b"\r", b"test@test2:~$ "),
                (b"\x03", b"^C\r\n\r\ntest@test2:~$ "),
                (b"\x03\x04", b"\r\nlogout\r\n\r\ntest2 login: "),
            ],
            isig_flush=True,
        )
        assert await c.reset() is ConsoleState.LOGGED_IN

    async def test_ctrl_d_waits_for_a_slow_interrupt_echo(self):
        """Under a stalled forward the ^C echo is late. A Ctrl-D written before
        the interrupt was handled is flushed with the queue; the reset must
        wait (bounded by login_timeout) for the echo, not a fixed pause."""
        c = _client(
            [
                (b"\r", b"test@test2:~$ "),
                (b"\x03", b"^C\r\n\r\ntest@test2:~$ ", 0.7),
                (b"\x03\x04", b"\r\nlogout\r\n\r\ntest2 login: "),
            ],
            isig_flush=True,
        )
        c.options.login_timeout = 1.5
        assert await c.reset() is ConsoleState.LOGGED_IN

    async def test_a_foreground_program_echoing_crlf_is_not_silent(self):
        # N1: spec §4 step 4 — SILENT is "timeout, zero bytes"; a foreground
        # program (sleep, cat, a script blocked on read) echoes the nudge CR
        # back as "\r\n", which is bytes, just not a prompt. reset() must
        # still try Ctrl-C/Ctrl-D instead of misdiagnosing the line as
        # silent (which would never attempt the recovery it exists to do).
        c = _client([(b"\r", b"\r\n"), (b"\x03\x04\r", b"login: ")])
        assert await c.reset() is ConsoleState.LOGGED_IN
        assert b"\x03\x04\r" in c.reader.written

    async def test_a_nested_shell_takes_two_rounds(self):
        c = _client(
            [
                (b"\r", b"$ "),
                (b"\x03\x04\r", b"\r\n$ "),
                (b"\x03\x04\r\x03\x04\r", b"\r\ntest2 login: "),
            ]
        )
        assert await c.reset() is ConsoleState.LOGGED_IN

    async def test_three_rounds_then_fail_loud(self):
        c = _client(
            [
                (b"\r", b"(END) "),
                (b"\x03\x04\r", b"(END) "),
                (b"\x03\x04\r\x03\x04\r", b"(END) "),
                (b"\x03\x04\r\x03\x04\r\x03\x04\r", b"(END) "),
            ]
        )
        with pytest.raises(ConsoleError, match=r"could not reset console.*\(END\)"):
            await c.reset()

    async def test_busy_when_the_server_closes_before_any_prompt(self):
        # M7: reset's BUSY branch had no covering test.
        c = _client([], eof_after=True)
        with pytest.raises(ConsoleError, match="busy"):
            await c.reset()

    async def test_silent_line_fails_loud_without_three_rounds_of_delay(self):
        # M4: a SILENT line used to fall through all three Ctrl-C/Ctrl-D
        # rounds (~4x login_timeout) before failing with an empty-tail
        # message instead of the silent diagnosis.
        c = _client([])
        with pytest.raises(ConsoleError, match="silent"):
            await c.reset()


class _DelayedLine:
    """A line that shows *first* at once, then *later* after *delay* seconds of silence.

    ``login(1)``'s failure delay (FAIL_DELAY / pam_faildelay, ~3 s; BusyBox
    sleeps too) is silent: the echoed handshake probes arrive, then nothing,
    then ``Login incorrect`` and a fresh prompt.
    """

    def __init__(self, first: bytes, later: bytes, delay: float) -> None:
        self._chunks = [first, later]
        self._delay = delay
        self.read_calls = 0

    async def read(self, n: int = 4096) -> bytes:
        self.read_calls += 1
        if not self._chunks:
            await asyncio.Event().wait()
        chunk = self._chunks.pop(0)
        if not self._chunks:
            await asyncio.sleep(self._delay)
        return chunk


@pytest.mark.asyncio
class TestRefusedAfterLogin:
    """After a failed marker handshake: getty back on the line means the login was refused."""

    def _logged_in(self, replies, *, login_timeout: float = 0.3) -> ConsoleClient:
        c = _client(replies)
        c.options.login_timeout = login_timeout
        c._logged_in = True
        return c

    async def test_a_tail_at_the_login_prompt_is_a_refused_login(self):
        c = self._logged_in([(b"", b"stty -echo\r\nLogin incorrect\r\ntest2 login: ")])
        with pytest.raises(ConsoleError) as exc_info:
            await c.refused_after_login()
        assert (
            str(exc_info.value) == "test2: console test1:4001 (dial=ssh) login refused for 'test'"
        )
        assert "Password1" not in str(exc_info.value)

    async def test_a_tail_at_the_password_prompt_is_a_refused_login(self):
        """The resent probes are read by login(1) as a username: the line parks at Password:."""
        c = self._logged_in(
            [(b"", b"Login incorrect\r\ntest2 login: stty -echo; echo; echo X\r\nPassword: ")]
        )
        with pytest.raises(ConsoleError, match=r"login refused for 'test'$"):
            await c.refused_after_login()

    async def test_login_1_is_ended_so_getty_respawns_a_clean_prompt(self):
        """Measured on the bed: login(1)'s retry prompt answers the next
        client's CR nudge with "Password:", so the next connect failed as
        "stuck at a password prompt". The refusal ends login(1) with Ctrl-C
        (never a credential) and waits for getty's fresh prompt."""
        c = self._logged_in(
            [
                (b"", b"Login incorrect\r\ntest2 login: stty -echo; echo\r\nPassword: "),
                (b"\x03", b"\r\r\nUbuntu 24.04.3 LTS test2 ttyV0\r\n\r\ntest2 login: "),
            ]
        )
        with pytest.raises(ConsoleError, match=r"login refused for 'test'$"):
            await c.refused_after_login()
        assert c.reader.written == b"\x03"
        assert not c.reader.replies, "getty's fresh prompt was read"
        assert not c.logged_in, "no shell started: close() must not send a logout"

    async def test_a_refusal_at_login_1s_retry_prompt_also_ends_it(self):
        c = self._logged_in(
            [
                (b"", b"Login incorrect\r\ntest2 login: "),
                (b"\x03", b"\r\r\ntest2 login: "),
            ]
        )
        with pytest.raises(ConsoleError, match="login refused"):
            await c.refused_after_login()
        assert c.reader.written == b"\x03"
        assert not c.logged_in

    async def test_a_prompt_after_a_silent_failure_delay_is_still_found(self):
        c = self._logged_in([], login_timeout=2.0)
        c.reader = _DelayedLine(b"stty -echo; echo\r\n", b"Login incorrect\r\ntest2 login: ", 0.6)
        with pytest.raises(ConsoleError, match=r"login refused for 'test'"):
            await c.refused_after_login()

    async def test_no_prompt_within_login_timeout_returns_what_the_line_showed(self):
        c = self._logged_in([(b"", b"\x1b[1mwelcome to test2\x1b[0m\r\n$ ")], login_timeout=0.2)
        loop = asyncio.get_running_loop()
        started = loop.time()
        seen = await c.refused_after_login()
        assert c.reader.written == b"", "a logged-in shell must never get the refusal's Ctrl-C"
        assert loop.time() - started < 1.0, "bounded by login_timeout"
        assert seen == " Seen: " + repr("welcome to test2\r\n$ ") + "."

    async def test_a_silent_line_returns_nothing_to_quote(self):
        c = self._logged_in([], login_timeout=0.1)
        assert await c.refused_after_login() == ""

    async def test_a_password_echoed_back_is_scrubbed_from_the_quote(self):
        """A line that echoes what was typed must not carry the password into any message."""
        c = self._logged_in([(b"", b"Password1\r\nPassword1: command not found\r\n$ ")])
        seen = await c.refused_after_login()
        assert "Password1" not in seen
        assert seen.count("***") == 2
        assert "Password1" not in c._quote("x Password1 y")
        assert "***" in c._quote("x Password1 y")

    async def test_a_client_that_never_logged_in_is_a_no_op(self):
        """login=False (an RTOS shell, a raw landing): no password typed, nothing refused."""
        c = _client([(b"", b"test2 login: ")], login=False)
        assert await c.refused_after_login() == ""
        assert c.reader.read_calls == 0


class TestSendNaws:
    """``run_telnet_login`` forwards SIGWINCH through ``client._send_naws`` —
    the console client carries the same frame as ``TelnetClient``'s."""

    @staticmethod
    def _client():
        from unittest.mock import MagicMock

        c = ConsoleClient(host="localhost", port=4001, user="test", password="Password1")
        c.writer = MagicMock()
        return c

    def test_uses_send_iac_not_write(self):
        """``write`` would re-escape the framing IAC bytes and corrupt the command."""
        c = self._client()
        c._send_naws(80, 24)
        c.writer.write.assert_not_called()
        c.writer.send_iac.assert_called_once()

    def test_frame_bytes_are_exact_rfc1073(self):
        import struct

        from telnetlib3.telopt import IAC, NAWS, SB, SE

        c = self._client()
        c._send_naws(80, 24)
        assert c.writer.send_iac.call_args.args[0] == (
            IAC + SB + NAWS + struct.pack(">HH", 80, 24) + IAC + SE
        )

    def test_a_0xff_byte_in_the_size_is_doubled(self):
        from telnetlib3.telopt import IAC

        c = self._client()
        c._send_naws(255, 24)
        frame = c.writer.send_iac.call_args.args[0]
        assert frame[3:6] == b"\x00" + IAC + IAC

    def test_no_writer_is_a_no_op(self):
        c = ConsoleClient(host="localhost", port=4001, user="test", password="Password1")
        c._send_naws(80, 24)  # must not raise


# -- take, then release ---------------------------------------------------------
#
# A console close takes the stream before its first await, waits for the reads
# already in flight to leave the gate, logs out on the taken stream, then hands
# it to the one release (drain to quiet, close, observe the fd's release). Every
# way out that is not graceful goes through telnet's one abort path.


@pytest.fixture
def empty_registry():
    """Start and end a test with an empty emergency registry."""
    _live_console_transports.clear()
    yield
    _live_console_transports.clear()


def _logged_in_client(replies, **opt) -> ConsoleClient:
    """A client whose shell has spoken since its login: logout types EOF at once."""
    c = _client(replies, **opt)
    c._logged_in = True
    return c


def _attach(c: ConsoleClient, transport) -> None:
    """Give *c*'s fake writer *transport*, registered as open() would have."""
    c.writer.transport = transport
    _register_console_transport(transport)


class _StalledWriter:
    """A writer over a ``StalledTransport``: writes and closes go to the transport."""

    def __init__(self, reader: ScriptedReader, transport) -> None:
        self._reader = reader
        self.transport = transport

    def write(self, data: bytes) -> None:
        self._reader.note_write(data)
        self.transport.write(data)

    def is_closing(self) -> bool:
        return self.transport.is_closing()

    def close(self) -> None:
        self.transport.close()


def _hold_the_gate(c: ConsoleClient, *, leave_after: float | None = None) -> "asyncio.Task[None]":
    """A read of *c*'s stream inside its gate that leaves after *leave_after*, or never."""

    async def read() -> None:
        with c._read_gate.reading():
            if leave_after is None:
                await asyncio.get_running_loop().create_future()
            await asyncio.sleep(leave_after)

    return asyncio.ensure_future(read())


async def _cancel(task: "asyncio.Task[Any]") -> None:
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


def _spy_release(monkeypatch, calls: list) -> None:
    """Record each ``release_telnet_stream`` the console makes, still running the real one."""
    from otto.host import console as console_mod

    real = console_mod.release_telnet_stream

    async def spy(*args, **kwargs):
        calls.append(kwargs)
        await real(*args, **kwargs)
        calls.append("released")

    monkeypatch.setattr(console_mod, "release_telnet_stream", spy)


def _spy_logout(monkeypatch, c: ConsoleClient, calls: list) -> None:
    """Record each logout with what the client still held, still running the real one."""
    real = c.logout

    async def spy(reader, writer):
        calls.append((c.reader, c.writer, reader, writer))
        await real(reader, writer)

    monkeypatch.setattr(c, "logout", spy)


@pytest.mark.asyncio
@pytest.mark.usefixtures("empty_registry")
class TestTakeThenRelease:
    async def test_close_order_take_idle_logout_release(self, monkeypatch):
        from otto.host import console as console_mod

        c = _logged_in_client([(b"\x04", b"\r\nlogout\r\n\r\ntest2 login: ")])
        order: list[str] = []
        real_take, real_idle, real_logout = c._take, c._read_gate.wait_idle, c.logout

        def take():
            order.append("take")
            return real_take()

        async def wait_idle(deadline):
            order.append("wait_idle")
            return await real_idle(deadline)

        async def logout(reader, writer):
            order.append("logout")
            await real_logout(reader, writer)

        async def release(*args, **kwargs):
            order.append("release")

        monkeypatch.setattr(c, "_take", take)
        monkeypatch.setattr(c._read_gate, "wait_idle", wait_idle)
        monkeypatch.setattr(c, "logout", logout)
        monkeypatch.setattr(console_mod, "release_telnet_stream", release)

        await c.close()

        assert order == ["take", "wait_idle", "logout", "release"]

    async def test_idle_timeout_skips_logout_and_drain(self, monkeypatch):
        from otto.host import console as console_mod

        monkeypatch.setattr(console_mod, "CONSOLE_QUIESCE_BUDGET", 0.3)
        c = _logged_in_client([(b"\x04", b"\r\nlogout\r\n\r\ntest2 login: ")])
        transport = FakeTransport()
        _attach(c, transport)
        logouts: list = []
        releases: list = []
        aborts: list = []
        _spy_logout(monkeypatch, c, logouts)
        _spy_release(monkeypatch, releases)
        real_abort = console_mod._abort_and_observe

        async def abort_spy(*args):
            aborts.append(args)
            await real_abort(*args)

        monkeypatch.setattr(console_mod, "_abort_and_observe", abort_spy)
        stuck = _hold_the_gate(c)
        await asyncio.sleep(0)
        loop = asyncio.get_running_loop()
        try:
            started = loop.time()
            await c.close()
            elapsed = loop.time() - started
        finally:
            await _cancel(stuck)

        assert logouts == []
        assert releases == []
        assert len(aborts) == 1
        assert transport.aborted
        assert c.reader is None
        assert c.writer is None
        assert c._released.is_set()
        assert elapsed <= 0.3 + 0.3

    async def test_idle_completes_before_logout_real_reader(self):
        c = _logged_in_client(
            [(b"", b"late output", 0.1), (b"\x04", b"\r\nlogout\r\n\r\ntest2 login: ")]
        )
        reader = c.reader
        starts: list[int] = []
        pending = 0
        real_read = reader.read

        async def read(n: int = 4096) -> bytes:
            # telnetlib3 refuses a second concurrent read with this error.
            nonlocal pending
            if pending:
                raise RuntimeError("already waiting")
            starts.append(c._read_gate.in_flight)
            pending += 1
            try:
                return await real_read(n)
            finally:
                pending -= 1

        reader.read = read
        in_flight = asyncio.ensure_future(c._read_chunk(5.0))
        await asyncio.sleep(0)
        assert c._read_gate.in_flight == 1

        await c.close()

        assert await in_flight == b"late output"
        assert starts[0] == 1, "the first read was the in-flight one, inside the gate"
        assert len(starts) > 1, "logout read the line"
        assert all(n == 0 for n in starts[1:]), "logout read while a gated read was in flight"
        assert reader.written == b"\x04"
        assert not reader.replies, "the login prompt came back"

    @pytest.mark.parametrize(
        "window", ["refused", "cancel_establishment", "cancel_negotiation", "login_refused"]
    )
    async def test_close_after_failed_open_is_bounded(self, monkeypatch, window):
        """``ConnectionManager.console()``'s own failure cleanup closes the client, bounded.

        A refused dial or a cancellation inside the opener leaves nothing
        installed: the opener released what it built, so the close returns at
        once. A cancellation inside the echo negotiation, or a refused login,
        comes after the streams are installed: the close drains the line (the
        guest's banner) before it closes it, with no abort, and unregisters it.
        """
        from otto.host import console as console_mod
        from otto.host import telnet
        from otto.host.connections import ConnectionManager, ConsoleEndpoint, TermContext
        from otto.host.telnet import CONSOLE_QUIESCE_BUDGET
        from tests.unit.host._telnet_fakes import SilentTelnetPeer
        from tests.unit.host.test_telnet_client import (
            BANNER,
            _cancel_in_establishment,
            _refused_port,
        )

        loop = asyncio.get_running_loop()
        closes: list[tuple[ConsoleClient, float]] = []
        real_close = ConsoleClient.close

        async def timed_close(self):
            started = loop.time()
            await real_close(self)
            closes.append((self, loop.time() - started))

        monkeypatch.setattr(ConsoleClient, "close", timed_close)
        drained: list[bytes] = []  # the peer's record of what otto's side read
        real_read_within = telnet._read_within

        async def recording(reader, timeout):
            data = await real_read_within(reader, timeout)
            if data:
                drained.append(data)
            return data

        # Only the release's drain reads through telnet's own copy.
        monkeypatch.setattr(telnet, "_read_within", recording)
        aborts: list = []
        real_abort = telnet.abort_telnet_stream

        def abort_spy(writer, transport):
            aborts.append(transport)
            real_abort(writer, transport)

        monkeypatch.setattr(telnet, "abort_telnet_stream", abort_spy)
        negotiating = asyncio.Event()
        real_negotiate = console_mod.negotiate_no_echo

        async def negotiate(writer, timeout):
            negotiating.set()
            await real_negotiate(writer, timeout)
            if window == "cancel_negotiation":
                # The silent peer never offers ECHO, so telnetlib3 already
                # counts it off; hold the negotiation open as a server that
                # never answers DONT ECHO would, until the caller cancels.
                await asyncio.get_running_loop().create_future()

        monkeypatch.setattr(console_mod, "negotiate_no_echo", negotiate)
        if window == "login_refused":

            async def refuse(self):
                raise ConsoleError("scripted refusal")

            monkeypatch.setattr(console_mod.ConsoleClient, "login_sequence", refuse)

        installed = window in ("cancel_negotiation", "login_refused")
        async with SilentTelnetPeer(BANNER if installed else None) as peer:
            peer.drained = drained
            port = _refused_port() if window == "refused" else peer.port
            options = ConsoleOptions(
                server="test1",
                port=port,
                dial="direct",
                settle=0.0,
                login_prompt=LOGIN,
                password_prompt=PASSWORD,
                echo_negotiation_timeout=0.05,
                extra={"connect_minwait": 0, "connect_maxwait": 0.05},
            )
            mgr = ConnectionManager.create(
                TermContext(
                    ip="127.0.0.1",
                    creds=[],
                    term="console",
                    name="dev1",
                    host_id="dev1",
                    console_options=options,
                    console_endpoint=lambda: ConsoleEndpoint(server_ip="127.0.0.1", hop=None),
                )
            )
            if window == "cancel_establishment":
                _cancel_in_establishment(monkeypatch)
            opening = asyncio.ensure_future(mgr.console())
            if window == "cancel_negotiation":
                await wait_for_async(
                    negotiating.is_set, 2.0, interval=0.01, on_timeout="never negotiated echo"
                )
                await asyncio.sleep(0.1)  # the banner has arrived; the echo never agrees
                assert _live_console_transports, "the opener registered it as single-client"
                opening.cancel()
            if window == "refused":
                expected: Any = OSError
            elif window == "login_refused":
                expected = ConsoleError
            else:
                expected = asyncio.CancelledError
            with pytest.raises(expected):
                await opening
            if installed:
                await wait_for_async(
                    lambda: peer.drained_at_eof is not None,
                    2.0,
                    interval=0.01,
                    on_timeout="the peer never saw the client's EOF",
                )

        ((client, took),) = closes
        assert mgr._console_conn is None
        assert client.reader is None
        assert client.writer is None
        assert client._released.is_set()
        assert not _live_console_transports
        if installed:
            assert took <= CONSOLE_QUIESCE_BUDGET + 0.3
            assert peer.drained_at_eof == b"".join(BANNER), "the banner was drained before the EOF"
            assert aborts == [], "a quiet line is released gracefully"
        else:
            assert took < 0.1, "nothing was installed, so nothing was left to release"
            assert drained == []
        await asyncio.wait_for(real_close(client), 0.5)

    async def test_cancel_during_idle_wait_aborts_taken_stream(self):
        c = _logged_in_client([(b"\x04", b"\r\nlogout\r\n\r\ntest2 login: ")])
        reader, writer = c.reader, c.writer
        transport = FakeTransport()
        _attach(c, transport)
        stuck = _hold_the_gate(c)
        try:
            closing = asyncio.ensure_future(c.close())
            await asyncio.sleep(0.05)  # inside the idle wait
            assert c.writer is None, "the stream was taken before the first await"
            closing.cancel()
            with pytest.raises(asyncio.CancelledError):
                await closing
        finally:
            await _cancel(stuck)

        assert transport.aborted
        assert writer.closed
        assert reader.written == b"", "no logout without exclusive use of the line"
        assert transport not in _live_console_transports
        assert c._released.is_set()
        assert not c.logged_in

    @pytest.mark.parametrize(
        "case", ["observed_release", "grace_expires", "cancel_repeated_in_grace"]
    )
    @pytest.mark.parametrize("path", ["idle_timeout", "cancel_in_idle", "cancel_in_logout"])
    async def test_console_abort_paths(self, monkeypatch, stalled_transport, path, case):
        from otto.host import console as console_mod
        from otto.host import telnet

        loop = asyncio.get_running_loop()
        monkeypatch.setattr(console_mod, "CONSOLE_QUIESCE_BUDGET", 0.3)
        if case == "cancel_repeated_in_grace":
            # A grace that waited through the second cancel would see the
            # release land at 0.3 s and unregister; the widened grace leaves
            # room for that.
            monkeypatch.setattr(telnet, "_ABORT_GRACE", 0.5)
            transport = stalled_transport(release_after_seconds=0.3)
        elif case == "grace_expires":
            transport = stalled_transport(release_after_turns=None)
        else:
            transport = stalled_transport(release_after_turns=1)
        c = _logged_in_client([])
        c.options.login_timeout = 30  # logout's read blocks until cancelled
        reader = c.reader
        c.writer = _StalledWriter(reader, transport)
        _register_console_transport(transport)
        in_grace = asyncio.Event()
        real_await = telnet._await_local_release

        async def entering_grace(*args):
            in_grace.set()
            return await real_await(*args)

        monkeypatch.setattr(telnet, "_await_local_release", entering_grace)

        stuck = _hold_the_gate(c) if path != "cancel_in_logout" else None
        try:
            closing = asyncio.ensure_future(c.close())
            if path == "cancel_in_logout":
                await wait_for_async(
                    lambda: reader.written.endswith(b"\x04"),
                    2.0,
                    interval=0.005,
                    on_timeout="logout never typed its EOF",
                )
                await asyncio.sleep(0.01)
                closing.cancel()
            elif path == "cancel_in_idle":
                await asyncio.sleep(0.05)
                closing.cancel()
            second_cancel = 0.0
            if case == "cancel_repeated_in_grace":
                # On the idle_timeout path this is the only cancel: the close
                # reached the abort's grace on its own, so it is a first
                # cancel delivered inside the grace, which must propagate as
                # promptly as a repeated one.
                await wait_for_async(
                    in_grace.is_set, 2.0, interval=0.005, on_timeout="the abort's grace never began"
                )
                second_cancel = loop.time()
                closing.cancel()
            if path == "idle_timeout" and case != "cancel_repeated_in_grace":
                await closing
            else:
                with pytest.raises(asyncio.CancelledError):
                    await closing
        finally:
            if stuck is not None:
                await _cancel(stuck)

        assert in_grace.is_set(), "the real abort-and-observe ran"
        assert transport.aborts == 1
        if case == "cancel_repeated_in_grace":
            assert loop.time() - second_cancel < 0.05
            assert transport.sock.fileno() != -1
            assert transport in _live_console_transports
        elif case == "grace_expires":
            assert transport.sock.fileno() != -1
            assert transport in _live_console_transports
        else:
            assert transport.sock.fileno() == -1
            assert transport not in _live_console_transports
        assert c._released.is_set()
        await asyncio.wait_for(c.close(), 0.5)  # a second closer returns
        if path != "cancel_in_logout":
            assert reader.written == b"", "logout ran without exclusive use of the line"

    async def test_two_closers_during_idle_wait_one_release(self, monkeypatch):
        c = _client([])
        releases: list = []
        _spy_release(monkeypatch, releases)
        leaving = _hold_the_gate(c, leave_after=0.1)
        await asyncio.sleep(0)

        async def close(name: str) -> None:
            await c.close()
            releases.append(f"{name} returned")

        await asyncio.gather(close("first"), close("second"))
        await leaving

        assert sum(isinstance(r, dict) for r in releases) == 1, "one release"
        assert releases.index("released") < releases.index("second returned")
        assert releases.index("released") < releases.index("first returned")

    async def test_logout_runs_once_under_concurrent_close(self, monkeypatch):
        c = _logged_in_client([(b"\x04", b"\r\nlogout\r\n\r\ntest2 login: ")])
        reader = c.reader
        logouts: list = []
        _spy_logout(monkeypatch, c, logouts)

        await asyncio.gather(c.close(), c.close())

        assert len(logouts) == 1
        assert reader.written.count(b"\x04") == 1

    async def test_logout_uses_taken_streams(self, monkeypatch):
        c = _logged_in_client([(b"\x04", b"\r\nlogout\r\n\r\ntest2 login: ")])
        reader, writer = c.reader, c.writer
        logouts: list = []
        _spy_logout(monkeypatch, c, logouts)

        await c.close()

        assert logouts == [(None, None, reader, writer)], "logout ran on the taken stream"
        assert reader.written == b"\x04"
        assert not reader.replies, "the login prompt came back"

    async def test_shell_unheard_logout_through_close(self, monkeypatch, caplog):
        from otto.host.connections import _console_logout_allowance

        c = _client(
            [
                (b"\r", b"login: "),
                (b"test\r", b"Password: "),
                (b"Password1\r", b"Last login: today\r\n$ ", 0.05),
                (b"\x04", b"\r\nlogout\r\n\r\ntest2 login: "),
            ],
            login_flush=True,
        )
        await c.login_sequence()
        assert c._shell_unheard
        assert c.options.settle > 0
        reader = c.reader
        loop = asyncio.get_running_loop()
        spent: list[float] = []
        real_logout = c.logout

        async def timed(reader, writer):
            started = loop.time()
            await real_logout(reader, writer)
            spent.append(loop.time() - started)

        monkeypatch.setattr(c, "logout", timed)

        with caplog.at_level(logging.WARNING, logger="otto.host.console"):
            await c.close()

        assert reader.written.endswith(b"Password1\r\x04"), "the shell spoke before the EOF"
        assert not reader.replies, "the shell's output and the login prompt were consumed"
        assert not [r for r in caplog.records if "logout failed" in r.getMessage()]
        assert len(spent) == 1
        assert spent[0] <= _console_logout_allowance(c.options)

    async def test_read_chunk_rejected_after_gate_closed(self):
        c = _client([(b"", b"data")])
        loop = asyncio.get_running_loop()
        assert await c._read_gate.wait_idle(loop.time() + 1.0)

        with pytest.raises(ReadGateClosed):
            await c._read_chunk(0.1)
        assert c.reader.read_calls == 0

    @pytest.mark.parametrize(
        ("replies", "typed"),
        [
            ([(b"\r", b"login: ", 0.2)], b"\r"),
            ([(b"\r", b"login: "), (b"test\r", b"Password: ", 0.2)], b"\rtest\r"),
        ],
        ids=["login-prompt-then-username", "password-prompt-then-password"],
    )
    async def test_a_close_during_a_prompt_read_drops_the_login(self, replies, typed):
        """The prompt arrives after a close took the stream: the login ends as a dropped line.

        The read was admitted before the take, so it returns its prompt, and
        the state machine's next step is a write. That write must fail as a
        lost connection (``ReadGateClosed``), never reach a nulled attribute,
        and never type a credential into a stream that is being released.
        """
        c = _client(replies)
        reader = c.reader
        login = asyncio.ensure_future(c.login_sequence())
        await wait_for_async(
            lambda: reader.written == typed and c._read_gate.in_flight == 1,
            2.0,
            interval=0.005,
            on_timeout="the login never started its prompt read",
        )
        closing = asyncio.ensure_future(c.close())
        await asyncio.sleep(0)
        assert c.writer is None, "the close took the stream"

        with pytest.raises(ReadGateClosed):
            await login
        await asyncio.wait_for(closing, 2.0)
        assert reader.written == typed, "a credential was typed after the take"
        assert not c.logged_in

    async def test_chatty_console_close_bounded_by_budget(self, monkeypatch):
        from otto.host import console as console_mod

        monkeypatch.setattr(console_mod, "CONSOLE_QUIESCE_BUDGET", 0.5)

        async def chatter(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
            with contextlib.suppress(ConnectionError):
                while not w.is_closing():
                    w.write(b"boot log line\r\n")
                    await w.drain()
                    await asyncio.sleep(0.01)
            w.close()
            with contextlib.suppress(Exception):
                await w.wait_closed()

        server = await asyncio.start_server(chatter, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        c = ConsoleClient(
            host="127.0.0.1",
            port=port,
            user="test",
            password=None,
            options=ConsoleOptions(
                server="test1",
                port=4001,
                login=False,
                logout=False,
                settle=0.0,
                echo_negotiation_timeout=0.05,
                extra={"connect_minwait": 0, "connect_maxwait": 0.05},
            ),
            name="test2",
            server="test1",
        )
        loop = asyncio.get_running_loop()
        try:
            await c.connect()
            transport = c.writer.transport
            sock = transport.get_extra_info("socket")
            assert transport in _live_console_transports
            started = loop.time()
            await c.close()
            elapsed = loop.time() - started
        finally:
            server.close()
            await server.wait_closed()

        assert elapsed <= 0.5 + 0.3
        assert sock.fileno() == -1, "otto's socket was released"
        assert transport not in _live_console_transports

    async def test_abort_after_reboot_releases_without_logout(self, monkeypatch):
        c = _logged_in_client([(b"\x04", b"\r\nlogout\r\n\r\ntest2 login: ")])
        reader, writer = c.reader, c.writer
        transport = FakeTransport()
        _attach(c, transport)
        logouts: list = []
        _spy_logout(monkeypatch, c, logouts)

        c.abort()

        assert logouts == []
        assert reader.written == b""
        assert writer.closed
        assert transport.aborted
        assert c._released.is_set()
        assert not c.alive
        assert not c.logged_in
        await asyncio.sleep(0)  # the release is observed on the loop's next turn
        assert transport not in _live_console_transports
        with pytest.raises(ReadGateClosed):
            await c._read_chunk(0.1)

    @pytest.mark.parametrize("where", ["idle_wait", "logout"])
    async def test_a_failure_before_the_release_aborts_and_never_raises(self, monkeypatch, where):
        c = _logged_in_client([(b"\x04", b"\r\nlogout\r\n\r\ntest2 login: ")])
        writer = c.writer
        transport = FakeTransport()
        _attach(c, transport)
        releases: list = []
        _spy_release(monkeypatch, releases)

        async def broken(*args):
            raise RuntimeError("scripted failure")

        if where == "idle_wait":
            monkeypatch.setattr(c._read_gate, "wait_idle", broken)
        else:
            monkeypatch.setattr(c, "logout", broken)

        await c.close()

        assert releases == [], "no drain after a failure"
        assert transport.aborted
        assert writer.closed
        assert transport not in _live_console_transports
        assert c._released.is_set()
        assert not c.logged_in

    async def test_a_stream_installed_during_the_close_is_left_alone(self):
        """A stream installed mid-close keeps its gate, login and pending release.

        The taken stream is still released in full: drained, closed, and
        unregistered once its release is observed.
        """
        from otto.host.telnet import ReadGate
        from tests.unit.host._telnet_fakes import RecordingTransport

        c = _logged_in_client([])  # logout's EOF gets no reply: it waits login_timeout
        old_reader, old_writer = c.reader, c.writer
        old_transport = RecordingTransport([])
        _attach(c, old_transport)
        close_writer = old_writer.close

        def close_closes_the_transport() -> None:
            close_writer()
            old_transport.closing = True

        old_writer.close = close_closes_the_transport
        closing = asyncio.ensure_future(c.close())
        await wait_for_async(
            lambda: old_reader.written.endswith(b"\x04"),
            2.0,
            interval=0.005,
            on_timeout="logout never typed its EOF",
        )
        fresh = _client([])  # installed as open() installs a stream
        c.reader, c.writer = fresh.reader, fresh.writer
        c._read_gate = ReadGate()
        c._released.clear()
        c._logged_in = True

        await closing

        assert c.writer is fresh.writer
        assert c._logged_in, "the new stream's login was cleared"
        assert not c._released.is_set(), "the new stream's release was marked done"
        assert not c._read_gate.closed, "the close waited on the new stream's gate"
        assert old_writer.closed, "the taken stream was closed"
        assert old_transport.aborts == 0, "the taken stream was released gracefully"
        assert old_transport not in _live_console_transports

    async def test_a_waiting_closer_returns_when_the_release_ends_despite_a_reconnect(
        self, monkeypatch
    ):
        """Close A takes; close B waits; a reconnect dials only after A's release.

        Without that wait, the reconnect would clear ``_released`` for its new
        stream while B still waited, and B would wait out the new stream's
        whole life.
        """
        from otto.host import console as console_mod

        c = _logged_in_client([])  # logout's EOF gets no reply: it waits login_timeout
        old_reader = c.reader
        events: list[str] = []
        _spy_release(monkeypatch, events)
        fresh = _client([], login=False)

        async def fake_open(host, **kw):
            events.append("dial")
            return fresh.reader, fresh.writer

        async def negotiate(writer, timeout):
            return None

        monkeypatch.setattr(console_mod, "open_telnet_connection", fake_open)
        monkeypatch.setattr(console_mod, "negotiate_no_echo", negotiate)

        async def close(name: str) -> None:
            await c.close()
            events.append(f"{name} returned")

        a = asyncio.ensure_future(close("A"))
        await wait_for_async(
            lambda: old_reader.written.endswith(b"\x04"),
            2.0,
            interval=0.005,
            on_timeout="logout never typed its EOF",
        )
        b = asyncio.ensure_future(close("B"))
        await asyncio.sleep(0)
        assert not b.done(), "B waits for A's release"
        await c.open()

        await asyncio.wait_for(asyncio.gather(a, b), 1.0)
        assert events.index("released") < events.index("dial"), "the reconnect waited"
        assert "B returned" in events
        assert c.writer is fresh.writer
        assert not c._released.is_set(), "the new stream is live"

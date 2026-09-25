"""Unit tests for :class:`otto.host.console.ConsoleClient` against scripted streams.

Each test scripts what the line shows in reply to what otto writes, so every
branch of the login state machine and of ``reset()`` is pinned without a
serial port. The fake reader hands out chunks on demand and blocks (until
the client's own deadline) when the script runs dry.
"""

import asyncio
import contextlib
import re

import pytest

from otto.host.console import ConsoleClient, ConsoleState, classify_tail, strip_ansi
from otto.host.errors import ConsoleError
from otto.host.options import ConsoleOptions
from otto.host.telnet import _live_console_transports, _register_console_transport

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
    """A minimal telnetlib3-transport stand-in for the cancel-mid-logout test."""

    def __init__(self):
        self.aborted = False

    def abort(self) -> None:
        self.aborted = True


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
    c.reader = reader
    c.writer = ScriptedWriter(reader)
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
        await c.logout()
        assert reader.written.endswith(b"Password1\r\x04"), (
            "the EOF must be typed only once the shell has spoken"
        )
        assert not reader.replies, "the login prompt came back: the EOF reached the shell"
        assert not c.logged_in

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

    async def test_abandon_releases_the_line_at_once_without_logging_out(self):
        # A replaced connection manager (reboot) cannot await: abandon() frees
        # the single-client slot synchronously and types nothing — the device
        # it would log out of has rebooted.
        c = _client([(b"\r", b"login: "), (b"test\r", b"Password: "), (b"Password1\r", b"$ ")])
        await c.login_sequence()
        reader, writer = c.reader, c.writer
        transport = FakeTransport()
        writer.transport = transport
        _register_console_transport(transport)

        c.abandon()

        assert reader.written == b"\rtest\rPassword1\r", "abandon typed on the line"
        assert writer.closed
        assert transport.aborted
        assert transport not in _live_console_transports
        assert not c.alive
        assert not c.logged_in
        c.abandon()  # idempotent

    async def test_abandon_never_raises_and_still_aborts_the_transport(self):
        c = _client([(b"\r", b"login: ")])
        transport = FakeTransport()
        c.writer.transport = transport
        _register_console_transport(transport)

        def closed_loop() -> None:
            raise RuntimeError("Event loop is closed")

        c.writer.close = closed_loop
        c.abandon()

        assert transport.aborted, "a close() that raises must not skip the abort"
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

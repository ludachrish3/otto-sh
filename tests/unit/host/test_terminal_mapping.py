"""The handshake measures how the remote terminal maps line endings, and never changes it.

A shell session runs on a tty, and a tty's ``onlcr`` (on by default) turns
every ``\\n`` the program writes into ``\\r\\n``. otto used to undo that by
deleting every ``\\r``, which also deleted the program's own carriage returns
(#713). Now the bash handshake prints one calibration line,
``printf 'X\\r\\nY\\nZ\\rW\\n'``, before READY, and the session classifies what
came back:

* ``identity``: nothing was translated (a pipe, or a tty without ``onlcr``);
* ``onlcr``: each ``\\n`` became ``\\r\\n`` and nothing else changed, so
  folding ``\\r\\n`` back to ``\\n`` is the exact inverse;
* ``unknown(<observed>)``: anything else, which otto cannot undo exactly, so
  the bytes are left as they arrived and a WARNING says so, once per session.

A frame with no calibration line is ``unmeasured``.
"""

import asyncio
import logging
from collections.abc import Coroutine

import pytest

from otto.host.command_frame import BashFrame, SessionMarkers, ZephyrFrame
from otto.host.session import HostSession, ready_pattern
from otto.host.terminal_mapping import CALIBRATION_COMMAND, measure
from otto.utils import wait_for_async
from tests.unit.host.test_session import MockSession

M = SessionMarkers.for_session("cafef00d")
R = M.ready


def identity_reply(ready: str = R) -> str:
    """What a pipe (or a tty without ``onlcr``) returns for the bash handshake."""
    return f"\nX\r\nY\nZ\rW\n{ready}\n"


def onlcr_reply(ready: str = R) -> str:
    """What a default Linux tty returns for the bash handshake."""
    return f"\r\nX\r\r\nY\r\nZ\rW\r\n{ready}\r\n"


def lossy_reply(ready: str = R) -> str:
    """A lone LF expanded but CRLF kept as it was."""
    return f"\r\nX\r\nY\r\nZ\rW\r\n{ready}\r\n"


def mapping(reply: str, ready: str) -> str:
    """The mapping :func:`measure` reads from *reply*."""
    return measure(reply, ready).mapping


class TestMeasure:
    def test_says_whether_the_calibration_line_came_back(self):
        assert measure(onlcr_reply(), R).calibrated is True
        assert measure(lossy_reply(), R).calibrated is True
        assert measure(f"\n{R}\n", R).calibrated is False


class TestTheMapping:
    def test_identity(self):
        assert mapping(identity_reply(), R) == "identity"

    def test_onlcr(self):
        assert mapping(onlcr_reply(), R) == "onlcr"

    def test_a_lossy_mapping_is_unknown_and_names_what_was_seen(self):
        # A lone LF expanded but CRLF kept as it was: X's CRLF and Y's LF now
        # look the same, so no fold can tell them apart again.
        got = mapping(lossy_reply(), R)
        assert got == "unknown('X\\r\\nY\\r\\nZ\\rW\\r\\n')"

    def test_a_cr_mapped_to_lf_is_unknown(self):
        got = mapping(f"\nX\n\nY\nZ\nW\n{R}\n", R)
        assert got.startswith("unknown(")

    def test_no_calibration_line_at_all_is_unknown(self):
        # A target without `printf`: the bare echo's newline is all that precedes READY.
        assert mapping(f"\n{R}\n", R) == "unknown('\\n')"

    def test_reads_the_output_copy_never_the_echoed_command(self):
        """On the first handshake echo is still on, so the typed line comes back first.

        The echoed copy spells the escapes as backslash text and is followed by
        ``echo <READY>``, never by READY at a line start; the output copy is
        the one right before READY.
        """
        typed = f"stty -echo 2>/dev/null; echo; {CALIBRATION_COMMAND}; echo {R}"
        reply = f"user@h:~$ {typed}\r\n\r\nX\r\r\nY\r\nZ\rW\r\n{R}\r\n"
        assert mapping(reply, R) == "onlcr"

    def test_an_sgr_sequence_before_ready_is_skipped(self):
        assert mapping(f"\nX\r\nY\nZ\rW\n\x1b[0m{R}\n", R) == "identity"


class TestBashHandshakeCarriesTheCalibrationLine:
    def test_calibration_runs_before_ready(self):
        hs = BashFrame().handshake(M)
        assert CALIBRATION_COMMAND in hs
        assert hs.index(CALIBRATION_COMMAND) < hs.index(f"echo {R[:1]}''{R[1:]}")

    def test_the_calibration_command_is_silent_when_printf_is_missing(self):
        assert CALIBRATION_COMMAND.endswith("2>/dev/null")

    def test_bash_ready_consumes_its_own_line_ending(self):
        pat = ready_pattern(M, line_ending=True)
        assert pat.search(f"\n{R}") is None
        assert pat.search(f"\n{R}\r") is None
        assert pat.search(f"\n{R}\r\n").group(0).endswith(f"{R}\r\n")
        assert pat.search(f"\n{R}\n").group(0).endswith(f"{R}\n")


async def _handshake(s: MockSession, *chunks: str) -> None:
    feed = asyncio.create_task(s.feed_after_write(*chunks))
    await s._ensure_initialized()
    await feed


def _feed_after_entry(s: MockSession, reply) -> Coroutine[None, None, None]:
    """Once ``enter_frame`` has written its handshake, answer it with ITS new READY marker.

    The written-count baseline is taken here, eagerly, before the handshake
    runs (see ``tests/unit/host/_session_feed.py`` for why).
    """
    baseline = len(s.written)

    async def _feed() -> None:
        await wait_for_async(
            lambda: len(s.written) > baseline,
            5.0,
            interval=0.005,
            on_timeout=f"enter_frame never wrote its handshake; written={s.written!r}",
        )
        s.feed(reply(s.markers.ready))

    return _feed()


class TestTheSessionMeasures:
    @pytest.mark.asyncio
    async def test_onlcr_from_a_fragmented_reply(self):
        """X, CR, LF and READY each in reads of their own: the result is the same."""
        s = MockSession()
        await s.open_transport()
        init = asyncio.create_task(s._ensure_initialized())
        await asyncio.sleep(0)
        ready = s.markers.ready
        for chunk in ["\r\n", "X", "\r", "\r", "\n", "Y\r", "\nZ", "\r", "W\r", "\n", ready, "\r"]:
            s.feed(chunk)
            await asyncio.sleep(0)
        # READY's own line ending has not arrived, so the handshake is still reading.
        await asyncio.sleep(0.05)
        assert not init.done()
        s.feed("\n")
        await asyncio.wait_for(init, 5)
        assert s.terminal_mapping == "onlcr"

    @pytest.mark.asyncio
    async def test_a_wrapped_echo_of_the_typed_line_is_never_read_as_ready(self):
        """Echo is still on at the first handshake, and a long typed line wraps.

        The terminal echoes the typed line with a carriage return where it
        wrapped. If that CR lands right before where READY sits in the typed
        text, the echo looks like READY at a line start. The typed line must
        not carry READY whole, so the session reads past the echo to the
        shell's real output and measures from it.
        """
        s = MockSession()
        await s.open_transport()
        init = asyncio.create_task(s._ensure_initialized())
        await wait_for_async(
            lambda: bool(s.written), 5.0, interval=0.005, on_timeout="no handshake written"
        )
        typed = s.written[-1].rstrip("\n")
        at = typed.rindex("echo ") + len("echo ")
        wrapped_echo = f"{typed[:at]}\r{typed[at:]}\r\n"
        s.feed(f"user@h:~$ {wrapped_echo}{onlcr_reply(s.markers.ready)}")
        await asyncio.wait_for(init, 5)
        assert s.terminal_mapping == "onlcr"

    @pytest.mark.asyncio
    async def test_identity(self):
        s = MockSession()
        await s.open_transport()
        await _handshake(s, identity_reply(s.markers.ready))
        assert s.terminal_mapping == "identity"

    @pytest.mark.asyncio
    async def test_unknown_warns_once_per_session_and_is_re_measured(self, caplog):
        s = MockSession()
        await s.open_transport()
        with caplog.at_level(logging.DEBUG, logger="otto.host.session"):
            await _handshake(s, lossy_reply(s.markers.ready))
            assert s.terminal_mapping.startswith("unknown(")
            # A second handshake on the same session (frame entry after a hook).
            # enter_frame mints new markers, so the reply is built inside the feed.
            feed = asyncio.create_task(_feed_after_entry(s, lossy_reply))
            await s.enter_frame(BashFrame())
            await feed
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1, [r.getMessage() for r in warnings]
        assert "terminal mapping: unknown(" in warnings[0].getMessage()
        assert "cannot undo exactly" in warnings[0].getMessage()
        debug = [r.getMessage() for r in caplog.records if r.levelno == logging.DEBUG]
        assert sum("terminal mapping: unknown(" in d for d in debug) == 2

    @pytest.mark.asyncio
    async def test_a_missing_calibration_line_is_warned_as_such(self, caplog):
        """No `printf` on the target: the WARNING says so, not that the terminal is lossy."""
        s = MockSession()
        await s.open_transport()
        with caplog.at_level(logging.WARNING, logger="otto.host.session"):
            await _handshake(s, f"\n{s.markers.ready}\n")
        assert s.terminal_mapping == "unknown('\\n')"
        [warning] = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert "No calibration line came back (no `printf` on the target?)" in warning
        assert "cannot undo exactly" not in warning

    @pytest.mark.asyncio
    async def test_every_handshake_re_measures(self, caplog):
        s = MockSession()
        await s.open_transport()
        await _handshake(s, onlcr_reply(s.markers.ready))
        assert s.terminal_mapping == "onlcr"
        feed = asyncio.create_task(_feed_after_entry(s, identity_reply))
        with caplog.at_level(logging.DEBUG, logger="otto.host.session"):
            await s.enter_frame(BashFrame())
        await feed
        assert s.terminal_mapping == "identity"
        assert any("terminal mapping: identity" in r.getMessage() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_a_frame_without_a_calibration_line_is_unmeasured(self):
        s = MockSession(command_frame=ZephyrFrame())
        await s.open_transport()
        # Zephyr's READY is an error line, not a line of its own; nothing waits for a newline.
        await _handshake(s, f"{s.markers.ready}: command not found")
        assert s.terminal_mapping == "unmeasured"

    @pytest.mark.asyncio
    async def test_the_host_session_handle_reads_it(self):
        s = MockSession()
        await s.open_transport()
        await _handshake(s, onlcr_reply(s.markers.ready))
        handle = HostSession(
            "n",
            s,
            log_command=lambda *_: None,
            log_output=lambda *_: None,
            deregister=lambda _n: None,
        )
        assert handle.terminal_mapping == "onlcr"

    def test_a_session_that_never_handshook_is_unmeasured(self):
        assert MockSession().terminal_mapping == "unmeasured"

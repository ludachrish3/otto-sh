"""Explicit feeder ordering for the ShellSession test doubles.

The family's old shape — ``await asyncio.sleep(0.01)`` then ``feed()`` — was
wall-clock ordering between a feeder task and the code under test. It never
flaked only because ``MockSession.feed()`` writes into an
``asyncio.StreamReader``, which buffers a feed that arrives before the read
(an emergent property nothing enforces — review §3.4: one MockSession change
turns all 65 sites flaky at once). ``feed_after_write`` replaces the sleep
with the ordering those sites actually mean: "the session has WRITTEN its
next command and is therefore about to read output."

Baseline capture is EAGER (in the plain, synchronous outer call), which is
load-bearing: ``run_cmd()`` executes synchronously through ``write_transport()`` until
it blocks in ``read_transport_until``, so a feeder task's body only starts
running AFTER the write has happened. A baseline read inside the coroutine
would already include the write it means to wait for, and the wait would
never fire. Calling ``feed_after_write(...)`` BEFORE ``run_cmd()`` (the
family's universal shape — the returned coroutine goes straight into
``asyncio.create_task``) captures the pre-command count.

On a session that never writes, the wait expires LOUDLY with a named premise
failure instead of the old shape's silent feed-into-the-void (whose eventual
symptom was an unrelated-looking read timeout or hang).
"""

import re
from collections.abc import Callable, Coroutine
from typing import TYPE_CHECKING, Any

from otto.utils import wait_for_async
from tests._fixtures.fake_shell import end_record_for, parse_bash_frame, probe_marker

if TYPE_CHECKING:
    from otto.host.command_frame import CommandFrame, SessionMarkers
    from tests.unit.host.test_session import MockSession


class FeedAfterWriteMixin:
    """Mixin for ShellSession test doubles: every double records outbound
    writes in ``self.written`` and exposes ``feed()``; the mixin turns that
    pair into an explicit feeder-ordering primitive."""

    written: list[str]

    def feed(self, data: str) -> None:  # pragma: no cover - provided by the double
        raise NotImplementedError

    def feed_after_write(
        self,
        *chunks: str,
        timeout: float = 5.0,
        then: Callable[[], None] | None = None,
    ) -> Coroutine[None, None, None]:
        """Feed *chunks*, in order, once the session has written a NEW command.

        Returns a coroutine (pass it to ``asyncio.create_task``); the
        written-count baseline is captured HERE, synchronously, before the
        command under test runs — see the module docstring for why that
        eagerness is load-bearing. Only the first chunk is ordered; later
        chunks follow immediately (one command writes once, and its output
        may arrive in as many pieces as the test likes).

        ``then`` is called after the chunks under the same ordering, for the
        transport-death doubles whose event is not content — ``feed_eof``,
        ``feed_connection_lost``, ``feed_write_broken_pipe`` — which are
        typically passed with no chunks at all.
        """
        baseline = len(self.written)

        async def _feed() -> None:
            await wait_for_async(
                lambda: len(self.written) > baseline,
                timeout,
                interval=0.005,
                on_timeout=(
                    f"session never wrote a command past baseline {baseline} — "
                    f"feeder premise failed; written={self.written!r}"
                ),
            )
            for chunk in chunks:
                self.feed(chunk)
            if then is not None:
                then()

        return _feed()


def identity_handshake_reply(ready: str) -> str:
    """The bash handshake's reply from a shell on a pipe: the calibration line, then READY.

    What every test double answers a handshake with unless the test is about
    the terminal mapping itself, so the session measures ``identity`` and the
    ``unknown`` WARNING only appears in the tests that want it.
    """
    return f"\nX\r\nY\nZ\rW\tT\n{ready}\n"


def zephyr_handshake_reply(ready: str) -> str:
    """The Zephyr handshake's reply: the shell rejects READY as an unknown command.

    Zephyr has no calibration line, so the error line is all it sends.
    """
    return f"{ready}: command not found\r\n"


def handshake_reply(frame: "CommandFrame", ready: str) -> str:
    """What a shell of *frame*'s dialect answers its handshake with (bash: on a pipe)."""
    if frame.measures_terminal_mapping:
        return identity_handshake_reply(ready)
    return zephyr_handshake_reply(ready)


def onlcr_handshake_reply(ready: str) -> str:
    """The bash handshake's reply from a default Linux tty: every LF arrives as CRLF."""
    return f"\r\nX\r\r\nY\r\nZ\rW\tT\r\n{ready}\r\n"


def writes_handshake(data: str, ready: str) -> bool:
    """Whether *data* is a handshake that prints the READY marker *ready*.

    The bash handshake types READY split across a variable (the shell prints
    it whole), so a double that answers handshakes reads the marker the
    shell would print, through :func:`~tests._fixtures.fake_shell.probe_marker`.
    """
    return probe_marker(data) == ready


async def onlcr_session(*, after_interrupt: str = "") -> "MockSession":
    """An opened bash :class:`MockSession` on a tty: its handshake measured ``onlcr``.

    Its recovery probe is answered with the recover marker, as a shell that
    took the interrupt does, so a timed-out command's ``_recover_session``
    confirms; *after_interrupt* is what it prints once the interrupt is
    written (an echoed ``^C`` and a prompt, say). Its markers are its own,
    so a test builds its chunks from ``s.markers`` once this returns.
    """
    import asyncio

    # Imported here: test_session imports this module.
    from tests.unit.host.test_session import MockSession

    class _OnlcrSession(MockSession):
        async def write_transport(self, data: str) -> None:
            await super().write_transport(data)
            if data == "\x03" and after_interrupt:
                self.feed(after_interrupt)
            if probe_marker(data) == self._recover_marker:
                self.feed(f"{self._recover_marker}0__\r\n")

    s = _OnlcrSession()
    await s.open_transport()
    feed = asyncio.create_task(s.feed_after_write(onlcr_handshake_reply(s.markers.ready)))
    await s._ensure_initialized()
    await feed
    assert s.terminal_mapping == "onlcr"
    s.written.clear()
    return s


def _frame_command(frame: "CommandFrame", m: "SessionMarkers", data: str) -> str | None:
    """The command text of a frame *frame* wrote as *data*, or ``None`` when it is not one."""
    if m.begin not in data:
        return None
    if frame.measures_terminal_mapping:  # bash family
        framed = parse_bash_frame(data)
        return framed.cmd if framed is not None else None
    parts = data.split("\r")
    return parts[1] if len(parts) > 1 else None


def _begin_lines(frame: "CommandFrame", m: "SessionMarkers") -> str:
    """What a shell of *frame*'s dialect prints when a frame's BEGIN runs."""
    if frame.measures_terminal_mapping:
        return f"{m.begin}\n"
    return f"{m.begin}: command not found\r\nuart:~$ \r\n"


def _completion(
    frame: "CommandFrame", m: "SessionMarkers", data: str, value: str, code: int
) -> str:
    """What a shell of *frame*'s dialect prints once the frame *data* finished: value, code, END."""
    framed = parse_bash_frame(data) if frame.measures_terminal_mapping else None
    if framed is not None:
        return f"{value}{end_record_for(framed, code)}\n"
    # The Zephyr shell prints its prompt after the output and after retval's line.
    return f"{value}\r\nuart:~$ \r\n{code}\r\nuart:~$ \r\n{m.end_prefix}"


def _probe_answer(frame: "CommandFrame", m: "SessionMarkers") -> str:
    """What a shell of *frame*'s dialect that took the interrupt answers the recover probe with."""
    if frame.measures_terminal_mapping:
        return f"{m.recover}0__\r\n"
    return f"{m.recover}: command not found\r\n"


def _scripted_shell_class() -> "type[MockSession]":
    """The class :func:`scripted_shell` builds, defined lazily: test_session imports this module."""
    import asyncio

    from pydantic import SecretStr

    from otto.host.command_frame import BashFrame
    from otto.host.secret_wire import reveal
    from tests.unit.host.test_session import MockSession

    class ScriptedShell(MockSession):
        """A shell that answers its frames by script: see :func:`scripted_shell`."""

        def __init__(
            self,
            *,
            frame: "CommandFrame | None",
            confirms: bool,
            hold_probe: bool,
            write_blocks_from: str | None,
            prompt_twice: str | None,
            after_interrupt: str,
            replies: "dict[str, tuple[str, int]] | None",
            hang: frozenset[str],
            refuses_fd0_dup: bool = False,
        ) -> None:
            super().__init__(command_frame=frame or BashFrame())
            self._confirms = confirms
            self._hold_probe = hold_probe
            self._write_blocks_from = write_blocks_from
            self._blocking = False
            self._prompt_twice = prompt_twice
            self._prompted = False
            self._after_interrupt = after_interrupt
            self._replies = replies or {}
            self._hang = hang
            self._refuses_fd0_dup = refuses_fd0_dup
            self._interrupted = False
            self._never = asyncio.Event()
            self._feeders: set[asyncio.Task[None]] = set()
            self.began = asyncio.Event()
            self.probe_sent = asyncio.Event()
            self.probe_reply = asyncio.Event()
            self.ops: list[str] = []
            self.written_frames: list[str] = []

        async def _reply_after(self, event: asyncio.Event, data: str) -> None:
            await event.wait()
            self.feed(data)

        async def write_transport(self, data: "str | SecretStr") -> None:
            data = reveal(data)  # what the transport writes
            self.ops.append(f"write:{data!r}")
            if self._write_blocks_from is not None and data.startswith(self._write_blocks_from):
                self._blocking = True
            if self._blocking:
                await self._never.wait()
            await super().write_transport(data)
            if writes_handshake(data, self._markers.ready) or data == self._frame.handshake(
                self._markers
            ):
                self.feed(handshake_reply(self._frame, self._markers.ready))
                return
            if data == "\x03":
                self._interrupted = True
                if self._after_interrupt:
                    self.feed(self._after_interrupt)
                return
            if self._interrupted and data == self._frame.recover(self._markers):
                self.probe_sent.set()
                if not self._confirms:
                    return
                if self._hold_probe:
                    feeder = asyncio.ensure_future(
                        self._reply_after(
                            self.probe_reply, _probe_answer(self._frame, self._markers)
                        )
                    )
                    self._feeders.add(feeder)
                    feeder.add_done_callback(self._feeders.discard)
                else:
                    self.feed(_probe_answer(self._frame, self._markers))
                return
            cmd = _frame_command(self._frame, self._markers, data)
            if cmd is not None:
                self.written_frames.append(data)
                self._interrupted = False
                self.feed(_begin_lines(self._frame, self._markers))
                if cmd in self._hang:
                    self.began.set()
                    if self._prompt_twice is not None:
                        self.feed(self._prompt_twice)
                    return
                value, code = self._replies.get(cmd, ("", 0))
                if self._refuses_fd0_dup and "3<&0" in data:
                    # BusyBox 1.16.1's ash, which will not duplicate fd 0.
                    value, code = FD0_DUP_REFUSAL
                self.feed(_completion(self._frame, self._markers, data, value, code))
                return
            if self._prompt_twice is not None and not self._prompted and self.began.is_set():
                self._prompted = True
                self.feed(self._prompt_twice)

        async def read_transport_until(self, pattern: "re.Pattern[str]") -> str:
            self.ops.append("read")
            return await super().read_transport_until(pattern)

    return ScriptedShell


FD0_DUP_REFUSAL = ("-sh: 0: Bad file descriptor", 2)
"""What BusyBox 1.16.1's ash prints, and its status, for a line duplicating fd 0 (``3<&0``).

Observed on the lab's BusyBox 1.16.1 guest over telnet, 2026-11-14."""


def scripted_shell_unopened(
    *,
    frame: "CommandFrame | None" = None,
    confirms: bool = True,
    hold_probe: bool = False,
    write_blocks_from: str | None = None,
    prompt_twice: str | None = None,
    after_interrupt: str = "",
    replies: "dict[str, tuple[str, int]] | None" = None,
    hang: frozenset[str] = frozenset({"c"}),
    refuses_fd0_dup: bool = False,
) -> Any:
    """A :func:`scripted_shell`, not yet opened: for a ``SessionManager`` session factory.

    Its handshake is answered when it is written, so whoever opens it (the
    manager, or :func:`scripted_shell`) gets a ready session. With
    *refuses_fd0_dup* a frame whose line holds ``3<&0`` is answered as
    BusyBox 1.16.1's ash answers it: :data:`FD0_DUP_REFUSAL`.
    """
    return _scripted_shell_class()(
        frame=frame,
        confirms=confirms,
        hold_probe=hold_probe,
        write_blocks_from=write_blocks_from,
        prompt_twice=prompt_twice,
        after_interrupt=after_interrupt,
        replies=replies,
        hang=hang,
        refuses_fd0_dup=refuses_fd0_dup,
    )


async def scripted_shell(
    *,
    frame: "CommandFrame | None" = None,
    confirms: bool = True,
    hold_probe: bool = False,
    write_blocks_from: str | None = None,
    prompt_twice: str | None = None,
    after_interrupt: str = "",
    replies: "dict[str, tuple[str, int]] | None" = None,
    hang: frozenset[str] = frozenset({"c"}),
) -> Any:
    """An opened, handshaken :class:`MockSession` that answers its frames by script.

    A command in *hang* is answered with its BEGIN line, sets ``began`` and
    gets nothing more until the interrupt; every other command completes with
    its *replies* value (default ``""`` and code 0) and its frame's END.
    After the interrupt (``\\x03``) the recover probe is answered when
    *confirms* (once ``probe_reply`` is set, when *hold_probe*; ``probe_sent``
    is set when the probe is written), and *after_interrupt* is fed once the
    interrupt is written. From the first write starting with
    *write_blocks_from*, every write blocks forever. *prompt_twice* is fed
    after the command's BEGIN and again after the answer is written.
    ``ops`` records ``"write:<repr>"`` and ``"read"`` per transport call;
    ``written_frames`` the command frames written.
    """
    s = scripted_shell_unopened(
        frame=frame,
        confirms=confirms,
        hold_probe=hold_probe,
        write_blocks_from=write_blocks_from,
        prompt_twice=prompt_twice,
        after_interrupt=after_interrupt,
        replies=replies,
        hang=hang,
    )
    await s.open_transport()
    await s._ensure_initialized()
    s.written.clear()
    s.ops.clear()
    return s

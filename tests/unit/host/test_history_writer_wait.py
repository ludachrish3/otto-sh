"""``_await_writer_exit`` must be a runaway guard, never a discriminator.

The shell-history e2e module samples ``~/.bash_history`` over a different
channel from the session that writes it, so it has to wait for the writing
shell to exit first. What it waits on matters: an earlier version of that fix
polled a five-second wall-clock window, which is the shape that misfires on a
slow or loaded host -- the positive control would fail spuriously and the
suppression assertions would silently stop looking after five seconds.

Waiting on "the shell's pid is gone" instead means a slow host waits longer and
still gets the right answer. The remaining timeout exists only to stop an
unbounded hang, and these tests pin that split: it returns the moment the
predicate holds, and when it never holds it RAISES rather than returning
quietly and letting a caller compare stale bytes.

The e2e module itself is bed-only (``integration``); the helper is pure
control flow over an injected host, so it is tested here where it runs on every
hostless lane.
"""

import pytest

from otto.utils import WaitTimeoutError
from tests.e2e.host import test_shell_history_e2e as hist


class _StubHost:
    """Answers ``exec`` from a scripted list; the final reply repeats forever."""

    def __init__(self, replies: list[str]) -> None:
        self._replies = replies
        self.calls = 0

    async def exec(self, command: str) -> object:
        self.calls += 1
        value = self._replies[min(self.calls - 1, len(self._replies) - 1)]
        return type("_Result", (), {"value": value})()


@pytest.mark.asyncio
async def test_returns_on_the_first_probe_when_the_shell_is_already_gone() -> None:
    """The common case costs exactly one round trip — measured at 0.12s on the bed."""
    host = _StubHost(["GONE"])

    await hist._await_writer_exit(host, "4242", "test1")

    assert host.calls == 1


@pytest.mark.asyncio
async def test_keeps_waiting_while_the_shell_is_still_alive() -> None:
    """A slow host is waited OUT, not failed: the predicate decides, not a clock."""
    host = _StubHost(["ALIVE", "ALIVE", "ALIVE", "GONE"])

    await hist._await_writer_exit(host, "4242", "test1")

    assert host.calls == 4


@pytest.mark.asyncio
async def test_raises_and_names_the_pid_when_the_shell_never_exits(monkeypatch) -> None:
    """The bound must FAIL LOUDLY, not return and let the caller compare stale bytes.

    This is the assertion that keeps the helper honest. If it ever returned on
    timeout instead of raising, every caller would silently sample a file whose
    write had not landed -- turning the suppression tests green against a leak,
    which is the exact defect the wait was introduced to prevent.

    The exception type is otto's own ``WaitTimeoutError``, not a bare
    ``AssertionError``: the wait is built on ``otto.utils.wait_for_async``
    rather than a hand-rolled deadline loop, which the
    ``no-handrolled-deadline-poll`` lint rule requires.
    """
    monkeypatch.setattr(hist, "_WRITER_EXIT_TIMEOUT", 0.2)
    monkeypatch.setattr(hist, "_WRITER_POLL", 0.01)
    host = _StubHost(["ALIVE"])

    with pytest.raises(WaitTimeoutError, match="4242"):
        await hist._await_writer_exit(host, "4242", "test1")

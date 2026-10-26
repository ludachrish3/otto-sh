"""Live CLI log sinks whose console is held back until the main thread has printed.

A log record reaches the terminal on the listener thread; the CLI's own output
is printed on the main thread. On the CLI path the stream proxies put that
output on the listener's queue too, behind the records logged before it.
:func:`live_sinks_with_a_held_console` makes the listener slow on purpose, so a
test can show which one lands first without depending on how fast either
thread runs: with the queue, the record always does; without it (writes go
straight to the stream), the direct output always does.

Both process streams are one in-memory *terminal*, so the order across stdout
and stderr can be read from one buffer, as a terminal shows it. The proxies
queue only for the process's own streams, and a test's buffer never is one,
so the fixture names the terminal as the process stream through
``management._process_streams``, the one seam for that. ``queued=False``
leaves the seam alone: the proxies are installed but write directly, which is
the control that proves a race test can fail.
"""

import io
import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from otto.logger import management
from otto.utils import WaitTimeoutError, wait_for


class Terminal(io.StringIO):
    """An in-memory terminal: one text buffer that both process streams write to."""

    def isatty(self) -> bool:
        return False


class AsciiTerminal(io.TextIOWrapper):
    """A terminal whose locale is ASCII (``LANG=C``): its encoding is ``ascii``."""

    def __init__(self) -> None:
        super().__init__(io.BytesIO(), encoding="ascii", errors="strict", write_through=True)

    def getvalue(self) -> str:
        buffer = self.buffer
        assert isinstance(buffer, io.BytesIO)
        return buffer.getvalue().decode("ascii")


# A direct print or a drain arrives within microseconds of the record; the
# limit only ends a test that failed before it released the hold, so that its
# teardown (which stops the listener and waits for it) cannot hang.
HOLD_LIMIT_SECONDS = 30.0


class HoldUntilDirectPrint(logging.Handler):
    """A sink placed first in the fan-out that holds every record back.

    It lets a record through only once the main thread has either finished its
    direct print (``printed``) or started to drain the queue (``draining``).
    So when direct output skips the queue it always lands first; when it joins
    the queue, or a drain waits for the queue, the record always does. After
    *hold_limit* seconds it lets the record through anyway (see
    ``HOLD_LIMIT_SECONDS``).
    """

    def __init__(
        self, printed: threading.Event, draining: threading.Event, hold_limit: float
    ) -> None:
        super().__init__()
        self._printed = printed
        self._draining = draining
        self._hold_limit = hold_limit

    def emit(self, record: logging.LogRecord) -> None:
        try:
            wait_for(
                lambda: self._printed.is_set() or self._draining.is_set(),
                self._hold_limit,
                interval=0.001,
                on_timeout="the test failed before releasing the held console",
            )
        except WaitTimeoutError:
            return  # let the record through, so teardown cannot hang


@dataclass
class HeldSinks:
    """The live sinks of :func:`live_sinks_with_a_held_console`.

    ``printed`` is the event the test sets once its direct output has returned
    (:meth:`release` sets it); ``draining`` is set when a
    :func:`~otto.logger.management.drain_log_queue` starts.
    """

    terminal: Any
    printed: threading.Event
    draining: threading.Event

    def release(self) -> str:
        """Release the held sink, flush everything still queued, and return the terminal."""
        self.printed.set()
        listener = management._state.listener
        assert listener is not None
        listener.stop()
        return self.terminal.getvalue()


def quiet_pytest_live_logging(monkeypatch: pytest.MonkeyPatch, config: pytest.Config) -> None:
    """Keep pytest's live-log handler from swapping ``sys.stdout`` back mid-test.

    With ``log_cli`` on, that handler suspends and resumes pytest's capture
    around every record it prints, and resuming puts pytest's own stream back
    in ``sys.stdout``, over the proxy. Raising its level past every record
    keeps it from ever emitting during the test. The handler is taken from
    pytest's logging plugin, never guessed, so a change in pytest fails here.
    """
    plugin = config.pluginmanager.get_plugin("logging-plugin")
    assert plugin is not None, "pytest's logging plugin is not registered"
    handler = plugin.log_cli_handler
    assert isinstance(handler, logging.Handler), handler
    monkeypatch.setattr(handler, "level", logging.CRITICAL + 1)


def live_sinks_with_a_held_console(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    config: pytest.Config,
    *,
    queued: bool = True,
    terminal: Any = None,
    hold_limit: float = HOLD_LIMIT_SECONDS,
) -> HeldSinks:
    """Install the CLI's sinks and stream proxies over a *terminal*, with a held sink first.

    :func:`~otto.logger.management.drain_log_queue` is wrapped so the held
    sink can see a drain start; every caller looks it up at call time, so the
    wrapper is what they call. *config* is the test's pytest config (the
    ``pytestconfig`` fixture), for :func:`quiet_pytest_live_logging`. The caller
    resets :mod:`otto.logger.management` around the test.
    """
    quiet_pytest_live_logging(monkeypatch, config)
    if terminal is None:
        terminal = Terminal()
    monkeypatch.setattr("sys.stdout", terminal)
    monkeypatch.setattr("sys.stderr", terminal)
    if queued:
        monkeypatch.setattr(
            management,
            "_process_streams",
            lambda: management._ProcessStreams(stdout=terminal, stderr=terminal),
        )
    management.init_cli_logging(xdir=tmp_path, log_level="INFO", keep_days=7)
    # install_sinks, not create_output_dir: tests/unit/cli patches the latter
    # out. The same call create_output_dir makes.
    output_dir = tmp_path / "run"
    output_dir.mkdir(parents=True)
    management.install_sinks(output_dir, proxy_streams=True)
    listener = management._state.listener
    assert listener is not None
    printed = threading.Event()
    draining = threading.Event()
    real_drain = management.drain_log_queue

    def drain_and_say_so(*args: Any, **kwargs: Any) -> bool:
        draining.set()
        return real_drain(*args, **kwargs)

    monkeypatch.setattr(management, "drain_log_queue", drain_and_say_so)
    listener.handlers = (HoldUntilDirectPrint(printed, draining, hold_limit), *listener.handlers)
    return HeldSinks(terminal=terminal, printed=printed, draining=draining)

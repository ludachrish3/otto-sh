"""Queued console log lines print before the direct output that follows them.

A log record reaches the terminal on the listener thread; the CLI's own
output (a dry-run plan, a report, a notice) is written on the main thread. On
the CLI path the stream proxies put that output on the same queue, so the
listener writes both, in the order they were produced. These tests make the
listener slow on purpose and check that order for every route the CLI prints
through; each also runs with the proxies writing directly (``direct``), where
the output must overtake the line, which proves the held sink can tell the two
apart. The rest pins :func:`~otto.logger.management.drain_log_queue`, which the
places that write past the proxies (a raw descriptor write) still call: that a
busy logger cannot hold it back, and that it can never hang.
"""

import logging
import threading
from collections.abc import Callable
from logging.handlers import QueueHandler
from queue import Queue
from typing import Any

import pytest
import rich
import typer

from otto.cli import docker as docker_cli
from otto.console import CONSOLE
from otto.docker.observe import HostOutput, ObserveReport
from otto.logger import management
from otto.result import CommandResult
from otto.utils import Status
from tests._fixtures.held_console import live_sinks_with_a_held_console


@pytest.fixture(autouse=True)
def _clean_management():
    management.reset()
    yield
    management.reset()


def _rich_print() -> None:
    rich.print("lab: direct output")


def _rich_console_print() -> None:
    rich.get_console().print("lab: direct output", markup=False, soft_wrap=True)


def _plain_print() -> None:
    print("lab: direct output")  # noqa: T201 — the route under test


def _typer_echo_to_stderr() -> None:
    typer.echo("lab: direct output", err=True)


def _otto_console_print() -> None:
    CONSOLE.print("lab: direct output", highlight=False)


def _docker_observe() -> None:
    result = CommandResult(Status.Success, value="lab: direct output\n", retcode=0)
    docker_cli._render_observe(ObserveReport([HostOutput("h1", "docker ps", result)]), header=False)


def _interactive_notice() -> None:
    from otto.host.interact import _print_stderr

    _print_stderr("lab: direct output")


_ROUTES = [
    pytest.param(_rich_print, id="rich.print"),
    pytest.param(_rich_console_print, id="rich.get_console().print"),
    pytest.param(_plain_print, id="print"),
    pytest.param(_typer_echo_to_stderr, id="typer.echo(err=True)"),
    pytest.param(_otto_console_print, id="otto CONSOLE (monitor urls)"),
    pytest.param(_docker_observe, id="docker observe"),
    pytest.param(_interactive_notice, id="interact notice"),
]


@pytest.mark.parametrize("direct_print", _ROUTES)
def test_a_queued_log_line_prints_before_the_direct_output_after_it(
    pytestconfig, tmp_path, monkeypatch, direct_print: Callable[[], None]
):
    sinks = live_sinks_with_a_held_console(tmp_path, monkeypatch, pytestconfig)

    logging.getLogger("otto").info("fleet of interest: logged first")
    direct_print()
    out = sinks.release()

    assert "fleet of interest: logged first" in out
    assert "lab: direct output" in out
    assert out.index("fleet of interest: logged first") < out.index("lab: direct output"), out


@pytest.mark.parametrize("direct_print", _ROUTES)
def test_without_the_queue_the_direct_output_overtakes_the_line(
    pytestconfig, tmp_path, monkeypatch, direct_print: Callable[[], None]
):
    """The control: the proxies write directly, and the held line lands second.

    So the test above is red whenever its route skips the queue.
    """
    sinks = live_sinks_with_a_held_console(tmp_path, monkeypatch, pytestconfig, queued=False)

    logging.getLogger("otto").info("fleet of interest: logged first")
    direct_print()
    out = sinks.release()

    assert out.index("lab: direct output") < out.index("fleet of interest: logged first"), out


def test_drain_without_a_listener_is_a_no_op():
    assert management._state.listener is None
    assert management.drain_log_queue() is True


def test_drain_with_nothing_pending_puts_no_barrier(monkeypatch):
    """Nothing queued and nothing being written: return at once, no round trip to the listener."""
    collected = _Collect()
    log_queue = _bare_listener(collected)
    log_queue.put_nowait(_record("written before the drain"))
    log_queue.join()
    puts: list[Any] = []
    real_put = log_queue.put_nowait
    monkeypatch.setattr(log_queue, "put_nowait", lambda item: puts.append(item) or real_put(item))

    assert management.drain_log_queue(timeout=3600) is True

    assert puts == []


def test_drain_still_waits_for_a_record_being_written():
    """A record the listener has taken but not finished still counts as pending."""
    handler = _Wedged()
    log_queue = _bare_listener(handler)
    log_queue.put_nowait(_record("being written"))
    try:
        # Taken off the queue and inside the sink: the queue itself is empty.
        assert handler.entered.wait(timeout=10)
        assert log_queue.qsize() == 0
        assert management.drain_log_queue(timeout=0.05) is False
    finally:
        handler.unstick.set()


def test_drain_after_the_listener_stopped_is_a_no_op(tmp_path):
    management.init_cli_logging(xdir=tmp_path, log_level="INFO", keep_days=7)
    management.create_output_dir("run")
    logging.getLogger("otto").info("flushed by stop")
    listener = management._state.listener
    assert listener is not None
    listener.stop()
    assert management.drain_log_queue() is True


def _bare_listener(*handlers: logging.Handler) -> "Queue[Any]":
    """A started listener in module state, fed by a queue the test writes to directly."""
    log_queue: Queue[Any] = Queue(-1)
    listener = management._SinkListener(log_queue, *handlers, respect_handler_level=True)
    management._state.listener = listener
    listener.start()
    return log_queue


def _record(msg: str) -> logging.LogRecord:
    return logging.LogRecord("otto", logging.INFO, __file__, 0, msg, None, None)


class _Collect(logging.Handler):
    """Every object the sinks were handed, in order."""

    def __init__(self) -> None:
        super().__init__()
        self.seen: list[Any] = []

    def handle(self, record: Any) -> bool:
        self.seen.append(record)
        return True


def _announce_drains(monkeypatch: pytest.MonkeyPatch) -> threading.Event:
    """Wrap drain_log_queue so a sink can see that a drain has started."""
    draining = threading.Event()
    real_drain = management.drain_log_queue

    def drain_and_say_so(*args: Any, **kwargs: Any) -> bool:
        draining.set()
        return real_drain(*args, **kwargs)

    monkeypatch.setattr(management, "drain_log_queue", drain_and_say_so)
    return draining


class _KeepTheQueueBusy(logging.Handler):
    """Stands in for a thread that never stops logging.

    Every record it handles puts another one behind it, so from the first
    record on the queue is never empty: a drain that waited for an empty queue
    would wait out its whole deadline. It stops feeding once told to.
    """

    def __init__(self) -> None:
        super().__init__()
        self.log_queue: Queue[Any] | None = None
        self.stop = threading.Event()
        self.handled = 0

    def emit(self, record: logging.LogRecord) -> None:
        assert self.log_queue is not None
        self.handled += 1
        if not self.stop.is_set():
            self.log_queue.put_nowait(_record(f"more {self.handled}"))


def test_a_logger_that_never_stops_cannot_hold_a_drain_back():
    busy = _KeepTheQueueBusy()
    collected = _Collect()
    log_queue = _bare_listener(busy, collected)
    busy.log_queue = log_queue
    log_queue.put_nowait(_record("first"))
    try:
        # Far below the test's own timeout, far above what reaching the barrier
        # takes: a drain waiting for an empty queue returns False here.
        assert management.drain_log_queue(timeout=10) is True
        assert log_queue.unfinished_tasks > 0  # the queue never emptied
    finally:
        busy.stop.set()
    log_queue.join()
    # The barrier was handled by the listener and never handed to a sink.
    assert all(isinstance(record, logging.LogRecord) for record in collected.seen)
    assert collected.seen[0].getMessage() == "first"


class _DieOnceTheDrainStarts(logging.Handler):
    """Kills the listener thread mid-drain: the barrier is never reached."""

    def __init__(self, draining: threading.Event) -> None:
        super().__init__()
        self.draining = draining
        self.abort = threading.Event()

    def handle(self, record: logging.LogRecord) -> bool:
        while not (self.draining.is_set() or self.abort.is_set()):
            self.abort.wait(0.001)
        raise RuntimeError("the listener thread dies here")


def test_drain_returns_when_the_listener_thread_dies_mid_drain(monkeypatch):
    # The dying thread's traceback is the point of the test, not a failure.
    monkeypatch.setattr(threading, "excepthook", lambda args: None)
    handler = _DieOnceTheDrainStarts(_announce_drains(monkeypatch))
    log_queue = _bare_listener(handler)
    log_queue.put_nowait(_record("never finished"))
    try:
        # A deadline far beyond the test's own timeout: only the liveness check
        # can end this wait, which is what is being pinned.
        assert management.drain_log_queue(timeout=3600) is False
        # The record and the barrier both still pending: Queue.join() would block forever.
        assert log_queue.unfinished_tasks == 2
    finally:
        handler.abort.set()  # a failed drain must not leave the thread spinning
        management._state.listener = None  # its thread is gone; nothing left to stop


class _Wedged(logging.Handler):
    """A sink stuck on a write, as a hung NFS mount would leave it."""

    def __init__(self) -> None:
        super().__init__()
        self.unstick = threading.Event()
        self.entered = threading.Event()

    def emit(self, record: logging.LogRecord) -> None:
        self.entered.set()
        self.unstick.wait()


def test_drain_gives_up_at_its_deadline_on_a_wedged_sink():
    handler = _Wedged()
    log_queue = _bare_listener(handler)
    log_queue.put_nowait(_record("stuck"))
    try:
        assert management.drain_log_queue(timeout=0.05) is False
    finally:
        handler.unstick.set()
    assert management.drain_log_queue(timeout=3600) is True


class _DrainsFromTheListenerThread(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.result: bool | None = None

    def emit(self, record: logging.LogRecord) -> None:
        self.result = management.drain_log_queue(timeout=3600)


def test_drain_on_the_listener_thread_returns_instead_of_waiting_on_itself():
    handler = _DrainsFromTheListenerThread()
    log_queue = _bare_listener(handler)
    log_queue.put_nowait(_record("a sink that prints directly"))
    assert management.drain_log_queue(timeout=3600) is True
    assert handler.result is False  # it would have waited on its own record


def test_the_cli_queue_handler_and_listener_keep_the_task_done_contract(tmp_path):
    """Every record otto queues is counted, and the listener marks each one done.

    The barrier included: a drain leaves ``Queue.join()`` able to return.
    """
    management.init_cli_logging(xdir=tmp_path, log_level="INFO", keep_days=7)
    management.create_output_dir("run")
    listener = management._state.listener
    assert isinstance(listener, management._SinkListener)
    queue_handlers = [
        h for h in logging.getLogger().handlers if isinstance(h, management._InProcessQueueHandler)
    ]
    assert len(queue_handlers) == 1
    assert isinstance(queue_handlers[0], QueueHandler)
    assert queue_handlers[0].queue is listener.queue

    logging.getLogger("otto").info("counted")
    management.record_command_failure(RuntimeError("boom"), "files only")
    assert management.drain_log_queue(timeout=3600) is True
    assert isinstance(listener.queue, Queue)
    listener.queue.join()
    assert listener.queue.unfinished_tasks == 0

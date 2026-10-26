"""The CLI's stream proxies: the listener thread is the terminal's only writer.

On the CLI path ``sys.stdout`` and ``sys.stderr`` are ``_QueuedStream``
proxies that put each finished piece of text on the log queue, so direct
output and log lines reach the terminal in the order they were produced (see
``tests/unit/logger/test_log_queue_drain.py`` for the order of each print
route). These pin the rest of the contract: what writes directly instead
(captures, the listener thread, a failed stream, a held terminal), how a write
error is survived, what is restored, and the corners click and Rich reach
through a stream's attributes.

The proxies queue only for the process's own streams, so the queue-path tests
name their buffer as the process stream through ``management._process_streams``
(see ``tests/_fixtures/held_console.py``).
"""

import datetime
import io
import logging
import os
import select
import signal
import subprocess
import sys
import threading
import weakref
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, NamedTuple

import pytest
import rich
import typer
from rich.console import Console
from typer._click import _compat as click_compat
from typer.testing import CliRunner

from otto import lifecycle
from otto.logger import management
from tests._fixtures.held_console import (
    AsciiTerminal,
    Terminal,
    live_sinks_with_a_held_console,
    quiet_pytest_live_logging,
)


@pytest.fixture(autouse=True)
def _clean_management():
    management.reset()
    yield
    management.reset()


def _install_over(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    config: pytest.Config,
    stdout: Any,
    stderr: Any,
) -> None:
    """The CLI's sinks and proxies over *stdout*/*stderr*, both named as the process streams."""
    quiet_pytest_live_logging(monkeypatch, config)
    monkeypatch.setattr("sys.stdout", stdout)
    monkeypatch.setattr("sys.stderr", stderr)
    monkeypatch.setattr(
        management,
        "_process_streams",
        lambda: management._ProcessStreams(stdout=stdout, stderr=stderr),
    )
    management.init_cli_logging(xdir=tmp_path, log_level="INFO", keep_days=7)
    management.create_output_dir("run")


def _proxy(name: str) -> management._QueuedStream:
    stream = getattr(sys, name)
    assert isinstance(stream, management._QueuedStream), stream
    return stream


def _queues(name: str) -> bool:
    return _proxy(name)._queue() is not None


def _ordered(out: str, *pieces: str) -> bool:
    return all(piece in out for piece in pieces) and [out.index(p) for p in pieces] == sorted(
        out.index(p) for p in pieces
    )


# ── scope: only the CLI path installs the proxies ───────────────────────────


def test_the_embedder_install_leaves_the_process_streams_alone(tmp_path, monkeypatch):
    terminal = Terminal()
    monkeypatch.setattr("sys.stdout", terminal)
    monkeypatch.setattr("sys.stderr", terminal)

    management.install(output_dir=tmp_path / "embedded")

    assert sys.stdout is terminal
    assert sys.stderr is terminal
    assert management._state.proxy_streams is False


def test_the_cli_path_installs_the_proxies(pytestconfig, tmp_path, monkeypatch):
    out, err = Terminal(), Terminal()
    _install_over(tmp_path, monkeypatch, pytestconfig, out, err)

    assert _proxy("stdout")._real is out
    assert _proxy("stderr")._real is err
    assert management._state.proxy_streams is True
    assert management._state.stream_proxies == {"stdout": sys.stdout, "stderr": sys.stderr}


def test_an_embedder_install_inside_the_cli_keeps_the_proxies_queueing(
    pytestconfig, tmp_path, monkeypatch
):
    """A new directory retires the CLI's listener; the new one serves the same proxies."""
    out = Terminal()
    _install_over(tmp_path, monkeypatch, pytestconfig, out, out)
    proxy = sys.stdout

    management.install(output_dir=tmp_path / "embedded")

    assert sys.stdout is proxy
    assert management._state.proxy_streams is True
    assert _queues("stdout")
    print("served by the new listener")  # noqa: T201 — the route under test
    assert management.drain_log_queue(timeout=10) is True
    assert "served by the new listener" in out.getvalue()
    management.reset()
    assert sys.stdout is out


# ── captures read their whole output at once ────────────────────────────────


def test_a_capture_gets_its_output_at_once_and_nothing_queues(
    pytestconfig, tmp_path, monkeypatch, capsys
):
    quiet_pytest_live_logging(monkeypatch, pytestconfig)
    queued: list[Any] = []
    monkeypatch.setattr(management, "_write_console_text", queued.append)
    management.init_cli_logging(xdir=tmp_path, log_level="INFO", keep_days=7)
    management.create_output_dir("run")
    assert isinstance(sys.stdout, management._QueuedStream)

    print("printed")  # noqa: T201 — the route under test
    typer.echo("echoed", err=True)
    rich.print("rich")

    captured = capsys.readouterr()
    assert "printed" in captured.out
    assert "rich" in captured.out
    assert "echoed" in captured.err
    assert queued == []


def test_a_closed_stdout_stays_none_and_print_stays_silent(pytestconfig, tmp_path, monkeypatch):
    """Started with descriptor 1 closed, Python has no stdout; a print does nothing."""
    err = Terminal()
    _install_over(tmp_path, monkeypatch, pytestconfig, None, err)

    assert sys.stdout is None
    assert "stdout" not in management._state.stream_proxies
    print("nowhere")  # noqa: T201 — the route under test
    assert management.drain_log_queue(timeout=10) is True
    assert management._state.dead_routes == {}
    assert err.getvalue() == ""
    assert _queues("stderr")


def test_a_proxy_never_queues_for_a_process_stream_that_is_none(
    pytestconfig, tmp_path, monkeypatch
):
    out = Terminal()
    _install_over(tmp_path, monkeypatch, pytestconfig, out, out)
    monkeypatch.setattr(
        management._state, "process_streams", management._ProcessStreams(stdout=None, stderr=out)
    )
    proxy = _proxy("stdout")
    monkeypatch.setattr(proxy, "_real", None)

    assert proxy._queue() is None


_PRINTS_WITH_DESCRIPTOR_1_CLOSED = """
import sys
from pathlib import Path

from otto.logger import management

assert sys.stdout is None and sys.__stdout__ is None
management.init_cli_logging(xdir=Path(sys.argv[1]), log_level="INFO", keep_days=7)
management.create_output_dir("run")
print("a")
management.drain_log_queue()
print("b")
"""


def test_a_command_started_with_descriptor_1_closed_prints_nothing_and_succeeds(tmp_path):
    """``otto ... >&-``: as without the proxies, a print is a silent no-op."""
    run = subprocess.run(
        [
            "sh",
            "-c",
            'exec "$0" -c "$1" "$2" >&-',
            sys.executable,
            _PRINTS_WITH_DESCRIPTOR_1_CLOSED,
            str(tmp_path),
        ],
        capture_output=True,
        env=_child_environ(),
        timeout=60,
        check=False,
    )

    assert run.stderr == b"", run.stderr.decode(errors="replace")
    assert run.returncode == 0


def _cli_app(xdirs: Iterator[Path]) -> typer.Typer:
    """A command on the CLI's logging path; each invocation takes the next of *xdirs*."""
    app = typer.Typer()

    @app.command()
    def run() -> None:
        management.init_cli_logging(xdir=next(xdirs), log_level="INFO", keep_days=7)
        management.create_output_dir("run")
        rich.print("rich line")
        typer.echo("echo line")
        # No newline and no flush: it reaches the runner's buffer only through
        # the flush the runner makes before it reads.
        print("unflushed tail", end="")  # noqa: T201 — the route under test

    return app


class _FrozenClock(datetime.datetime):
    """``datetime`` whose ``now()`` never moves: two output directories in one millisecond."""

    @classmethod
    def now(cls, tz: "datetime.tzinfo | None" = None) -> "_FrozenClock":
        return cls(2026, 10, 25, 12, 0, 0, 123000, tzinfo=tz)


def test_cli_runner_reads_back_the_whole_output_twice_in_one_process(tmp_path, monkeypatch):
    queued: list[Any] = []
    monkeypatch.setattr(management, "_write_console_text", queued.append)
    # Both invocations name their output directory in the same millisecond, as
    # a fast machine can: the test must not depend on the clock moving.
    monkeypatch.setattr(management, "datetime", _FrozenClock)
    runner = CliRunner()
    # One root per invocation, so their directories never share a name.
    app = _cli_app(iter([tmp_path / "first", tmp_path / "second"]))

    for _ in range(2):
        result = runner.invoke(app, [])
        assert result.exit_code == 0, result.output
        assert "rich line" in result.output
        assert "echo line" in result.output
        assert "unflushed tail" in result.output
    assert queued == []


# ── the listener thread, and a held terminal ─────────────────────────────────


class _WritesFromTheListener(logging.Handler):
    """A sink that writes to ``sys.stdout`` itself, as the console handler does."""

    def __init__(self, terminal: Terminal) -> None:
        super().__init__()
        self.terminal = terminal
        self.seen_at_once: bool | None = None

    def emit(self, record: logging.LogRecord) -> None:
        sys.stdout.write("from the listener\n")
        self.seen_at_once = "from the listener" in self.terminal.getvalue()


def test_a_write_on_the_listener_thread_goes_straight_through(pytestconfig, tmp_path, monkeypatch):
    out = Terminal()
    _install_over(tmp_path, monkeypatch, pytestconfig, out, out)
    listener = management._state.listener
    assert listener is not None
    sink = _WritesFromTheListener(out)
    listener.handlers = (sink, *listener.handlers)

    logging.getLogger("otto").info("handled on the listener thread")
    assert management.drain_log_queue(timeout=10) is True

    assert sink.seen_at_once is True


def test_a_prompt_holds_the_terminal_after_draining_the_queue(pytestconfig, tmp_path, monkeypatch):
    """The question is on screen, after the line logged before it, when ``input()`` waits."""
    sinks = live_sinks_with_a_held_console(tmp_path, monkeypatch, pytestconfig)
    on_screen_at_input: list[str] = []

    def reading(prompt: str) -> str:
        on_screen_at_input.append(sinks.terminal.getvalue())
        # Held by construction, not by timing: a question queued now could
        # still reach the screen after input() has started waiting.
        assert management._state.terminal_holds > 0
        assert _proxy("stdout")._queue() is None
        return "answer"

    monkeypatch.setattr("typer._click.termui.visible_prompt_func", reading)
    logging.getLogger("otto").info("logged before the question")

    with management.direct_terminal():
        assert typer.prompt("Product name") == "answer"
    sinks.release()

    [screen] = on_screen_at_input
    assert _ordered(screen, "logged before the question", "Product name"), screen
    assert management._state.terminal_holds == 0


def test_holds_nest_and_the_last_one_out_lets_the_queue_back(pytestconfig, tmp_path, monkeypatch):
    out = Terminal()
    _install_over(tmp_path, monkeypatch, pytestconfig, out, out)

    with management.direct_terminal():
        with management.direct_terminal():
            assert not _queues("stdout")
        assert not _queues("stdout")
    assert _queues("stdout")


def test_a_reset_inside_a_held_block_leaves_no_hold_behind(pytestconfig, tmp_path, monkeypatch):
    out = Terminal()
    _install_over(tmp_path, monkeypatch, pytestconfig, out, out)

    with management.direct_terminal():
        management.reset()
    assert management._state.terminal_holds == 0

    _install_over(tmp_path / "again", monkeypatch, pytestconfig, out, out)
    assert _queues("stdout"), "a hold that outlived its reset would keep the proxies direct"


def test_a_race_test_that_fails_before_release_cannot_wedge_the_reset(
    pytestconfig, tmp_path, monkeypatch
):
    """The held sink lets a record through after its limit, so teardown's stop returns."""
    sinks = live_sinks_with_a_held_console(tmp_path, monkeypatch, pytestconfig, hold_limit=0.2)
    logging.getLogger("otto").info("held, and never released")
    resetting = threading.Thread(target=management.reset, daemon=True)

    resetting.start()
    resetting.join(timeout=10)
    wedged = resetting.is_alive()
    sinks.printed.set()  # unstick a wedged listener so the thread can end
    resetting.join(timeout=10)

    assert not wedged, "reset() hung behind a held record nobody released"


# ── write errors never kill the listener ─────────────────────────────────────


def test_a_closed_stream_marks_its_route_dead_and_says_so_on_the_other(
    pytestconfig, tmp_path, monkeypatch
):
    out, err = Terminal(), Terminal()
    _install_over(tmp_path, monkeypatch, pytestconfig, out, err)
    out.close()

    print("dropped")  # noqa: T201 — the route under test
    assert management.drain_log_queue(timeout=10) is True

    listener = management._state.listener
    assert listener is not None
    assert listener._thread is not None
    assert listener._thread.is_alive()
    assert management._state.dead_routes["stdout"].cause is ValueError
    notice = err.getvalue()
    assert notice.startswith("--- console output error ---\n"), notice
    assert "ValueError" in notice
    # Later writes go to the stream directly, so the failure is the caller's.
    assert not _queues("stdout")
    with pytest.raises(ValueError, match="closed"):
        print("direct now")  # noqa: T201 — the route under test
    # The other stream keeps its queue.
    assert _queues("stderr")
    typer.echo("still queued", err=True)
    assert management.drain_log_queue(timeout=10) is True
    assert "still queued" in err.getvalue()


def test_an_unencodable_line_on_an_ascii_terminal_is_reported_once(
    pytestconfig, tmp_path, monkeypatch
):
    """Without the queue it raised on the caller; queued, the notice is its trace."""
    out, err = AsciiTerminal(), Terminal()
    _install_over(tmp_path, monkeypatch, pytestconfig, out, err)

    print("caf\u00e9")  # noqa: T201 — the route under test
    assert management.drain_log_queue(timeout=10) is True

    assert management._state.dead_routes["stdout"].cause is UnicodeEncodeError
    assert err.getvalue().count("--- console output error ---") == 1
    assert "UnicodeEncodeError" in err.getvalue()
    # The next one goes direct and raises on the caller, as it did unqueued.
    with pytest.raises(UnicodeEncodeError):
        print("caf\u00e9 again")  # noqa: T201 — the route under test
    assert err.getvalue().count("--- console output error ---") == 1


class _BrokenPipe(Terminal):
    def write(self, text: str) -> int:
        raise BrokenPipeError(32, "Broken pipe")


def test_a_broken_pipe_marks_the_route_dead_silently(pytestconfig, tmp_path, monkeypatch):
    out, err = _BrokenPipe(), Terminal()
    _install_over(tmp_path, monkeypatch, pytestconfig, out, err)

    print("into a closed pipe")  # noqa: T201 — the route under test
    assert management.drain_log_queue(timeout=10) is True

    assert management._state.dead_routes["stdout"].cause is BrokenPipeError
    assert err.getvalue() == ""
    assert _queues("stderr")
    with pytest.raises(BrokenPipeError):
        print("the caller sees it now")  # noqa: T201 — the route under test


class _CountingBrokenPipe(Terminal):
    def __init__(self) -> None:
        super().__init__()
        self.tries = 0

    def write(self, text: str) -> int:
        self.tries += 1
        raise BrokenPipeError(32, "Broken pipe")


class _HoldEverything(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.let_go = threading.Event()

    def emit(self, record: logging.LogRecord) -> None:
        self.let_go.wait()


def test_text_still_queued_for_a_closed_pipe_is_dropped_untried(
    pytestconfig, tmp_path, monkeypatch
):
    out, err = _CountingBrokenPipe(), Terminal()
    _install_over(tmp_path, monkeypatch, pytestconfig, out, err)
    listener = management._state.listener
    assert listener is not None
    holder = _HoldEverything()
    listener.handlers = (holder,)
    logging.getLogger("otto").info("holds the listener")
    try:
        for line in ["first", "second", "third"]:
            print(line)  # noqa: T201 — all three queue behind the held record
    finally:
        holder.let_go.set()
    assert management.drain_log_queue(timeout=10) is True

    assert out.tries == 1


def test_no_notice_when_the_other_route_is_dead_too(pytestconfig, tmp_path, monkeypatch):
    out, err = Terminal(), _BrokenPipe()
    _install_over(tmp_path, monkeypatch, pytestconfig, out, err)
    typer.echo("into a closed pipe", err=True)
    assert management.drain_log_queue(timeout=10) is True
    out.close()

    print("dropped")  # noqa: T201 — the route under test
    assert management.drain_log_queue(timeout=10) is True

    assert set(management._state.dead_routes) == {"stdout", "stderr"}
    listener = management._state.listener
    assert listener is not None
    assert listener._thread is not None
    assert listener._thread.is_alive()


# ── a crash ──────────────────────────────────────────────────────────────────


def test_a_crash_traceback_prints_after_the_output_before_it(pytestconfig, tmp_path, monkeypatch):
    """The crash goes through ``sys.excepthook`` as Typer installs it, on its stderr console."""
    monkeypatch.delenv("TYPER_STANDARD_TRACEBACK", raising=False)
    monkeypatch.delenv("_TYPER_STANDARD_TRACEBACK", raising=False)
    monkeypatch.setattr("sys.excepthook", sys.excepthook)  # restored after the test
    app = typer.Typer()

    @app.command()
    def run() -> None:
        raise RuntimeError("boom from the command")

    sinks = live_sinks_with_a_held_console(tmp_path, monkeypatch, pytestconfig)
    logging.getLogger("otto").info("logged before the crash")
    rich.print("output before the crash")
    with pytest.raises(RuntimeError) as crashed:
        app([], standalone_mode=False)
    assert sys.excepthook is typer.main.except_hook
    sys.excepthook(crashed.type, crashed.value, crashed.tb)
    out = sinks.release()

    assert _ordered(
        out, "logged before the crash", "output before the crash", "boom from the command"
    ), out
    assert "Traceback" in out


# ── the corners click and Rich reach ─────────────────────────────────────────


def test_bytes_are_refused_so_click_never_treats_the_proxy_as_binary(
    pytestconfig, tmp_path, monkeypatch
):
    out = Terminal()
    _install_over(tmp_path, monkeypatch, pytestconfig, out, out)

    with pytest.raises(TypeError):
        sys.stdout.write(b"")  # type: ignore[arg-type]
    assert click_compat._is_binary_writer(sys.stdout, False) is False
    assert click_compat._find_binary_writer(sys.stdout) is None


def test_the_first_click_write_leaves_the_listener_running(pytestconfig, tmp_path, monkeypatch):
    sinks = live_sinks_with_a_held_console(tmp_path, monkeypatch, pytestconfig)

    typer.echo("the first click write")
    logging.getLogger("otto").info("logged after it")
    listener = management._state.listener
    assert listener is not None
    assert listener._thread is not None
    out = sinks.release()

    assert _ordered(out, "the first click write", "logged after it"), out
    assert management._state.dead_routes == {}


@pytest.mark.parametrize("echo", ["typer.echo", "click.echo"])
def test_click_on_an_ascii_terminal_still_queues_behind_the_log_line(
    pytestconfig, tmp_path, monkeypatch, echo
):
    """An ASCII locale makes click look for a binary stream; it must find none."""
    import click

    write = typer.echo if echo == "typer.echo" else click.echo
    assert write is not (click.echo if echo == "typer.echo" else typer.echo)
    sinks = live_sinks_with_a_held_console(
        tmp_path, monkeypatch, pytestconfig, terminal=AsciiTerminal()
    )

    logging.getLogger("otto").info("logged first")
    write("echoed second")
    out = sinks.release()

    assert _ordered(out, "logged first", "echoed second"), out


def test_writelines_queues_like_write(pytestconfig, tmp_path, monkeypatch):
    sinks = live_sinks_with_a_held_console(tmp_path, monkeypatch, pytestconfig)

    logging.getLogger("otto").info("logged first")
    sys.stdout.writelines(["written ", "second\n"])
    out = sinks.release()

    assert _ordered(out, "logged first", "written second"), out


class _RichAndBinaryTerminal(Terminal):
    """A terminal that has every attribute a proxy must refuse to forward."""

    rich_proxied_file = Terminal()
    buffer = io.BytesIO()
    raw = io.BytesIO()

    def detach(self) -> Any:
        return self.buffer


def test_rich_cannot_unwrap_the_proxy_and_click_finds_no_binary_stream(
    pytestconfig, tmp_path, monkeypatch
):
    out = _RichAndBinaryTerminal()
    _install_over(tmp_path, monkeypatch, pytestconfig, out, out)
    proxy = _proxy("stdout")

    for name in ["rich_proxied_file", "buffer", "detach", "raw"]:
        with pytest.raises(AttributeError):
            getattr(proxy, name)
    assert Console().file is proxy
    assert proxy.encoding == out.encoding
    weakref.ref(proxy)
    assert {proxy: 1}[proxy] == 1


def test_isatty_on_a_closed_stream_is_false(pytestconfig, tmp_path, monkeypatch):
    out = io.StringIO()
    _install_over(tmp_path, monkeypatch, pytestconfig, out, out)
    out.close()
    # The premise: the stream itself raises once closed, as Rich's probe meets it.
    with pytest.raises(ValueError, match="closed"):
        out.isatty()

    assert sys.stdout.isatty() is False


# ── restore ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "stop",
    [
        management.reset,
        management.shutdown_listener,
        lambda: management.shutdown_listener(join_timeout=5.0),
    ],
    ids=["reset", "shutdown_listener", "shutdown_listener(join_timeout)"],
)
def test_stopping_puts_the_real_streams_back_after_the_queue_is_written(
    pytestconfig, tmp_path, monkeypatch, stop
):
    out, err = Terminal(), Terminal()
    _install_over(tmp_path, monkeypatch, pytestconfig, out, err)
    print("queued before the stop")  # noqa: T201 — the route under test

    stop()

    assert sys.stdout is out
    assert sys.stderr is err
    assert "queued before the stop" in out.getvalue()


def test_stopping_leaves_a_nested_replacement_alone(pytestconfig, tmp_path, monkeypatch):
    out = Terminal()
    _install_over(tmp_path, monkeypatch, pytestconfig, out, out)
    nested = Terminal()
    sys.stdout = nested

    management.reset()

    assert sys.stdout is nested
    assert sys.stderr is out


def test_a_stale_proxy_writes_directly_and_the_next_install_peels_it(
    pytestconfig, tmp_path, monkeypatch
):
    out = Terminal()
    _install_over(tmp_path, monkeypatch, pytestconfig, out, out)
    stale = sys.stdout
    management.reset()
    sys.stdout = stale  # put back by a nested owner that had saved it

    stale.write("direct from a stale proxy\n")
    assert "direct from a stale proxy" in out.getvalue()

    management.init_cli_logging(xdir=tmp_path / "again", log_level="INFO", keep_days=7)
    management.create_output_dir("run")
    fresh = _proxy("stdout")
    assert fresh is not stale
    assert fresh._real is out, "a proxy must never wrap a proxy"
    assert _queues("stdout")
    print("queued again")  # noqa: T201 — the route under test
    assert management.drain_log_queue(timeout=10) is True
    assert "queued again" in out.getvalue()


# ── the signal handler and the force path ────────────────────────────────────


class _Tripwire:
    """Stands in for ``sys.stderr``: any use of it fails the test."""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"the signal handler touched sys.stderr.{name}")


class _RawWrite(NamedTuple):
    fd: int
    data: bytes


def _os_with(**replaced: Any) -> SimpleNamespace:
    """A stand-in for one module's ``os``: the real functions, with *replaced* swapped in.

    Patching the module's name, not the ``os`` module itself, keeps every other
    thread in the process on the real functions.
    """
    names = {name: getattr(os, name) for name in dir(os) if not name.startswith("__")}
    return SimpleNamespace(**{**names, **replaced})


def test_the_ctrl_c_notice_is_a_raw_write_that_neither_drains_nor_uses_the_proxy(monkeypatch):
    guard = lifecycle.SyncPhaseGuard(5.0, "test", lambda: None)
    raw_writes: list[_RawWrite] = []

    def no_drain(*args: Any, **kwargs: Any) -> bool:
        raise AssertionError("the signal handler drained the log queue")

    monkeypatch.setattr(management, "drain_log_queue", no_drain)

    def write(fd: int, data: bytes) -> int:
        raw_writes.append(_RawWrite(fd, data))
        return len(data)

    with monkeypatch.context() as patch:
        patch.setattr("sys.stderr", _Tripwire())
        patch.setattr("sys.stdout", _Tripwire())
        patch.setattr(lifecycle, "os", _os_with(write=write))
        with pytest.raises(lifecycle.SyncPhaseInterrupt):
            guard._on_signal(signal.SIGINT, None)

    assert _RawWrite(2, guard._notice) in raw_writes


def test_a_wedged_producer_does_not_keep_the_watchdog_from_exiting(
    pytestconfig, tmp_path, monkeypatch
):
    """The force path takes no blocking lock on the queue: it never drains.

    A drain puts its barrier with the queue's mutex taken blocking, so a force
    path that drained would hang here behind the wedged producer.
    """
    out = Terminal()
    _install_over(tmp_path, monkeypatch, pytestconfig, out, out)
    listener = management._state.listener
    assert listener is not None
    queue = listener.queue
    assert hasattr(queue, "mutex")
    exits: list[int] = []
    monkeypatch.setattr(lifecycle, "os", _os_with(_exit=exits.append))
    guard = lifecycle.SyncPhaseGuard(
        60.0, "test", lambda: management.shutdown_listener(join_timeout=0.5)
    )
    guard._wake_r, guard._wake_w = os.pipe()
    os.write(guard._wake_w, b"AF")  # two deliveries: force at once
    watchdog = threading.Thread(target=guard._watch, daemon=True)
    queue.mutex.acquire()  # a producer wedged inside Queue.put
    try:
        watchdog.start()
        watchdog.join(timeout=30)
        assert not watchdog.is_alive(), "the watchdog hung behind the queue's mutex"
    finally:
        queue.mutex.release()
        os.close(guard._wake_r)
        os.close(guard._wake_w)
    assert exits == [128 + signal.SIGINT]
    assert sys.stdout is out
    listener.stop()


# ── `| head`: a broken stdout, in a real process ─────────────────────────────


def _child_environ() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if not k.startswith("COV_CORE")}


class _ChildRun(NamedTuple):
    """What a child read by :func:`_read_one_line_then_close` did."""

    first: bytes
    returncode: int
    stderr: bytes


def _read_one_line_then_close(
    script: str,
    tmp_path: Path,
    *,
    stdin_bytes: "bytes | None" = None,
    first_line_within: float = 30,
    exit_within: float = 60,
    started: "list[subprocess.Popen[bytes]] | None" = None,
) -> _ChildRun:
    """Run *script*, read its first line as ``head -1`` does, close the pipe, collect the rest.

    Every wait has a deadline and the child is killed on every way out, so a
    child that never prints or never exits fails the test instead of
    outliving it. *started* receives the child, for a test of this helper.
    """
    child = subprocess.Popen(
        [sys.executable, "-c", script, str(tmp_path)],
        stdin=subprocess.PIPE if stdin_bytes is not None else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_child_environ(),
    )
    if started is not None:
        started.append(child)
    try:
        assert child.stdout is not None
        ready, _, _ = select.select([child.stdout], [], [], first_line_within)
        assert ready, f"the child printed nothing within {first_line_within} s"
        first = child.stdout.readline()
        child.stdout.close()  # what head does after its first line
        _, stderr = child.communicate(input=stdin_bytes, timeout=exit_within)
        return _ChildRun(first=first, returncode=child.returncode, stderr=stderr)
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=10)
        for pipe in (child.stdin, child.stdout, child.stderr):
            if pipe is not None:
                pipe.close()


_PRINTS_LINE_0_THEN_HANGS = """
import sys, time
sys.stdout.write("line 0\\n")
sys.stdout.flush()
time.sleep(3600)
"""

_NEVER_PRINTS = """
import time
time.sleep(3600)
"""


@pytest.mark.timeout(90)
def test_the_pipe_helper_kills_a_child_that_never_exits(tmp_path):
    started: "list[subprocess.Popen[bytes]]" = []

    with pytest.raises(subprocess.TimeoutExpired):
        _read_one_line_then_close(
            _PRINTS_LINE_0_THEN_HANGS, tmp_path, exit_within=1, started=started
        )

    [child] = started
    assert child.returncode is not None, "the child outlived the test"


@pytest.mark.timeout(90)
def test_the_pipe_helper_kills_a_child_that_never_prints(tmp_path):
    started: "list[subprocess.Popen[bytes]]" = []

    with pytest.raises(AssertionError, match="printed nothing"):
        _read_one_line_then_close(_NEVER_PRINTS, tmp_path, first_line_within=1, started=started)

    [child] = started
    assert child.returncode is not None, "the child outlived the test"


_RICH_PRINTS_UNTIL_THE_PIPE_BREAKS = """
import sys
from pathlib import Path

import rich

from otto.logger import management

management.init_cli_logging(xdir=Path(sys.argv[1]), log_level="INFO", keep_days=7)
management.create_output_dir("run")
assert type(sys.stdout).__name__ == "_QueuedStream"
line = 0
while True:
    rich.print(f"line {line}")
    line += 1
"""

_TYPER_ECHOES_UNTIL_THE_PIPE_BREAKS = """
import sys
from pathlib import Path

import typer

from otto.logger import management

app = typer.Typer()


@app.command()
def run() -> None:
    management.init_cli_logging(xdir=Path(sys.argv[1]), log_level="INFO", keep_days=7)
    management.create_output_dir("run")
    assert type(sys.stdout).__name__ == "_QueuedStream"
    line = 0
    while True:
        typer.echo(f"line {line}")
        line += 1


typer.main.get_command(app).main(args=[], standalone_mode=True)
"""


@pytest.mark.timeout(90)
@pytest.mark.parametrize(
    "script",
    [_RICH_PRINTS_UNTIL_THE_PIPE_BREAKS, _TYPER_ECHOES_UNTIL_THE_PIPE_BREAKS],
    ids=["rich.print", "typer.echo"],
)
def test_a_reader_that_stops_early_ends_the_command_with_exit_1_and_no_noise(tmp_path, script):
    """``otto ... | head``: Rich's or click's broken-pipe exit, and nothing on stderr at exit."""
    run = _read_one_line_then_close(script, tmp_path)

    assert run.first == b"line 0\n"
    assert run.returncode == 1, run.stderr.decode(errors="replace")
    assert run.stderr == b"", run.stderr.decode(errors="replace")


_EXITS_AFTER_A_QUEUED_WRITE_TO_A_CLOSED_PIPE = """
import sys
from pathlib import Path

from otto.logger import management

management.init_cli_logging(xdir=Path(sys.argv[1]), log_level="INFO", keep_days=7)
management.create_output_dir("run")
sys.stdout.write("line 0\\n")
sys.stdout.flush()
sys.stdin.readline()  # the reader has closed the pipe by now
# Queued, and the last write the command makes: only the listener sees it fail.
sys.stdout.write("x" * 100_000)
"""


@pytest.mark.timeout(90)
def test_a_pipe_that_breaks_under_the_listener_leaves_the_exit_quiet(tmp_path):
    """The interpreter's exit flush into the closed pipe must not print ``Exception ignored``."""
    run = _read_one_line_then_close(
        _EXITS_AFTER_A_QUEUED_WRITE_TO_A_CLOSED_PIPE, tmp_path, stdin_bytes=b"go\n"
    )

    assert run.first == b"line 0\n"
    assert run.stderr == b"", run.stderr.decode(errors="replace")
    assert run.returncode == 0


# ── a raw descriptor write drains first ──────────────────────────────────────


@pytest.mark.asyncio
async def test_the_interactive_pump_drains_before_it_writes_to_the_descriptor(
    pytestconfig, tmp_path, monkeypatch
):
    """The remote's bytes go to fd 1 past the proxies, after the notice queued before them."""
    from otto.host import interact

    sinks = live_sinks_with_a_held_console(tmp_path, monkeypatch, pytestconfig)

    def write(fd: int, data: bytes) -> int:
        sinks.terminal.write(data.decode())
        return len(data)

    monkeypatch.setattr(interact, "os", SimpleNamespace(write=write))
    chunks = [b"remote prompt$ ", b""]

    async def read() -> bytes:
        return chunks.pop(0)

    logging.getLogger("otto").info("logged first")
    interact._print_stderr("[otto] connected")
    await interact._pump_remote_to_stdout(read, interact._LineBuffer(lambda line: None))
    out = sinks.release()

    assert _ordered(out, "logged first", "[otto] connected", "remote prompt$"), out


class _Wedged(logging.Handler):
    """A sink stuck on a write, as a hung NFS mount would leave it."""

    def __init__(self) -> None:
        super().__init__()
        self.unstick = threading.Event()

    def emit(self, record: logging.LogRecord) -> None:
        self.unstick.wait()


@pytest.mark.asyncio
async def test_the_pump_stops_draining_once_a_drain_gives_up(pytestconfig, tmp_path, monkeypatch):
    """A stuck sink costs one bounded wait per session, not one per chunk of the remote's output."""
    from otto.host import interact

    out = Terminal()
    _install_over(tmp_path, monkeypatch, pytestconfig, out, out)
    listener = management._state.listener
    assert listener is not None
    wedged = _Wedged()
    listener.handlers = (wedged,)
    logging.getLogger("otto").info("stuck behind a hung mount")
    real_drain = management.drain_log_queue
    drains: list[bool] = []

    def short_drain(*args: Any, **kwargs: Any) -> bool:
        drains.append(real_drain(timeout=0.05))
        return drains[-1]

    monkeypatch.setattr(management, "drain_log_queue", short_drain)
    written: list[bytes] = []
    monkeypatch.setattr(
        interact, "os", SimpleNamespace(write=lambda fd, data: written.append(data) or len(data))
    )
    chunks = [b"a", b"b", b"c", b""]

    async def read() -> bytes:
        return chunks.pop(0)

    try:
        await interact._pump_remote_to_stdout(read, interact._LineBuffer(lambda line: None))
    finally:
        wedged.unstick.set()

    assert written == [b"a", b"b", b"c"]
    assert drains == [False]

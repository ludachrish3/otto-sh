"""Tracebacks in otto's sinks: a Rich traceback with locals, verbatim, once per file.

Every assertion reads the real ``console.log`` / ``verbose.log`` an
:func:`otto.logger.install` fan-out wrote, after :func:`management.reset`
drained the listener -- the same queue, listener and formatters the CLI uses.

The marker value lives in a module constant, never on a source line near a
raise: Rich prints three lines of code around every frame, so a value spelled
on such a line would be found in the CODE context and prove nothing about the
LOCALS panel.
"""

import builtins
import logging
import pickle
import re
import sys
import threading
import time
import weakref
from dataclasses import dataclass
from pathlib import Path

import pytest
from rich.console import Console

from otto.logger import formatters, install, management

_MARKER = "distinctive local value"
_MARKUP_MARKER = "[bold]kept[/bold] verbatim"
_LIST_MARKER = ["[red]", "x"]


@pytest.fixture(autouse=True)
def _clean_management():
    management.reset()
    yield
    management.reset()


def _explode(marker_local: object) -> None:
    raise ValueError("the logged failure")


def _explode_with_brackets(marker_local: object) -> None:
    raise ValueError("bad [red]input[/red] here")


def _caught(fn=_explode, marker: object = _MARKER) -> BaseException:
    try:
        fn(marker)
    except ValueError as exc:
        return exc
    raise AssertionError("unreachable: the helper always raises")


def _log_files(out: Path) -> dict[str, str]:
    """Drain the listener, then read both files."""
    management.reset()
    return {name: (out / name).read_text() for name in ("console.log", "verbose.log")}


def test_a_logged_exception_writes_a_rich_traceback_with_locals_to_both_files(tmp_path):
    out = tmp_path / "out"
    install(log_level="INFO", output_dir=out)

    exc = _caught()
    logging.getLogger("acme.product").error("caught it", exc_info=exc)

    for name, text in _log_files(out).items():
        assert "caught it" in text, name
        assert "ValueError: the logged failure" in text, name
        assert "in _explode" in text, f"{name}: the raising frame is not named"
        assert re.search(r"marker_local\s*=\s*'distinctive local value'", text), (
            f"{name} has no locals panel:\n{text}"
        )
        assert "\x1b" not in text, f"{name} carries ANSI without --rich-log-file"


def test_rich_log_files_keep_the_traceback_colour(tmp_path):
    out = tmp_path / "out"
    install(log_level="INFO", output_dir=out)
    for handler in (management._state.console_log_handler, management._state.verbose_handler):
        assert handler is not None
        assert isinstance(handler.formatter, formatters.RichFormatter)
        handler.formatter.rich = True

    logging.getLogger("acme.product").error("caught it", exc_info=_caught())

    for name, text in _log_files(out).items():
        assert "\x1b[" in text, f"{name} lost its colour under rich log files"
        assert re.search(r"marker_local.*distinctive local value", text), name


def test_traceback_text_is_never_read_as_markup(tmp_path):
    """Source lines, local values and the exception message come out verbatim.

    A traceback is not log text: pushed through markup rendering, ``[red]``
    in a message is a style tag and vanishes, and so does ``[bold]`` in a
    local's value.
    """
    out = tmp_path / "out"
    install(log_level="INFO", output_dir=out)

    exc = _caught(_explode_with_brackets, _MARKUP_MARKER)
    logging.getLogger("acme.product").error("first", exc_info=exc)
    logging.getLogger("acme.product").error("second", exc_info=_caught(_explode, _LIST_MARKER))

    for name, text in _log_files(out).items():
        assert "ValueError: bad [red]input[/red] here" in text, name
        assert "'[bold]kept[/bold] verbatim'" in text, name
        assert "['[red]', 'x']" in text, name


def test_the_file_traceback_does_not_wrap_to_the_terminal_width(tmp_path, monkeypatch):
    """A frame's location line stays whole however narrow the operator's terminal is."""
    monkeypatch.setenv("COLUMNS", "40")
    monkeypatch.setattr(formatters, "_console", Console(highlight=False, width=40))
    out = tmp_path / "out"
    install(log_level="INFO", output_dir=out)

    logging.getLogger("acme.product").error("caught it", exc_info=_caught())

    for name, text in _log_files(out).items():
        assert re.search(r"test_tracebacks\.py:\d+ in _explode", text), (
            f"{name}: the frame line was wrapped:\n{text}"
        )


def test_one_logged_exception_is_written_once_per_file(tmp_path):
    out = tmp_path / "out"
    install(log_level="INFO", output_dir=out)

    logging.getLogger("acme.product").error("caught it", exc_info=_caught())

    for name, text in _log_files(out).items():
        assert text.count("ValueError: the logged failure") == 1, f"{name}:\n{text}"
        assert text.count("caught it") == 1, name


def test_the_same_exception_logged_twice_carries_one_traceback_per_file(tmp_path):
    out = tmp_path / "out"
    install(log_level="INFO", output_dir=out)

    exc = _caught()
    logging.getLogger("acme.product").error("first report", exc_info=exc)
    logging.getLogger("acme.product").error("second report", exc_info=exc)

    for name, text in _log_files(out).items():
        assert "first report" in text, name
        assert "second report" in text, name
        assert text.count("ValueError: the logged failure") == 1, f"{name}:\n{text}"


def test_a_written_exception_stays_picklable(tmp_path):
    """The once-per-file bookkeeping rides on the exception; it must not break pickling."""
    out = tmp_path / "out"
    install(log_level="INFO", output_dir=out)

    exc = _caught()
    logging.getLogger("acme.product").error("caught it", exc_info=exc)
    _log_files(out)

    assert pickle.dumps(exc), "the exception no longer pickles"


def test_a_sink_whose_level_refuses_the_record_gets_none_of_it(tmp_path):
    out = tmp_path / "out"
    install(log_level="WARNING", output_dir=out)

    logging.getLogger("acme.product").info("noted in passing", exc_info=_caught())

    files = _log_files(out)
    assert "ValueError: the logged failure" in files["verbose.log"]
    assert "noted in passing" not in files["console.log"]
    assert "the logged failure" not in files["console.log"]


def test_the_console_renders_a_rich_traceback_without_locals(tmp_path, capsys):
    """A logged exception is a Rich traceback on the terminal; its locals go to the files only."""
    install(log_level="INFO", output_dir=tmp_path / "out")

    logging.getLogger("acme.product").error("caught it", exc_info=_caught())
    management.reset()

    shown = capsys.readouterr().out
    assert "the logged failure" in shown
    assert "in _explode" in shown, f"no Rich frame on the console:\n{shown}"
    assert "distinctive local value" not in shown, f"the console printed locals:\n{shown}"


def test_a_command_failure_reaches_both_files_whatever_the_level(tmp_path, capsys):
    """The record that ends a command is in both files even at ``--log-level CRITICAL``.

    And nowhere else: the terminal already said what it says about a failure.
    """
    out = tmp_path / "out"
    install(log_level="CRITICAL", output_dir=out)

    management.record_command_failure(_caught(), "error: the command failed")

    files = _log_files(out)
    for name, text in files.items():
        assert "error: the command failed" in text, name
        assert "ValueError: the logged failure" in text, name
        assert re.search(r"marker_local\s*=\s*'distinctive local value'", text), name
    assert "the logged failure" not in capsys.readouterr().out


def test_a_command_failure_message_is_not_markup(tmp_path):
    out = tmp_path / "out"
    install(log_level="INFO", output_dir=out)

    management.record_command_failure(_caught(), "error: expected list[str], got [bold]")

    for name, text in _log_files(out).items():
        assert "error: expected list[str], got [bold]" in text, name


def test_a_command_failure_already_logged_is_not_written_twice(tmp_path):
    out = tmp_path / "out"
    install(log_level="INFO", output_dir=out)

    exc = _caught()
    logging.getLogger("acme.product").error("caught it", exc_info=exc)
    management.record_command_failure(exc, "error: the command failed")

    for name, text in _log_files(out).items():
        assert "error: the command failed" in text, name
        assert text.count("ValueError: the logged failure") == 1, f"{name}:\n{text}"


def test_a_command_failure_with_no_files_writes_nothing_anywhere(capsys):
    install(log_level="INFO")

    management.record_command_failure(_caught(), "error: the command failed")
    management.reset()

    assert "the command failed" not in capsys.readouterr().out


# -- The listener never touches a live frame -----------------------------------


class _BlocksOffItsThread:
    """A value whose ``repr`` waits, on any thread but the one that built it.

    It stands in for the moment a renderer on the listener thread is halfway
    through walking a local while the frame that owns the local keeps running.
    Read on the logging thread, it returns at once.
    """

    def __init__(self) -> None:
        self.owner = threading.current_thread()
        self.entered = threading.Event()
        self.release = threading.Event()

    def __repr__(self) -> str:
        if threading.current_thread() is not self.owner:
            self.entered.set()
            self.release.wait(timeout=5)
        return "<blocks off its thread>"


def test_a_local_changed_after_the_log_call_cannot_kill_the_listener(tmp_path, monkeypatch):
    """The frame that logged keeps running; nothing the listener does may race it.

    The dict below is walked for its locals. If that walk happens on the
    listener thread, the blocker pauses it mid-iteration, the still-running
    frame grows the dict, and the walk raises "dictionary changed size during
    iteration" -- which nothing on the listener thread catches, so the thread
    dies and every later record is lost. Captured on the logging thread, the
    walk is over before the frame moves on.
    """
    died: list[str] = []
    monkeypatch.setattr(threading, "excepthook", lambda args: died.append(repr(args.exc_value)))
    out = tmp_path / "out"
    install(log_level="INFO", output_dir=out)
    blocker = _BlocksOffItsThread()

    def keeps_running() -> dict[int, object]:
        results: dict[int, object] = {0: blocker, 1: "x"}
        try:
            _explode(results)
        except ValueError:
            logging.getLogger("acme.product").exception("caught it")
        blocker.entered.wait(timeout=1)
        results.update({i: i for i in range(2, 200)})
        blocker.release.set()
        return results

    keeps_running()
    logging.getLogger("acme.product").error("a later record")
    files = _log_files(out)

    assert died == [], f"the listener thread died: {died}"
    for name, text in files.items():
        assert "ValueError: the logged failure" in text, name
        assert "a later record" in text, f"{name} lost the record after the race:\n{text}"


class _NotesItsFinalizer:
    def __init__(self, seen: list[str]) -> None:
        self.seen = seen

    def __del__(self) -> None:
        self.seen.append(threading.current_thread().name)


def test_a_logged_exceptions_locals_are_freed_at_once_on_the_logging_thread(tmp_path, monkeypatch):
    """Nothing queued keeps a frame alive, so its locals' finalizers run where they belong.

    A queued live traceback pins every frame and local until the listener lets
    go of the record, which ``QueueListener`` does only when the NEXT record
    arrives; the finalizers (an unclosed transport's warning, a coroutine never
    awaited) then run on the listener thread at an arbitrary later moment.
    """
    out = tmp_path / "out"
    install(log_level="INFO", output_dir=out)
    # pytest's own capture handlers keep every record for the test report, and
    # with it the exception; only otto's handlers are under test here.
    root = logging.getLogger()
    monkeypatch.setattr(
        root,
        "handlers",
        [h for h in root.handlers if getattr(h, management.OTTO_HANDLER_ATTR, False)],
    )
    seen: list[str] = []

    def work() -> "weakref.ref[_NotesItsFinalizer]":
        resource = _NotesItsFinalizer(seen)
        ref = weakref.ref(resource)
        try:
            _explode(resource)
        except ValueError:
            logging.getLogger("acme.product").exception("caught it")
        return ref

    ref = work()
    time.sleep(0.2)  # let the listener take the record off the queue

    assert ref() is None, "a queued record still holds the raising frame's locals"
    assert seen == [threading.current_thread().name], seen
    files = _log_files(out)
    assert "ValueError: the logged failure" in files["verbose.log"]


@dataclass
class _UnhashableError(Exception):
    """A dataclass exception: ``@dataclass`` sets ``__hash__ = None``."""

    detail: str


def _explode_unhashably(marker_local: object) -> None:
    raise _UnhashableError("unhashable failure")


def test_an_unhashable_exception_still_gets_a_rich_trace(tmp_path):
    """Capture must never hash the logged exception itself, or this one falls back to plain text."""
    out = tmp_path / "out"
    install(log_level="INFO", output_dir=out)

    try:
        _explode_unhashably(_MARKER)
    except _UnhashableError:
        logging.getLogger("acme.product").exception("caught it")

    text = _log_files(out)["verbose.log"]
    assert "in _explode_unhashably" in text, text
    assert "Traceback (most recent call last):" not in text, f"fell back to plain text:\n{text}"
    assert re.search(r"marker_local\s*=\s*'distinctive local value'", text), text


def test_a_capture_that_fails_falls_back_to_the_plain_traceback(tmp_path, monkeypatch, capsys):
    from rich.traceback import Traceback

    def refuse(*args, **kwargs):
        raise RuntimeError("rich could not take the trace")

    monkeypatch.setattr(Traceback, "extract", refuse)
    out = tmp_path / "out"
    install(log_level="INFO", output_dir=out)

    logging.getLogger("acme.product").error("caught it", exc_info=_caught())

    for name, text in _log_files(out).items():
        assert "Traceback (most recent call last):" in text, name
        assert "ValueError: the logged failure" in text, name
    assert "ValueError: the logged failure" in capsys.readouterr().out


def test_a_render_that_fails_falls_back_to_the_plain_traceback(tmp_path, monkeypatch):
    def refuse(*args, **kwargs):
        raise RuntimeError("rich could not render the trace")

    monkeypatch.setattr(formatters, "render_traceback", refuse)
    out = tmp_path / "out"
    install(log_level="INFO", output_dir=out)

    logging.getLogger("acme.product").error("caught it", exc_info=_caught())

    for name, text in _log_files(out).items():
        assert "Traceback (most recent call last):" in text, name
        assert "ValueError: the logged failure" in text, name


class _FailsOnce:
    """A stream whose first write raises, as a full disk would."""

    def __init__(self, stream) -> None:
        self.stream = stream
        self.failed = False

    def write(self, text: str) -> int:
        if not self.failed:
            self.failed = True
            raise OSError("no space left on device")
        return self.stream.write(text)

    def flush(self) -> None:
        self.stream.flush()

    def close(self) -> None:
        self.stream.close()


def test_a_traceback_whose_write_failed_is_not_counted_as_written(tmp_path, monkeypatch):
    """``(traceback written above)`` must be true: a failed write leaves the next copy whole."""
    monkeypatch.setattr(logging, "raiseExceptions", False)  # no "--- Logging error ---" noise
    out = tmp_path / "out"
    install(log_level="INFO", output_dir=out)
    handler = management._state.console_log_handler
    assert handler is not None
    handler.stream = _FailsOnce(handler.stream)

    exc = _caught()
    logging.getLogger("acme.product").error("first report, lost", exc_info=exc)
    logging.getLogger("acme.product").error("second report", exc_info=exc)

    text = _log_files(out)["console.log"]
    assert "first report, lost" not in text
    assert "(traceback written above)" not in text, text
    assert text.count("ValueError: the logged failure") == 1, text


def test_a_command_failure_keeps_its_colour_with_rich_log_files(tmp_path):
    out = tmp_path / "out"
    install(log_level="INFO", output_dir=out)
    for handler in (management._state.console_log_handler, management._state.verbose_handler):
        assert handler is not None
        assert isinstance(handler.formatter, formatters.RichFormatter)
        handler.formatter.rich = True

    management.record_command_failure(_caught(), "error: the command failed")

    for name, text in _log_files(out).items():
        assert "\x1b[" in text, f"{name} lost its colour under rich log files"
        assert "error: the command failed" in text, name


# -- Bounds on what a traceback writes ------------------------------------------


def _nest(depth: int) -> object:
    return [_nest(depth - 1) for _ in range(10)] if depth else 1


_DEEP = _nest(5)


def _explode_deep(marker_local: object) -> None:
    raise ValueError("deep")


def test_a_deeply_nested_local_is_bounded(tmp_path):
    """Rich bounds a container's length but not its depth; otto bounds both.

    ``_DEEP`` holds 100,000 leaves five levels down, and two frames hold it.
    Unbounded in depth, each locals panel lists every leaf: about 20 MB per
    file, and seconds of the listener's time. At ``LOCALS_MAX_DEPTH`` a panel
    stops three levels down, at most 1,000 entries, and the file stays under
    1 MB.
    """
    out = tmp_path / "out"
    install(log_level="INFO", output_dir=out)

    logging.getLogger("acme.product").error("caught it", exc_info=_caught(_explode_deep, _DEEP))

    for name, text in _log_files(out).items():
        assert "ValueError: deep" in text, name
        assert len(text.encode()) < 1_000_000, f"{name} is {len(text.encode())} bytes"


def _raise_as_group(member: BaseException) -> None:
    # Spelled through builtins: ExceptionGroup does not exist on 3.10.
    raise vars(builtins)["ExceptionGroup"]("the task group failed", [member])


@pytest.mark.skipif(sys.version_info < (3, 11), reason="ExceptionGroup is new in 3.11")
def test_a_deeply_nested_local_in_an_exception_group_member_is_bounded(tmp_path):
    """The bounds hold for a group's members too, not only for the group's own frames.

    Rich extracts a group's members itself and passes them only some of the
    locals bounds: no depth limit and its default string limit. A TaskGroup,
    anyio or ``except*`` failure would then write ``_DEEP`` in full, about
    100 times the plain case. Same exception, same 1 MB bound as above.
    """
    out = tmp_path / "out"
    install(log_level="INFO", output_dir=out)

    member = _caught(_explode_deep, _DEEP)
    try:
        _raise_as_group(member)
    except Exception:
        logging.getLogger("acme.product").exception("caught it")

    for name, text in _log_files(out).items():
        assert "the task group failed" in text, name
        assert "ValueError: deep" in text, name
        assert "in _explode_deep" in text, f"{name} lost the member's frames"
        assert len(text.encode()) < 1_000_000, f"{name} is {len(text.encode())} bytes"


_TYPER_MARKER = "a value only typer and click frames hold"


def test_typer_and_click_frames_are_written_without_their_locals(tmp_path):
    """Frames inside Typer and click show their location only, as Typer's own hook does."""
    import typer

    # Not short: Typer's short mode cuts every frame above the command from
    # the trace (a Rich guard local), and those are the frames under test.
    app = typer.Typer(pretty_exceptions_short=False)

    @app.command()
    def boom(value: str) -> None:
        del value  # the value now lives only in Typer's and click's frames
        raise ValueError("raised under typer")

    out = tmp_path / "out"
    install(log_level="INFO", output_dir=out)
    try:
        typer.main.get_command(app).main([_TYPER_MARKER], standalone_mode=False)
    except ValueError:
        logging.getLogger("acme.product").exception("caught it")

    for name, text in _log_files(out).items():
        assert "ValueError: raised under typer" in text, name
        assert "in boom" in text, name
        assert re.search(r"typer.*\.py:\d+ in ", text), f"{name} lost the typer frames:\n{text}"
        assert _TYPER_MARKER not in text, f"{name} wrote a typer/click frame's locals:\n{text}"

"""A log record renders Rich markup only when it opts in with ``extra={"markup": True}`` (#697).

Driven through the real fan-out: :func:`otto.logger.management.init_cli_logging`
plus :func:`~otto.logger.management.create_output_dir` install the console
handler, queue, listener and the :class:`~otto.logger.formatters.RichFormatter`
file sinks the CLI uses, and each test reads back what the console,
``console.log`` and ``verbose.log`` actually show.
"""

import io
import logging
import re

import pytest

from otto.logger import management
from otto.logger.formatters import RichFormatter
from otto.logger.relay import log_relayed

_ANSI = re.compile(r"\x1b\[[0-9;]*m")

# Brackets Rich would read as tags (an open, a stray close, a bracketed word),
# and a trailing backslash: a record that does not opt in shows all of it.
_PLAIN = "[red]x [/y] [conn=0, chan=1] root:x:0:0 \\"


@pytest.fixture(autouse=True)
def _clean_management():
    management.reset()
    yield
    management.reset()


def _log_and_read(tmp_path, monkeypatch, emit) -> dict[str, str]:
    """Run *emit* under the CLI's sinks and return what each one shows, ANSI stripped."""
    from otto.console import CONSOLE

    console = io.StringIO()
    monkeypatch.setattr(CONSOLE, "_file", console)
    management.init_cli_logging(xdir=tmp_path, log_level="INFO", keep_days=7)
    out = management.create_output_dir("test")
    emit()
    management.reset()  # drain the listener into every sink
    return {
        "console": _ANSI.sub("", console.getvalue()),
        "console.log": (out / "console.log").read_text(),
        "verbose.log": (out / "verbose.log").read_text(),
    }


def test_a_plain_record_is_shown_exactly_as_written(tmp_path, monkeypatch):
    sinks = _log_and_read(tmp_path, monkeypatch, lambda: logging.getLogger("otto").info(_PLAIN))
    for sink, text in sinks.items():
        assert _PLAIN in text, (sink, text)


def test_a_plain_record_keeps_its_emoji_codes(tmp_path, monkeypatch):
    """No markup pass, so no ``:emoji:`` substitution either, on any sink."""
    sinks = _log_and_read(
        tmp_path, monkeypatch, lambda: logging.getLogger("otto").info("done :thumbs_up: now")
    )
    for sink, text in sinks.items():
        assert "done :thumbs_up: now" in text, (sink, text)


def test_an_opted_in_record_is_styled(tmp_path, monkeypatch):
    def emit() -> None:
        logging.getLogger("otto").info("[bold]ok[/bold] fine", extra={"markup": True})

    sinks = _log_and_read(tmp_path, monkeypatch, emit)
    for sink, text in sinks.items():
        assert "ok fine" in text, (sink, text)
        assert "[bold]" not in text, (sink, text)


def test_an_opted_in_record_substitutes_emoji_codes(tmp_path, monkeypatch):
    def emit() -> None:
        logging.getLogger("otto").info("[bold]ok[/bold] :thumbs_up:", extra={"markup": True})

    sinks = _log_and_read(tmp_path, monkeypatch, emit)
    for sink, text in sinks.items():
        assert "ok \U0001f44d" in text, (sink, text)
        assert ":thumbs_up:" not in text, (sink, text)


def test_an_opted_in_record_can_turn_the_emoji_pass_off(tmp_path, monkeypatch):
    def emit() -> None:
        logging.getLogger("otto").info(
            "[bold]ok[/bold] :thumbs_up:", extra={"markup": True, "emoji": False}
        )

    sinks = _log_and_read(tmp_path, monkeypatch, emit)
    for sink, text in sinks.items():
        assert "ok :thumbs_up:" in text, (sink, text)


def test_an_opted_in_record_carries_its_style_on_the_console(tmp_path, monkeypatch):
    """The console's own render of the record: the style is a span, not text."""
    management.init_cli_logging(xdir=tmp_path, log_level="INFO", keep_days=7)
    handler = management._state.console_handler
    assert handler is not None
    styled = logging.LogRecord("otto", logging.INFO, __file__, 1, "[bold]ok[/bold]", None, None)
    styled.markup = True
    rendered = handler.render_message(styled, styled.getMessage())
    assert rendered.plain == "ok"
    assert [(s.start, s.end, str(s.style)) for s in rendered.spans] == [(0, 2, "bold")]

    plain = logging.LogRecord("otto", logging.INFO, __file__, 1, "[bold]ok[/bold]", None, None)
    rendered = handler.render_message(plain, plain.getMessage())
    assert rendered.plain == "[bold]ok[/bold]"
    assert rendered.spans == []


def test_a_relayed_line_keeps_the_relayed_text_on_every_sink(tmp_path, monkeypatch):
    def emit() -> None:
        log_relayed(
            logging.getLogger("otto"), logging.INFO, "[bold]@h[/] ", "[red]dev root:x:0:0 :warning:"
        )

    sinks = _log_and_read(tmp_path, monkeypatch, emit)
    for sink, text in sinks.items():
        assert "@h [red]dev root:x:0:0 :warning:" in text, (sink, text)
        assert "\\[" not in text, (sink, text)


def test_a_third_party_record_is_shown_as_written(tmp_path, monkeypatch):
    message = "channel [conn=0, chan=1] closed: [red]x [/y]"
    sinks = _log_and_read(
        tmp_path, monkeypatch, lambda: logging.getLogger("asyncssh").warning(message)
    )
    for sink, text in sinks.items():
        assert message in text, (sink, text)


@pytest.mark.parametrize("rich", [False, True])
def test_the_file_formatter_leaves_a_plain_record_untouched(rich):
    formatter = RichFormatter(fmt="{message}")
    formatter.rich = rich
    record = logging.LogRecord("otto", logging.INFO, __file__, 1, _PLAIN, None, None)
    assert formatter.format(record) == _PLAIN
    assert record.msg == _PLAIN


def test_the_file_formatter_renders_an_opted_in_record():
    formatter = RichFormatter(fmt="{message}")
    record = logging.LogRecord("otto", logging.INFO, __file__, 1, "[bold]ok[/bold]", None, None)
    record.markup = True
    first = formatter.format(record)
    assert first.rstrip("\n") == "ok"
    # The record is restored, so the second file renders the same thing.
    assert record.msg == "[bold]ok[/bold]"
    assert formatter.format(record) == first

"""``log_relayed``: otto's markup prefix, then relayed text every sink shows verbatim (#697)."""

import logging

import pytest
from rich.text import Text

from otto.logger.formatters import RichFormatter
from otto.logger.relay import escape_relayed, log_relayed

_FOREIGN = [
    "a [red]b[/red] c",
    "d [link=x]e[/link] f",
    "g [x:y] h",
    "image:[sha256:abc]",
    "i [bold] j",
    "k [/x] l",
    r"m \[n] o",
    "root:x:0:0 :warning: y",
    "ends in a backslash \\",
    "[1, 2] and [DRY RUN] were never tags",
]


def _capture(**kwargs) -> list[logging.LogRecord]:
    kept: list[logging.LogRecord] = []

    class _Keep(logging.Handler):
        def emit(self, record):
            kept.append(record)

    handler = _Keep()
    lg = logging.getLogger("otto.test_relay")
    lg.addHandler(handler)
    lg.setLevel(logging.DEBUG)
    try:
        log_relayed(lg, logging.INFO, "[yellow]@h > | ", **kwargs)
    finally:
        lg.removeHandler(handler)
    return kept


@pytest.mark.parametrize("text", _FOREIGN)
def test_the_helper_sets_the_record_flags(text):
    (record,) = _capture(text=text, extra={"host": "h"})
    assert record.markup is True  # type: ignore[attr-defined]
    assert record.emoji is False  # type: ignore[attr-defined]
    assert record.host == "h"  # type: ignore[attr-defined]


@pytest.mark.parametrize("text", _FOREIGN)
def test_the_relayed_text_renders_verbatim_after_the_styled_prefix(text):
    (record,) = _capture(text=text)
    rendered = Text.from_markup(record.getMessage(), emoji=False)
    assert rendered.plain == f"@h > | {text}"
    # otto's style is still markup, and it runs over the relayed text too.
    assert [(s.start, s.end, str(s.style)) for s in rendered.spans] == [
        (0, len(rendered.plain), "yellow")
    ]


def test_per_line_puts_the_prefix_on_every_line():
    (record,) = _capture(text="a [red]\nb :warning:", per_line=True)
    assert record.getMessage() == "[yellow]@h > | a \\[red]\n[yellow]@h > | b :warning:"


def test_escape_relayed_keeps_a_trailing_backslash_single():
    assert Text.from_markup(escape_relayed("x \\") + " [bold]y[/]").plain == "x \\ y"


@pytest.mark.parametrize("text", _FOREIGN)
def test_a_log_file_shows_the_text_once_escaped_however_often_it_is_formatted(text):
    """Both files format the SAME opted-in record in turn: the second must match the first."""
    formatter = RichFormatter(fmt="{message}")
    (record,) = _capture(text=text)
    first = formatter.format(record)
    second = formatter.format(record)
    assert first.rstrip("\n") == f"@h > | {text}"
    assert second == first


def test_the_record_points_at_the_caller_not_the_helper():
    (record,) = _capture(text="x")
    assert record.pathname == __file__
    assert record.funcName == "_capture"  # the function that called log_relayed


def test_a_disabled_level_builds_nothing(monkeypatch):
    def boom(text):
        raise AssertionError("escaped text for a record nobody will see")

    monkeypatch.setattr("otto.logger.relay.escape_relayed", boom)
    lg = logging.getLogger("otto.test_relay_off")
    lg.setLevel(logging.ERROR)
    log_relayed(lg, logging.INFO, "[yellow]p ", "text")

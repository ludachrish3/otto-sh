"""``relay_line``: otto's markup prefix, then relayed text every sink shows verbatim (#697)."""

import logging

import pytest
from rich.text import Text

from otto.logger.formatters import RichFormatter
from otto.logger.relay import relay_line

_FOREIGN = [
    "a [red]b[/red] c",
    "d [link=x]e[/link] f",
    "g [x:y] h",
    "image:[sha256:abc]",
    "i [bold] j",
    "k [/x] l",
    r"m \[n] o",
    "ends in a backslash \\",
    "[1, 2] and [DRY RUN] were never tags",
]


@pytest.mark.parametrize("text", _FOREIGN)
def test_the_relayed_text_renders_verbatim_after_the_styled_prefix(text):
    rendered = Text.from_markup(relay_line("[yellow]@h > | ", text))
    assert rendered.plain == f"@h > | {text}"
    # otto's style is still markup, and it runs over the relayed text too.
    assert [(s.start, s.end, str(s.style)) for s in rendered.spans] == [
        (0, len(rendered.plain), "yellow")
    ]


def test_a_hand_escaped_prefix_stays_otto_markup():
    rendered = Text.from_markup(relay_line(r"\[docker] failed: ", "x [/y] z"))
    assert rendered.plain == "[docker] failed: x [/y] z"


@pytest.mark.parametrize("text", _FOREIGN)
def test_a_log_file_shows_the_text_once_escaped_however_often_it_is_formatted(text):
    """Both files format the SAME record in turn: the second must match the first."""
    formatter = RichFormatter(fmt="{message}")
    record = logging.LogRecord("otto", logging.INFO, __file__, 1, relay_line("", text), None, None)
    first = formatter.format(record)
    second = formatter.format(record)
    assert first.rstrip("\n") == text
    assert second == first

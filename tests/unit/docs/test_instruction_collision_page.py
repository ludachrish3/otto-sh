"""docs/setup/writing-instructions.md quotes otto's collision error word for word.

The page's "The collision error" section shows the message a project gets when
a standalone instruction takes a project instruction's name. This test raises
that error for real and compares it with the page's fenced ``text`` block, so
a reworded message fails here instead of leaving the page quoting an old one.
"""

import re

import pytest

from otto.instructions import instruction
from otto.registry import registering_project
from tests._fixtures.paths import CHECKOUT_ROOT

_PAGE = CHECKOUT_ROOT / "docs" / "setup" / "writing-instructions.md"
_HEADING = "## The collision error"


def _quoted_error() -> str:
    text = _PAGE.read_text(encoding="utf-8")
    section = text.split(_HEADING, 1)[1]
    match = re.search(r"^```text\n(.*?)^```", section, flags=re.MULTILINE | re.DOTALL)
    assert match is not None, f"no ```text block under {_HEADING!r}"
    return match.group(1)


def _normalized(text: str) -> str:
    return " ".join(text.split())


def test_the_page_quotes_the_collision_error_otto_raises():
    async def install() -> None: ...

    refused = pytest.raises(ValueError, match="is a project instruction")
    with registering_project("widget"), refused as raised:
        instruction()(install)
    assert _normalized(str(raised.value)) == _normalized(_quoted_error())

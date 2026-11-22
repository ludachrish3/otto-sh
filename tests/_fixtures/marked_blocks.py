"""The fenced blocks a docs page pins to a test with an HTML-comment marker.

A page pins a block by writing a comment right before its fence:

    <!-- fixture: tech1-inventory/lab.json -->
    ```json
    ...
    ```

:func:`marked_blocks` finds every such block, so every docs test that pins a
block reads the page the same way. Duplicates and missing blocks are the
caller's to judge, since what counts as "once" differs per page.
"""

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class MarkedBlock:
    """One fenced block after a marker."""

    name: "str | None"
    """The marker's ``name`` group, when its pattern has one."""
    lang: str
    """The fence's language."""
    body: str
    """The block's text, up to its closing fence."""


def marked_blocks(text: str, marker: str, *langs: str) -> list[MarkedBlock]:
    """Every block in *text* whose fence, in one of *langs*, follows ``<!-- marker -->``.

    *marker* is a regular expression for the comment's content: escape the
    literal parts, and name a group ``name`` to record what each marker
    names. The blocks come in page order.
    """
    fences = "|".join(re.escape(lang) for lang in langs)
    pattern = re.compile(
        rf"<!-- {marker} -->\s*```(?P<lang>{fences})\n(?P<body>.*?)```", flags=re.DOTALL
    )
    return [
        MarkedBlock(name=m.groupdict().get("name"), lang=m["lang"], body=m["body"])
        for m in pattern.finditer(text)
    ]

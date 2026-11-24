"""Scope a docs guard to one ``## `` section of a page.

A guard that searches a whole page is satisfied by a row in the wrong table.
Scoping it to the section it is about makes the check about that table.
"""

from pathlib import Path


def page_section(text: str, heading: "str | None", page: Path) -> str:
    """Return *heading*'s section of *text*, up to the next ``## ``, or the whole page.

    Fails loudly on a heading that is not there: a renamed section must not
    quietly widen the search back to the whole page, which is the failure this
    scoping exists to prevent.
    """
    if heading is None:
        return text
    assert heading in text, f"{page.name}: no {heading!r} heading to scope the search to"
    rest = text.split(heading, 1)[1]
    return rest.split("\n## ", 1)[0]

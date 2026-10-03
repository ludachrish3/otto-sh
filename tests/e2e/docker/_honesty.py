"""Parsers for the honesty differentials: what build prints, what the daemon lists.

Both read otto's real console output, so a hostless unit test pins each one
against output the product itself rendered
(``tests/unit/docker/test_honesty_parsers.py``). Imported by name; the leading
underscore keeps pytest from collecting this.
"""

import re
from dataclasses import dataclass

MARK = "OTTO-HONESTY"
"""Starts every row the passthrough asks the daemon for, so the rows are findable
in the console output that also carries otto's own command echo."""

DAEMON_LIST_COMMAND = (
    f"docker images --format '{MARK} {{{{.Repository}}}}:{{{{.Tag}}}} {{{{.ID}}}}'"
)
"""The daemon query, through ``otto host <id> exec``, which shares no code with the docker verbs."""

_BUILT = re.compile(
    r"(?P<image>\S+): built (?P<refs>\S+(?:, \S+)*)\s+(?P<id>[0-9a-f]{12})\s+\((?P<host>[^)]+)\)"
)
_SHORT_ID = re.compile(r"[0-9a-f]{12}")


@dataclass(frozen=True)
class BuiltLine:
    """One ``<repo>/<image>: built <refs>  <id>  (<host>)`` report line."""

    image: str
    """``<repo>/<image>``."""
    refs: "list[str]"
    """The references the line names."""
    image_id: str
    """The id the line names."""
    host: str
    """The host the line names."""


def parse_built_line(text: str, image: str) -> "BuiltLine | None":
    """Return the report line for *image* (``<repo>/<image>``) in *text*, or ``None``.

    Whitespace between fields is matched as any run, so the raw console text and
    the same text with its wrapping collapsed both parse.
    """
    for found in _BUILT.finditer(text):
        if found["image"] == image:
            return BuiltLine(image, found["refs"].split(", "), found["id"], found["host"])
    return None


def parse_daemon_rows(text: str) -> "dict[str, str]":
    """Return ``reference -> short id`` from the rows :data:`DAEMON_LIST_COMMAND` printed in *text*.

    The passthrough relays the daemon's output through the console logger, so a
    row arrives as ``INFO     @test3 > | OTTO-HONESTY repo1-api:latest cbd8571d4b6e``
    (and as ``         @test3 > | OTTO-HONESTY ...`` for the lines after the
    first), and the echo of the command itself carries the mark too. A row is
    what follows the mark and has docker's id shape; the echo's unexpanded
    ``{{.ID}}`` template does not.
    """
    rows: dict[str, str] = {}
    for line in text.splitlines():
        _, sep, rest = line.partition(f"{MARK} ")
        if not sep:
            continue
        fields = rest.split()
        if len(fields) >= 2 and _SHORT_ID.fullmatch(fields[1]):
            rows[fields[0]] = fields[1]
    return rows

"""Shared Rich Console instance for otto, and the one "this text is literal" decision."""

import os
from typing import TYPE_CHECKING, Any

import rich
from rich.console import Console
from rich.text import Text

from ._terminal import usable_columns, usable_terminal_size

if TYPE_CHECKING:
    from rich.table import Table

CONSOLE = Console()

# Rich re-probes fds 0/1/2 for the terminal width on every render, and an
# in-process pytest.main() run (otto test) leaves those descriptors non-tty,
# which would collapse the console to 80 columns mid-command. So pin stdout's
# size now, while it is still the real terminal, but only when stdout is a
# terminal with a usable size (a fresh pty reports 0x0) and no usable COLUMNS
# was given: a usable COLUMNS, the user's or the one the CLI's shim set, is
# already stored by the constructor, and every other console the CLI builds
# honours it too. The usable-terminal decision is otto._terminal's, the one
# the shim's default uses.
if (
    usable_columns(os.environ.get("COLUMNS")) is None
    and (size := usable_terminal_size(1)) is not None
):
    CONSOLE.size = size


def literal_cell(value: object) -> Text:
    """Return *value* as text Rich prints exactly as written: no markup, no ``:emoji:`` codes.

    A :class:`~rich.text.Text` comes back unchanged, so a caller that styled a
    prefix with :meth:`Text.assemble <rich.text.Text.assemble>` keeps its
    styles; anything else becomes a plain ``Text(str(value))``. The same
    decision as :func:`print_literal`, made for a table cell.
    """
    return value if isinstance(value, Text) else Text(str(value))


def print_literal(text: "str | Text", *, console: "Console | None" = None) -> None:
    """Print *text* exactly, on one line: no markup, no emoji, no highlighting, no wrapping.

    For a path, an identifier or a command a reader may copy: Rich would read
    ``[red]`` as a style tag and delete it, turn ``:smile:`` into an emoji, and
    fold a long path mid-token at the console width. Prints through *console*,
    else Rich's global console. Style a prefix with
    ``Text.assemble(("Wrote schemas to", "green"), " ", str(path))``: a
    :class:`~rich.text.Text`'s own styles survive, and the path stays a plain span.
    """
    (console or rich.get_console()).print(
        literal_cell(text), markup=False, emoji=False, highlight=False, soft_wrap=True
    )


def id_column(table: "Table", header: str, **kwargs: Any) -> None:
    """Add a column for identifiers, paths, addresses or values echoed from config.

    Its cells fold onto more lines instead of ending in "…", so a cell that is
    too wide for its column loses nothing but its line breaks. Add every cell
    in it through :func:`literal_cell`, so markup and ``:emoji:`` codes print
    as written. Rich's wrapping still drops a space that lands exactly on a
    fold, and cannot fit a double-width character into a one-cell column; both
    apply at any width. *kwargs* go to :meth:`Table.add_column
    <rich.table.Table.add_column>`.
    """
    table.add_column(header, overflow="fold", **kwargs)

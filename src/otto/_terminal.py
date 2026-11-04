"""The one "is this a usable terminal" decision, and the interactive terminal's size.

Stdlib only, and dependent on nothing in otto: the shim imports it before the
CLI graph, ``otto.console`` pins its console width with it, and ``otto.host``
sizes a login's remote terminal with it. Importing it changes nothing.
"""

import os
from collections.abc import Iterable


def usable_columns(value: "str | None") -> "int | None":
    """Return *value* as a column count when it is a usable ``COLUMNS``, else ``None``.

    Usable means ASCII digits only, with no stripping, worth at least 1. That
    is stricter than Rich and :func:`shutil.get_terminal_size`, which both read
    full-width digits as their ASCII value; Rich also accepts ``0`` (a
    zero-width console) and a superscript ``2`` (on which ``int()`` then raises).
    """
    if value is None or not (value.isascii() and value.isdigit()):
        return None
    columns = int(value)
    return columns if columns >= 1 else None


def usable_terminal_size(fd: int) -> "os.terminal_size | None":
    """Return *fd*'s terminal size when *fd* is a terminal reporting at least 1x1, else ``None``.

    A fresh pty reports 0x0, and a terminal whose size read fails is no more
    use than a pipe: both give ``None``.
    """
    if not os.isatty(fd):
        return None
    try:
        size = os.get_terminal_size(fd)
    except OSError:
        return None
    return size if size.columns >= 1 and size.lines >= 1 else None


def first_usable_terminal_size(fds: Iterable[int]) -> "os.terminal_size | None":
    """Return the size of the first of *fds*, in the order given, that is a usable terminal.

    The probing is shared; which descriptors to ask, and in which order, is
    each caller's policy.
    """
    for fd in fds:
        size = usable_terminal_size(fd)
        if size is not None:
            return size
    return None


_LOGIN_FALLBACK = os.terminal_size((80, 24))
"""The size a login asks for when no descriptor is a usable terminal."""


def interactive_terminal_size(fallback: os.terminal_size = _LOGIN_FALLBACK) -> os.terminal_size:
    """Return the size of the local interactive terminal, for a login's remote pty.

    It asks stdin first, because that is the terminal a login reads from, then
    stdout, then stderr, and gives *fallback* when none is a usable terminal
    (a login with no terminal at all, or a terminal whose size cannot be
    read). It ignores ``COLUMNS`` and ``LINES``, explicit values included:
    the CLI may have set ``COLUMNS`` for its run as a launch-time snapshot,
    and a remote pty sized from that would never follow a resize.
    """
    return first_usable_terminal_size((0, 1, 2)) or fallback

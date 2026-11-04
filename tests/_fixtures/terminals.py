"""Fake descriptors for the "is this a usable terminal" decision (#785).

:func:`fake_terminals` patches :func:`os.isatty` and :func:`os.get_terminal_size`
so a test states which of fds 0, 1 and 2 is a terminal and what it reports,
without a real pty.
"""

import os

import pytest


def fake_terminals(
    monkeypatch: pytest.MonkeyPatch, sizes: "dict[int, tuple[int, int] | OSError | None]"
) -> None:
    """Make the descriptors in *sizes* terminals, and every other descriptor not one.

    Each value is what that terminal reports: a ``(columns, lines)`` pair;
    ``None`` for a terminal that reports 0x0 (a fresh pty); or an
    :class:`OSError` instance for a terminal whose size read raises.
    """

    def isatty(fd: int) -> bool:
        return fd in sizes

    def get_terminal_size(fd: int = 1) -> os.terminal_size:
        if fd not in sizes:
            raise OSError(25, "Inappropriate ioctl for device")
        size = sizes[fd]
        if isinstance(size, OSError):
            raise size
        return os.terminal_size(size if size is not None else (0, 0))

    monkeypatch.setattr(os, "isatty", isatty)
    monkeypatch.setattr(os, "get_terminal_size", get_terminal_size)

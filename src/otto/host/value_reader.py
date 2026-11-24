"""Read one value out of what a command printed, or refuse it.

The rule is decided in one place, :func:`read_value` (or its non-raising
form, :func:`read_value_or_none`): surrounding ASCII whitespace is stripped
and what is left must be exactly one value of the asked-for kind, on one
line. Anything else (a stderr line merged ahead of a count, a banner after a
path, full-width digits) is not a value.

The callers that use it today are the netcat backend's port finder and
listener check, the shell backend's size read, the login-home probe and the
docker clock read. A lint rule refuses a number parsed straight from a
command's stripped value, but only where it can see that shape; other
conversions in the code base do not yet go through this reader.
"""

import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import overload

from .errors import CommandValueError

_ASCII_WHITESPACE = " \t\n\r\x0b\x0c"
_DIGITS = re.compile(r"[0-9]+")
_MESSAGE_TEXT_LIMIT = 200


@dataclass(frozen=True)
class Integer:
    """Base-10 ASCII digits only (no sign, no ``_``), within inclusive optional bounds."""

    minimum: int | None = None
    maximum: int | None = None


@dataclass(frozen=True)
class Word:
    """Exactly one of :attr:`words`, matched case-sensitively."""

    words: frozenset[str]


@dataclass(frozen=True)
class AbsolutePath:
    """One line that starts with ``/``."""


def cut_for_message(text: str) -> str:
    """Return *text* cut to the length a message quotes (200 characters)."""
    return text[:_MESSAGE_TEXT_LIMIT]


def _describe(kind: "Integer | Word | AbsolutePath") -> str:
    if isinstance(kind, Integer):
        if kind.minimum is None and kind.maximum is None:
            return "a base-10 integer"
        low = "" if kind.minimum is None else f" >= {kind.minimum}"
        high = "" if kind.maximum is None else f" <= {kind.maximum}"
        joiner = " and" if low and high else ""
        return f"a base-10 integer{low}{joiner}{high}"
    if isinstance(kind, Word):
        return "one of " + ", ".join(repr(w) for w in sorted(kind.words))
    return "one absolute path"


def _parse(
    text: str | None, kind: "Integer | Word | AbsolutePath"
) -> "int | str | PurePosixPath | None":
    if text is None:
        return None
    body = text.strip(_ASCII_WHITESPACE)
    if not body or "\n" in body or "\r" in body:
        return None
    if isinstance(kind, Integer):
        if _DIGITS.fullmatch(body) is None:
            return None
        try:
            number = int(body)
        except ValueError:
            # More digits than Python converts by default
            # (``sys.get_int_max_str_digits``): no command's count is that long.
            return None
        if kind.minimum is not None and number < kind.minimum:
            return None
        if kind.maximum is not None and number > kind.maximum:
            return None
        return number
    if isinstance(kind, Word):
        return body if body in kind.words else None
    return PurePosixPath(body) if body.startswith("/") else None


@overload
def read_value(text: str | None, kind: Integer, *, what: str) -> int: ...
@overload
def read_value(text: str | None, kind: Word, *, what: str) -> str: ...
@overload
def read_value(text: str | None, kind: AbsolutePath, *, what: str) -> PurePosixPath: ...
def read_value(
    text: str | None, kind: Integer | Word | AbsolutePath, *, what: str
) -> int | str | PurePosixPath:
    """Return the one value of *kind* that *text* holds.

    Args:
        text: What the command printed; ``None`` or empty is not a value.
        kind: The value asked for.
        what: What the caller asked for, named in the message.

    Raises:
        ~otto.host.errors.CommandValueError: if *text* is not exactly one
            value of *kind*. The message quotes *text*, cut to 200 characters.
    """
    value = _parse(text, kind)
    if value is None:
        shown = text if text is None else cut_for_message(text)
        raise CommandValueError(
            f"{what}: expected {_describe(kind)}, got {shown!r}", what=what, text=text
        )
    return value


@overload
def read_value_or_none(text: str | None, kind: Integer) -> int | None: ...
@overload
def read_value_or_none(text: str | None, kind: Word) -> str | None: ...
@overload
def read_value_or_none(text: str | None, kind: AbsolutePath) -> PurePosixPath | None: ...
def read_value_or_none(
    text: str | None, kind: Integer | Word | AbsolutePath
) -> int | str | PurePosixPath | None:
    """:func:`read_value`, with the refusal turned into ``None``; the only non-raising form."""
    try:
        return read_value(text, kind, what="value")
    except CommandValueError:
        return None

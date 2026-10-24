r"""How a session's remote terminal maps line endings, measured at the handshake.

A shell session runs on a terminal, and the terminal's output processing is
between the program and otto: with ``onlcr`` (on by default on Linux) every
``\n`` the program writes leaves as ``\r\n``. otto never changes that setting
(no ``stty -onlcr``: it would add tty state otto must restore, and buys
nothing, because the inverse of ``onlcr`` is exact). It measures it instead,
for free, on the handshake line it already sends: the bash handshake runs
:data:`CALIBRATION_COMMAND` just before it echoes READY, and
:func:`measure` reads what came back.

The outcome is one of four strings, which is also what ``otto host <id>
probe`` prints:

* :data:`IDENTITY`: the calibration came back as written, so nothing is folded;
* :data:`ONLCR`: each ``\n`` came back as ``\r\n`` and nothing else changed, so
  the session folds each ``\r\n`` back to ``\n`` (the exact inverse);
* ``unknown(<observed>)``: any other mapping (one that loses information,
  such as a server that expands a lone LF but leaves CRLF as it is). Nothing
  is folded, the bytes are returned as they arrived, and the session logs a
  WARNING once;
* :data:`UNMEASURED`: the session's dialect sends no calibration line.

A command that changes its own terminal (``stty``, an ``ioctl``) changes
what later output on that session looks like, until the next session
measures again.
"""

import re
from dataclasses import dataclass

__all__ = [
    "CALIBRATION_COMMAND",
    "IDENTITY",
    "ONLCR",
    "UNMEASURED",
    "Measurement",
    "is_unknown",
    "measure",
]

CALIBRATION_COMMAND = r"printf 'X\r\nY\nZ\rW\n' 2>/dev/null"
r"""The shell text the bash handshake runs before READY.

``X\r\nY`` and ``Y\nZ`` tell a translated ``\n`` from a program's own
``\r\n``; ``Z\rW`` shows whether a lone CR survives. ``2>/dev/null`` keeps a
target with no ``printf`` silent: its handshake still reaches READY and the
mapping reads as unknown."""

IDENTITY = "identity"
"""The calibration came back as written, so the terminal translates nothing."""

ONLCR = "onlcr"
"""Each ``\n`` came back as ``\r\n`` and nothing else changed."""

UNMEASURED = "unmeasured"
"""No calibration line was sent (the dialect has none), or no session has opened."""

_AS_WRITTEN = "X\r\nY\nZ\rW\n"
_KNOWN = {_AS_WRITTEN: IDENTITY, _AS_WRITTEN.replace("\n", "\r\n"): ONLCR}

# The calibration output, ending where READY starts. Only CR and LF may sit
# between the letters, and the escapes the typed (echoed) copy spells as
# backslash text can never match, so this reads the output copy only.
_CALIBRATION = re.compile(r"X[\r\n]*Y[\r\n]*Z[\r\n]*W[\r\n]*\Z")
_TRAILING_SGR = re.compile(r"(?:\x1b\[[0-9;]*m)+\Z")
_OBSERVED_TAIL = 32
"""How much of what preceded READY an ``unknown(...)`` quotes when no calibration line came back."""


@dataclass(frozen=True)
class Measurement:
    """What one handshake reply showed about the terminal."""

    mapping: str
    """``identity``, ``onlcr`` or ``unknown(<observed>)``."""
    calibrated: bool
    """Whether the calibration line came back at all; ``False`` means the
    target never printed it (no ``printf``), so ``mapping`` quotes whatever
    preceded READY instead."""


def measure(reply: str, ready: str) -> Measurement:
    """Measure the terminal from the handshake *reply*: everything up to READY and its line ending.

    *ready* is the session's READY marker. The calibration output is the text
    immediately before the LAST occurrence of it, which is the line the shell
    printed. The handshake types READY split, so an echoed copy of the typed
    line (echo still on) does not carry it; one that did would come earlier
    and read ``echo <READY>``.
    """
    at = reply.rfind(ready)
    head = reply[:at] if at != -1 else reply
    head = _TRAILING_SGR.sub("", head)
    found = _CALIBRATION.search(head)
    if found is None:
        return Measurement(f"unknown({head[-_OBSERVED_TAIL:]!r})", calibrated=False)
    observed = found.group(0)
    return Measurement(_KNOWN.get(observed) or f"unknown({observed!r})", calibrated=True)


def is_unknown(mapping: str) -> bool:
    """Whether *mapping* is an ``unknown(...)`` outcome."""
    return mapping.startswith("unknown(")

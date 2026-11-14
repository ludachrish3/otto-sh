r"""What each shell sends back for a framed command: one model per frame, and the recordings.

Each ``*_transcript(case, m)`` builds the buffer a frame's ``parse_output`` is
handed for one :class:`~otto.testing.OutputCase`, after the session has undone
any terminal translation. The models are written from the recordings in
``tests/unit/host/shell_transcripts/`` (their README states each model), and
``tests/unit/host/test_shell_transcripts.py`` proves each one reproduces every
recorded buffer byte for byte.

The recordings are loaded here (:func:`recordings`) and a Zephyr command's
output is cut out of one here (:func:`zephyr_recorded_output`), so the unit
tests and the conformance suite's Zephyr vocabularies read the same bytes the
same way.

The Zephyr models also answer for inputs no stock Zephyr command can produce.
For those they ASSUME, and each assumption is stated on :func:`zephyr_output`.
"""

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from otto.host.command_frame import SessionMarkers
from otto.testing import OutputCase
from tests._fixtures.paths import TESTS_ROOT

TRANSCRIPTS = TESTS_ROOT / "unit" / "host" / "shell_transcripts"
"""The directory ``make shell-transcripts`` records into: one JSON file per ``(element, term)``."""

ERROR_ON = "\x1b[1;31m"
"""The colour the Zephyr shell opens its own error output (and a rejected marker) with."""

ERROR_OFF = "\x1b[m"
r"""The reset that closes each coloured message-level line, after that line's ``\r\n``."""

WARN_ON = "\x1b[1;33m"
"""The colour the Zephyr shell opens a warning line (``shell_warn``) with."""

LEVEL_ON = [ERROR_ON, WARN_ON]
"""Every message-level colour measured on the bed (README, "Message-level colouring")."""

ZEPHYR_EXTRA_COMMANDS = [
    "devmem",
    "fs cat /nonexistent /nonexistent2",
    "log enable inf otto_nope1 otto_nope2",
    "dynamic show",
    "kernel log_level otto_nope 1",
    "llext call_fn otto_nope otto_nope",
]
"""Stock commands whose output mixes the shell's message levels, recorded per guest.

``make shell-transcripts`` asks each Zephyr guest every one whose first word
its ``help`` lists, and the conformance vocabulary turns each recorded one into
an output case. Measured 2026-11-13: ``devmem`` -- an error line, then the
command's help (3.7, 4.4; 2.7 prints nothing); ``fs cat`` of two missing files
and ``log enable`` of two unknown sources -- two error lines; ``dynamic show``
with no command added -- one warning line; ``kernel log_level`` of an unknown
source -- a warning line, then an error line (4.4; 3.7 answers this spelling
with its ``kernel`` help); ``llext call_fn`` of an absent extension -- the
uncoloured ``No such extension <name>`` line
``LlextHexLoader.reports_not_loaded`` reads. None changes guest state: each refuses its
arguments before setting anything.
"""

ZEPHYR_PROMPT = "\x1b[1;32m~$ \x1b[m"
"""The prompt of the Zephyr telnet shell (2.7, 3.7 and 4.4)."""

ZEPHYR_SERIAL_PROMPT = "\x1b[1;32muart:~$ \x1b[m"
"""The prompt of the Zephyr UART shell (3.7 and 4.4)."""

NOT_FOUND = ": command not found\r\n"


def bash_transcript(case: OutputCase, m: SessionMarkers) -> str:
    """The bash and ash frames' buffer, after the session's ``onlcr`` fold.

    The recorded buffer also carries the prompt ahead of BEGIN (see
    :func:`bash_recorded`); the frame finds BEGIN by search, so the kit omits it.
    """
    return f"{m.begin}\n{case.text}{m.end_prefix}{case.retcode}__\n"


def bash_recorded(output: str, *, retcode: int, m: SessionMarkers, ps1: str, first: bool) -> str:
    r"""The bash frame's recorded buffer: README model, after the ``onlcr`` fold."""
    lead = "" if first else "\n"
    return f"{lead}{ps1}> {m.begin}\n{output}{m.end_prefix}{retcode}__"


def zephyr_output(text: str, *, error: bool) -> str:
    r"""What the Zephyr shell sends for a program that wrote *text*.

    Recorded: each ``\n`` the program writes arrives as ``\r\n``; the shell's
    own error output arrives as ``ERROR_ON <text>\r\n ERROR_OFF``.

    Assumed (no stock command shows it):

    - a ``\r`` the program writes passes through unchanged, so a written
      ``\r\n`` arrives as ``\r\r\n`` and a lone ``\r`` as ``\r``;
    - output that does not end in a newline is followed directly by what comes
      next (the prompt, or 2.7's ``retCode`` line): the shell inserts nothing;
    - empty output sends nothing;
    - leading and trailing spaces, tabs and every other byte pass unchanged;
    - output a command writes to stderr (``>&2`` in the kit) is wrapped as the
      shell wraps its own error output.
    """
    out = text.replace("\n", "\r\n")
    return f"{ERROR_ON}{out}{ERROR_OFF}" if error else out


def zephyr_buffer(out: str, *, retcode: int, m: SessionMarkers, prompt: str) -> str:
    r"""The ``zephyr`` / ``zephyr-serial`` buffer around *out*, as the shell sent it.

    The head is the tail of the previous command's END rejection; the read
    stops at END, which arrives in the error colour.
    """
    return (
        f"{NOT_FOUND}{ERROR_OFF}{prompt}\r\n"
        f"{ERROR_ON}{m.begin}{NOT_FOUND}{ERROR_OFF}{prompt}\r\n"
        f"{out}"
        f"{prompt}\r\n{retcode}\r\n{prompt}\r\n"
        f"{ERROR_ON}{m.end_prefix}"
    )


def zephyr_inline_buffer(out: str, *, retcode: int, m: SessionMarkers, prompt: str) -> str:
    r"""The ``zephyr-inline`` (2.7) buffer around *out*: ``retCode = <n>`` replaces ``retval``.

    Assumed: a code other than ``0`` and ``-8`` prints in the same
    ``retCode = <n>`` form (only those two were recorded).
    """
    return (
        f"{NOT_FOUND}{ERROR_OFF}retCode = -8\r\n{prompt}\r\n"
        f"{ERROR_ON}{m.begin}{NOT_FOUND}{ERROR_OFF}retCode = -8\r\n{prompt}\r\n"
        f"{out}"
        f"retCode = {retcode}\r\n{prompt}\r\n"
        f"{ERROR_ON}{m.end_prefix}"
    )


def _errs(case: OutputCase) -> bool:
    return ">&2" in case.command


def zephyr_transcript(case: OutputCase, m: SessionMarkers) -> str:
    """The ``zephyr`` frame's buffer for *case* (telnet prompt)."""
    out = zephyr_output(case.text, error=_errs(case))
    return zephyr_buffer(out, retcode=case.retcode, m=m, prompt=ZEPHYR_PROMPT)


def zephyr_serial_transcript(case: OutputCase, m: SessionMarkers) -> str:
    """The ``zephyr-serial`` frame's buffer for *case*: only the prompt differs."""
    out = zephyr_output(case.text, error=_errs(case))
    return zephyr_buffer(out, retcode=case.retcode, m=m, prompt=ZEPHYR_SERIAL_PROMPT)


def zephyr_inline_transcript(case: OutputCase, m: SessionMarkers) -> str:
    """The ``zephyr-inline`` frame's buffer for *case*."""
    out = zephyr_output(case.text, error=_errs(case))
    return zephyr_inline_buffer(out, retcode=case.retcode, m=m, prompt=ZEPHYR_PROMPT)


def recordings() -> "dict[str, dict[str, Any]]":
    """Every recorded transcript file, parsed, keyed by its file name's stem, in name order."""
    return {path.stem: json.loads(path.read_text()) for path in sorted(TRANSCRIPTS.glob("*.json"))}


def recording_markers(recording: "dict[str, Any]") -> SessionMarkers:
    """The markers *recording*'s session framed its commands with."""
    begin = recording["markers"]["begin"]
    return SessionMarkers.for_session(
        session_id=begin.removeprefix("__OTTO_").removesuffix("_BEGIN__")
    )


@dataclass(frozen=True)
class _ZephyrModel:
    build: "Callable[..., str]"
    prompt: str
    lines_after_begin: int


#: Each Zephyr frame's buffer model, its prompt, and how many ``\r\n``-ended
#: lines follow BEGIN before the command's own output (BEGIN's rejection, and
#: on 2.7 its ``retCode`` line, then the prompt's line feed).
ZEPHYR_MODELS: "dict[str, _ZephyrModel]" = {
    "zephyr": _ZephyrModel(zephyr_buffer, ZEPHYR_PROMPT, 2),
    "zephyr-serial": _ZephyrModel(zephyr_buffer, ZEPHYR_SERIAL_PROMPT, 2),
    "zephyr-inline": _ZephyrModel(zephyr_inline_buffer, ZEPHYR_PROMPT, 3),
}


@dataclass(frozen=True)
class RecordedOutput:
    """What a recorded Zephyr command wrote, by the README's rule."""

    text: str
    r"""The command's output: frame lines removed, ``\r\n`` read as ``\n``, other bytes kept."""

    sent: str
    """The command's region as the shell sent it: between the frame's lines, nothing removed."""

    levels: "list[str]"
    """The colour each message-level line in it opened with, in order (empty: none)."""


def zephyr_recorded_output(recording: "dict[str, Any]", entry: "dict[str, Any]") -> RecordedOutput:
    r"""Cut *entry*'s output out of its ``raw`` bytes, by the README's model, never by a frame.

    Positional: after BEGIN's lines, before the tail the frame's model builds
    for the entry's exit code. Then the shell's additions come off -- each
    message-level line's colour and reset (:func:`_strip_levels`) and the
    ``\r`` of each ``\r\n`` -- and everything else the
    command wrote stays, the help formatter's cursor moves included. Raises
    if what is left is not the README's shape.
    """
    model = ZEPHYR_MODELS[recording["frame"]]
    m = recording_markers(recording)
    raw, rc = entry["raw"], entry["retcode"]
    tail = model.build("", retcode=rc, m=m, prompt=model.prompt)
    tail = tail[tail.index(m.begin) :].split("\r\n", model.lines_after_begin)[-1]
    start = raw.rindex(m.begin)
    for _ in range(model.lines_after_begin):
        start = raw.index("\r\n", start) + 2
    if not raw.endswith(tail):
        raise ValueError(f"the recorded tail is not the model's: {raw[-120:]!r}")
    sent = raw[start : len(raw) - len(tail)]
    levels: list[str] = []
    text = _strip_levels(sent, levels).replace("\r\n", "\n")
    if "\r" in text:
        raise ValueError(f"a \\r outside a \\r\\n line ending: {sent!r}")
    if text and not text.endswith("\n"):
        raise ValueError(f"a recorded Zephyr output that does not end in a newline: {sent!r}")
    return RecordedOutput(text=text, sent=sent, levels=levels)


def _strip_levels(sent: str, levels: "list[str]") -> str:
    r"""Remove each message-level line's colouring from *sent*, by the README's shape.

    Each line arrives as ``<colour> <text> \r\n ERROR_OFF``, wrapped on its own;
    the colour and the reset come off, the text and its ``\r\n`` stay. Raises
    on an opener that does not close that way. Returns the remainder; each
    line's colour is appended to *levels*, in order.
    """
    out: list[str] = []
    at = 0
    while True:
        hits = [(sent.find(on, at), on) for on in LEVEL_ON if sent.find(on, at) != -1]
        if not hits:
            out.append(sent[at:])
            return "".join(out)
        pos, on = min(hits)
        close = sent.find("\r\n" + ERROR_OFF, pos)
        if close == -1 or "\r\n" in sent[pos:close]:
            raise ValueError(f"a message-level line that does not close: {sent[pos:]!r}")
        out += [sent[at:pos], sent[pos + len(on) : close + 2]]
        levels.append(on)
        at = close + 2 + len(ERROR_OFF)

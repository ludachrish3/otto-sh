r"""How a session's remote terminal maps its output, measured at the handshake.

A shell session runs on a terminal, and the terminal's output processing is
between the program and otto. Two of its settings change what a command
wrote, and otto treats them differently, for one reason: whether the change
has an exact inverse.

* **Line endings.** With ``onlcr`` (on by default on Linux) every ``\n`` the
  program writes leaves as ``\r\n``. otto never changes that setting (no
  ``stty -onlcr``: it would add tty state otto must restore, and buys
  nothing, because the inverse of ``onlcr`` is exact). It measures it
  instead and folds each ``\r\n`` back.
* **Tabs.** With ``tab3`` (``XTABS``) every tab leaves as the spaces up to
  the next tab stop. That has no inverse: ``a\tb`` and ``a       b`` arrive
  the same. Measured 2026-11-13, the pty BusyBox telnetd allocates has it on
  (``stty -a`` reads ``tab3`` on every BusyBox release of the lab, 1.16.1 to
  1.35.0), where sshd's pty and a GNU telnetd's do not. So where the
  terminal expands tabs otto turns it off on the session it opened:
  the bash handshake runs :data:`TABS_OFF_COMMAND` (``stty tab0``, the
  spelling GNU coreutils and every BusyBox ``stty`` measured accept;
  ``-tabs`` is the opposite setting on both). BSD and macOS ``stty`` spell
  the setting differently (``oxtabs``); that has not been measured on such
  a host, and the tolerant invocation plus the calibration cover it (a
  terminal still expanding tabs is measured and warned about).

  Whether otto puts the setting back depends on whose terminal it is:

  * On an ssh or telnet login the pty is the session's own and goes away
    with it, so nothing is restored. The hand-back to an interactive user
    (:meth:`~otto.host.command_frame.CommandFrame.restore_interactive`)
    turns echo back on and leaves tabs unexpanded for the rest of that
    login: a terminal emulator renders a raw tab itself.
  * On a console (a serial line) the terminal outlives otto's session
    (:attr:`~otto.host.session.ShellSession.terminal_outlives_session`), so
    otto restores what it found. The handshake's first statement after its
    anchor, :func:`save_settings_command` (``stty -g``), prints the
    terminal's settings before anything changes them, on the line otto
    already sends, behind a marker of this session's own, so nothing else
    the console shows can pass for them; the session keeps that record
    (:func:`saved_settings`)
    and types it back with ``stty <record>``
    (:meth:`~otto.host.command_frame.CommandFrame.restore_terminal`), which
    restores echo and tab expansion in one step, at the hand-back to an
    interactive user and before the session closes. Only the first
    handshake on the line saves, so a second one (frame entry after a
    session-setup hook) cannot record otto's own settings as the user's.
    A session that ends without a clean close (otto killed, the link cut)
    cannot restore, and neither does a close while the session needs
    recovery (a command timed out and was not re-synced) or sits inside an
    application shell: the restore would be typed into a running command or
    a shell that is not bash, so otto types nothing. In each case the line
    keeps echo off and ``tab0`` until a login resets it or a user runs
    ``stty sane``. A target with no ``stty`` (or
    one with no ``-g``) prints no record, nothing is restored, and its
    handshake still reaches READY.

Both are measured, for free, on the handshake line otto already sends: the
bash handshake runs :data:`CALIBRATION_COMMAND` just before it echoes READY,
and :func:`measure` reads what came back. The two facts are independent.

The line-ending mapping is one of four strings:

* :data:`IDENTITY`: the calibration came back as written, so nothing is folded;
* :data:`ONLCR`: each ``\n`` came back as ``\r\n`` and nothing else changed, so
  the session folds each ``\r\n`` back to ``\n`` (the exact inverse);
* ``unknown(<observed>)``: any other mapping (one that loses information,
  such as a server that expands a lone LF but leaves CRLF as it is). Nothing
  is folded, the bytes are returned as they arrived, and the session logs a
  WARNING once;
* :data:`UNMEASURED`: the session's dialect sends no calibration line.

Whether tabs survive is the second fact. Where the calibration's tab came
back as anything but a tab, ``stty tab0`` did not take (a ``stty`` that
refused it, or none at all): the reported mapping (:func:`reported`, which
is what ``otto host <id> probe`` and ``host.terminal_mapping`` show) gains
``, tabs expanded``, the line-ending fold is unaffected, a tab a command
writes arrives as spaces, and the session logs a WARNING once.

A third fact rides on the same line: whether the terminal still echoes.
The handshake turns echo off (``stty -echo``); where that fails (no ``stty``
on the target, #953, or one that refuses it) the statement's ``||`` branch
sets :data:`ECHO_VAR` to :data:`ECHO_ON`, and the calibration prints that
variable immediately before its ``X``. :func:`measure` reads it as
:attr:`Measurement.echoes`, the reported mapping gains ``, echo on``, and the
session logs a WARNING once where it matters (an ash shell). It decides how
long a line otto may type there (:func:`otto.host.session.typed_line_ceiling`). A
handshake that printed no calibration line (no ``printf``) shows nothing, so
it reads as possibly echoing.

A command that changes its own terminal (``stty``, an ``ioctl``) changes
what later output on that session looks like, until the next session
measures again.
"""

import re
from dataclasses import dataclass

__all__ = [
    "CALIBRATION_COMMAND",
    "ECHO_OFF_COMMAND",
    "ECHO_ON",
    "ECHO_RESET",
    "ECHO_STILL_ON",
    "ECHO_UNSET",
    "ECHO_VAR",
    "IDENTITY",
    "ONLCR",
    "SAVED_SETTINGS_PROBE",
    "TABS_EXPANDED",
    "TABS_OFF_COMMAND",
    "UNMEASURED",
    "Measurement",
    "is_unknown",
    "measure",
    "reported",
    "save_settings_command",
    "saved_settings",
]

ECHO_VAR = "__otto_echo"
"""The shell variable the bash handshake records a failed ``stty -echo`` in."""

ECHO_ON = "E"
"""What :data:`ECHO_VAR` holds, and the calibration prints before its ``X``, if echo is on."""

ECHO_RESET = f"{ECHO_VAR}="
"""The statement that empties :data:`ECHO_VAR` before ``stty -echo`` runs.

A whole line always runs it, so a value left in the shell before (or by an
earlier line's cut tail) cannot pass for this line's answer."""

ECHO_OFF_COMMAND = f"2>/dev/null stty -echo || {ECHO_VAR}={ECHO_ON}"
"""The statement the bash handshake turns echo off with, recording a failure.

Silent, and its ``||`` branch is an assignment, which cannot fail, so a
target without ``stty`` still reaches READY under ``set -e`` (#953). It keeps
:func:`~otto.host.command_frame.whole_line_echo`'s cut rules: a tail that
starts inside the assignment assigns another variable or runs a command
named ``E`` or ``=E``, which is not found."""

ECHO_UNSET = f"unset {ECHO_VAR}"
"""The statement that removes :data:`ECHO_VAR` once the calibration has printed it."""

CALIBRATION_COMMAND = rf"2>/dev/null printf %sX\\r\\nY\\nZ\\rW\\tT\\012 ${ECHO_VAR}"
r"""The shell text the bash handshake runs before READY.

Its ``%s`` prints :data:`ECHO_VAR` just before the ``X``: empty where
``stty -echo`` took, :data:`ECHO_ON` where it failed. The variable is
expanded unquoted and unbraced (no tail may open a quote or a ``${``, the
cut rules below), so an empty value is no argument at all and ``%s`` prints
nothing. Under ``set -u`` a whole line has set it already; a tail that
skipped :data:`ECHO_RESET` may stop on it unset, which prints no READY, and
the line is resent whole.

``X\r\nY`` and ``Y\nZ`` tell a translated ``\n`` from a program's own
``\r\n``; ``Z\rW`` shows whether a lone CR survives; ``W\tT`` whether a tab
does (W is in column 0 after the CR, so an expanded tab is seven spaces).
The format is unquoted and the redirection comes first, as the cut rules of
:func:`~otto.host.command_frame.whole_line_echo` require: the shell turns
each ``\\`` into ``\``, so ``printf`` receives ``X\r\nY\nZ\rW\tT\012``. The
final newline is spelled in octal for the same rules: a tail that starts
inside it leaves ``012``, ``12`` or ``2``, where ``\\n`` left the command
``n``. The printed bytes are the same.
``2>/dev/null`` keeps a target with no ``printf`` silent: its handshake still
reaches READY and the mapping reads as unknown."""

TABS_OFF_COMMAND = "2>/dev/null stty tab0 || :"
"""The statement the bash handshake turns the terminal's tab expansion off with.

A statement of its own, so a ``stty`` that rejects ``tab0`` cannot take
``stty -echo`` down with it; silent and ``|| :``-guarded like that one, so a
target without ``stty`` still reaches READY (#953). It runs before the
calibration, which then measures whether it took."""

SAVED_SETTINGS_PROBE = "2>/dev/null stty -g"
"""The command :func:`save_settings_command` reads the terminal's settings with.

``stty -g`` prints every setting as one line of colon-separated hex (GNU
coreutils and BusyBox alike), the form ``stty`` takes back as its argument.
Silent on failure, so a target without ``stty`` (or one without ``-g``)
prints nothing (#953)."""


def save_settings_command(tag: str) -> str:
    """Return the statement a console's bash handshake saves the terminal's settings with.

    ``echo <tag>$(2>/dev/null stty -g)``: the record is printed right after
    *tag*, the session's :attr:`~otto.host.command_frame.SessionMarkers.saved`
    marker, on a line of its own, so :func:`saved_settings` reads this
    session's record and nothing else the console shows. A line of hex
    groups already on the line (a MAC address or IPv6 address in a banner,
    the unread restore of an earlier session) carries no tag, an earlier
    session's tagged record carries another session's id, and the typed
    line's echo has ``$(`` after the tag, never a hex digit.

    It is the first statement after the line's anchor, so the record is the
    settings as otto found them, read on the line otto already sends: no
    extra round trip. It keeps
    :func:`~otto.host.command_frame.whole_line_echo`'s cut rules: no quote;
    every tail that holds ``$(`` holds its ``)``, and a tail that starts
    inside the substitution is a syntax error the shell rejects whole
    (the resend then answers); a tail that starts inside the tag word runs
    a command named by its suffix, which is not found. A target without
    ``stty`` prints the tag alone, which saves nothing, and ``echo`` cannot
    fail, so the line still reaches READY under ``set -e`` (#953).
    """
    return f"echo {tag}$({SAVED_SETTINGS_PROBE})"


_RECORD = r"([0-9A-Fa-f]+(?::[0-9A-Fa-f]+){3,})\r*$"


def saved_settings(reply: str, tag: str) -> str | None:
    """Return the settings record :func:`save_settings_command` printed behind *tag* in *reply*.

    The FIRST record behind *tag* that runs to the end of its line: a resend
    of the handshake runs the save again after the first run changed the
    settings, and its record comes later. ``None`` when no record came back
    (no ``stty``, or one without ``-g``). The record is hex digits and colons
    only, so it can be typed back unquoted.

    One case this cannot tell: a first handshake that the console cut, whose
    tail skipped the save but still ran ``stty -echo`` and ``stty tab0``,
    leaves the resend saving otto's own settings, which the session then
    restores.
    """
    found = re.search(re.escape(tag) + _RECORD, reply, re.MULTILINE)
    return found.group(1) if found else None


TABS_EXPANDED = "tabs expanded"
"""What :func:`reported` appends when the calibration's tab came back as spaces."""

ECHO_STILL_ON = "echo on"
"""What :func:`reported` appends when the calibration showed ``stty -echo`` failed."""

IDENTITY = "identity"
"""The calibration came back as written, so the terminal translates nothing."""

ONLCR = "onlcr"
"""Each ``\n`` came back as ``\r\n`` and nothing else changed."""

UNMEASURED = "unmeasured"
"""No calibration line was sent (the dialect has none), or no session has opened."""

_AS_WRITTEN = "X\r\nY\nZ\rW\tT\n"
_KNOWN = {_AS_WRITTEN: IDENTITY, _AS_WRITTEN.replace("\n", "\r\n"): ONLCR}

# The calibration output, ending where READY starts. Only CR and LF may sit
# between the letters, tabs or spaces between W and T, and the escapes the
# typed (echoed) copy spells as backslash text can never match, so this
# reads the output copy only. An ECHO_ON right before the X is the `%s` the
# calibration prints its echo variable with.
_CALIBRATION = re.compile(
    rf"(?P<echo>{ECHO_ON}?)X[\r\n]*Y[\r\n]*Z[\r\n]*W(?P<tab>[\t ]*)T[\r\n]*\Z"
)
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
    tabs_expanded: bool = False
    """Whether the calibration's tab came back as anything but a tab; always
    ``False`` when nothing was calibrated, since nothing was seen."""
    echoes: bool = True
    """Whether the terminal may still echo what otto types. ``True`` where the
    calibration printed :data:`ECHO_ON` (``stty -echo`` failed), and ``True``
    where nothing was calibrated, since a target that printed no calibration
    line (no ``printf``, and perhaps no ``stty``) showed nothing and must be
    treated as the risky case. ``False`` only where the calibration came back
    without the marker."""


def measure(reply: str, ready: str) -> Measurement:
    """Measure the terminal from the handshake *reply*: everything up to READY and its line ending.

    *ready* is the session's READY marker. The calibration output is the text
    immediately before the LAST occurrence of it, which is the line the shell
    printed. The typed line never carries READY, because the handshake splits
    it across a variable (:func:`~otto.host.command_frame.whole_line_echo`),
    so an echoed copy of the typed line (echo still on) cannot be mistaken
    for the output.
    """
    at = reply.rfind(ready)
    head = reply[:at] if at != -1 else reply
    head = _TRAILING_SGR.sub("", head)
    found = _CALIBRATION.search(head)
    if found is None:
        return Measurement(f"unknown({head[-_OBSERVED_TAIL:]!r})", calibrated=False)
    echoes = found.group("echo") == ECHO_ON
    observed = found.group(0)[len(found.group("echo")) :]
    tab = found.group("tab")
    # The line endings are classified with the tab put back, so a terminal
    # that expands tabs still folds CRLF exactly: the two facts are apart.
    start = found.end("echo")
    tab_at, tab_end = found.start("tab") - start, found.end("tab") - start
    lines = observed[:tab_at] + "\t" + observed[tab_end:]
    mapping = _KNOWN.get(lines) or f"unknown({observed!r})"
    return Measurement(mapping, calibrated=True, tabs_expanded=tab != "\t", echoes=echoes)


def reported(seen: Measurement) -> str:
    """Report *seen* as otto shows it: the mapping, then ``, tabs expanded``, ``, echo on`` if so.

    ``, echo on`` is shown only where the calibration printed the marker: an
    uncalibrated reply already reads ``unknown(...)``, which says nothing was seen.
    """
    notes = [TABS_EXPANDED] * seen.tabs_expanded + [ECHO_STILL_ON] * (
        seen.calibrated and seen.echoes
    )
    return ", ".join([seen.mapping, *notes])


def is_unknown(mapping: str) -> bool:
    """Whether *mapping* is an ``unknown(...)`` outcome."""
    return mapping.startswith("unknown(")

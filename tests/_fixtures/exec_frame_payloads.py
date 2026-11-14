"""The exec frame's payloads as A2 measures them: DEF-TAIL and DEF-REC, rendered a second time.

Issue #935, slice 2b. The exec frame (a shell-routed ``exec`` isolated in a
subshell, its terminal and its parent's ``set -e`` put back at the tail) is
rendered by the product's ``BashFrame`` once the code task that creates it
lands. The lab measurement that decides its open questions (task A2) runs
before that, so the exact bytes it types live here, built only from names
the product already has (``SessionMarkers``, ``BashFrame.frame``'s body rule,
``BashFrame.restore_terminal``, ``whole_line_echo``, ``RECOVER_SUFFIX``,
``SAVED_SETTINGS_PROBE``). This is a second rendering on purpose and for a
bounded time: the product task's equivalence test asserts byte equality
between these functions and the product frame (both restores, both stdin
values), so the two cannot drift. Nothing here imports a name that task
creates.

The rules, one per function:

* :func:`terminal_restore` -- when the restore variable is non-empty, the
  product's guarded ``stty`` restore of it; every read is ``${V-}``, so the
  statement never aborts under ``set -u``. Quote-free, because it also rides
  the anchored restore line, whose cut rules forbid a quoted span. The
  start snapshot is then unset, so no later restore applies it again.
* :func:`errexit_restore` -- ``case ${__otto_e-} in e) set -e;; esac``: no
  proper tail of it sets ``-e`` (where ``[ -z ... ] || set -e`` has the tail
  ``set -e``); then ``unset __otto_e``, so no later restore applies it again.
* :func:`restore_line` -- the post-recovery line: an anchored
  ``whole_line_echo`` on the session's ``RESTORED__`` marker, carrying the
  two halves separated by ``"; "``.
* :func:`shape_c` -- two physical lines: the parent's errexit saved in
  ``__otto_e`` and turned off, the body in a subshell that turns it back on,
  the caller's stdin kept on fd 3 (and stdin at end of file when asked),
  then the tail (flag ``N``; ``B`` and the terminal restore only when the
  restore variable is set; the errexit restore always) and the flagged end
  record ``<end_prefix><flag><status>__``.
* :func:`shape_console` -- a console's exec frame: the terminal snapshot,
  then the same tail around a subshell that becomes a clean login shell
  (``exec env -i`` keeping ``HOME``, ``TERM``, ``USER``, ``LOGNAME`` and
  ``PATH`` and setting ``SHELL`` to the program it starts, then
  ``"${SHELL:-/bin/sh}" -l -c``), which changes to ``$HOME``,
  prints BEGIN and runs the body, all one quoted word.
"""

import shlex

from otto.host.command_frame import (
    RECOVER_SUFFIX,
    BashFrame,
    SessionMarkers,
    _continues,
    _runs_something,
    whole_line_echo,
)
from otto.host.terminal_mapping import SAVED_SETTINGS_PROBE

CREDENTIAL_FD = 3
"""The fd the exec frame keeps the caller's stdin on, for an elevation's password."""

ERREXIT_VAR = "__otto_e"
"""The unexported variable the frame saves the parent's errexit in."""

STATUS_VAR = "__otto_rc"
"""The unexported variable the frame holds the subshell's status in (as ``run``'s frame does)."""

FLAG_VAR = "__otto_f"
"""The unexported variable the tail holds the record's flag in: ``B`` present, ``N`` lost."""

BASELINE_VAR = "__otto_base"
"""The restore variable of a pooled exec: the session's handshake record."""

START_VAR = "__otto_tty"
"""The restore variable of a console exec: the terminal as the exec found it."""

RESTORED_SUFFIX = "RESTORED__"
"""The fixed end of the restore line's marker, where :func:`whole_line_echo` splits it."""


def terminal_restore(var: str) -> str:
    """The terminal half: restore the ``stty -g`` record in *var* when it is non-empty.

    The start snapshot (:data:`START_VAR`) is then unset; the baseline stays."""
    restore = BashFrame().restore_terminal(f"${{{var}-}}")
    put_back = f"case ${{{var}-}} in ?*) {restore};; esac"
    return f"{put_back}; unset {var}" if var == START_VAR else put_back


def errexit_restore() -> str:
    """The errexit half: turn ``set -e`` back on when the frame saved it on, then unset the save."""
    return f"case ${{{ERREXIT_VAR}-}} in e) set -e;; esac; unset {ERREXIT_VAR}"


def restored_marker(m: SessionMarkers) -> str:
    """The restore line's marker, ``__OTTO_<id>_RESTORED__``, from the recover marker's head."""
    return m.recover.removesuffix(RECOVER_SUFFIX) + RESTORED_SUFFIX


def restore_line(var: str, m: SessionMarkers) -> str:
    """The post-recovery restore line: both halves on an anchored ``whole_line_echo``."""
    return whole_line_echo(
        restored_marker(m),
        RESTORED_SUFFIX,
        statements=terminal_restore(var) + "; " + errexit_restore() + "; ",
    )


def saved_into(var: str) -> str:
    """Snapshot ``stty -g`` into *var*; empty when it cannot be read, and never aborts."""
    return f"{var}=$({SAVED_SETTINGS_PROBE}) || {var}="


def frame_body(cmd: str) -> str:
    """``BashFrame.frame``'s body rule: ``:`` for nothing to run, a newline after a continuation."""
    body = cmd if _runs_something(cmd) else ":"
    if _continues(body):
        body += "\n"
    return body


def exec_tail(var: str) -> str:
    """The tail: flag ``N``; ``B`` and the terminal restore only when *var* is set; errexit."""
    return (
        f"{FLAG_VAR}=N; case ${{{var}+x}} in x) {FLAG_VAR}=B; {terminal_restore(var)};; esac; "
        f"{errexit_restore()}"
    )


def end_record(m: SessionMarkers) -> str:
    """The flagged end record's echo: ``<end_prefix><flag><status>__``."""
    return f'echo "{m.end_prefix}${{{FLAG_VAR}}}${{{STATUS_VAR}}}__"'


def shape_c(
    body: str,
    *,
    m: SessionMarkers,
    stdin_eof: bool,
    start_var: "str | None",
    credential: bool = False,
) -> str:
    """The exec frame around *body*: two physical lines, ending in a newline.

    *start_var* ``None`` is a pooled exec, restoring :data:`BASELINE_VAR`;
    a name (the console's :data:`START_VAR`) snapshots the terminal into it
    first and restores that. *credential* keeps the caller's terminal on
    :data:`CREDENTIAL_FD` (``3<&0``), for a body that reads a password there.
    """
    start = "" if start_var is None else f"{saved_into(start_var)}; "
    var = BASELINE_VAR if start_var is None else start_var
    stdin = " </dev/null" if stdin_eof else ""
    keep = f" {CREDENTIAL_FD}<&0" if credential else ""
    return (
        f'{start}echo "{m.begin}"; case $- in *e*) {ERREXIT_VAR}=e;; *) {ERREXIT_VAR}=;; esac; '
        f"set +e; ( {errexit_restore()}; {frame_body(body)}\n"
        f"){keep}{stdin}; {STATUS_VAR}=$?; {exec_tail(var)}; {end_record(m)}\n"
    )


def shape_console(
    body: str, *, m: SessionMarkers, stdin_eof: bool, credential: bool = False
) -> str:
    """A console's exec frame around *body*: the login shell prints BEGIN, not the parent."""
    stdin = " </dev/null" if stdin_eof else ""
    keep = f" {CREDENTIAL_FD}<&0" if credential else ""
    keeps = (
        'HOME="${HOME-}" TERM="${TERM-}" USER="${USER-}" LOGNAME="${LOGNAME-}" PATH="${PATH-}" '
        'SHELL="${SHELL:-/bin/sh}"'
    )
    word = shlex.quote(f'cd "${{HOME:-/}}" || exit; echo "{m.begin}"; {frame_body(body)}\n')
    return (
        f"{saved_into(START_VAR)}; case $- in *e*) {ERREXIT_VAR}=e;; *) {ERREXIT_VAR}=;; esac; "
        f'set +e; ( exec env -i {keeps} "${{SHELL:-/bin/sh}}" -l -c {word}\n'
        f"){keep}{stdin}; {STATUS_VAR}=$?; {exec_tail(START_VAR)}; {end_record(m)}\n"
    )

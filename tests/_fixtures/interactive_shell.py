"""Interactive shells on a controlling pty, for typing probe lines the way a console does.

otto types its probe lines (the readiness handshake, the resync probe, the
recovery probe) into a login shell that reads them INTERACTIVELY from a
terminal, and a console server that delivers input in pieces lets login(1) or
su flush a line's head first, so the shell runs only a tail (#761, #853).
Under ``sh -c`` an unbalanced quote or brace is only a syntax error; on a pty
it is a continuation (PS2) that joins every later line, and a
redirection-only statement can start a ``cat`` that eats them. Only a real
interactive shell on its own controlling terminal shows either, so this module
starts one per case and models the session's rainy day: a tail, one probe
interval, one whole resend (:func:`cut_case`).

It also owns the shell matrix and the probe lines' rendering, so the ``sh -c``
portability cases, the fixed interactive cases and the nightly every-offset
sweep decide which shells run and what is typed in one place.
"""

import contextlib
import functools
import os
import re
import select
import shutil
import tempfile
import time
import uuid
from collections.abc import Iterator

import pytest

from otto.host.command_frame import BashFrame, SessionMarkers
from otto.host.console import strip_ansi
from otto.host.login_proxy import _identity_pattern, _identity_probe
from otto.host.session import ShellSession, ready_pattern
from otto.host.terminal_mapping import ONLCR, measure
from otto.utils import WaitTimeoutError, wait_for
from tests._fixtures.controlling_pty import ControllingPty, spawn_on_controlling_pty

# Always present on any machine that can run otto's own test suite; a missing
# one is a broken runner, not a skip.
REQUIRED_SHELLS: dict[str, list[str]] = {
    "sh": ["sh"],
    "bash": ["bash"],
}

# Tested when installed, NOT skipped-if-absent: a runner with more shells gets
# more coverage and a runner with fewer never silently loses the baseline. The
# dev VM provisions zsh/ksh/mksh for exactly this matrix (Vagrantfile,
# "dev-root"); CI's ubuntu-latest has neither ksh nor mksh, which is why these
# cannot be promoted to REQUIRED_SHELLS. Which ones actually ran is visible in
# the parametrized test ids — the suites cannot warn about the gap because
# `filterwarnings = ["error"]` would turn that into a failure.
#
# Each has earned its place by catching something the others could not:
#   busybox-sh  the ash dialect on container/embedded-ish targets; rejects
#               `set +o history`.
#   zsh         its `command` is a precommand modifier restricted to EXTERNAL
#               commands, so the POSIX de-specialization idiom is inert there;
#               interactively, a redirection-only command runs $NULLCMD (cat).
#   ksh         reports a readonly assignment from OUTSIDE the simple command's
#               own redirection, which a plain trailing `2>/dev/null` misses.
OPTIONAL_SHELLS: dict[str, list[str]] = {
    "dash": ["dash"],
    "ash": ["ash"],
    "zsh": ["zsh"],
    "ksh": ["ksh"],
    "mksh": ["mksh"],
    "busybox-sh": ["busybox", "sh"],
}

# Installed by the dev VM's "dev-root" provisioner specifically for this
# matrix. Not required (CI's ubuntu-latest lacks ksh/mksh) but their ABSENCE
# from OPTIONAL_SHELLS is a defect — see
# test_every_installed_shell_of_interest_is_actually_in_the_matrix.
PROVISIONED_SHELLS: tuple[str, ...] = ("zsh", "ksh", "mksh")


def _available() -> list[tuple[str, list[str]]]:
    found = [(name, argv) for name, argv in REQUIRED_SHELLS.items() if shutil.which(argv[0])]
    missing = sorted(set(REQUIRED_SHELLS) - {name for name, _ in found})
    if missing:
        raise RuntimeError(f"runner is missing baseline shell(s): {missing}")
    found += [(name, argv) for name, argv in OPTIONAL_SHELLS.items() if shutil.which(argv[0])]
    return found


SHELLS = _available()
"""``(name, argv)`` for every shell of the matrix installed here, the required ones first."""

# What each shell needs to run interactive and read no rc file. ksh and mksh
# have no such flag; they get ENV=/./dev/null instead (_SHELL_ENV).
_INTERACTIVE_FLAGS: dict[str, list[str]] = {
    "bash": ["--norc", "--noprofile", "-i"],
    "zsh": ["-f", "-i"],
}

SHELL_ARGV: dict[str, list[str]] = {
    name: [*argv, *_INTERACTIVE_FLAGS.get(name, ["-i"])]
    for name, argv in {**REQUIRED_SHELLS, **OPTIONAL_SHELLS}.items()
}
"""The interactive argv of each shell in the matrix, reading no rc file.

``zsh -f`` skips every user and system startup file except the system
``zshenv`` (``/etc/zsh/zshenv`` on Debian), which zsh reads before it parses
its options."""

_SHELL_ENV: dict[str, dict[str, str]] = {
    "ksh": {"ENV": "/./dev/null"},
    "mksh": {"ENV": "/./dev/null"},
}

_PTY_PROBE_INTERVAL = ShellSession._init_probe_interval
"""How long the session waits on one probe before resending it, so the delay
between a cut tail and its whole resend in :func:`cut_case`."""

READY_TIMEOUT = 5.0
"""Bound on any one wait for output. Only missing output ever reaches it."""

_FAILURE_TAIL = 300
"""How much of the terminal a failed wait quotes (a run of PS2 ``> `` is a wedge)."""

_PROMPT = "$ "


class InteractiveShell:
    """One interactive shell on the controller side of its controlling pty."""

    def __init__(self, name: str, terminal: ControllingPty) -> None:
        self.name = name
        self.terminal = terminal
        self._poll = select.poll()
        self._poll.register(terminal.controller, select.POLLIN)
        self._raw = bytearray()

    @property
    def output(self) -> str:
        """Everything the terminal printed so far, raw (control sequences and CRs kept)."""
        self._drain()
        return self._raw.decode(errors="replace")

    def send(self, text: str) -> None:
        """Type *text* into the terminal, all of it."""
        data = text.encode()
        while data:
            written = os.write(self.terminal.controller, data)
            data = data[written:]

    def wait_for_output(self, pattern: "re.Pattern[str]", timeout: float) -> str:
        """Wait until *pattern* matches the output; return everything printed so far.

        Fails the test after *timeout* seconds with the last characters the
        terminal printed, PS2 continuation prompts included.
        """
        return self._wait(lambda: pattern.search(self.output) is not None, pattern.pattern, timeout)

    def run(self, command: str) -> str:
        """Type *command* as one line and wait until it finished and the shell prompts again.

        Completion is a fresh token the line's own execution prints at a line
        start (the quotes keep it out of the typed text, so out of its echo),
        followed by the prompt. Waiting for the prompt too means the line
        editor has taken the terminal back, so what is typed next is read as
        a new line rather than echoed as typeahead. Returns what the command
        printed, its typed line's echo and the token line included.
        """
        mark = len(self.output)
        token = uuid.uuid4().hex
        self.send(f'{command}; echo DONE_"{token}"\n')
        done = re.compile(rf"\nDONE_{token}\r*\n")
        found = done.search(self.wait_for_output(done, READY_TIMEOUT), mark)
        assert found is not None
        self._wait_for_prompt(after=found.end())
        return self.output[mark : found.end()]

    def _wait_for_prompt(self, after: int = 0) -> None:
        """Wait until the output past *after*, control sequences stripped, ends in the PS1 text."""

        def prompted() -> bool:
            # The prompt view drops CSI (zsh's bracketed paste, BusyBox's cursor
            # query) and OSC sequences; the raw output keeps them, because the
            # calibration is measured on raw bytes.
            return strip_ansi(self.output[after:]).endswith(_PROMPT)

        self._wait(prompted, f"the prompt {_PROMPT!r}", READY_TIMEOUT)

    def _wait(self, ready, what: str, timeout: float) -> str:
        try:
            wait_for(ready, timeout, interval=0.01, on_timeout=f"{self.name} never printed {what}")
        except WaitTimeoutError:
            pytest.fail(
                f"{self.name}: no {what} within {timeout}s; the terminal ended "
                f"{self.output[-_FAILURE_TAIL:]!r}"
            )
        return self.output

    def _drain(self) -> None:
        while self._poll.poll(0):
            try:
                chunk = os.read(self.terminal.controller, 65536)
            except OSError:  # EIO: the shell side is gone
                return
            if not chunk:
                return
            self._raw.extend(chunk)


@contextlib.contextmanager
def interactive_shell(
    name: str, *, onlcr: bool = True, setup: str = ""
) -> Iterator[InteractiveShell]:
    """Start shell *name* (a :data:`SHELL_ARGV` key) interactive on its own controlling pty.

    Its environment is only an empty ``HOME``, ``PATH``, ``PS1="$ "``,
    ``PS2="> "`` and ``TERM=dumb`` (plus :data:`_SHELL_ENV`). It yields once
    the shell has printed its prompt. *onlcr* sets the terminal's ``onlcr``
    before the shell starts. *setup*, when given, is typed first as one line
    (:meth:`InteractiveShell.run`). The shell and its foreground job are
    killed on exit.
    """
    with tempfile.TemporaryDirectory(prefix="otto-ishell-") as home:
        env = {
            "HOME": home,
            "PATH": os.environ["PATH"],
            "PS1": _PROMPT,
            "PS2": "> ",
            "TERM": "dumb",
            **_SHELL_ENV.get(name, {}),
        }
        terminal = spawn_on_controlling_pty(SHELL_ARGV[name], env=env, onlcr=onlcr)
        try:
            shell = InteractiveShell(name, terminal)
            shell._wait_for_prompt()
            if setup:
                shell.run(setup)
            yield shell
        finally:
            terminal.close()


def cut_case(
    shell: InteractiveShell,
    line_a: str,
    offset: int,
    line_b: str,
    answered_b: "re.Pattern[str]",
    answered_a: "re.Pattern[str]",
) -> str:
    """Type a cut line and its one whole resend, the way ``confirm_live`` does on a rainy day.

    Types the tail ``line_a[offset:]`` (login flushed the head), waits one
    :data:`_PTY_PROBE_INTERVAL`, types *line_b* once, and waits for
    *answered_b*. The shell need not print a prompt between them. Asserts
    the tail never answered (*answered_a*, so *line_a* and *line_b* carry
    distinct markers) and returns everything the terminal printed.
    """
    shell.send(line_a[offset:])
    time.sleep(_PTY_PROBE_INTERVAL)  # the session's resend interval: the one fixed delay
    shell.send(line_b)
    out = shell.wait_for_output(answered_b, READY_TIMEOUT)
    assert answered_a.search(out) is None, (
        f"{shell.name}: the tail {line_a[offset:]!r} answered for the whole line; "
        f"the terminal ended {out[-_FAILURE_TAIL:]!r}"
    )
    return out


TAIL_MARKERS = SessionMarkers.for_session("cafef00d")
"""The markers of a line that is cut (and of a line typed whole once)."""
RESEND_MARKERS = SessionMarkers.for_session("beadfeed")
"""The whole resend's markers, distinct from :data:`TAIL_MARKERS`, so an answer names its line."""

PROBE_LINES = ("handshake", "resync", "recovery")
"""The probe lines otto types into a shell that may run only a tail of them.

The readiness handshake and the resync probe open a fresh shell; the
recovery probe follows an interrupt after a command timed out."""

HISTORY_LINES = ("handshake", "resync")
"""The :data:`PROBE_LINES` that carry history suppression when the host asks for it.

They are the first line a fresh shell runs. The recovery probe is written
into a shell that is already set up, so it never carries it."""

_RENDER = {
    "handshake": lambda m, prefix: BashFrame().handshake(m, prefix),
    "resync": _identity_probe,
    "recovery": lambda m, _prefix: BashFrame().recover(m),
}

_ANSWER = {
    "handshake": lambda m: ready_pattern(m, line_ending=True),
    "resync": _identity_pattern,
    "recovery": BashFrame().recover_pattern,
}


@functools.cache
def render_probe(kind: str, m: SessionMarkers, history: bool = True) -> str:
    """The product's *kind* line (a :data:`PROBE_LINES` entry) rendered with markers *m*.

    With *history*, history suppression rides in front of it, as on a
    session's real first line. A line outside :data:`HISTORY_LINES` never
    carries it, so there *history* changes nothing.
    """
    return _RENDER[kind](m, BashFrame().quiet_history() if history else "")


def probe_answer(kind: str, m: SessionMarkers) -> "re.Pattern[str]":
    """What the shell prints when it ran a whole *kind* line rendered with markers *m*."""
    return _ANSWER[kind](m)


def cut_probe(
    name: str,
    kind: str,
    offset: int,
    tail: SessionMarkers,
    resend: SessionMarkers,
    *,
    history: bool = True,
    setup: str = "",
) -> str:
    """Cut a *kind* line at *offset* in a fresh *name* shell, resend it whole, require the answer.

    The tail is the line rendered with *tail*'s markers and the resend the
    line rendered with *resend*'s, so an answer from the tail is told apart
    (:func:`cut_case`). A handshake's answer must also carry its
    calibration, measured ``onlcr`` as on any default Linux terminal, so the
    resend ran from its first byte. *setup* is typed before the cut
    (:func:`interactive_shell`). Returns everything the terminal printed.
    """
    with interactive_shell(name, setup=setup) as shell:
        out = cut_case(
            shell,
            render_probe(kind, tail, history),
            offset,
            render_probe(kind, resend, history),
            probe_answer(kind, resend),
            probe_answer(kind, tail),
        )
    if kind == "handshake":
        found = probe_answer(kind, resend).search(out)
        assert found is not None
        assert measure(out[: found.end()], resend.ready).mapping == ONLCR, (
            f"{name}: {out[-_FAILURE_TAIL:]!r}"
        )
    return out

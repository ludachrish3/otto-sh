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
import dataclasses
import functools
import os
import pathlib
import re
import select
import shutil
import tempfile
import time
import uuid
from collections.abc import Iterator

import pytest

from otto.host.command_frame import (
    BashFrame,
    CommandFrame,
    ExecRestore,
    SessionMarkers,
    handshake_prefix,
    shell_prefix,
)
from otto.host.console import strip_ansi
from otto.host.login_proxy import _identity_pattern, _identity_probe
from otto.host.session import ShellSession, TelnetSession, ready_pattern
from otto.host.terminal_mapping import ONLCR, establish_exec_state, measure
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

READS_THE_TERMINAL: tuple[str, ...] = ("n", "ed", "fc", "b", "r", "vi", "less", "more")
"""Command names no tail of a probe line may run: each reads its terminal on common systems.

A cut tail's first word is a suffix of some word of the line, and the shell
runs it as a command (:func:`~otto.host.command_frame.whole_line_echo`'s cut
rules). On a pty, a program that waits for input eats every resend. ``n`` is
node's version manager on GitHub's ubuntu runner (it once ran from the
calibration's last escape), ``ed`` is BusyBox's editor (it once ran from a
marker id ending in ``ed``), ``fc`` and ``r`` are history editors (builtins in
most interactive shells, which open ``$FCEDIT``, ``vi`` or ``ed``), and the
pagers and editors complete the set any target may have installed."""

FAKE_RAN = "__FAKE_TERMINAL_READER_RAN__"
"""What a :func:`terminal_reader_fakes` fake prints when it runs, before it reads stdin."""


def terminal_reader_fakes(directory: str) -> str:
    """Write an input-waiting fake for each :data:`READS_THE_TERMINAL` name into *directory*.

    Each fake prints :data:`FAKE_RAN` and its own name, then reads stdin until
    it closes, as the real program waits on a terminal. Returns ``PATH`` with
    *directory* first and the inherited ``PATH`` after it, so ``stty`` and
    ``printf`` still resolve. A tail that runs one of these names then fails
    on any machine, whatever that machine happens to have installed: on a
    pty the fake eats the resend, and under ``sh -c`` its output names it.
    """
    for name in READS_THE_TERMINAL:
        fake = pathlib.Path(directory, name)
        fake.write_text(f'#!/bin/sh\necho "{FAKE_RAN}:{name}"\nexec cat\n')
        fake.chmod(0o755)
    return f"{directory}{os.pathsep}{os.environ['PATH']}"


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
        end = self._typed(
            f'{command}; echo DONE_"{token}"\n', re.compile(rf"\nDONE_{token}\r*\n"), READY_TIMEOUT
        )
        return self.output[mark:end]

    def type_line(self, line: str, answered: "re.Pattern[str]", timeout: float) -> str:
        """Type *line* (its newline included), wait for *answered* and for the next prompt.

        For a line that prints its own answer, such as a session's handshake.
        Returns everything the terminal printed so far. The prompt is waited
        for as :meth:`run` waits for it: a shell prints its answer and its
        next prompt in two writes, and a caller that went on between them
        would find that prompt at the head of what its next command printed
        (#1343).
        """
        self._typed(line, answered, timeout)
        return self.output

    def _typed(self, line: str, answered: "re.Pattern[str]", timeout: float) -> int:
        """Type *line*; wait for *answered* past here, then the prompt. Return the answer's end.

        The one statement of "this line is over": its answer printed and the
        line editor has the terminal back.
        """
        mark = len(self.output)
        self.send(line)
        self._wait(
            lambda: answered.search(self.output, mark) is not None, answered.pattern, timeout
        )
        found = answered.search(self.output, mark)
        assert found is not None
        self._wait_for_prompt(after=found.end())
        return found.end()

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


def shell_env(
    name: str, home: str, fakes: str, *, overlay: "dict[str, str] | None" = None
) -> dict[str, str]:
    """The environment a matrix shell *name* starts interactive in: the one builder of it.

    Only an empty ``HOME`` (*home*), ``PATH`` led by
    :func:`terminal_reader_fakes` written into *fakes*, ``PS1="$ "``,
    ``PS2="> "``, ``TERM=dumb`` and :data:`_SHELL_ENV`'s entry for *name*,
    then *overlay* on top.
    """
    return {
        "HOME": home,
        "PATH": terminal_reader_fakes(fakes),
        "PS1": _PROMPT,
        "PS2": "> ",
        "TERM": "dumb",
        **_SHELL_ENV.get(name, {}),
        **(overlay or {}),
    }


@contextlib.contextmanager
def interactive_shell(
    name: str,
    *,
    onlcr: bool = True,
    setup: str = "",
    env: "dict[str, str] | None" = None,
) -> Iterator[InteractiveShell]:
    """Start shell *name* (a :data:`SHELL_ARGV` key) interactive on its own controlling pty.

    Its environment is :func:`shell_env`'s, with *env* on top (its fake
    terminal readers make a tail that runs one fail here on any machine). It
    yields once the shell has printed its prompt. *onlcr* sets the
    terminal's ``onlcr`` before the shell starts. *setup*, when given, is
    typed first as one line (:meth:`InteractiveShell.run`). The shell and
    its foreground job are killed on exit.
    """
    with (
        tempfile.TemporaryDirectory(prefix="otto-ishell-") as home,
        tempfile.TemporaryDirectory(prefix="otto-ishell-fakes-") as fakes,
    ):
        terminal = spawn_on_controlling_pty(
            SHELL_ARGV[name], env=shell_env(name, home, fakes, overlay=env), onlcr=onlcr
        )
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


TAIL_MARKERS = SessionMarkers.for_session(session_id="cafef00d")
"""The markers of a line that is cut (and of a line typed whole once)."""
RESEND_MARKERS = SessionMarkers.for_session(session_id="beadfeed")
"""The whole resend's markers, distinct from :data:`TAIL_MARKERS`, so an answer names its line."""
HAZARD_MARKERS: dict[str, SessionMarkers] = {
    name: SessionMarkers.for_session(session_id=session_id)
    for name, session_id in (("ed", "cafef0ed"), ("fc", "cafef0fc"), ("b", "cafef00b"))
}
"""Cut-line markers whose id ends in a :data:`READS_THE_TERMINAL` name, keyed by that name.

Ids are random hex, so some session's id ends in each of these; a tail cut
inside the echo word's head must not leave it as a word of its own."""

PROBE_LINES = (
    "handshake",
    "console-handshake",
    "resync",
    "recovery",
    "exec-restore",
    "work-console-handshake",
    "work-entry",
)
"""The probe lines otto types into a shell that may run only a tail of them.

The readiness handshake and the resync probe open a fresh shell; the
recovery probe follows an interrupt after a command timed out, and the
post-recovery restore line (``exec-restore``) follows the recovery of a
timed-out ``exec``. The console handshake is the readiness handshake a
session whose terminal outlives it sends first, with the settings save in
front (``handshake_prefix``). On a console whose shell outlives the session
the first line is the work-shell console handshake instead (the save and no
shell prefix, typed into the person's shell), and ``work-entry`` is the work
shell's entry proof, the resync probe carrying the frame's work-shell lines,
cut in each state :data:`WORK_OWNERSHIP` names. Both are rendered here
without the frame's lead: a cut inside the lead leaves the whole line, which
answers."""

ESTABLISHED = establish_exec_state(replace=True)
"""What a session's handshake leaves in its shell: the exec frame's variables, set."""

PROBE_STATES: dict[str, tuple[bool, ...]] = {
    "handshake": (False,),
    "console-handshake": (False,),
    "resync": (False, True),
    "recovery": (False,),
    "exec-restore": (True,),
    "work-console-handshake": (False,),
    "work-entry": (False, True),
}
"""Per :data:`PROBE_LINES` kind, whether the shell it is cut in has :data:`ESTABLISHED` typed first.

A handshake opens a fresh shell. The resync probe meets both: a fresh shell
after a switch, which it establishes, and an established one (the outer
shell a rejected ``su`` returned to, the one an undo lands back in), which
it keeps. The restore line follows an ``exec`` on an established session;
the same line with the record lost is
``test_exec_terminal.py::test_exec_restore_line_with_lost_state_answers_and_restores_nothing``."""

HANDSHAKE_LINES = ("handshake", "console-handshake", "work-console-handshake")
"""The :data:`PROBE_LINES` that carry the terminal-mapping calibration."""

HISTORY_LINES = ("handshake", "console-handshake", "resync", "work-entry")
"""The :data:`PROBE_LINES` that carry history suppression when the host asks for it.

They are the first line a fresh shell runs. The recovery probe is written
into a shell that is already set up, so it never carries it. The work-shell
console handshake is typed into the person's shell, which keeps its history:
the entry proof that follows carries the suppression."""

WORK_OWNERSHIP = ("unowned", "started", "leftover")
"""The states a work shell's entry proof is cut in (:func:`cut_probe`'s *ownership*).

``unowned`` is a shell as it was spawned, a person's shell; ``started`` a
shell the start line started (``__otto_child=1`` in its environment);
``leftover`` a work shell an earlier session left (the work mark's alias
defined). The proof must answer the mark in the last two and never in the
first."""

TAB_BINDING_QUERY = (
    "case ${BASH_VERSION:-} in ?*) bind -p 2>/dev/null;; esac; "
    "case ${ZSH_VERSION:-} in ?*) bindkey '^I';; esac; "
    "case ${KSH_VERSION:-} in *MIRBSD*|*LEGACY*) bind;; esac"
)
"""What prints a shell's TAB binding in its line editor: bash ``bind -p``, zsh's, mksh's.

The binding :meth:`~otto.host.command_frame.BashFrame.script_parsing` changes;
the other shells print nothing."""


@dataclasses.dataclass(frozen=True)
class ShellState:
    """What a person's shell holds that a work shell must leave alone."""

    pid: int
    program: str
    options: str
    """``set +o``."""
    tab: str
    """The line editor's TAB binding, as :data:`TAB_BINDING_QUERY` prints it."""
    pv: str
    """``${PV-unset}``: an unexported variable typed by hand."""
    pfunc: bool
    palias: bool
    work_mark: bool
    """Whether the work mark's alias is defined (``__otto_as_w`` unless another is named)."""
    histfile: str
    """``${HISTFILE-unset}``."""
    cwd: str
    """``pwd``."""
    otto_t: str
    """``${OTTO_T-unset}``: what a session's ``export OTTO_T=...`` would leave behind."""


@dataclasses.dataclass(frozen=True)
class PersonState:
    """A person's shell and the terminal it reads."""

    shell: ShellState
    terminal: str
    """``stty -g``."""


_PERSON_PROBES = {
    "pid": "echo $$",
    "program": "readlink /proc/$$/exe",
    "options": "set +o",
    "tab": TAB_BINDING_QUERY,
    "pv": "echo ${PV-unset}",
    "pfunc": "command -v pfunc >/dev/null 2>&1 && echo 1 || echo 0",
    "palias": "alias palias >/dev/null 2>&1 && echo 1 || echo 0",
    "mark": "alias {mark} >/dev/null 2>&1 && echo 1 || echo 0",
    "hist": "echo ${HISTFILE-unset}",
    "cwd": "pwd",
    "otto_t": "echo ${OTTO_T-unset}",
    "tty": "stty -g",
}
"""Each :class:`PersonState` field's command, in the order :func:`person_state_line` types them.

``{mark}`` is the work mark's alias the reading asks about."""


@dataclasses.dataclass(frozen=True)
class PersonStateLine:
    """The line :func:`person_state_line` types, and how its output is read."""

    line: str
    head: str
    """The section words' common head: a fresh token."""

    @property
    def done(self) -> "re.Pattern[str]":
        """What ends the line's output: its last section word."""
        return re.compile(rf"{self.head}end\r*\n")


def person_state_line(*, mark: str = "__otto_as_w") -> PersonStateLine:
    """One line reading a person's shell state (:class:`PersonState`).

    Each command's output follows a section word of its own, made of a fresh
    token, which the quotes split in the typed text so no echo of the line
    carries it. *mark* is the work mark's alias
    :attr:`ShellState.work_mark` reads. The one builder of the reading, for
    a session's harness and the synchronous one (:func:`read_person_state`).
    """
    head = f"__PS_{uuid.uuid4().hex[:8]}_"

    def section(name: str) -> str:
        return f'echo {head}""{name}'

    line = "; ".join(
        f"{section(name)}; {cmd.replace('{mark}', mark)}" for name, cmd in _PERSON_PROBES.items()
    )
    return PersonStateLine(line=f"{line}; {section('end')}\n", head=head)


def parse_person_state(out: str, typed: PersonStateLine) -> PersonState:
    """Read the :class:`PersonState` in *out*, what the shell printed for *typed*."""
    head = typed.head
    lines = out.replace("\r", "").split("\n")

    def at(name: str) -> int:
        # The line the section's echo printed; a prompt may lead it, and the
        # typed line's echo never ends with it.
        return max(i for i, text in enumerate(lines) if text.endswith(head + name))

    names = list(_PERSON_PROBES)
    found = {
        name: "\n".join(lines[at(name) + 1 : at(nxt)])
        for name, nxt in zip(names, [*names[1:], "end"], strict=True)
    }
    return PersonState(
        shell=ShellState(
            pid=int(found["pid"]),
            program=found["program"],
            options=found["options"],
            tab=found["tab"],
            pv=found["pv"],
            pfunc=found["pfunc"] == "1",
            palias=found["palias"] == "1",
            work_mark=found["mark"] == "1",
            histfile=found["hist"],
            cwd=found["cwd"],
            otto_t=found["otto_t"],
        ),
        terminal=found["tty"],
    )


def read_person_state(shell: "InteractiveShell") -> PersonState:
    """Read the state of the shell reading *shell*'s terminal now (:func:`person_state_line`)."""
    typed = person_state_line()
    mark = len(shell.output)
    shell.send(typed.line)
    shell.wait_for_output(typed.done, READY_TIMEOUT)
    found = typed.done.search(shell.output, mark)
    assert found is not None
    return parse_person_state(shell.output[mark : found.end()], typed)


_WORK_STATE = {
    "unowned": ({}, ""),
    "started": ({"__otto_child": "1"}, ""),
    "leftover": ({}, "alias __otto_as_w=:"),
}
"""Per :data:`WORK_OWNERSHIP` state: the environment the shell starts with, what is typed first."""


def _handshake(m: SessionMarkers, history: bool, *, save_terminal: bool) -> str:
    """The session's handshake line: its prefix composed as the session composes it."""
    prefix = handshake_prefix(
        BashFrame(), m, shell_history=not history, save_terminal=save_terminal
    )
    return BashFrame().handshake(m, prefix=prefix)


@dataclasses.dataclass(frozen=True)
class TypedWorkShellLines:
    """The three lines a console session types to enter its work shell, as it types them."""

    line_one: str
    start: str
    proof: str
    lead: str


def typed_work_shell_lines(
    frame: CommandFrame, *, quiet: bool, m: "SessionMarkers | None" = None
) -> TypedWorkShellLines:
    """The lines a session on *frame*, on a console whose shell outlives it, enters with.

    Rendered by the session itself: line 1 by its ``_handshake_payload`` with
    the lines its ``_enters_work_shell`` chose, and the proof by the one probe
    builder its entry resyncs through. *quiet* is history suppression; *m*
    the markers, the session's own by default.
    """
    from tests.unit.host._pty_session import outliving_console  # it builds on this module

    session = TelnetSession(
        reader=None,
        writer=None,
        console_client=outliving_console(),
        command_frame=frame,
        shell_history=not quiet,
    )
    work = session._enters_work_shell()
    assert work is not None
    m = session.markers if m is None else m
    return TypedWorkShellLines(
        line_one=session._handshake_payload(m, save=session._saves_terminal(), work=work),
        start=work.lead + work.start,
        proof=_identity_probe(m, "", work=work),
        lead=work.lead,
    )


def _work_console_handshake(m: SessionMarkers) -> str:
    """The first line typed into the person's shell, without its lead: the save, no prefix."""
    typed = typed_work_shell_lines(BashFrame(), quiet=False, m=m)
    return typed.line_one.removeprefix(typed.lead)


def _work_entry(m: SessionMarkers, history: bool) -> str:
    """The work shell's entry proof without its lead, history suppressed where *history* asks."""
    typed = typed_work_shell_lines(BashFrame(), quiet=history, m=m)
    return typed.proof.removeprefix(typed.lead)


def _restore_line(m: SessionMarkers) -> str:
    """The post-recovery restore line of a pooled session's ``exec``, as the session writes it."""
    line = BashFrame().restore_line(ExecRestore.BASELINE, m)
    assert line is not None
    return line


def _restored_pattern(m: SessionMarkers) -> "re.Pattern[str]":
    """What the shell prints when it ran the whole restore line."""
    pattern = BashFrame().restored_pattern(m)
    assert pattern is not None
    return pattern


_RENDER = {
    "handshake": lambda m, history: _handshake(m, history, save_terminal=False),
    "console-handshake": lambda m, history: _handshake(m, history, save_terminal=True),
    # The prefix the host's switch and its undo give the probe (PosixPrivilege._shell_prefix).
    "resync": lambda m, history: _identity_probe(
        m, shell_prefix(BashFrame(), shell_history=not history)
    ),
    "recovery": lambda m, _history: BashFrame().recover(m),
    "exec-restore": lambda m, _history: _restore_line(m),
    "work-console-handshake": lambda m, _history: _work_console_handshake(m),
    "work-entry": _work_entry,
}

_ANSWER = {
    "handshake": lambda m: ready_pattern(m, line_ending=True),
    "console-handshake": lambda m: ready_pattern(m, line_ending=True),
    "resync": _identity_pattern,
    "recovery": BashFrame().recover_pattern,
    "exec-restore": _restored_pattern,
    "work-console-handshake": lambda m: ready_pattern(m, line_ending=True),
    "work-entry": lambda m: _identity_pattern(m, reads_work_mark=True),
}


@functools.cache
def render_probe(kind: str, m: SessionMarkers, history: bool = True) -> str:
    """The product's *kind* line (a :data:`PROBE_LINES` entry) rendered with markers *m*.

    With *history*, history suppression rides in front of it, as on a
    session's real first line. A line outside :data:`HISTORY_LINES` never
    carries it, so there *history* changes nothing.
    """
    return _RENDER[kind](m, history)


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
    established: bool = False,
    ownership: "str | None" = None,
) -> str:
    """Cut a *kind* line at *offset* in a fresh *name* shell, resend it whole, require the answer.

    The tail is the line rendered with *tail*'s markers and the resend the
    line rendered with *resend*'s, so an answer from the tail is told apart
    (:func:`cut_case`). A handshake's answer must also carry its
    calibration, measured ``onlcr`` as on any default Linux terminal, so the
    resend ran from its first byte. *setup*, then :data:`ESTABLISHED` when
    *established* (:data:`PROBE_STATES`), is typed before the cut as one
    line (:func:`interactive_shell`). *ownership*, for the ``work-entry``
    kind only, is the :data:`WORK_OWNERSHIP` state the shell is put in
    first, and the whole resend's readback must be ``1`` exactly where that
    state owns the mark. Returns everything the terminal printed.
    """
    if (ownership is not None) != (kind == "work-entry"):
        raise ValueError(f"ownership {ownership!r} is for the work-entry kind only, not {kind!r}")
    env, owned = _WORK_STATE[ownership] if ownership is not None else ({}, "")
    typed = "; ".join(part for part in (owned, setup, ESTABLISHED if established else "") if part)
    with interactive_shell(name, setup=typed, env=env) as shell:
        out = cut_case(
            shell,
            render_probe(kind, tail, history),
            offset,
            render_probe(kind, resend, history),
            probe_answer(kind, resend),
            probe_answer(kind, tail),
        )
    if kind in HANDSHAKE_LINES:
        found = probe_answer(kind, resend).search(out)
        assert found is not None
        assert measure(out[: found.end()], resend.ready).mapping == ONLCR, (
            f"{name}: {out[-_FAILURE_TAIL:]!r}"
        )
    if ownership is not None:
        found = probe_answer(kind, resend).search(out)
        assert found is not None
        assert found.group(3) == ("" if ownership == "unowned" else "1"), (
            f"{name} ({ownership}): readback {found.group(3)!r}; {out[-_FAILURE_TAIL:]!r}"
        )
    return out


# What a user's rc file may have set before otto's first line: each completion
# binding bash's readline offers for TAB, and vi editing mode.
RC_EDITING = [
    *(
        ("bash", rc)
        for rc in (
            "bind 'TAB: menu-complete'",
            "bind 'TAB: glob-complete-word'",
            "bind 'TAB: complete-filename'",
            "set -o vi",
            "set -o vi; bind 'TAB: menu-complete'",
        )
    ),
    ("mksh", "set -o vi"),
]
"""(shell, rc line) pairs; a test keeps the ones whose shell is installed."""

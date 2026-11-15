"""The pid witness (DEF-WIT): one format, one observer, one cleanup, typed and hermetic.

A stimulus that starts a process the device must later have stopped records
that process in a WITNESS file: one line ``<pid> <starttime>``, two decimal
fields each matching ``[1-9][0-9]*``, written by the process itself (``$$``
and field 22 of ``/proc/$$/stat``). The start time tells a reused pid from
the recorded process, as :func:`scripts.process_group.alive` does locally.

Shell side, for a device:

* :func:`witness_write_template` -- the statement that writes the witness
  for the current shell process.
* :func:`process_state_template` -- the ONE observer. It prints exactly one
  of :data:`NO_WITNESS`, :data:`RUNNING`, :data:`STOPPED`,
  :data:`UNOBSERVABLE` and never fails or exits, under any inherited
  options: it runs in a subshell that turns ``-e``/``-u`` off first, and the
  subshell's status is forced to 0.
* :func:`long_running_template`, :func:`running_in_background_template` and
  :func:`finished_template` -- the stimuli that write it: one the device must
  stop, one left running, one that has exited.
* :func:`witness_cleanup_template` -- the ONE cleanup: the observer, then
  ``kill -9`` only on :data:`RUNNING`, then the file's removal, every step
  guarded. The check and the kill are not atomic, so PID reuse is narrowed,
  not excluded.

:func:`format_template` turns any of them into the ``{path}`` template a
:class:`~tests.conformance._vocabulary.Vocabulary` field holds, so the suite's
vocabulary carries these statements and no second spelling of them.

:class:`FakeWitnesses` answers those lines in memory, for the suite's fake hosts.

Hermetic side, the same table over the local ``/proc``: :func:`observe` and
:func:`clean` (and :func:`observation`, the answer with the pid it read),
through :mod:`scripts.process_group` (the repository's one ``/proc`` parser).
``tests/_fixtures/liveness.py::pid_running`` is not used for witnesses: it
has no start time.

The table (pinned by ``tests/unit/test_conformance_witness.py``):

====================================================  ==================
witness file                                          answer
====================================================  ==================
missing or empty                                      no-witness
not exactly two fields, a field not ``[1-9][0-9]*``,  unobservable
or ``/proc`` absent or unreadable
pid gone, start time differs, or a zombie             stopped
otherwise                                             running
====================================================  ==================
"""

import contextlib
import dataclasses
import os
import re
import signal
from collections.abc import Callable
from pathlib import Path

from scripts.process_group import alive, proc_stat

NO_WITNESS = "otto-cf-no-witness"
RUNNING = "otto-cf-running"
STOPPED = "otto-cf-stopped"
UNOBSERVABLE = "otto-cf-unobservable"
ANSWERS = (NO_WITNESS, RUNNING, STOPPED, UNOBSERVABLE)

ANSWER_PATTERN = re.compile("|".join(re.escape(a) for a in ANSWERS))
"""Matches the one answer the observer printed (the typed line never contains one whole)."""

_FIELD = r"[1-9][0-9]*"
WITNESS_FORMAT = re.compile(rf"({_FIELD}) ({_FIELD})\n?")
"""The witness file's whole content: ``<pid> <starttime>``, one optional final newline."""


def witness_write_template(path: str) -> str:
    """The statement that records the current shell process into the witness at *path*.

    Quote-free, so it can sit inside a single-quoted ``sh -c '...'``.
    Field 22 of ``/proc/$$/stat`` is the 20th field after the ``(comm)``
    word.

    THE FILE IS READ BY ANOTHER PROCESS (``cat`` in a command substitution),
    never by the shell itself with ``read``. Measured 2026-11-14 on this dev
    VM: a BusyBox shell run by qemu-user reads its own ``/proc/<pid>/stat``
    through qemu's emulation, whose start time was one tick off the kernel's
    in 1 of 5 runs, so the observer read the live process as "stopped".
    qemu emulates only a process's own entry; ``cat``'s pid differs, so it
    reads the kernel's.
    """
    return (
        f"__w_s=$(cat /proc/$$/stat) && __w_s=${{__w_s##*)}} && set -f && set -- $__w_s "
        f"&& shift 19 && echo $$ $1 >{path}"
    )


STARTED_TOKEN = "otto-cf-started"
"""What the long-running stimulus prints once it has written its witness: the one declaration
its producer (:func:`long_running_template`), the cancel contract that waits for it and the
support matrix's fake that relays it all read."""


_WITNESSED_SHELL = "sh -c '"
"""How :func:`long_running_template` opens the shell that becomes the witnessed process."""


def long_running_template(path: str, *, seconds: int = 60) -> str:
    """A stimulus process that writes its own witness at *path*, then sleeps as the same pid."""
    return (
        f"{_WITNESSED_SHELL}{witness_write_template(path)}; echo {STARTED_TOKEN}; "
        f"exec sleep {seconds}'"
    )


def ignoring(command: str, signals: str) -> str:
    """*command* (a :func:`long_running_template` spelling), its witnessed shell ignoring *signals*.

    The ignore is set BY THE WITNESSED SHELL ITSELF, not inherited from a
    wrapper. Measured 2026-11-16 on the lab's BusyBox 1.16.1, 1.21.1, 1.28.1
    and 1.31.0 guests (``SigIgn`` in ``/proc/<pid>/status``): a new ``sh``
    started with ``SIGHUP`` ignored (``(trap '' HUP; exec sh -c ...)``)
    resets it to the default, so its sleeper dies of the hang-up; BusyBox
    1.35.0 and GNU shells keep it. A ``trap "" HUP`` inside the shell holds
    across its ``exec sleep`` on all five guests.
    """
    assert command.count(_WITNESSED_SHELL) == 1, command
    return command.replace(_WITNESSED_SHELL, f'{_WITNESSED_SHELL}trap "" {signals}; ')


def without_ignoring(command: str) -> str:
    """*command* with an :func:`ignoring` ignore taken out again (a fake host's reading of it)."""
    return re.sub(rf"{re.escape(_WITNESSED_SHELL)}trap \"\" [A-Z ]+; ", _WITNESSED_SHELL, command)


def running_in_background_template(path: str, *, seconds: int = 30) -> str:
    """A process left running in the background, its witness at *path*; the call returns at once.

    Its stdin and output are closed, so neither ``run``'s session nor an
    ``exec``'s channel waits for it; the cleanup is what stops it.

    STARTED DETACHED, NEVER A JOB OF THE SHELL THAT TYPES IT: the subshell
    that puts it in the background exits at once, so the starting shell has
    no job to track. Measured 2026-11-14 on the lab's BusyBox 1.28.1 guest
    over telnet: an interactive BusyBox shell (1.28.1 and later) whose own
    background job is killed with ``SIGKILL`` can end, taking the session
    with it, and the cleanup ``kill -9`` s this process. Detached, the kill
    leaves the session's shell alone, on every shell.
    """
    return (
        f"( sh -c '{witness_write_template(path)}; exec sleep {seconds}' "
        "</dev/null >/dev/null 2>&1 & )"
    )


def finished_template(path: str) -> str:
    """A process that writes its witness at *path* and exits before the call returns."""
    return f"sh -c '{witness_write_template(path)}'"


def format_template(render: "Callable[[str], str]") -> str:
    """*render*'s statement as a ``str.format`` template over ``{path}``.

    The statements hold shell braces (``${s##*)}``), so the rendered text
    doubles every brace and leaves one ``{path}`` field where *render* put
    the path: ``format_template(f).format(path=p) == f(p)``.
    """
    hole = "\0"
    return render(hole).replace("{", "{{").replace("}", "}}").replace(hole, "{path}")


def _observer_body(path: str) -> str:
    """The observer's statements, setting ``a`` (the answer) and ``p`` (the pid) in a subshell.

    Reads the file without ``cat``'s fork where possible: ``read`` stops at
    the first line, so a second line is caught by comparing with ``$(cat)``.
    """
    return (
        "set +eu; set -f; a=; p=; "
        f"w=$(cat {path} 2>/dev/null); "
        'if [ -z "$w" ]; then a=' + NO_WITNESS + "; "
        "else IFS=' '; set -- $w; IFS=; "
        'if [ $# -ne 2 ] || [ "$1 $2" != "$w" ]; then a=' + UNOBSERVABLE + "; "
        "else case $1$2 in *[!0-9]*) a=" + UNOBSERVABLE + ";; esac; "
        "case $1 in 0*) a=" + UNOBSERVABLE + ";; esac; "
        "case $2 in 0*) a=" + UNOBSERVABLE + ";; esac; "
        "fi; "
        'if [ -z "$a" ]; then p=$1; t=$2; '
        "if [ ! -d /proc/self ]; then a=" + UNOBSERVABLE + "; "
        "elif [ ! -e /proc/$p ]; then a=" + STOPPED + "; "
        "else s=$(cat /proc/$p/stat 2>/dev/null); "
        'if [ -z "$s" ]; then if [ -e /proc/$p ]; then a=' + UNOBSERVABLE + "; "
        "else a=" + STOPPED + "; fi; "
        "else s=${s##*)}; IFS=' '; set -- $s; IFS=; st=$1; shift 19; "
        'if [ "$st" = Z ] || [ "$1" != "$t" ]; then a=' + STOPPED + "; "
        "else a=" + RUNNING + "; fi; fi; fi; fi; fi; "
    )


def process_state_template(path: str) -> str:
    """The one observer of the witness at *path*: prints one answer, never fails, never exits."""
    return f"( {_observer_body(path)}echo $a ) || :"


def witness_cleanup_template(path: str) -> str:
    """The one cleanup: observe, ``kill -9`` a running witness only, then remove the file."""
    return (
        f"( {_observer_body(path)}echo $a; "
        f"case $a in {RUNNING}) kill -9 $p 2>/dev/null || :;; esac; "
        f"rm -f {path} 2>/dev/null || : ) || :"
    )


@dataclasses.dataclass(frozen=True, slots=True)
class Observation:
    """What :func:`observe` answered, and the identity it matched when it read one.

    :func:`observation` returns it, for a test that needs the witnessed pid itself."""

    answer: str
    pid: int | None = None
    started: int | None = None


def observation(path: "Path | str") -> Observation:
    """Read the witness at *path* once and judge it over this machine's ``/proc``."""
    try:
        text = Path(path).read_text()
    except FileNotFoundError:
        return Observation(NO_WITNESS)
    except OSError:
        return Observation(UNOBSERVABLE)
    if not text:
        return Observation(NO_WITNESS)
    found = WITNESS_FORMAT.fullmatch(text)
    if found is None:
        return Observation(UNOBSERVABLE)
    if not Path("/proc/self").is_dir():
        return Observation(UNOBSERVABLE)
    pid, started = int(found.group(1)), int(found.group(2))
    try:
        if proc_stat(pid) is None:
            return Observation(STOPPED, pid, started)
        return Observation(RUNNING if alive(pid, started) else STOPPED, pid, started)
    except PermissionError:
        return Observation(UNOBSERVABLE, pid, started)


def observe(path: "Path | str") -> str:
    """The observer's answer for the witness at *path*, over this machine's ``/proc``."""
    return observation(path).answer


def clean(path: "Path | str") -> str:
    """The cleanup over this machine: kill a running witness's process, remove the file; the answer.

    Returns what :func:`observe` answered before anything was done. The kill
    goes to the pid and start time that observation matched, not to a re-read
    of the file, and that identity is checked again (``alive``) immediately
    before the signal. A window remains between that check and the signal: a
    process that dies and whose pid is reused inside it would be signalled.
    """
    seen = observation(path)
    if seen.answer == RUNNING and seen.pid is not None and seen.started is not None:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            if alive(seen.pid, seen.started):
                os.kill(seen.pid, signal.SIGKILL)
    with contextlib.suppress(FileNotFoundError):
        Path(path).unlink()
    return seen.answer


def _path_in(template: "str | None", command: str) -> "str | None":
    """The path *command* fills into the ``{path}`` *template*; ``None`` for another line."""
    if template is None:
        return None
    # format_template doubled the braces; every `{path}` field is the same path.
    parts = [re.escape(part.format()) for part in template.split("{path}")]
    found = re.fullmatch(
        r"(?P<path>\S+)".join(parts[:2]) + "(?P=path)".join(["", *parts[2:]]), command
    )
    return found.group("path") if found else None


@dataclasses.dataclass
class FakeWitnesses:
    """The witness stimuli answered in memory, for a fake host that answers a vocabulary.

    Each field is that vocabulary's ``{path}`` template (``None`` where it has
    none). :meth:`answer` plays a line the fake recognises, recording the
    witnessed process's state by path; the long-running stimulus is the one
    line it does not answer, because only the fake knows whether its budget
    stopped it: :meth:`long_running_path` names it and :meth:`record` states
    the outcome.
    """

    long_running: "str | None"
    exec_long_running: "str | None"
    running: "str | None"
    finished: "str | None"
    observer: "str | None"
    cleanup: "str | None"
    states: "dict[str, str]" = dataclasses.field(default_factory=dict)

    def long_running_path(self, command: str) -> "str | None":
        """The witness path of either spelling of the long-running stimulus *command*."""
        found = _path_in(self.long_running, command)
        return found if found is not None else _path_in(self.exec_long_running, command)

    def record(self, path: str, answer: str) -> None:
        """The witnessed process at *path* is now in *answer*'s state."""
        self.states[path] = answer

    def answer(self, command: str) -> "str | None":
        """What *command* prints when it is a witness line, recording its effect; else ``None``."""
        if (path := _path_in(self.running, command)) is not None:
            self.record(path, RUNNING)
            return ""
        if (path := _path_in(self.finished, command)) is not None:
            self.record(path, STOPPED)
            return ""
        if (path := _path_in(self.observer, command)) is not None:
            return self.states.get(path, NO_WITNESS)
        if (path := _path_in(self.cleanup, command)) is not None:
            return self.states.pop(path, NO_WITNESS)
        return None

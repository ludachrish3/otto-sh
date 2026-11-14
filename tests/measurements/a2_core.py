"""Safety core of the A2 bed measurement (#935): ask, snapshot, restore, nested shells, rows.

The measurement types lines into a host's PERSISTENT shell. This module is
everything that keeps that shell as it was found:

* :func:`ask` types one command and reads its status without ever failing
  the shell, whatever ``set -e`` it inherited;
* :func:`take_snapshot` reads every field a row may change, as values that
  can be put back (variables with their set/unset state and export flag,
  the working directory, the option letters, the INT trap, the terminal
  settings, the user and the shell's pid);
* :func:`restore` puts them back in the shell that was changed and reads
  the snapshot again; a difference marks the cell uncertain;
* :func:`identify_nested` and :func:`leave_nested` type ``exit`` only into a
  shell positively identified as a disposable child of the outer one;
* :func:`run_row` runs one measurement under a deadline, with the restore in
  a bounded ``finally``; an uncertain cell gets no further command.

Nothing here knows about the lab: a *line* is anything with ``send`` and
``expect`` (a host, a named session, or the local shells of the dry run).
"""

import asyncio
import dataclasses
import re
import shlex
import uuid
from collections.abc import Awaitable, Callable
from typing import Protocol

from pydantic import SecretStr

from otto.lifecycle import compensate, held_cancellation

RC_TAG = "A2RC"
"""Head of the status line every asked command ends with."""

STATUS_VAR = "__a2_r"
"""The one variable :func:`ask` leaves in the shell; :func:`restore` unsets it."""

READ_TIMEOUT_S = 30.0
"""Bound of one prompt or read (the brief's Step 1-2 bound)."""

CLEANUP_BOUND_S = 30.0
"""Bound of a row's restore; past it the cell is uncertain."""

RESTORE_READ_S = CLEANUP_BOUND_S / 5
"""Bound of each read inside a restore (two or three snapshots and the restore line),
so one slow read is told apart from the whole restore running out of time."""

MAX_HOPS = 3
"""How far up :func:`identify_nested` follows a nested shell's ancestry."""

FIXED_OPTIONS = frozenset("isc")
"""Option letters a shell reports in ``$-`` but that ``set`` cannot change."""

WATCHED = (
    "PWD",
    "OLDPWD",
    "OTTO_CF_HOOK",
    "OTTO_CF_RUN",
    "OTTO_CF_EXEC",
    "X",
    "__otto_e",
    "__otto_rc",
    "__otto_f",
    "__otto_base",
    "__otto_tty",
    "__otto_cmd",
    "__otto_v",
    "__otto_probe",
)
"""Every variable a row may touch. A row that touches another names it in its own list."""


class Line(Protocol):
    """Where the measurement types and reads: a host, a named session or a dry-run shell."""

    async def send(self, text: "str | SecretStr") -> None:
        """Type *text* as given; a SecretStr is typed as its value and a newline."""
        ...

    async def expect(self, pattern: "re.Pattern[str]", *, timeout: float) -> str:
        """Return the output up to and including *pattern*'s first match."""
        ...


@dataclasses.dataclass(frozen=True, slots=True)
class Answer:
    """One typed command: the exact bytes, everything read back, and its status."""

    typed: str
    buffer: str
    status: int


@dataclasses.dataclass(slots=True)
class Record:
    """One row of the raw output: what was typed and read, as ``repr``, with an outcome."""

    row: str
    typed: str
    buffer: str
    outcome: str


@dataclasses.dataclass(slots=True)
class Cell:
    """One line under measurement, its raw records, and whether it can still be typed into."""

    name: str
    line: Line
    records: "list[Record]" = dataclasses.field(default_factory=list)
    uncertain: "str | None" = None
    row: str = "setup"
    secrets: "list[SecretStr]" = dataclasses.field(default_factory=list)

    def note(self, typed: str, buffer: str, outcome: str) -> None:
        """Append one raw record under the current row, with every known secret struck out.

        A terminal echoes a password typed at a sudo -S prompt, so the
        raw buffer can hold it; the record never does.
        """
        for secret in self.secrets:
            typed = typed.replace(secret.get_secret_value(), "<secret>")
            buffer = buffer.replace(secret.get_secret_value(), "<secret>")
        self.records.append(Record(self.row, repr(typed), repr(buffer), outcome))

    def give_up(self, reason: str) -> None:
        """Mark the cell uncertain: nothing more is typed into it. The first reason is kept."""
        if self.uncertain is None:
            self.uncertain = reason
        self.note("", "", f"CELL UNCERTAIN: {reason}")


class CellUncertainError(RuntimeError):
    """A command was about to be typed into a cell that is uncertain."""


def _status_pattern(tag: str) -> "re.Pattern[str]":
    # The typed line holds `$__a2_r`, never digits, so a terminal's echo of
    # it cannot match: only the shell's own answer can.
    return re.compile(rf"{RC_TAG}:{tag}:(\d+)")


def asked(cmd: str, tag: str) -> str:
    """The line :func:`ask` types for *cmd*: the command guarded, then its status on a marker.

    The ``||`` keeps a failing command from ending a shell that inherited
    ``set -e``; the newline before ``}`` lets *cmd* end in a comment.
    """
    return f'{STATUS_VAR}=0; {{ {cmd}\n}} || {STATUS_VAR}=$?; echo "{RC_TAG}:{tag}:${STATUS_VAR}"\n'


async def ask(cell: Cell, cmd: str, *, timeout: float = READ_TIMEOUT_S) -> Answer:
    """Type *cmd* into *cell* and read up to its status. Refused on an uncertain cell."""
    if cell.uncertain is not None:
        raise CellUncertainError(f"{cell.name}: {cell.uncertain}")
    tag = uuid.uuid4().hex[:8]
    typed = asked(cmd, tag)
    pattern = _status_pattern(tag)
    await cell.line.send(typed)
    try:
        buffer = await cell.line.expect(pattern, timeout=timeout)
    except BaseException:
        cell.note(typed, "", "NO ANSWER")
        raise
    found = pattern.search(buffer)
    status = int(found.group(1)) if found else -1
    cell.note(typed, buffer, f"status {status}")
    return Answer(typed, buffer, status)


@dataclasses.dataclass(frozen=True, slots=True)
class Variable:
    """One shell variable as found: whether it was set, its value, whether it was exported."""

    is_set: bool
    value: str
    exported: bool


@dataclasses.dataclass(frozen=True, slots=True)
class Snapshot:
    """Every field a row may change, as values :func:`restore` can put back."""

    variables: "dict[str, Variable]"
    options: str
    int_trap: str
    tty: str
    user: str
    pid: int


def snapshot_command(names: "tuple[str, ...]" = WATCHED) -> str:
    """The one command that prints a :class:`Snapshot`. It changes nothing and cannot fail.

    Everything but ``trap`` runs in a subshell, so the loop's own variables
    and its ``set +eu`` never reach the shell being read; ``$-`` is printed
    before that ``set``.
    """
    loop = (
        f"for v in {' '.join(names)}; do "
        'eval "s=\\${$v+set}; x=\\${$v-}"; '
        'printf \'A2S:VAR:%s:%s:[%s]A2S\\n\' "$v" "${s:-unset}" "$x"; done'
    )
    return (
        "echo 'A2S:TRAP['; trap; echo ']A2S:TRAP'; "
        '( echo "A2S:OPTS:$-:"; set +eu; '
        f"{loop}; "
        "echo 'A2S:EXP['; export -p; echo ']A2S:EXP'; "
        'echo "A2S:TTY:$({ stty -g </dev/tty; } 2>/dev/null || echo none):"; '
        'echo "A2S:ID:$(id -un):"; echo "A2S:PID:$$:" ) || :'
    )


_VAR = re.compile(r"A2S:VAR:(\w+):(set|unset):\[(.*?)\]A2S", re.DOTALL)
_SECTION = "A2S:{0}\\[\r?\n(.*?)\\]A2S:{0}"
_EXPORTED = re.compile(r"^(?:export|declare -\w*x\w*) (\w+)(?:=|$)", re.MULTILINE)
_INT_TRAP = re.compile(r"^trap -- .* (?:SIG)?INT\r?$", re.MULTILINE)


class SnapshotUnreadableError(RuntimeError):
    """A snapshot's output lacked a field: the shell's state is not known."""


def parse_snapshot(buffer: str, names: "tuple[str, ...]" = WATCHED) -> Snapshot:
    """Read a :class:`Snapshot` out of :func:`snapshot_command`'s output."""

    def one(pattern: str, what: str) -> str:
        found = re.search(pattern, buffer, re.DOTALL)
        if found is None:
            raise SnapshotUnreadableError(f"snapshot has no {what}")
        return found.group(1)

    exported = set(_EXPORTED.findall(one(_SECTION.format("EXP"), "export list")))
    traps = one(_SECTION.format("TRAP"), "trap list")
    found_trap = _INT_TRAP.search(traps)
    seen = {m.group(1): m for m in _VAR.finditer(buffer)}
    missing = [n for n in names if n not in seen]
    if missing:
        raise SnapshotUnreadableError(f"snapshot has no variable {missing[0]}")
    variables = {
        n: Variable(seen[n].group(2) == "set", seen[n].group(3), n in exported) for n in names
    }
    return Snapshot(
        variables=variables,
        options=one(r"A2S:OPTS:([A-Za-z]*):", "option letters"),
        int_trap=found_trap.group(0).rstrip("\r") if found_trap else "",
        tty=one(r"A2S:TTY:([0-9A-Fa-f:=]+|none):", "terminal settings"),
        user=one(r"A2S:ID:([\w.-]+):", "user"),
        pid=int(one(r"A2S:PID:(\d+):", "pid")),
    )


async def take_snapshot(
    cell: Cell, names: "tuple[str, ...]" = WATCHED, *, timeout: float = READ_TIMEOUT_S
) -> Snapshot:
    """Read *cell*'s shell state. An unreadable answer makes the cell uncertain."""
    answer = await ask(cell, snapshot_command(names), timeout=timeout)
    try:
        return parse_snapshot(answer.buffer, names)
    except SnapshotUnreadableError as exc:
        cell.give_up(str(exc))
        raise


def differences(before: Snapshot, after: Snapshot) -> "list[str]":
    """The fields of *after* that differ from *before*, by name; empty when equal."""
    out = [name for name, was in before.variables.items() if after.variables.get(name) != was]
    out += [
        field
        for field in ("options", "int_trap", "tty", "user", "pid")
        if getattr(before, field) != getattr(after, field)
    ]
    return out


def restore_command(before: Snapshot, now: Snapshot) -> str:
    """The statements that put *before* back in a shell that reads *now*. None can fail.

    Options the row turned on go off first, so ``-e`` and ``-u`` cannot stop
    the rest; options the row turned off come back last. A variable is unset
    and assigned again, which is the one portable way to drop an export flag.
    ``cd`` writes ``OLDPWD``, so ``OLDPWD`` is put back after it.
    """
    was, cur = set(before.options) - FIXED_OPTIONS, set(now.options) - FIXED_OPTIONS
    steps = [f"set +{letter}" for letter in sorted(cur - was)]
    pwd = before.variables.get("PWD")
    if pwd is not None and pwd.is_set:
        steps.append(f"cd -- {shlex.quote(pwd.value)} 2>/dev/null || :")
    for name, var in before.variables.items():
        if name == "PWD":
            continue
        steps.append(f"unset {name} 2>/dev/null || :")
        if var.is_set:
            steps.append(f"{name}={shlex.quote(var.value)}")
            if var.exported:
                steps.append(f"export {name}")
    steps.append(f"unset {STATUS_VAR} 2>/dev/null || :")
    steps.append(before.int_trap or "trap - INT")
    if before.tty != "none":
        steps.append(f"{{ stty {before.tty} </dev/tty; }} 2>/dev/null || :")
    steps += [f"set -{letter}" for letter in sorted(was - cur)]
    return "; ".join(steps)


async def restore(cell: Cell, before: Snapshot, names: "tuple[str, ...]" = WATCHED) -> None:
    """Put *before* back in *cell*'s shell and verify it. A difference makes the cell uncertain."""
    now = await take_snapshot(cell, names, timeout=RESTORE_READ_S)
    if now.pid != before.pid:
        cell.give_up(f"RESET FAILED: the shell is pid {now.pid}, the row began in {before.pid}")
        return
    if not differences(before, now):
        return
    # The restore line is typed raw: `ask`'s guard would leave its status
    # variable behind, and every statement here is already unfailing.
    tag = uuid.uuid4().hex[:8]
    typed = f'{restore_command(before, now)}; echo "{RC_TAG}:{tag}:$((0))"\n'
    pattern = _status_pattern(tag)
    await cell.line.send(typed)
    buffer = await cell.line.expect(pattern, timeout=RESTORE_READ_S)
    cell.note(typed, buffer, "restore typed")
    left = differences(before, await take_snapshot(cell, names, timeout=RESTORE_READ_S))
    if left:
        cell.give_up(f"RESET INCOMPLETE: {', '.join(left)}")


def ancestry_command() -> str:
    """Print the current shell's ancestry, one ``A2A:<pid>:<comm>:<ppid>:`` line per process.

    Reads ``/proc`` only; on a host without it nothing is printed and the
    identity is ambiguous.
    """
    hops = MAX_HOPS + 1
    return (
        f"( set +eu; p=$$; n=0; while [ $n -le {hops} ]; do "
        "c=$(cat /proc/$p/comm 2>/dev/null) || break; "
        "q=$(sed -n 's/^PPid:[[:space:]]*//p' /proc/$p/status 2>/dev/null); "
        'echo "A2A:$p:$c:$q:"; [ -n "$q" ] || break; p=$q; n=$((n+1)); done ) || :'
    )


_ANCESTOR = re.compile(r"A2A:(\d+):([^:\r\n]*):(\d*):")


def nested_identity(buffer: str, *, outer_pid: int, entry: "frozenset[str]") -> "str | None":
    """Why *buffer*'s shell is NOT a disposable child of *outer_pid*; ``None`` if it is.

    Positive identification: the shell's own pid differs from *outer_pid*,
    its parents reach *outer_pid* within :data:`MAX_HOPS`, and every process
    in between is one of the recorded *entry* commands.
    """
    chain = [(int(p), comm, q) for p, comm, q in _ANCESTOR.findall(buffer)]
    if not chain:
        return "no ancestry could be read (no /proc?)"
    if chain[0][0] == outer_pid:
        return "the shell is the outer shell itself"
    for hop, (_pid, comm, parent) in enumerate(chain):
        if hop > 0 and comm not in entry:
            return f"an ancestor is {comm!r}, not the recorded entry {sorted(entry)}"
        if parent == str(outer_pid):
            return None
        if hop + 1 >= MAX_HOPS:
            break
    return f"the outer shell {outer_pid} is not within {MAX_HOPS} parents"


async def identify_nested(cell: Cell, *, outer: Snapshot, entry: "frozenset[str]") -> bool:
    """Whether *cell*'s current shell is positively a disposable child of *outer*'s shell.

    An ambiguous identity makes the cell uncertain: no ``exit`` is typed and
    it gets no further rows.
    """
    answer = await ask(cell, ancestry_command())
    reason = nested_identity(answer.buffer, outer_pid=outer.pid, entry=entry)
    if reason is not None:
        cell.give_up(f"RESET FAILED: {reason}")
        return False
    return True


async def leave_nested(cell: Cell, *, outer: Snapshot, entry: "frozenset[str]") -> None:
    """Type ``exit`` into a positively identified nested shell, then verify the outer one answers.

    The ONLY place the driver types ``exit``.
    """
    if not await identify_nested(cell, outer=outer, entry=entry):
        return
    await cell.line.send("exit\n")
    cell.note("exit\n", "", "left the nested shell")
    back = await take_snapshot(cell)
    if back.pid != outer.pid or back.user != outer.user:
        cell.give_up(
            f"RESET FAILED: after exit the shell is {back.user}/{back.pid}, "
            f"not {outer.user}/{outer.pid}"
        )


async def run_row(
    cell: Cell,
    name: str,
    body: "Callable[[], Awaitable[None]]",
    *,
    deadline_s: float,
    names: "tuple[str, ...]" = WATCHED,
    on_timeout: "Callable[[], Awaitable[None]] | None" = None,
) -> None:
    """Run one measurement on *cell* under *deadline_s*, then restore the shell it changed.

    The restore runs in a ``finally`` under :data:`CLEANUP_BOUND_S`, holding a
    cancellation that lands meanwhile. A restore that fails, differs or runs
    out of time makes the cell uncertain.

    A row that runs out of time leaves something unknown waiting on the line
    (a command, or a password prompt): typing the restore there would hand
    that text to whatever is waiting. So a timed-out row makes the cell
    uncertain and nothing more is typed, unless *on_timeout* is given and
    interrupts what the row left running before the restore. A row that
    fails any other way makes the cell uncertain too, and the error is
    recorded, not raised: the other cells are still measured. Only a
    cancellation leaves this function as an exception.
    """
    cell.row = name
    if cell.uncertain is not None:
        cell.note("", "", f"SKIPPED: {cell.uncertain}")
        return
    before = await take_snapshot(cell, names)
    held: "asyncio.CancelledError | None" = None
    try:
        await asyncio.wait_for(body(), deadline_s)
    except asyncio.TimeoutError:
        cell.note("", "", "TIMED OUT")
        if on_timeout is None:
            cell.give_up("a row timed out; what is waiting on the line is not known")
        else:
            await compensate(on_timeout(), timeout=READ_TIMEOUT_S, what=f"{name}: interrupt")
    except CellUncertainError:
        pass
    except Exception as exc:  # noqa: BLE001 - one cell's failure must not end the other cells' rows
        cell.note("", "", f"ROW FAILED: {type(exc).__name__}: {exc}")
        cell.give_up(f"a row failed: {type(exc).__name__}")
    except BaseException as exc:
        held = held_cancellation(exc)
        raise
    finally:
        done = asyncio.Event()

        async def put_back() -> None:
            if cell.uncertain is None:
                await restore(cell, before, names)
            done.set()

        try:
            await compensate(
                put_back(), timeout=CLEANUP_BOUND_S, what=f"{name}: restore", held=held
            )
        finally:
            if not done.is_set():
                cell.give_up("cleanup did not finish within its bound")

"""The A2 measurement's rows (#935): sudo policy, the sudo sequences, the console candidates.

Every row runs through :func:`a2_core.run_row`, so each is bounded and the
shell it types into is put back afterwards. Nothing here knows whether the
line is a lab host or a dry-run shell.

What the rows decide:

* **C5b**, per sudo policy class: whether a validation (``sudo -S -v``)
  followed at once, in the same call, by ``sudo -n <cmd>`` runs the command.
  A class needs the fallback only when the validation succeeded and that
  ``sudo -n`` then failed asking for a password (:func:`needs_fallback`).
* **C3**, exploratory: which of the state fields each candidate child of a
  console's shell starts with.

Process costs are a pid delta (``ns_last_pid`` before and after), which is
an upper bound on a quiet guest and is recorded as "not an exact count": the
driver attaches no tracer to a guest's shell.
"""

import dataclasses
import re
import shlex
import uuid

from pydantic import SecretStr

from otto.host.command_frame import SessionMarkers
from tests._fixtures import exec_frame_payloads as payloads
from tests.measurements import a2_core as core

ROW_DEADLINE_S = 60.0
"""Bound of one sudo or console row (the brief's Step 3-4 bound)."""

PROMPT = "otto-sudo-a2:"
"""What sudo prints when it asks. Typed as :data:`PROMPT_ASSIGNMENT`, so a terminal's echo
of the typed line never holds this text and only sudo's own prompt can match it."""

PROMPT_ASSIGNMENT = "SUDO_PROMPT=otto-sudo''-a2:"

PASSWORD_REQUIRED = re.compile(r"a password is required|a terminal is required")

STATE_SET = "cd /tmp; export OTTO_CF_RUN=1; set -f"
"""Run-side state, as the state contract sets it: a directory, a variable, an option."""

STATE_READ = (
    'echo "A2F:cwd:$(pwd):"; echo "A2F:var:${OTTO_CF_RUN-unset}:"; echo "A2F:opts:$-:"; '
    'echo "A2F:hook:${OTTO_CF_HOOK-unset}:"; echo "A2F:user:$(id -un):"; '
    'echo "A2F:tty:$({ stty -g </dev/tty; } 2>/dev/null || echo none):"'
)
"""The six fields a candidate is judged on, each on its own marked line."""

_FIELD = re.compile(r"A2F:(\w+):([^\r\n]*):\r?$", re.MULTILINE)


def fields(buffer: str) -> "dict[str, str]":
    """The state fields :data:`STATE_READ` printed into *buffer* (the last of each)."""
    return dict(_FIELD.findall(buffer))


def markers() -> SessionMarkers:
    """A fresh marker set for one typed frame."""
    return SessionMarkers.for_session(session_id=uuid.uuid4().hex[:8])


def end_pattern(m: SessionMarkers) -> "re.Pattern[str]":
    """The flagged end record of :func:`payloads.shape_c`, as the shell prints it."""
    return re.compile(rf"{re.escape(m.end_prefix)}([BN])(\d+)__")


@dataclasses.dataclass(frozen=True, slots=True)
class Framed:
    """One typed exec frame: everything read back, the record's flag and the body's status."""

    buffer: str
    flag: str
    status: int


async def type_frame(
    cell: core.Cell,
    body: str,
    *,
    stdin_eof: bool = True,
    start_var: "str | None" = None,
    answers: "tuple[tuple[re.Pattern[str], str | SecretStr], ...]" = (),
    timeout: float = core.READ_TIMEOUT_S,
) -> Framed:
    """Type *body* in the exec frame and read to its end record, answering prompts on the way.

    Every prompt of *answers* not yet answered is waited for together with
    the end record, in whatever order they appear (a sudo that asks no
    password goes straight to the command's own prompt). Each answer is sent
    at most once; a frame that ends before a prompt appears simply ends, and
    the answers left over are not sent.
    """
    if cell.uncertain is not None:
        raise core.CellUncertainError(f"{cell.name}: {cell.uncertain}")
    m = markers()
    # The product opens the credential descriptor only for an elevation that
    # reads its password there; a row's body says so by reading it.
    credential = f"<&{payloads.CREDENTIAL_FD}" in body
    typed = payloads.shape_c(
        body, m=m, stdin_eof=stdin_eof, start_var=start_var, credential=credential
    )
    end = end_pattern(m)
    await cell.line.send(typed)
    buffer = ""
    pending = list(answers)
    while not end.search(buffer):
        any_of = "|".join(f"(?:{p.pattern})" for p, _ in pending) + ("|" if pending else "")
        chunk = await cell.line.expect(re.compile(f"{any_of}(?:{end.pattern})"), timeout=timeout)
        buffer += chunk
        if end.search(buffer):
            break
        # `expect` returned at the first match, so the prompt that appeared
        # is the one whose match ends the chunk.
        asked = next(i for i, (p, _) in enumerate(pending) if _ends_with(p, chunk))
        await cell.line.send(pending.pop(asked)[1])
    found = end.search(buffer)
    assert found is not None
    cell.note(typed, buffer, f"frame {found.group(1)} status {found.group(2)}")
    return Framed(buffer, found.group(1), int(found.group(2)))


def _ends_with(pattern: "re.Pattern[str]", text: str) -> bool:
    return any(found.end() == len(text) for found in pattern.finditer(text))


# --- sudo ---------------------------------------------------------------


def validation_body(cmd: str) -> str:
    """V, the measured form: validate on the credential fd, print its status, then ``sudo -n``.

    The ``if`` keeps the status marker under an inherited ``set -e``; the
    ``exit`` keeps the command gated on the validation, as the product form
    (``|| exit``) does.
    """
    fd = payloads.CREDENTIAL_FD
    return (
        f"if <&{fd} {PROMPT_ASSIGNMENT} sudo -S -v; then __otto_v=0; else __otto_v=$?; fi; "
        'echo "V:$__otto_v:"; [ "$__otto_v" -eq 0 ] || exit "$__otto_v"; '
        f"exec {fd}<&-; sudo -n {cmd}"
    )


def refused_body(cmd: str, wrong: str) -> str:
    """V with a WRONG password, fed on a pipe: one attempt, and sudo cannot make another.

    After the wrong line sudo asks again and reads end of file, so it stops
    at one failed attempt whatever the host's ``passwd_tries`` or PAM stack
    do with an empty answer. Nothing is typed at a prompt, so a row that
    runs out of time leaves no prompt waiting for the next typed line.
    *wrong* is not a secret and is typed in the clear.
    """
    return (
        f"if printf '%s\\n' {shlex.quote(wrong)} | {PROMPT_ASSIGNMENT} sudo -S -v; "
        "then __otto_v=0; else __otto_v=$?; fi; "
        'echo "V:$__otto_v:"; [ "$__otto_v" -eq 0 ] || exit "$__otto_v"; '
        f"sudo -n {cmd}"
    )


def wrapped_body(cmd: str) -> str:
    """W, the fallback: the whole command as ONE argument to an elevated ``sh``'s ``eval``.

    So every statement of a compound command runs elevated, with stdin at
    end of file inside the elevated shell and the password read from the
    credential fd.
    """
    fd = payloads.CREDENTIAL_FD
    return (
        f"__otto_cmd={shlex.quote(cmd)}; "
        f"<&{fd} {fd}<&- {PROMPT_ASSIGNMENT} sudo -S sh -c "
        '\'exec </dev/null; eval "$1"\' otto-sudo "$__otto_cmd"'
    )


_V = re.compile(r"V:(\d+):")


@dataclasses.dataclass(frozen=True, slots=True)
class Validation:
    """What one V frame showed: the validation's status, the command's, and sudo's complaint."""

    validated: "int | None"
    status: int
    asked_again: bool
    buffer: str


def read_validation(framed: Framed) -> Validation:
    """Read a :class:`Validation` out of a typed V frame."""
    found = _V.search(framed.buffer)
    return Validation(
        validated=int(found.group(1)) if found else None,
        status=framed.status,
        asked_again=PASSWORD_REQUIRED.search(framed.buffer) is not None,
        buffer=framed.buffer,
    )


def needs_fallback(v: Validation) -> bool:
    """The C5b rule: the validation succeeded and the ``sudo -n`` right after it was refused."""
    return v.validated == 0 and v.status != 0 and v.asked_again


def _prompt() -> "re.Pattern[str]":
    return re.compile(re.escape(PROMPT))


async def validate_then_run(
    cell: core.Cell, cmd: str, password: SecretStr, *, stdin_eof: bool = True
) -> Validation:
    """Type V around *cmd*, answer sudo's prompt once, and read what happened."""
    framed = await type_frame(
        cell,
        validation_body(cmd),
        stdin_eof=stdin_eof,
        answers=((_prompt(), password),),
        timeout=ROW_DEADLINE_S / 2,
    )
    return read_validation(framed)


@dataclasses.dataclass(slots=True)
class SudoFindings:
    """One policy class's results, row by row, and the C5b decision for it."""

    cell: str
    rows: "dict[str, str]" = dataclasses.field(default_factory=dict)
    needs_fallback: "bool | None" = None


CALLER_ASKS = """sh -c 'printf "name""?"; read n; echo got-$n'"""
"""A command that asks its caller a question on the terminal. Typed with the
prompt split by quotes, so the terminal's echo of the typed line never holds
the text :data:`CALLER_PROMPT` waits for: only the command's own prompt does."""

CALLER_PROMPT = re.compile(r"name\?")

SUDO_COMMANDS = (
    "id -un",
    "cat; echo eof",
    "printf first; cat; echo eof",
    "echo hi | cat; cat; echo eof",
    "id -un; id -un",
)
"""Step 4.3's commands: stdin at end of file, and the compound whose elevation #1086 asks about."""


async def sudo_rows(cell: core.Cell, password: SecretStr, wrong: SecretStr) -> SudoFindings:
    """Step 4 on one line: the discriminator, its variants, and the fallbacks when it fires."""
    out = SudoFindings(cell.name)

    def said(v: Validation) -> str:
        return f"validated={v.validated} status={v.status} asked_again={v.asked_again}"

    async def forget() -> None:
        await core.ask(cell, "sudo -k")

    async def discriminator() -> None:
        await forget()
        v = await validate_then_run(cell, "id -un", password)
        out.rows["4.1 discriminator"] = said(v)
        out.needs_fallback = needs_fallback(v)

    async def second_call() -> None:
        framed = await type_frame(cell, validation_body("id -un"), answers=((_prompt(), password),))
        asked = PROMPT in framed.buffer
        out.rows["4.2 second call"] = f"{said(read_validation(framed))} prompted={asked}"

    async def commands() -> None:
        for cmd in SUDO_COMMANDS:
            await forget()
            v = await validate_then_run(cell, cmd, password)
            out.rows[f"4.3 {cmd}"] = said(v)

    async def caller_expects() -> None:
        await forget()
        framed = await type_frame(
            cell,
            validation_body(CALLER_ASKS),
            stdin_eof=False,
            answers=((_prompt(), password), (CALLER_PROMPT, "otto\n")),
        )
        out.rows["4.4 caller expects"] = (
            f"{said(read_validation(framed))} got={'got-otto' in framed.buffer}"
        )

    async def inherited(option: str) -> None:
        read = (
            'echo "A2F:opts:$-:"; '
            'echo "A2F:tty:$({ stty -g </dev/tty; } 2>/dev/null || echo none):"'
        )
        await core.ask(cell, f"set -{option}")
        await forget()
        v = await validate_then_run(cell, "id -un", password)
        after = fields((await core.ask(cell, read)).buffer)
        out.rows[f"4.5 under set -{option}"] = (
            f"{said(v)} opts={after.get('opts')} tty={after.get('tty')}"
        )
        await forget()
        framed = await type_frame(
            cell,
            validation_body(CALLER_ASKS),
            stdin_eof=False,
            answers=((_prompt(), password), (CALLER_PROMPT, "otto\n")),
        )
        after = fields((await core.ask(cell, read)).buffer)
        out.rows[f"4.5 caller expects under set -{option}"] = (
            f"{said(read_validation(framed))} got={'got-otto' in framed.buffer} "
            f"opts={after.get('opts')} tty={after.get('tty')}"
        )

    async def passwordless() -> None:
        # No password is offered and none is asked for: under a NOPASSWD
        # policy this is status 0, under any other sudo refuses at once
        # without an authentication attempt.
        await forget()
        framed = await type_frame(cell, "sudo -n true")
        out.rows["4.7 sudo -n true"] = f"status={framed.status}"

    async def wrong_password() -> None:
        await forget()
        # Exactly one attempt: the wrong line comes on a pipe, and the
        # re-prompt reads end of file (:func:`refused_body`).
        framed = await type_frame(cell, refused_body("echo MUST-NOT-RUN", wrong.get_secret_value()))
        v = read_validation(framed)
        reprompts = framed.buffer.count(PROMPT)
        ran = re.search(r"^MUST-NOT-RUN\r?$", framed.buffer, re.MULTILINE) is not None
        out.rows["4.6 wrong password"] = f"{said(v)} prompts={reprompts} command_ran={ran}"

    async def fallbacks() -> None:
        await forget()
        probe = await type_frame(
            cell,
            f"<&{payloads.CREDENTIAL_FD} {PROMPT_ASSIGNMENT} sudo -S -v && sudo -n true; "
            'echo "PROBE:$?:"',
            answers=((_prompt(), password),),
        )
        found = re.search(r"PROBE:(\d+):", probe.buffer)
        out.rows["4.8 probe"] = f"probe={found.group(1) if found else None}"
        await forget()
        w = await type_frame(cell, wrapped_body("id -un; id -un"), answers=((_prompt(), password),))
        # A login name alone on its line; the end record's own line is not one.
        users = re.findall(r"^([a-z_][a-z0-9_-]*)\r?$", w.buffer, re.MULTILINE)
        out.rows["4.8 wrapped"] = f"status={w.status} users={users[-2:]}"

    steps = [
        ("sudo 4.1 discriminator", discriminator),
        ("sudo 4.2 second call", second_call),
        ("sudo 4.3 commands", commands),
        ("sudo 4.4 caller expects", caller_expects),
        ("sudo 4.5 set -e", lambda: inherited("e")),
        ("sudo 4.5 set -eu", lambda: inherited("eu")),
        ("sudo 4.6 wrong password", wrong_password),
        ("sudo 4.7 passwordless", passwordless),
    ]
    for name, body in steps:
        await core.run_row(cell, name, body, deadline_s=ROW_DEADLINE_S)
    if out.needs_fallback:
        await core.run_row(cell, "sudo 4.8 fallbacks", fallbacks, deadline_s=ROW_DEADLINE_S)
    if cell.uncertain is None:
        cell.row = "sudo cleanup"
        await core.ask(cell, "sudo -k")
    return out


# --- sudo policy inventory ---------------------------------------------------


@dataclasses.dataclass(frozen=True, slots=True)
class PolicyClass:
    """What decides how sudo behaves for one login: the key rows are grouped by."""

    version: str
    timestamp_type: str
    timestamp_timeout: str
    nopasswd: bool
    requiretty: bool
    all_commands: bool


def policy_class(version: str, listing: str) -> PolicyClass:
    """Read a :class:`PolicyClass` out of ``sudo -V``'s first line and ``sudo -l``'s output."""

    def default(name: str, missing: str) -> str:
        found = re.search(rf"\b{name}=([^,\s]+)", listing)
        return found.group(1) if found else missing

    ticket = "tty" if re.search(r"(?<!!)\btty_tickets\b", listing) else "unset"
    found_version = re.search(r"\d+(?:\.\d+)+\w*", version)
    return PolicyClass(
        version=found_version.group(0) if found_version else "unknown",
        timestamp_type=default("timestamp_type", ticket),
        timestamp_timeout=default("timestamp_timeout", "default"),
        nopasswd="NOPASSWD" in listing,
        requiretty=re.search(r"(?<!!)\brequiretty\b", listing) is not None,
        all_commands=re.search(r"\(ALL(?:\s*:\s*ALL)?\)\s+(?:\w+:\s*)*ALL\b", listing) is not None,
    )


async def inventory(cell: core.Cell, password: SecretStr, *, login: str) -> "PolicyClass | None":
    """Step 2 on one line: who the login is and what sudo's policy says. ``None`` if unread.

    The line must be logged in as *login*, whose password *password* is:
    on any other user the cell is given up BEFORE the first ``sudo``, since
    every answer would be a failed attempt on someone else's account.
    """
    found: "list[PolicyClass]" = []

    async def body() -> None:
        who = await core.ask(cell, 'echo "A2F:user:$(id -un):"; id; echo "$SHELL"')
        user = fields(who.buffer).get("user")
        if user != login:
            cell.give_up(f"logged in as {user!r}, not {login!r}: no sudo is typed")
            raise core.CellUncertainError(cell.name)
        version = await core.ask(cell, "sudo -V | head -1")
        await core.ask(cell, "sudo -n -l 2>&1")
        await core.ask(cell, "sudo -k")
        listing = await type_frame(
            cell,
            f"<&{payloads.CREDENTIAL_FD} {PROMPT_ASSIGNMENT} sudo -S -l",
            answers=((_prompt(), password),),
        )
        await core.ask(cell, "sudo -k")
        found.append(policy_class(version.buffer, listing.buffer))

    await core.run_row(cell, "inventory", body, deadline_s=ROW_DEADLINE_S)
    return found[0] if found else None


# --- console candidates, exploratory ------------------------------------------


def candidates(hook: str) -> "dict[str, str]":
    """Step 3's child-process candidates, each a frame body that prints the state fields."""
    read = shlex.quote(STATE_READ)
    clean = (
        'exec env -i HOME="$HOME" TERM="$TERM" USER="$USER" LOGNAME="$LOGNAME" PATH="$PATH" '
        '"${SHELL:-/bin/sh}" -l -c '
    )
    return {
        "i login shell": f'exec "${{SHELL:-/bin/sh}}" -l -c {read} </dev/null',
        "ii clean login shell": clean + shlex.quote(f'cd "$HOME" && {STATE_READ}') + " </dev/null",
        "iii clean login shell with hook": clean
        + shlex.quote(f'{hook}; cd "$HOME" && {STATE_READ}')
        + " </dev/null",
    }


PID_COUNTER = 'echo "A2F:pids:$(cat /proc/sys/kernel/ns_last_pid 2>/dev/null || echo none):"'


async def console_rows(cell: core.Cell) -> "dict[str, str]":
    """Step 3 on one console: what each candidate child starts with, and what the parent keeps.

    Run only while the console's shell is the login shell (no ``su`` active);
    the driver switches no user here.
    """
    out: "dict[str, str]" = {}
    hook = "export OTTO_CF_HOOK=ran"

    async def measure(name: str, body: str) -> None:
        await core.ask(cell, f"{STATE_SET}; stty -icanon </dev/tty; {hook}")
        before = await core.ask(cell, PID_COUNTER)
        framed = await type_frame(cell, body, start_var=payloads.START_VAR)
        after = await core.ask(cell, PID_COUNTER)
        parent = await core.ask(cell, STATE_READ)
        out[name] = (
            f"child={fields(framed.buffer)} status={framed.status} flag={framed.flag} "
            f"parent={fields(parent.buffer)} "
            f"pids={fields(before.buffer).get('pids')}..{fields(after.buffer).get('pids')} "
            "(not an exact count)"
        )

    for name, body in candidates(hook).items():
        await core.run_row(
            cell, f"console {name}", lambda n=name, b=body: measure(n, b), deadline_s=ROW_DEADLINE_S
        )

    async def second_shell() -> None:
        # Candidate iv, feasibility only: a second login-state shell on the
        # same line, read, then left under the positive-identification rule.
        await core.ask(cell, f"{STATE_SET}; {hook}")
        outer = await core.take_snapshot(cell)
        named = await core.ask(cell, 'echo "A2F:shell:$(basename "${SHELL:-/bin/sh}"):"')
        shell = fields(named.buffer)["shell"]
        await cell.line.send('"${SHELL:-/bin/sh}" -l\n')
        cell.note('"${SHELL:-/bin/sh}" -l\n', "", "entered a nested login shell")
        try:
            inside = await core.ask(cell, STATE_READ)
            out["iv second login shell"] = f"child={fields(inside.buffer)}"
        finally:
            await core.leave_nested(cell, outer=outer, entry=frozenset({shell}))

    await core.run_row(
        cell, "console iv second login shell", second_shell, deadline_s=ROW_DEADLINE_S
    )
    return out

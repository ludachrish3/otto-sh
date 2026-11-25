"""One honest model of the shell a login proxy drives.

Every login-proxy test needs a stand-in for "the thing on the other end of the
pty", and before this existed each module grew its own. They drifted into
describing shells that cannot exist -- one that answers ``"Password:"`` to
EVERY read (a shell re-challenging forever, which is a REJECTED password, not
a prompt), and one that replays the same probe reply forever (which never
suspends, so the event loop never runs and the caller's ``wait_for`` can never
fire -- a CPU spin that presents as a hang). Both passed only because nothing
read the reply closely; both stopped passing the moment the engine did.

So the model here is a small state machine rather than a lookup on "what was
written last":

* a switch line raises a prompt only if this shell challenges for that login;
* the prompt is CLEARED by the password that answers it, right or wrong;
* a WRONG password leaves the user unchanged, which is what `su` really does
  (it reports the failure and exits back to the calling shell);
* each liveness probe is answered EXACTLY ONCE, like a real shell;
* every probe reply carries the CURRENT user, so a caller that checks identity
  can tell a switch that took from one that did not.

``reply()`` returning ``None`` means "this shell has nothing to say" -- the
adapter should block or time out, never return empty (which reads as EOF) and
never repeat itself (which spins).
"""

import asyncio
import re
import shlex
from typing import NamedTuple, Protocol

from pydantic import SecretBytes, SecretStr

from otto.host.command_frame import _PROBE_VAR, RECOVER_SUFFIX, BashFrame
from otto.host.secret_wire import reveal, reveal_bytes
from otto.logger.mode import LogMode

# An anchored probe line (otto.host.command_frame.whole_line_echo): the
# variable is set by the line's FIRST statement, and the echo prints the
# marker's head followed by it. Both built from the product's own name.
_PROBE_ASSIGN_RE = re.compile(rf"{_PROBE_VAR}=(\w+); ")
_PROBE_ECHO_RE = re.compile(rf"echo ([^\s$]+)\${_PROBE_VAR}(?!\w)")
_SWITCH_RE = re.compile(r"^\s*(?:sudo\s+)?su\b(.*)$", re.MULTILINE)

# The two shapes a bash-family frame types: run's brace group and exec's
# subshell (otto.host.command_frame.BashFrame.frame / frame_exec). Pinned to
# the product by a round-trip test (test_the_test_side_frame_reader_reads_both_shapes).
_RUN_FRAME_RE = re.compile(
    r'echo "(__OTTO_\w+_BEGIN__)"; \{ (.*)\n\}; echo "(__OTTO_\w+_END__)\$\?__"\n',
    re.DOTALL,
)
_EXEC_FRAME_RE = re.compile(
    r'echo "(__OTTO_\w+_BEGIN__)"; case \$- in .*?; set \+e; '
    r"\( " + re.escape(BashFrame().errexit_restore()) + r"; (.*)\n"
    r"\)( 3<&0)?( </dev/null)?; __otto_rc=\$\?; .*"
    r'echo "(__OTTO_\w+_END__)\$\{__otto_f\}\$\{__otto_rc\}__"\n',
    re.DOTALL,
)


class BashFramed(NamedTuple):
    """What :func:`parse_bash_frame` reads off a typed bash-family frame."""

    begin: str
    cmd: str
    end_prefix: str
    isolated: bool
    """The exec frame (a subshell), rather than run's brace group."""
    stdin_eof: bool
    """The exec frame gives the command a stdin at end of file."""
    credential: bool = False
    """The exec frame keeps the caller's terminal on the credential descriptor (``3<&0``)."""


def parse_bash_frame(text: str) -> "BashFramed | None":
    """Read a bash-family frame out of *text*, any shape; ``None`` when *text* holds none.

    The shapes are run's, a pooled exec's and a console exec's.

    The one test-side reader of what ``BashFrame.frame`` and
    ``BashFrame.frame_exec`` type, for the doubles that answer frames.
    """
    if (run := _RUN_FRAME_RE.search(text)) is not None:
        return BashFramed(run.group(1), run.group(2), run.group(3), False, False)
    if (exec_ := _EXEC_FRAME_RE.search(text)) is not None:
        return BashFramed(
            exec_.group(1),
            exec_.group(2),
            exec_.group(5),
            True,
            exec_.group(4) is not None,
            exec_.group(3) is not None,
        )
    return None


class _HasEndPrefix(Protocol):
    @property
    def end_prefix(self) -> str: ...


def end_record(m: _HasEndPrefix, code: int, *, flag: str = "") -> str:
    """The end record a bash-family shell prints for a finished frame: no trailing newline.

    *flag* is ``""`` for run's frame and ``"B"`` (or ``"N"``, the record
    lost) for the exec frame. *m* is the session's markers, or anything
    holding the end prefix (a :class:`BashFramed`). The one builder every
    double answering a frame uses.
    """
    return f"{m.end_prefix}{flag}{code}__"


def end_record_for(framed: BashFramed, code: int) -> str:
    """The end record a shell prints for *framed*: flagged ``B`` when it is the exec frame."""
    return end_record(framed, code, flag="B" if framed.isolated else "")


_EXIT_RE = re.compile(r"^\s*exit\s*$", re.MULTILINE)


def probe_marker(text: str) -> str | None:
    """The marker a POSIX shell prints when it runs *text* as a whole probe line.

    ``None`` when *text* is not an anchored probe, or does not open with its
    anchor: a line the shell ran only the tail of prints no whole marker,
    which is the property the anchor exists for. The one place the doubles
    read the anchored spelling, pinned to the product by a round-trip test
    (``test_the_double_reads_what_the_shell_prints``).
    """
    assign = _PROBE_ASSIGN_RE.match(text)
    echo = _PROBE_ECHO_RE.search(text)
    if assign is None or echo is None:
        return None
    return echo.group(1) + assign.group(1)


class ShellModel:
    """A shell that switches users, may challenge, and answers probes once.

    Args:
        user: who this shell is running as to begin with.
        password: the password this shell will ACCEPT, or None to never
            challenge. A switch to a login this shell challenges for raises a
            prompt until it is answered.
        challenges: whether a switch raises a password prompt at all. Models
            "who is asking": root is not challenged, an unprivileged user is.
    """

    def __init__(
        self,
        user: str = "admin",
        *,
        password: str | None = None,
        challenges: bool = False,
        switch_re: re.Pattern[str] = _SWITCH_RE,
        exit_re: re.Pattern[str] = _EXIT_RE,
    ) -> None:
        self.user = user
        # A custom login proxy drives whatever user-switching wrapper its host
        # actually ships (`become`/`leave`, a vendor `pbrun`, ...). That is a
        # property of the SHELL, so it belongs here as configuration rather
        # than as a test that quietly opts out of identity tracking. Group 1
        # of *switch_re* is the tail after the verb, parsed like a `su` tail.
        self._switch_re = switch_re
        self._exit_re = exit_re
        self._password = password
        self._challenges = challenges
        self._pending_prompt = False
        self._pending_target: str | None = None
        self._answered: set[str] = set()
        self.writes: list[str] = []
        self.auth_failures = 0
        # `su` opens a NESTED shell and `exit` closes it, so who you are after
        # an undo is who you were before the hop -- not a guess, a stack.
        self._stack: list[str] = []

    def wrote(self, text: "str | SecretStr") -> None:
        """Record a write and advance the shell's state the way a real one would.

        A ``SecretStr`` (a password answer) is recorded as the transport writes
        it (:func:`~otto.host.secret_wire.reveal`): the shell sees the wire.
        """
        text = reveal(text)
        self.writes.append(text)
        if self._pending_prompt:
            supplied = text.rstrip("\n")
            # No declared password means "this shell challenges but accepts
            # whatever it is given" -- the right model for a test about the
            # SEQUENCE of sends. Declare one only when the test is about
            # authentication succeeding or failing.
            if self._password is None or supplied == self._password:
                self.user = self._pending_target or self.user
            else:
                # `su` prints "Authentication failure" and EXITS: the user does
                # NOT change, and the shell answering afterwards is the one we
                # started in. That asymmetry is the whole reason a liveness
                # probe alone cannot see a rejected password.
                self.auth_failures += 1
            self._pending_prompt = False
            self._pending_target = None
            return
        if self._exit_re.search(text):
            if self._stack:
                self.user = self._stack.pop()
            return
        switch = self._switch_re.search(text)
        if switch is not None:
            target = _switch_target(switch.group(1))
            self._stack.append(self.user)
            if self._challenges:
                self._pending_prompt = True
                self._pending_target = target
            else:
                self.user = target

    def reply(self) -> str | None:
        """What this shell says next, or None when it has nothing to say."""
        if self._pending_prompt:
            return "Password:"
        for text in reversed(self.writes):
            marker = probe_marker(text)
            if marker is None or not marker.endswith(RECOVER_SUFFIX):
                continue
            if marker in self._answered:
                return None  # already answered once; a real shell does not repeat
            self._answered.add(marker)
            return f"{marker}0__{self.user}__"
        return None


_SWITCH_FLAGS = {"-", "-l", "-c", "-s", "/bin/bash", "/bin/sh"}


def _switch_target(tail: str) -> str:
    """The login a `su` tail names, defaulting to root for a bare/-only switch.

    Split with :mod:`shlex` rather than on whitespace: otto quotes the login
    (``su - 'od d'``), and a naive split would report ``od`` for an account
    actually called ``od d`` -- turning a correct switch into an identity
    mismatch that looks like a product bug.
    """
    try:
        words = shlex.split(tail)
    except ValueError:
        words = tail.split()
    for word in words:
        if word not in _SWITCH_FLAGS:
            return word
    return "root"


def drive(mock, *, user: str = "admin", prompts: bool = True, **model_kw) -> ShellModel:
    """Back a mock shell's ``send``/``expect`` with one :class:`ShellModel`.

    The wiring every caller was writing by hand, in one place: ``send`` feeds
    the model, ``expect`` reports what the model has to say, and an empty
    string stands in for "nothing" where the caller's mock cannot suspend.

    Returns the model, so a test can read ``model.user`` or ``auth_failures``.
    """
    from unittest.mock import AsyncMock

    model = ShellModel(user=user, challenges=prompts, **model_kw)

    async def _send(text, **_kw) -> None:
        model.wrote(text)

    async def _expect(*_a, **_kw) -> str:
        return model.reply() or ""

    mock.send = AsyncMock(side_effect=_send)
    mock.expect = AsyncMock(side_effect=_expect)
    return model


def _wire_text(write: "bytes | str | SecretStr | SecretBytes") -> str:
    """Decode a recorded write the way the device received it."""
    if isinstance(write, SecretBytes):
        write = reveal_bytes(write)
    if isinstance(write, bytes):
        return write.decode("utf-8", "replace")
    return reveal(write)


def replay(writes, *, user: str = "admin", **model_kw) -> ShellModel:
    """Rebuild a shell's identity by replaying a write log through a model.

    For fakes that keep a plain list of writes rather than holding a model:
    the identity a probe reply should carry is a pure function of the switch
    and exit lines already sent, so it can be derived instead of pinned. A
    pinned one goes stale the moment a test grows a hop -- and pinning it is
    what let the identity half of the resync go unexercised here at all.

    Accepts ``bytes`` or ``str`` entries.
    """
    model = ShellModel(user=user, **model_kw)
    for write in writes:
        model.wrote(_wire_text(write))
    return model


class ModelIO:
    """ProxyIO backed by :class:`ShellModel` -- an honest shell, not a script.

    ``expect`` honours its timeout for real (``asyncio.wait_for`` over an
    event that never fires when the shell has nothing to say), so the engine's
    own settle and probe budgets are exercised rather than waved through by a
    fake that returns instantly no matter what it was asked for.
    """

    def __init__(self, shell: ShellModel) -> None:
        self.shell = shell
        self.sent: list[tuple[str, LogMode]] = []

    async def send(self, text: "str | SecretStr", *, log: LogMode = LogMode.NORMAL) -> None:
        text = reveal(text)  # what the transport writes
        self.sent.append((text, log))
        self.shell.wrote(text)

    async def expect(self, pattern: "str | re.Pattern[str]", timeout: float = 10.0) -> str:
        reply = self.shell.reply()
        if reply is None or not re.search(pattern, reply):
            await asyncio.wait_for(asyncio.Event().wait(), timeout)
            raise AssertionError("unreachable")
        return reply

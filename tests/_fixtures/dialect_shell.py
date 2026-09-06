"""A two-dialect shell for session-setup tests.

Answers the BASH handshake and frame until a scripted *transition* line is
written, then answers only the ZEPHYR token and frame — a landing shell and
a target application on one console. With ``menu=True`` the landing state
echoes NOTHING (a boot menu that reads a digit) until the transition line.
With ``app_launch=`` set, a third state models an application an
:class:`~otto.host.app_shell.AppShell` attaches to: the exact launch line is
answered with the app's prompt, the exact quit line hands the console back to
bash, and everything else is met with silence.

The patterns are written against what the frames actually render
(:meth:`~otto.host.command_frame.BashFrame.frame` and friends), and each
dialect answers ONLY its own probes. That second half is the load-bearing
one: a shell that answered the other dialect's readiness probe would confirm
frame entry into a console it is not in, which is the exact failure
``SessionManager``'s post-hook confirmation exists to catch. So the READY
answers are anchored — bash's marker is answered only when it arrives as
``echo <marker>`` (what ``BashFrame.handshake`` renders) and Zephyr's only
when the bare token is the whole line — because that is what the real shells
do: a bash marker typed at a Zephyr prompt comes back as
``stty: command not found`` with no marker in it, and a Zephyr marker typed
at a bash prompt comes back as ``bash: <marker>: command not found``, whose
marker is not at the start of a line and so cannot match the readiness
pattern.

The login-proxy identity probe is answered too, and its identity comes from
replaying the write log through the shared
:class:`~tests._fixtures.fake_shell.ShellModel` rather than from a pinned
string: who the shell is after ``su - <login>`` is a function of the lines
already sent, and a pinned answer is what let the identity half of the
resync go unexercised elsewhere.

The landing shell is INTERACTIVE, because the shells otto actually lands on
are: it starts with terminal echo on and a ``PS1``, the bash readiness
payload's own ``stty -echo`` turns the echo off, and from then on the prompt
is printed immediately before the next command's output ON THE SAME LINE —
which is what a real shell does and what defeated the line-anchored readiness
matcher on the live bed (the marker arrived as ``<prompt><marker>``, never at
a line start, so a second handshake on one session could not confirm). The
echoed command line and the prompt are the only console state modelled: this
shell EXECUTES nothing, so a later ``stty echo`` typed as a command does not
turn echo back on here. That is deliberate — every handshake after the first
is answered in the hostile echo-off state, which is the state the dialect's
payload has to survive. A real login bridge does restore echo before its
second handshake, so the bridge is friendlier in production than it is here.
"""

import asyncio
import re

from otto.host.session import ShellSession
from tests._fixtures.fake_shell import replay

_BASH_READY_RE = re.compile(r"echo (__OTTO_[0-9a-f]+_READY__)\n")
_BASH_FRAME_RE = re.compile(
    r'echo "(__OTTO_[0-9a-f]+_BEGIN__)"; (.*); echo "(__OTTO_[0-9a-f]+_END__)\$\?__"'
)
_BASH_IDENTITY_RE = re.compile(r'echo "(__OTTO_[0-9a-f]+_RECOVER__)\$\?__\$\(id -un\)__"')
_BASH_RECOVER_RE = re.compile(r'echo "(__OTTO_[0-9a-f]+_RECOVER__)\$\?__"')
_ZEPHYR_FRAME_RE = re.compile(
    r"(__OTTO_[0-9a-f]+_BEGIN__)\r(.*)\rretval\r(__OTTO_[0-9a-f]+_END__)\r"
)
_ZEPHYR_RECOVER_RE = re.compile(r"(__OTTO_[0-9a-f]+_RECOVER__)\n")
_ZEPHYR_READY_RE = re.compile(r"\A(__OTTO_[0-9a-f]+_READY__)\n\Z")


class DialectShell:
    def __init__(
        self,
        *,
        transition: str = "python3\n",
        menu: bool = False,
        user: str = "admin",
        app_launch: str | None = None,
        app_prompt: str = "app> ",
        app_quit: str = "quit\n",
    ) -> None:
        self.transition = transition
        self.menu = menu
        self.user = user
        # *app_launch* and *app_quit* are whole LINES, terminator included,
        # exactly as ``AppShell`` writes them (``launch + "\n"``).
        self.app_launch = app_launch
        self.app_prompt = app_prompt
        self.app_quit = app_quit
        self.mode = "bash"
        self.writes: list[str] = []
        self.commands: list[tuple[str, str]] = []  # (mode, command)
        # Terminal state, as a real interactive shell keeps it: echo starts on
        # and the bash handshake's `stty -echo` turns it off.
        self.echo_on = True
        self.ps1 = "vagrant@fake:~$ "

    def wrote(self, text: str) -> None:
        self.writes.append(text)
        if self.mode == "app":
            if text == self.app_quit:
                self.mode = "bash"
            return
        if self.app_launch is not None and text == self.app_launch:
            self.mode = "app"
            return
        if text == self.transition:
            self.mode = "zephyr"

    def reply(self) -> str | None:
        """What the console prints in answer to the LAST write, or None."""
        if not self.writes:
            return None
        text = self.writes[-1]
        if self.mode == "app":
            return self._app_reply(text)
        return self._bash_reply(text) if self.mode == "bash" else self._zephyr_reply(text)

    def _app_reply(self, text: str) -> str | None:
        """The application answers its own launch line with its prompt, and nothing else.

        Anchored on the exact launch bytes for the same reason the two shell
        dialects are: an app that printed its prompt in answer to anything
        would confirm an ``AppShell`` that never actually started, which is
        precisely the failure ``AppShellTimeoutError`` exists to report. The
        application models no commands of its own — ``cmd()`` is not what the
        session-setup tests here are about — and the quit line is answered by
        BASH, because by the time it is read the app has already exited and
        ``AppShell._exit`` is probing the shell underneath.
        """
        return self.app_prompt if text == self.app_launch else None

    def _bash_reply(self, text: str) -> str | None:
        if self.menu:
            return None
        if (m := _BASH_FRAME_RE.search(text)) is not None:
            self.commands.append(("bash", m.group(2)))
            return f"{self._prompt()}{m.group(1)}\nbash:{m.group(2)}\n{m.group(3)}0__\n"
        if (m := _BASH_IDENTITY_RE.search(text)) is not None:
            user = replay(self.writes, user=self.user).user
            return f"{self._prompt()}{m.group(1)}0__{user}__\n"
        if (m := _BASH_RECOVER_RE.search(text)) is not None:
            return f"{self._prompt()}{m.group(1)}0__\n"
        if (m := _BASH_READY_RE.search(text)) is not None:
            return self._bash_handshake_reply(text, m.group(1))
        return None

    def _prompt(self) -> str:
        """``PS1``, which a shell prints before output once echo is off.

        Empty while echo is on: there the prompt is followed by the ECHOED
        command line, which ends in its own newline, so the output that
        matters still starts a line of its own.
        """
        return "" if self.echo_on else self.ps1

    def _bash_handshake_reply(self, payload: str, ready: str) -> str:
        """Answer the readiness payload the way a real bash terminal would.

        Echo on (the first handshake): the typed line comes back, newline and
        all. Echo off (frame entry after a session-setup hook): nothing is
        echoed and the pending prompt leads the line instead — so the marker
        begins a line only if the payload itself printed one.

        What the payload prints is READ OFF THE PAYLOAD, clause by clause,
        rather than recognised as a known string: every bare ``echo`` ahead of
        the marker's own clause emits an empty line, exactly as the shell
        would. A dialect that reaches a line start by some other rendering is
        then free to differ here without going red for a non-defect, and a
        dialect that reaches none still produces the bed's ``PS1READY``.
        """
        lead = payload.rstrip("\r\n") + "\r\n" if self.echo_on else self._prompt()
        self.echo_on = False  # the payload's own `stty -echo`
        return f"{lead}{self._blank_lines_before(payload, ready)}{ready}\r\n"

    @staticmethod
    def _blank_lines_before(payload: str, ready: str) -> str:
        """One empty line per bare ``echo`` clause preceding the marker's clause."""
        clauses = [c.strip() for c in payload.split(";")]
        marker_clause = f"echo {ready}"
        before = clauses[: clauses.index(marker_clause)] if marker_clause in clauses else clauses
        return "\r\n" * sum(1 for c in before if c == "echo")

    def _zephyr_reply(self, text: str) -> str | None:
        if (m := _ZEPHYR_FRAME_RE.search(text)) is not None:
            self.commands.append(("zephyr", m.group(2)))
            return (
                f"{m.group(1)}: command not found\nuart:~$ \n"
                f"zephyr:{m.group(2)}\nuart:~$ \n0\nuart:~$ \n{m.group(3)}: command not found\n"
            )
        if (m := _ZEPHYR_RECOVER_RE.search(text)) is not None:
            return f"{m.group(1)}: command not found\n"
        if (m := _ZEPHYR_READY_RE.search(text)) is not None:
            return f"{m.group(1)}: command not found\n"
        return None


class DialectSession(ShellSession):
    """A ShellSession driven by a :class:`DialectShell`."""

    def __init__(self, shell: DialectShell, **kw) -> None:
        super().__init__(**kw)
        self.shell = shell
        self.opens = 0
        self.closed = False

    async def _open(self) -> None:
        self.opens += 1

    async def _write(self, data: str) -> None:
        self.shell.wrote(data)

    async def _read_until_pattern(self, pattern: re.Pattern[str]) -> str:
        reply = self.shell.reply()
        if reply is None or pattern.search(reply) is None:
            raise asyncio.TimeoutError("the console has nothing matching to say")
        return reply

    async def close(self) -> None:
        self.closed = True
        self._alive = False
        self._initialized = False

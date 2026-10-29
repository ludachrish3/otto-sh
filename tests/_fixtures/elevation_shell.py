"""A bash shell that runs ``sudo -S`` and ``su -c`` the way the real ones prompt.

The elevation tests need the other end of a pty that behaves like sudo and su
around a password: the prompt arrives after the BEGIN marker (bash runs the
``echo BEGIN`` before it runs sudo), a wrong password makes sudo prompt again
(three tries) while su reports and exits, a cached sudo credential prompts for
nothing, and root is never asked by su. Ctrl+C abandons the command line, so
no END marker follows, and the recover probe is answered unless the shell is
told to stay wedged.

Writes are recorded as the wire carries them (``secret_wire.reveal``), so a
test can count how many times the password was typed. Replies are fed into a
stream the session reads one byte at a time, so the chunks the session sees
are the ones its combined pattern cuts, as on a real transport.
"""

import asyncio
import re
import shlex
from typing import Any

from pydantic import SecretStr

from otto.host.secret_wire import reveal
from otto.host.session import ShellSession

_FRAME_RE = re.compile(
    r'echo "(__OTTO_[0-9a-f]+_BEGIN__)"; \{ (.*)\n\}; echo "(__OTTO_[0-9a-f]+_END__)\$\?__"\n',
    re.DOTALL,
)
_RECOVER_RE = re.compile(r'echo "(__OTTO_[0-9a-f]+_RECOVER__)\$\?__"')
_SUDO_RE = re.compile(r"\ASUDO_PROMPT='([^']*)' sudo -S (.*)\Z", re.DOTALL)
_SU_RE = re.compile(r"\Asu -c (.*)\Z", re.DOTALL)
_SUDO_TRIES = 3


class ElevationShell:
    """The shell's state: who it is, what sudo has cached, what a command prints.

    Args:
        user: who the shell runs as; ``root`` is never prompted by su.
        password: the password sudo (for *user*) and su (for root) accept.
        cached: sudo has a cached credential, so it prompts for nothing.
        echo: the terminal echoes each typed line before running it.
        outputs: what a command prints, keyed by the command sudo or su runs;
            ``{prompt}`` in it becomes the prompt the sudo line set, which is
            what a command that reads sudo's environment could print.
        wedged: the recover probe is never answered.
    """

    def __init__(
        self,
        *,
        user: str = "admin",
        password: str = "right-password",  # noqa: S107 — a test shell's accepted password
        cached: bool = False,
        echo: bool = False,
        outputs: dict[str, str] | None = None,
        wedged: bool = False,
    ) -> None:
        self.user = user
        self.password = password
        self.cached = cached
        self.echo = echo
        self.outputs = outputs or {}
        self.wedged = wedged
        self.writes: list[str] = []
        self._pending: tuple[str, str, str, str] | None = None  # kind, prompt, cmd, end
        self._tries = 0
        self.prompt = ""

    def writes_of(self, text: str) -> int:
        """How many writes were exactly *text* and a newline: a password typed, and how often."""
        return self.writes.count(f"{text}\n")

    def wrote(self, text: str) -> str:
        """Record one write and return what the terminal prints in answer.

        An empty write puts nothing on the wire, so the shell never sees it.
        """
        if not text:
            return ""
        self.writes.append(text)
        if self._pending is not None:
            return self._answer_prompt(text)
        if (m := _RECOVER_RE.search(text)) is not None:
            return "" if self.wedged else f"{m.group(1)}0__\n"
        if (m := _FRAME_RE.search(text)) is not None:
            typed = text if self.echo else ""
            return f"{typed}{m.group(1)}\n{self._start(m.group(2), m.group(3))}"
        return ""

    def _start(self, body: str, end: str) -> str:
        if (m := _SUDO_RE.match(body)) is not None:
            self.prompt = m.group(1)
            if self.cached or self.user == "root":
                return self._finish(m.group(2), end)
            self._pending, self._tries = ("sudo", m.group(1), m.group(2), end), 0
            return m.group(1)
        if (m := _SU_RE.match(body)) is not None:
            inner = shlex.split(m.group(1))[0]
            if self.user == "root":
                return self._finish(inner, end)
            self._pending = ("su", "Password: ", inner, end)
            return "Password: "
        return self._finish(body, end)

    def _finish(self, cmd: str, end: str, code: int = 0) -> str:
        out = self.outputs.get(cmd, "").replace("{prompt}", self.prompt)
        return f"{out}{end}{code}__\n"

    def _answer_prompt(self, text: str) -> str:
        assert self._pending is not None
        kind, prompt, cmd, end = self._pending
        if text == "\x03":
            self._pending = None
            return "^C\n"
        if text == f"{self.password}\n":
            self._pending = None
            return "\n" + self._finish(cmd, end)
        if kind == "su":
            self._pending = None
            return "\nsu: Authentication failure\n" + f"{end}1__\n"
        self._tries += 1
        if self._tries < _SUDO_TRIES:
            return f"\nSorry, try again.\n{prompt}"
        self._pending = None
        return f"\nsudo: {_SUDO_TRIES} incorrect password attempts\n{end}1__\n"


class ElevationSession(ShellSession):
    """A ready :class:`ShellSession` over an :class:`ElevationShell`."""

    def __init__(self, shell: ElevationShell, **kw: Any) -> None:
        super().__init__(**kw)
        self.shell = shell
        self.current_user = shell.user
        self._stream = asyncio.StreamReader()
        self._initialized = True
        self._alive = True

    async def open_transport(self) -> None:
        return None

    async def write_transport(self, data: str | SecretStr) -> None:
        reply = self.shell.wrote(reveal(data))  # what the transport writes
        if reply:
            self._stream.feed_data(reply.encode())

    async def read_transport_until(self, pattern: re.Pattern[str]) -> str:
        buf = ""
        while True:
            chunk = await self._stream.read(1)
            if not chunk:
                raise asyncio.IncompleteReadError(buf.encode(), None)
            buf += chunk.decode()
            if pattern.search(buf):
                return buf

    async def close(self) -> None:
        self._alive = False
        self._initialized = False


def attach(host: Any, session: ElevationSession) -> ElevationSession:
    """Make *session* *host*'s default shell, so ``host.run`` drives it as it would a real one."""
    host._live_session_mgr()._session = session
    return session

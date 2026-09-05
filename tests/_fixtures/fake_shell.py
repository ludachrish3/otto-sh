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

import re
import shlex

_PROBE_RE = re.compile(r"__OTTO_([0-9a-f]+)_RECOVER__")
_SWITCH_RE = re.compile(r"^\s*(?:sudo\s+)?su\b(.*)$", re.MULTILINE)
_EXIT_RE = re.compile(r"^\s*exit\s*$", re.MULTILINE)


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

    def wrote(self, text: str) -> None:
        """Record a write and advance the shell's state the way a real one would."""
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
            probe = _PROBE_RE.search(text)
            if probe is None:
                continue
            marker = probe.group(0)
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
        model.wrote(write.decode("utf-8", "replace") if isinstance(write, bytes) else write)
    return model

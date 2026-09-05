import asyncio
import re
from dataclasses import replace

import pytest

from otto.host import login_proxy
from otto.host.command_frame import BashFrame
from otto.host.login_proxy import (
    LOGIN_PROXIES,
    Cred,
    LoginProxyError,
    _resync_shell,
    perform_switch,
    register_login_proxy,
    resolve_chain,
    run_proxy,
    run_undo,
)
from otto.logger.mode import LogMode
from tests._fixtures.fake_shell import ShellModel


@pytest.fixture(autouse=True)
def _fast_resync_settle(monkeypatch: pytest.MonkeyPatch) -> None:
    """Zero the post-transition resync settle so unit tests don't pay its wall-clock.

    ``_resync_shell`` passes ``_RESYNC_SETTLE`` to ``confirm_live`` as its
    ``settle`` (absorbing the su/sudo/exit tty-flush window before the first
    probe); every ``perform_switch``/``run_undo`` test would otherwise add
    that real delay. The settle behavior itself is covered by
    ``test_settles_before_first_probe`` in ``test_shell_liveness.py``.
    """
    monkeypatch.setattr(login_proxy, "_RESYNC_SETTLE", 0.0)


# Every run_proxy/run_undo call now ends with a post-transition resync (a
# confirm_live probe rendered by BashFrame.recover — an
# `echo "__OTTO_<id>_RECOVER__$?__"` send) — see
# otto.host.login_proxy._resync_shell. Filter it out of `sent` before
# asserting on the exact send sequence a test cares about, so these
# assertions stay meaningful (and don't just re-pin the resync's own noise).
_RESYNC_ECHO_PREFIX = 'echo "__OTTO_'

# The settle as SHIPPED, read at import time -- the autouse fixture above
# zeroes the module's copy, and one test needs the real window back.
_REAL_SETTLE = login_proxy._RESYNC_SETTLE


def _is_resync(text: str) -> bool:
    """Whether *text* is the engine's post-transition resync probe.

    Substring rather than prefix match: history suppression prepends a
    ``HISTFILE=…`` statement to the probe line, so the echo is no longer
    first on it.
    """
    return _RESYNC_ECHO_PREFIX in text


def _without_resync(
    sent: list[tuple[str, LogMode]],
) -> list[tuple[str, LogMode]]:
    """Drop the engine's post-transition resync echo probes from a `sent` log."""
    return [s for s in sent if not _is_resync(s[0])]


class RecorderIO:
    """ProxyIO fake: records sends, replays canned expect output.

    Probe replies are delegated to a :class:`ShellModel` so they carry the
    marker AND the current user, which is what the engine's resync actually
    checks -- a stub string satisfied "expect did not raise" but proved
    nothing about who answered. The model also tracks the `su` lines it is
    sent, so the identity it reports follows the switch under test rather than
    being pinned per test.

    ``replies`` remains for tests that want to hand a hop a canned prompt.
    """

    def __init__(
        self, replies: list[str] | None = None, user: str = "admin", prompts: bool = False
    ) -> None:
        self.sent: list[tuple[str, LogMode]] = []
        self._replies = list(replies or [])
        # *user* is for proxies whose mechanism this model cannot infer (a
        # custom proxy that sends nothing, or something that is not a `su`
        # line): the switch still happened, so the shell has to report it.
        # *prompts* models a shell that challenges on a switch, for the tests
        # about WHETHER otto arms itself to answer one.
        self.shell = ShellModel(user=user, challenges=prompts)

    async def send(self, text: str, *, log: LogMode = LogMode.NORMAL) -> None:
        self.sent.append((text, log))
        self.shell.wrote(text)

    async def expect(self, pattern, timeout: float = 10.0) -> str:
        if self.sent and _is_resync(self.sent[-1][0]):
            reply = self.shell.reply()
            return reply if reply is not None else ""
        return self._replies.pop(0) if self._replies else ""

    def resync_probes(self) -> list[str]:
        """Just the post-transition resync probes, in send order."""
        return [t for t, _ in self.sent if _is_resync(t)]


ADMIN = Cred(login="admin", password="hunter2")
MYSQL = Cred(login="mysql", password="sqlpw", proxy="su", via="admin")


def test_resolve_chain_direct():
    direct, hops = resolve_chain([ADMIN, MYSQL], "admin")
    assert direct == ADMIN
    assert hops == []


def test_resolve_chain_one_hop():
    direct, hops = resolve_chain([ADMIN, MYSQL], "mysql")
    assert direct == ADMIN
    assert hops == [MYSQL]


def test_resolve_chain_default_via_is_first_directly_loginable():
    orphan = Cred(login="svc", proxy="su")  # no via
    direct, hops = resolve_chain([ADMIN, orphan], "svc")
    assert direct == ADMIN
    assert hops == [orphan]


def test_resolve_chain_unknown_login_is_loud():
    with pytest.raises(LoginProxyError, match="admin"):
        resolve_chain([ADMIN], "nobody")


@pytest.mark.asyncio
async def test_su_proxy_sends_su_and_password():
    io = RecorderIO(replies=["Password:"])
    await run_proxy(io, MYSQL, via=ADMIN, host_id="h1")
    assert io.sent[0] == ("su - mysql\n", LogMode.NORMAL)
    assert io.sent[1] == ("sqlpw\n", LogMode.NEVER)


@pytest.mark.asyncio
async def test_su_proxy_passwordless_skips_expect():
    io = RecorderIO()
    await run_proxy(io, Cred(login="svc"), via=ADMIN, host_id="h1")
    assert _without_resync(io.sent) == [("su - svc\n", LogMode.NORMAL)]


@pytest.mark.asyncio
async def test_su_proxy_root_default():
    io = RecorderIO()
    await run_proxy(io, Cred(login=""), via=ADMIN, host_id="h1")
    assert _without_resync(io.sent) == [("su -\n", LogMode.NORMAL)]


@pytest.mark.asyncio
async def test_su_proxy_login_shell_opt_out_sends_bare_su():
    """``params={"login_shell": False}`` reverts one cred to the pre-login-shell form.

    The escape hatch for the ``su -`` default: a cred that must keep the
    caller's environment and cwd (the old bare-``su`` behavior) opts out here
    rather than having to register a whole custom proxy.
    """
    io = RecorderIO()
    cred = Cred(login="svc", proxy="su", params={"login_shell": False})
    await run_proxy(io, cred, via=ADMIN, host_id="h1")
    assert _without_resync(io.sent) == [("su svc\n", LogMode.NORMAL)]


@pytest.mark.asyncio
async def test_su_proxy_login_shell_opt_out_root_default():
    """The opt-out reaches the bare-``su``-to-root form too, not just named logins."""
    io = RecorderIO()
    cred = Cred(login="", proxy="su", params={"login_shell": False})
    await run_proxy(io, cred, via=ADMIN, host_id="h1")
    assert _without_resync(io.sent) == [("su\n", LogMode.NORMAL)]


@pytest.mark.asyncio
async def test_su_proxy_login_shell_true_is_the_default_spelling():
    """An explicit ``login_shell: True`` is the same as omitting it."""
    io = RecorderIO()
    cred = Cred(login="svc", proxy="su", params={"login_shell": True})
    await run_proxy(io, cred, via=ADMIN, host_id="h1")
    assert _without_resync(io.sent) == [("su - svc\n", LogMode.NORMAL)]


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

    async def send(self, text: str, *, log: LogMode = LogMode.NORMAL) -> None:
        self.sent.append((text, log))
        self.shell.wrote(text)

    async def expect(self, pattern, timeout: float = 10.0) -> str:
        reply = self.shell.reply()
        if reply is None or not re.search(pattern, reply):
            await asyncio.wait_for(asyncio.Event().wait(), timeout)
            raise AssertionError("unreachable")
        return reply


def _passwords(io: ModelIO) -> list[str]:
    return [t for t, mode in io.sent if mode is LogMode.NEVER]


@pytest.mark.asyncio
async def test_no_prompt_means_the_password_is_never_sent():
    """A known password must not be typed at a `su` that did not ask for one."""
    io = ModelIO(ShellModel(user="admin", challenges=False))
    await run_proxy(io, MYSQL, via=ADMIN, host_id="h1")
    assert _passwords(io) == []
    assert io.shell.user == "mysql"


@pytest.mark.asyncio
async def test_a_prompt_is_answered_and_the_switch_takes():
    """The common case: `su` challenges, the password answers it, we are mysql."""
    io = ModelIO(ShellModel(user="admin", password="sqlpw", challenges=True))
    await run_proxy(io, MYSQL, via=ADMIN, host_id="h1")
    assert _passwords(io) == ["sqlpw\n"]
    assert io.shell.user == "mysql"


@pytest.mark.asyncio
async def test_a_prompt_with_no_password_names_the_account():
    """Refuse loudly rather than spend authentication attempts guessing."""
    io = ModelIO(ShellModel(user="admin", password="x", challenges=True))
    with pytest.raises(LoginProxyError, match=r"'svc' asked for a password"):
        await run_proxy(io, Cred(login="svc", proxy="su"), via=ADMIN, host_id="h1")
    assert _passwords(io) == []


@pytest.mark.asyncio
async def test_a_rejected_password_is_not_reported_as_success():
    """THE regression the identity probe exists for.

    A wrong password makes `su` report the failure and EXIT, so the shell that
    answers the next liveness probe is the one we started in -- alive, correct,
    and the wrong user. Liveness alone calls that a successful switch; the
    identity in the probe is what makes it a failure.
    """
    io = ModelIO(ShellModel(user="admin", password="the-right-one", challenges=True))
    wrong = Cred(login="mysql", password="the-wrong-one", proxy="su", via="admin")
    with pytest.raises(LoginProxyError, match=r"answered as 'admin', not 'mysql'"):
        await run_proxy(io, wrong, via=ADMIN, host_id="h1")
    assert io.shell.auth_failures == 1, "exactly one attempt, never a retry loop"


@pytest.mark.asyncio
async def test_a_proxy_that_declares_no_prompt_is_never_answered_for():
    """A custom proxy answering its own prompt must not be answered for again.

    Left to the engine, the prompt text the proxy already dealt with is still
    in the buffer; answering it a second time types the password into the new
    shell AS A COMMAND, where it lands in history and in `ps`. Opt-in is what
    prevents that, so this pins that a proxy declaring no prompt gets no
    credential typed on its behalf.
    """
    io = ModelIO(ShellModel(user="admin", challenges=False))

    async def quiet(io_, ctx):
        await io_.send(f"become {ctx.target.login}\n")

    register_login_proxy("quiet-test", quiet)  # no prompt= declared
    try:
        hop = Cred(login="mysql", password="sqlpw", proxy="quiet-test", via="admin")
        io.shell.user = "mysql"  # the proxy's own mechanism did the switch
        await run_proxy(io, hop, via=ADMIN, host_id="h1")
    finally:
        LOGIN_PROXIES.unregister("quiet-test")
    assert _passwords(io) == [], "the engine typed a credential for a proxy that never asked"


@pytest.mark.asyncio
async def test_undo_does_not_answer_prompts():
    """`exit` has nothing to ask, so the undo path never offers a credential."""
    io = ModelIO(ShellModel(user="mysql", password="sqlpw", challenges=True))
    io.shell.user = "admin"  # exit landed us back at admin
    await run_undo(io, MYSQL, via=ADMIN, host_id="h1")
    assert _passwords(io) == []


@pytest.mark.asyncio
async def test_run_proxy_wraps_failure_with_context():
    async def boom(io, ctx):
        raise TimeoutError("no prompt")

    register_login_proxy("boom", boom)
    try:
        with pytest.raises(LoginProxyError, match=r"h1.*mysql.*boom"):
            await run_proxy(
                io=RecorderIO(), hop=Cred(login="mysql", proxy="boom"), via=ADMIN, host_id="h1"
            )
    finally:
        LOGIN_PROXIES.unregister("boom")


@pytest.mark.asyncio
async def test_run_proxy_unknown_proxy_name_raises_login_proxy_error():
    with pytest.raises(LoginProxyError, match=r"h1.*no-such-proxy"):
        await run_proxy(
            RecorderIO(), Cred(login="mysql", proxy="no-such-proxy"), via=ADMIN, host_id="h1"
        )


@pytest.mark.asyncio
async def test_run_undo_failure_wrapped_with_context():
    async def enter(io, ctx): ...

    async def bad_leave(io, ctx):
        raise RuntimeError("undo went sideways")

    register_login_proxy("bad-undo", enter, undo=bad_leave)
    try:
        with pytest.raises(LoginProxyError, match=r"h1.*mysql.*bad-undo"):
            await run_undo(
                RecorderIO(), Cred(login="mysql", proxy="bad-undo"), via=ADMIN, host_id="h1"
            )
    finally:
        LOGIN_PROXIES.unregister("bad-undo")


@pytest.mark.asyncio
async def test_default_undo_sends_exit():
    io = RecorderIO()
    await run_undo(io, MYSQL, via=ADMIN, host_id="h1")
    assert _without_resync(io.sent) == [("exit\n", LogMode.NORMAL)]


@pytest.mark.asyncio
async def test_custom_undo_used_when_registered():
    steps: list[str] = []

    async def enter(io, ctx):
        steps.append("enter")

    async def leave(io, ctx):
        steps.append("leave")

    register_login_proxy("custom", enter, undo=leave)
    try:
        hop = Cred(login="x", proxy="custom")
        await run_proxy(RecorderIO(user="x"), hop, via=ADMIN, host_id="h1")
        await run_undo(RecorderIO(user="admin"), hop, via=ADMIN, host_id="h1")
    finally:
        LOGIN_PROXIES.unregister("custom")
    assert steps == ["enter", "leave"]


def test_duplicate_registration_is_loud():
    async def p(io, ctx): ...

    register_login_proxy("dup-test", p)
    try:
        with pytest.raises(ValueError, match="already registered"):
            register_login_proxy("dup-test", p)
    finally:
        LOGIN_PROXIES.unregister("dup-test")


@pytest.mark.asyncio
async def test_perform_switch_plain_su_known_cred():
    io = RecorderIO(replies=["Password:"])
    applied = await perform_switch(
        io, [ADMIN], user="admin", password=None, current_user="operator", host_id="h1"
    )
    assert [c.login for c in applied] == ["admin"]
    assert io.sent[0] == ("su - admin\n", LogMode.NORMAL)
    assert io.sent[1] == ("hunter2\n", LogMode.NEVER)


@pytest.mark.asyncio
async def test_perform_switch_unknown_user_ad_hoc():
    io = RecorderIO()
    applied = await perform_switch(
        io, [ADMIN], user="ghost", password=None, current_user="admin", host_id="h1"
    )
    assert [c.login for c in applied] == ["ghost"]
    assert _without_resync(io.sent) == [("su - ghost\n", LogMode.NORMAL)]  # no password known


@pytest.mark.asyncio
async def test_perform_switch_explicit_password_overrides():
    io = RecorderIO(replies=["Password:"])
    await perform_switch(
        io, [ADMIN], user="admin", password="other", current_user="operator", host_id="h1"
    )
    assert io.sent[1] == ("other\n", LogMode.NEVER)


@pytest.mark.asyncio
async def test_perform_switch_recurses_through_via():
    io = RecorderIO(replies=["Password:", "Password:"])
    applied = await perform_switch(
        io, [ADMIN, MYSQL], user="mysql", password=None, current_user="operator", host_id="h1"
    )
    assert [c.login for c in applied] == ["admin", "mysql"]
    meaningful = _without_resync(io.sent)
    assert meaningful[0][0] == "su - admin\n"  # via first
    assert meaningful[2][0] == "su - mysql\n"  # then the proxy


@pytest.mark.asyncio
async def test_perform_switch_skips_via_when_already_there():
    io = RecorderIO(replies=["Password:"])
    applied = await perform_switch(
        io, [ADMIN, MYSQL], user="mysql", password=None, current_user="admin", host_id="h1"
    )
    assert [c.login for c in applied] == ["mysql"]


# ---------------------------------------------------------------------------
# _resync_shell — the post-transition marker-echo handshake itself
# ---------------------------------------------------------------------------


class _FlakyResyncIO:
    """ProxyIO fake whose expect() times out a fixed number of times first.

    Each send() is recorded so a test can confirm one echo probe is sent per
    attempt; expect() alternates between the two suppressed timeout types
    (``TimeoutError`` and ``asyncio.TimeoutError``) while "failing", proving
    ``_resync_shell`` tolerates either.

    ``sleep_on_fail`` makes a "failing" call actually await ``timeout`` before
    raising, like a real round-trip that timed out — needed for a deadline
    test: without it, this fake's instant (non-sleeping) failures let
    ``confirm_live``'s tight retry loop burn through any fixed ``fail_times``
    faster than a short deadline elapses, reaching a spurious "success" on the
    call after (mirrors ``_FakeIO.sleep_on_fail`` in ``test_shell_liveness.py``).
    """

    def __init__(self, fail_times: int, sleep_on_fail: bool = False) -> None:
        self.sent: list[str] = []
        self._fail_times = fail_times
        self._calls = 0
        self._sleep_on_fail = sleep_on_fail

    async def send(self, text: str, *, log: LogMode = LogMode.NORMAL) -> None:
        self.sent.append(text)

    async def expect(self, pattern: str, timeout: float = 10.0) -> str:
        self._calls += 1
        if self._calls <= self._fail_times:
            if self._sleep_on_fail:
                await asyncio.sleep(timeout)
            if self._calls % 2:
                raise TimeoutError("prompt lost in the flush window")
            raise asyncio.TimeoutError("prompt lost in the flush window")
        # A REAL reply, rendered from the marker the caller is asking about.
        # Echoing the pattern object back satisfied "expect returned", but the
        # engine now reads what it gets -- it has to tell a probe reply from a
        # credential prompt -- so a fake that answers with a regex's repr
        # would never confirm.
        marker = re.search(r"__OTTO_[0-9a-f]+_RECOVER__", pattern.pattern)
        assert marker, f"not a resync probe pattern: {pattern.pattern!r}"
        return f"\n{marker.group(0)}0__admin__\n"


@pytest.mark.asyncio
async def test_resync_shell_retries_past_timeouts_then_succeeds():
    io = _FlakyResyncIO(fail_times=2)
    await _resync_shell(io, host_id="h1", hop_login="mysql")
    assert io._calls == 3  # 2 failed attempts + the one that finally landed
    assert len(io.sent) == 3  # one fresh "echo <marker>" probe per attempt


@pytest.mark.asyncio
async def test_resync_shell_raises_login_proxy_error_when_deadline_elapses(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(login_proxy, "_RESYNC_DEADLINE", 0.05)  # short deadline for the test
    # sleep_on_fail: each failed attempt consumes real wall-clock time (like a
    # genuine timed-out round-trip), so the retry loop can't spin past
    # fail_times faster than the deadline elapses — see the fake's docstring.
    io = _FlakyResyncIO(fail_times=999, sleep_on_fail=True)  # never lands
    with pytest.raises(LoginProxyError, match=r"h1.*resync.*mysql"):
        await _resync_shell(io, host_id="h1", hop_login="mysql")
    assert io._calls >= 1  # it probed; the deadline (not a fixed count) ended it


# ---------------------------------------------------------------------------
# History suppression across a login-proxy transition
# ---------------------------------------------------------------------------

_QUIET = BashFrame().quiet_history()


@pytest.mark.asyncio
async def test_resync_probe_carries_suppression_when_requested():
    # `su` spawns a NEW shell that re-reads rc files, so HISTFILE resets to its
    # default. The payload has to RIDE the probe: sent afterwards, otto's own
    # resync echoes would already be in the elevated user's history.
    io = RecorderIO(["Password:"])
    await run_proxy(io, MYSQL, via=ADMIN, host_id="h1", history_prefix=_QUIET)
    probes = io.resync_probes()
    assert probes
    assert all(p.startswith(_QUIET) for p in probes)


@pytest.mark.asyncio
async def test_resync_probe_still_ends_in_the_echo_proof_probe():
    # Suppression must not displace the exit-code probe confirm_live matches on.
    io = RecorderIO(["Password:"])
    await run_proxy(io, MYSQL, via=ADMIN, host_id="h1", history_prefix=_QUIET)
    # Still echo-proof ($? cannot be faked by an echo of the probe text), and
    # now also identity-bearing -- both must survive the suppression prefix.
    assert all(p.rstrip("\n").endswith('$?__$(id -un)__"') for p in io.resync_probes())


@pytest.mark.asyncio
async def test_resync_probe_untouched_by_default():
    # Every existing caller keeps the byte-identical probe it had before.
    io = RecorderIO(["Password:"])
    await run_proxy(io, MYSQL, via=ADMIN, host_id="h1")
    probes = io.resync_probes()
    assert probes
    assert all(p.startswith(_RESYNC_ECHO_PREFIX) for p in probes)


@pytest.mark.asyncio
async def test_undo_resync_also_carries_suppression():
    # Returning via `exit` lands in a shell otto already quieted, but the undo
    # path shares the resync and the payload is idempotent — so it rides along
    # rather than special-casing direction.
    io = RecorderIO()
    await run_undo(io, MYSQL, via=ADMIN, host_id="h1", history_prefix=_QUIET)
    assert all(p.startswith(_QUIET) for p in io.resync_probes())


@pytest.mark.asyncio
async def test_perform_switch_threads_suppression_through_every_hop():
    # A multi-hop via-chain must quiet each new shell, not just the last.
    io = RecorderIO(["Password:", "Password:"])
    await perform_switch(io, [ADMIN, MYSQL], "mysql", None, "admin", "h1", history_prefix=_QUIET)
    probes = io.resync_probes()
    assert probes
    assert all(p.startswith(_QUIET) for p in probes)


@pytest.mark.asyncio
async def test_a_prompt_slower_than_the_settle_is_still_waited_for(monkeypatch):
    """A `su` that challenges LATE must not have a probe typed at its prompt.

    The prompt watch is deliberately longer than the tty-flush settle, for
    exactly this: a loaded host, or a PAM stack that takes its time, raises the
    prompt after the flush window has closed. If otto probed at that moment the
    probe line would be read AS THE PASSWORD -- one real authentication failure
    on the account, followed by an identity mismatch blaming a rejected
    password for a password that was never sent.

    The real settle is restored here (the module fixture zeroes it for speed
    everywhere else) so the two windows are the ones that ship, and the shell
    stays silent for a span that falls BETWEEN them. Shortening the watch back
    to the settle sends a probe first and turns this red.
    """
    monkeypatch.setattr(login_proxy, "_RESYNC_SETTLE", _REAL_SETTLE)
    late = (_REAL_SETTLE + login_proxy._PROMPT_WATCH) / 2
    assert _REAL_SETTLE < late < login_proxy._PROMPT_WATCH

    class LatePromptIO(ModelIO):
        """A shell whose `su` takes its time to challenge."""

        def __init__(self) -> None:
            super().__init__(ShellModel(user="admin", password="sqlpw", challenges=True))
            self.opened = asyncio.get_running_loop().time()

        async def expect(self, pattern, timeout: float = 10.0) -> str:
            # SILENT until `late`, then whatever the shell has to say -- and it
            # must not time out on the caller's behalf before then. Raising
            # early is what a shell does when it will never speak, and the
            # engine reads that as "no prompt is coming" and starts probing:
            # the fake would have hidden the very window under test.
            elapsed = asyncio.get_running_loop().time() - self.opened
            remaining = timeout - max(0.0, late - elapsed)
            if remaining <= 0:  # the caller gives up before this shell speaks
                await asyncio.wait_for(asyncio.Event().wait(), timeout)
                raise AssertionError("unreachable")
            if elapsed < late:
                await asyncio.sleep(late - elapsed)
            return await super().expect(pattern, remaining)

    io = LatePromptIO()
    await run_proxy(io, MYSQL, via=ADMIN, host_id="h1")

    assert _passwords(io) == ["sqlpw\n"]
    assert io.shell.user == "mysql"
    assert io.shell.auth_failures == 0

    written = [t for t, _ in io.sent]
    first_probe = next((i for i, t in enumerate(written) if _is_resync(t)), len(written))
    assert written.index("sqlpw\n") < first_probe, (
        f"a probe was typed at the password prompt: {written}"
    )


@pytest.mark.asyncio
async def test_a_shell_that_cannot_name_itself_still_resyncs():
    """A userland with no `id -un` degrades to liveness, it does not fail.

    ``$(id -un)`` expands to NOTHING where the applet is missing, so the reply
    arrives well-formed but anonymous. That must leave otto exactly where it
    stood before identity was proven at all — refusing the switch instead
    would break every host the probe simply cannot interrogate, which is the
    opposite of what an added check is for.
    """

    class AnonymousIO(RecorderIO):
        """A shell that answers probes but will not say who it is."""

        async def expect(self, pattern, timeout: float = 10.0) -> str:
            reply = await super().expect(pattern, timeout)
            return re.sub(r"__[^_]*__$", "____", reply) if reply else reply

    io = AnonymousIO()
    await run_proxy(io, MYSQL, via=ADMIN, host_id="h1")  # must not raise
    assert io.resync_probes()


@pytest.mark.asyncio
async def test_a_root_caller_is_not_watched_for_a_prompt_it_cannot_get():
    """From root, `su` does not challenge — so otto must not wait to find out.

    The wait is the whole cost: measured on the bed, arming it for a hop that
    cannot be challenged added ~0.2s to every such switch. Whether `su` asks is
    a property of WHO IS ASKING, and otto already knows that, so this is a
    decision rather than an observation.
    """
    io = RecorderIO(user="root")
    await run_proxy(io, MYSQL, via=Cred(login="root"), host_id="h1")
    assert not any(
        text == f"{MYSQL.password}\n" for text, _ in io.sent
    ), "sent a password to a `su` that never asks"


@pytest.mark.asyncio
async def test_an_unknown_caller_is_still_watched():
    """An empty via login is UNKNOWN, and must never be read as root.

    `via.login or "root"` makes those two indistinguishable, and defaults the
    unknown case to the unsafe side: answering disarmed for a hop that is in
    fact challenged. Unknown costs the watch; that is the right price.
    """
    io = RecorderIO(replies=["Password:"], user="", prompts=True)
    await run_proxy(io, MYSQL, via=Cred(login=""), host_id="h1")
    assert any(text == f"{MYSQL.password}\n" for text, _ in io.sent), (
        "an unidentified caller was treated as root and left unanswered"
    )


@pytest.mark.asyncio
async def test_a_host_that_challenges_root_can_force_the_watch_back_on():
    """The escape hatch for a PAM stack without `pam_rootok`."""
    forced = replace(MYSQL, params={**MYSQL.params, "expect_prompt": True})
    io = RecorderIO(replies=["Password:"], user="root", prompts=True)
    await run_proxy(io, forced, via=Cred(login="root"), host_id="h1")
    assert any(text == f"{forced.password}\n" for text, _ in io.sent), (
        "params={'expect_prompt': True} did not re-arm the prompt watch"
    )


class _SlowPromptIO:
    """A host that raises its credential prompt LATE, after a startup pause.

    Models the window that matters: between otto's ``su`` line and the prompt,
    ``su`` is starting up and its terminal is live. Anything otto writes into
    that window is either discarded by su's typeahead flush or read AS THE
    PASSWORD, and which one happens is a matter of microseconds.
    """

    def __init__(self, delay: float, target: str = "mysql") -> None:
        self.delay, self.target = delay, target
        self.sent: list[str] = []
        self.wrote_into_startup_window: list[str] = []
        self._su_at: float | None = None
        self._prompted = False

    async def send(self, text: str, *, log: LogMode = LogMode.NORMAL) -> None:
        self.sent.append(text)
        if text.startswith("su"):
            self._su_at = asyncio.get_running_loop().time()
        elif self._su_at is not None and not self._prompted:
            self.wrote_into_startup_window.append(text)

    async def expect(self, pattern, timeout: float = 10.0) -> str:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            if self._su_at is not None and loop.time() - self._su_at >= self.delay:
                if not self._prompted:
                    self._prompted = True
                    if pattern.search("Password:"):
                        return "Password:"
                if any(t.strip() == "sqlpw" for t in self.sent):
                    for text in reversed(self.sent):
                        found = re.search(r'echo "(__OTTO_[0-9a-f]+_RECOVER__)', text)
                        if found:
                            reply = f"{found.group(1)}0__{self.target}__"
                            if pattern.search(reply):
                                return reply
                            break
            await asyncio.sleep(0.005)
        raise asyncio.TimeoutError()


@pytest.mark.asyncio
async def test_a_slow_prompt_is_waited_for_rather_than_probed_over():
    """otto must not write into the window where `su` is still starting up.

    A host that takes a moment to raise its prompt used to get otto's first
    resync probe typed into that window. Two things could then happen, decided
    by microseconds: `su` flushes the typeahead and all is well, or it reads
    the probe AS THE PASSWORD, fails the authentication and EXITS -- leaving a
    stale `Password:` on screen with the CALLER's shell behind it. Answering
    that stale prompt puts the password into the caller's shell as a command,
    in its history and in `ps`.

    The identity assertion notices the failed switch, but noticing does not
    un-send a password. So the guarantee has to be that nothing is written
    before the prompt is due at all -- which is affordable precisely because
    otto only waits when it knows a prompt is coming.
    """
    io = _SlowPromptIO(delay=0.7)
    await run_proxy(io, MYSQL, via=Cred(login="admin"), host_id="h1")

    assert io.wrote_into_startup_window == [], (
        "otto wrote into su's startup window, where the write can be eaten as "
        f"the password: {io.wrote_into_startup_window}"
    )
    assert sum(1 for t in io.sent if t.strip() == "sqlpw") == 1, "password not sent exactly once"

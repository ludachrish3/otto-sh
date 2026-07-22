import asyncio

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

    Resync-aware: if the most recent send was the login-proxy engine's
    post-transition ``confirm_live`` resync probe, expect() answers it
    without raising (confirming it immediately) rather than popping a queued
    reply meant for a real hop's prompt — otherwise a resync inserted between
    two hops would silently consume the next hop's canned "Password:" reply.
    """

    def __init__(self, replies: list[str] | None = None) -> None:
        self.sent: list[tuple[str, LogMode]] = []
        self._replies = list(replies or [])

    async def send(self, text: str, *, log: LogMode = LogMode.NORMAL) -> None:
        self.sent.append((text, log))

    async def expect(self, pattern, timeout: float = 10.0) -> str:
        if self.sent and _is_resync(self.sent[-1][0]):
            return "resync-ok"  # confirm_live treats a non-raising expect as confirmed
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
    assert io.sent[0] == ("su mysql\n", LogMode.NORMAL)
    assert io.sent[1] == ("sqlpw\n", LogMode.NEVER)


@pytest.mark.asyncio
async def test_su_proxy_passwordless_skips_expect():
    io = RecorderIO()
    await run_proxy(io, Cred(login="svc"), via=ADMIN, host_id="h1")
    assert _without_resync(io.sent) == [("su svc\n", LogMode.NORMAL)]


@pytest.mark.asyncio
async def test_su_proxy_root_default():
    io = RecorderIO()
    await run_proxy(io, Cred(login=""), via=ADMIN, host_id="h1")
    assert _without_resync(io.sent) == [("su\n", LogMode.NORMAL)]


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
        await run_proxy(RecorderIO(), hop, via=ADMIN, host_id="h1")
        await run_undo(RecorderIO(), hop, via=ADMIN, host_id="h1")
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
        io, [ADMIN], user="admin", password=None, current_user="root", host_id="h1"
    )
    assert [c.login for c in applied] == ["admin"]
    assert io.sent[0] == ("su admin\n", LogMode.NORMAL)
    assert io.sent[1] == ("hunter2\n", LogMode.NEVER)


@pytest.mark.asyncio
async def test_perform_switch_unknown_user_ad_hoc():
    io = RecorderIO()
    applied = await perform_switch(
        io, [ADMIN], user="ghost", password=None, current_user="admin", host_id="h1"
    )
    assert [c.login for c in applied] == ["ghost"]
    assert _without_resync(io.sent) == [("su ghost\n", LogMode.NORMAL)]  # no password known


@pytest.mark.asyncio
async def test_perform_switch_explicit_password_overrides():
    io = RecorderIO(replies=["Password:"])
    await perform_switch(
        io, [ADMIN], user="admin", password="other", current_user="root", host_id="h1"
    )
    assert io.sent[1] == ("other\n", LogMode.NEVER)


@pytest.mark.asyncio
async def test_perform_switch_recurses_through_via():
    io = RecorderIO(replies=["Password:", "Password:"])
    applied = await perform_switch(
        io, [ADMIN, MYSQL], user="mysql", password=None, current_user="root", host_id="h1"
    )
    assert [c.login for c in applied] == ["admin", "mysql"]
    meaningful = _without_resync(io.sent)
    assert meaningful[0][0] == "su admin\n"  # via first
    assert meaningful[2][0] == "su mysql\n"  # then the proxy


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
        return f"\n{pattern}\n"


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
    assert all(p.rstrip("\n").endswith('$?__"') for p in io.resync_probes())


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

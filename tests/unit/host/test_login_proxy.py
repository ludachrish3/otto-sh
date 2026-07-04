import asyncio

import pytest

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

# Every run_proxy/run_undo call now ends with a post-transition resync (an
# "echo <marker>\n" send + expect(marker) pair) — see
# otto.host.login_proxy._resync_shell. Filter it out of `sent` before
# asserting on the exact send sequence a test cares about, so these
# assertions stay meaningful (and don't just re-pin the resync's own noise).
_RESYNC_ECHO_PREFIX = "echo __OTTO_LP_SYNC_"


def _without_resync(
    sent: list[tuple[str, LogMode]],
) -> list[tuple[str, LogMode]]:
    """Drop the engine's post-transition resync echo probes from a `sent` log."""
    return [s for s in sent if not s[0].startswith(_RESYNC_ECHO_PREFIX)]


class RecorderIO:
    """ProxyIO fake: records sends, replays canned expect output.

    Resync-aware: if the most recent send was the login-proxy engine's
    post-transition "echo <marker>" resync probe, expect() answers it with
    the marker directly rather than popping a queued reply meant for a real
    hop's prompt — otherwise a resync inserted between two hops would
    silently consume the next hop's canned "Password:" reply.
    """

    def __init__(self, replies: list[str] | None = None) -> None:
        self.sent: list[tuple[str, LogMode]] = []
        self._replies = list(replies or [])

    async def send(self, text: str, *, log: LogMode = LogMode.NORMAL) -> None:
        self.sent.append((text, log))

    async def expect(self, pattern, timeout: float = 10.0) -> str:
        if self.sent and self.sent[-1][0].startswith(_RESYNC_ECHO_PREFIX):
            marker = self.sent[-1][0].removeprefix("echo ").rstrip("\n")
            return f"\n{marker}\n"
        return self._replies.pop(0) if self._replies else ""


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
    """

    def __init__(self, fail_times: int) -> None:
        self.sent: list[str] = []
        self._fail_times = fail_times
        self._calls = 0

    async def send(self, text: str, *, log: LogMode = LogMode.NORMAL) -> None:
        self.sent.append(text)

    async def expect(self, pattern: str, timeout: float = 10.0) -> str:
        self._calls += 1
        if self._calls <= self._fail_times:
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
async def test_resync_shell_raises_login_proxy_error_after_exhausting_attempts():
    io = _FlakyResyncIO(fail_times=999)  # never lands
    with pytest.raises(LoginProxyError, match=r"h1.*resync.*mysql"):
        await _resync_shell(io, host_id="h1", hop_login="mysql")
    assert io._calls == 5  # _RESYNC_ATTEMPTS, no more and no fewer

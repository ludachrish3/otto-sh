import pytest

from otto.host.login_proxy import (
    LOGIN_PROXIES,
    Cred,
    LoginProxyError,
    register_login_proxy,
    resolve_chain,
    run_proxy,
    run_undo,
)
from otto.logger.mode import LogMode


class RecorderIO:
    """ProxyIO fake: records sends, replays canned expect output."""

    def __init__(self, replies: list[str] | None = None) -> None:
        self.sent: list[tuple[str, LogMode]] = []
        self._replies = list(replies or [])

    async def send(self, text: str, *, log: LogMode = LogMode.NORMAL) -> None:
        self.sent.append((text, log))

    async def expect(self, pattern, timeout: float = 10.0) -> str:
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
    assert io.sent == [("su svc\n", LogMode.NORMAL)]


@pytest.mark.asyncio
async def test_su_proxy_root_default():
    io = RecorderIO()
    await run_proxy(io, Cred(login=""), via=ADMIN, host_id="h1")
    assert io.sent == [("su\n", LogMode.NORMAL)]


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
async def test_default_undo_sends_exit():
    io = RecorderIO()
    await run_undo(io, MYSQL, via=ADMIN, host_id="h1")
    assert io.sent == [("exit\n", LogMode.NORMAL)]


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

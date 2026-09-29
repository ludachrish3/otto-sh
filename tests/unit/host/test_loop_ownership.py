"""A host's connection belongs to one event loop, and each loop's scope closes what it owns."""

import asyncio
import dataclasses
import logging
import re
import time

import pytest

from otto.context import reset_context, set_context
from otto.host.docker_host import DockerContainerHost
from otto.host.local_host import LocalHost
from otto.host.loop_owner import LOOP_LABELS, HostLoopError, loop_label
from otto.host.options import SshOptions
from tests.conftest import make_host


@pytest.fixture
def ctx(make_otto_context):
    ctx = make_otto_context()
    token = set_context(ctx)
    yield ctx
    reset_context(token)


def _local(host_id: str) -> LocalHost:
    """A LocalHost under its own id: the family pins ``id``/``name``, so the test renames it."""
    host = LocalHost()
    host.id = host_id
    return host


async def _run_then_free_the_shell(host: LocalHost) -> None:
    """Run a command, then free the shell process while the host still owns this loop.

    What a loop leaves behind when it closes unswept, minus the live bash
    process: an abandoned shell's pipes outlive the test (this directory's fd
    bracket counts them), and the stale-owner path drops the session whether
    or not its process still runs.
    """
    await host.run("true")
    await host._session_mgr.close_all()


def test_first_use_registers_with_the_running_loops_scope(ctx):
    host = LocalHost()

    async def use():
        await host.run("true")
        loop = asyncio.get_running_loop()
        assert ctx.scope_for(loop)._hosts == [host]
        return await ctx.sweep_loop(loop, label="probe's loop")

    assert asyncio.run(use()) == [host.id]


def test_a_live_foreign_loop_fails_fast_naming_the_host_and_both_loops(ctx):
    host = LocalHost()
    owner = asyncio.new_event_loop()
    other = asyncio.new_event_loop()
    LOOP_LABELS[owner] = "TestRouter's loop"
    LOOP_LABELS[other] = "the session's loop"
    try:
        owner.run_until_complete(host.run("true"))
        with pytest.raises(HostLoopError) as err:
            other.run_until_complete(host.run("true"))
        text = str(err.value)
        assert host.id in text
        assert "TestRouter's loop" in text
        assert "the session's loop" in text
        assert "host-scopes" in text
        with pytest.raises(HostLoopError):
            other.run_until_complete(host.close())
        owner.run_until_complete(host.close())
    finally:
        owner.close()
        other.close()


_SESSION_HINT = "stays on the session's loop for the whole run"


def test_a_session_owned_host_used_from_a_narrower_loop_says_why():
    """Owned by the session's loop: the error says unpinned use keeps it there all run."""
    owner = asyncio.new_event_loop()
    caller = asyncio.new_event_loop()
    LOOP_LABELS[owner] = "the session's loop"
    LOOP_LABELS[caller] = "TestPinned's loop"
    try:
        text = str(HostLoopError("dut1", owner, caller))
    finally:
        owner.close()
        caller.close()
    assert text.startswith(
        "host 'dut1' is connected on the session's loop but was used from TestPinned's loop."
    )
    assert _SESSION_HINT in text
    assert "need hosts only they use" in text
    assert "host-scopes" in text


def test_a_host_owned_by_a_narrower_loop_gets_no_session_hint():
    owner = asyncio.new_event_loop()
    caller = asyncio.new_event_loop()
    LOOP_LABELS[owner] = "TestRouter's loop"
    LOOP_LABELS[caller] = "the session's loop"
    try:
        text = str(HostLoopError("dut1", owner, caller))
    finally:
        owner.close()
        caller.close()
    assert _SESSION_HINT not in text


def test_an_unlabelled_caller_gets_no_session_hint():
    """A plain ``asyncio.run`` loop (say, from a sync test) is not a narrower pytest loop."""
    owner = asyncio.new_event_loop()
    LOOP_LABELS[owner] = "the session's loop"

    async def use_from_a_plain_loop() -> str:
        return str(HostLoopError("dut1", owner, asyncio.get_running_loop()))

    try:
        text = asyncio.run(use_from_a_plain_loop())
    finally:
        owner.close()
    assert "was used from event loop 0x" in text
    assert _SESSION_HINT not in text


def test_a_caller_that_is_another_sessions_loop_gets_no_session_hint():
    """A nested in-process session's loop carries the session label too, and is not narrower."""
    owner = asyncio.new_event_loop()
    caller = asyncio.new_event_loop()
    LOOP_LABELS[owner] = "the session's loop"
    LOOP_LABELS[caller] = "the session's loop"
    try:
        text = str(HostLoopError("dut1", owner, caller))
    finally:
        owner.close()
        caller.close()
    assert _SESSION_HINT not in text


def test_the_error_is_otto_and_runtime_rooted_and_names_an_unlabelled_loop_by_id():
    loop = asyncio.new_event_loop()
    try:
        assert loop_label(loop) == f"event loop {id(loop):#x}"
        err = HostLoopError("dut1", loop, loop)
        assert isinstance(err, RuntimeError)
        assert f"{id(loop):#x}" in str(err)
    finally:
        loop.close()


def test_close_frees_the_host_for_another_loop(ctx):
    host = LocalHost()
    owner = asyncio.new_event_loop()
    other = asyncio.new_event_loop()
    try:
        owner.run_until_complete(host.run("true"))
        owner.run_until_complete(host.close())
        out = other.run_until_complete(host.run("echo moved"))
        assert out.only.value.strip() == "moved"
        other.run_until_complete(host.close())
    finally:
        owner.close()
        other.close()


def test_a_loop_closed_without_a_sweep_leaves_nothing_to_trip_on(ctx):
    host = LocalHost()
    asyncio.run(_run_then_free_the_shell(host))  # a sync test's own asyncio.run; nobody sweeps it
    stale_mgr = host._session_mgr

    async def later():
        try:
            return (await host.run("echo again")).only.value.strip()
        finally:
            await host.close()

    assert asyncio.run(later()) == "again"
    assert host._session_mgr is not stale_mgr


def test_close_after_the_owner_loop_closed_does_no_network_work(ctx, monkeypatch):
    host = LocalHost()
    asyncio.run(_run_then_free_the_shell(host))
    stale_mgr = host._session_mgr
    calls: "list[str]" = []

    async def spy() -> None:
        calls.append("_close")

    monkeypatch.setattr(host, "_close", spy)
    asyncio.run(host.close())
    assert calls == []
    assert host._session_mgr is not stale_mgr
    assert host._owner_loop is None


def test_override_copies_are_owned_and_swept_separately(ctx_with_local_lab):
    """LocalHost takes no option override; its copy is built the way the override path builds it."""
    ctx = ctx_with_local_lab

    async def use():
        a = ctx.get_host("local")
        b = dataclasses.replace(a)
        await a.run("true")
        await b.run("true")
        assert a is not b
        loop = asyncio.get_running_loop()
        owned = ctx.scope_for(loop)._hosts
        assert len(owned) == 2
        assert owned[0] is a
        assert owned[1] is b
        closed = await ctx.sweep_loop(loop, label="x")
        assert closed == [a.id, b.id]

    asyncio.run(use())


def test_an_override_copy_starts_unowned_with_its_own_managers(make_otto_context):
    ctx = make_otto_context("test1")
    (host_id,) = ctx.lab.hosts
    token = set_context(ctx)
    try:

        async def use():
            a = ctx.get_host(host_id)
            a._claim_loop()
            b = ctx.get_host(host_id, ssh_options=SshOptions())
            assert b is not a
            assert b._owner_loop is None
            assert b._session_mgr is not a._session_mgr
            assert b._connections is not a._connections
            b._claim_loop()
            loop = asyncio.get_running_loop()
            owned = ctx.scope_for(loop)._hosts
            assert len(owned) == 2
            assert owned[0] is a
            assert owned[1] is b

        asyncio.run(use())
    finally:
        reset_context(token)


def test_get_host_alone_registers_nothing(ctx_with_local_lab):
    ctx = ctx_with_local_lab

    async def use():
        ctx.get_host("local")
        list(ctx.all_hosts(include_local=True))
        assert ctx.scope_for(asyncio.get_running_loop())._hosts == []

    asyncio.run(use())


def test_the_sweep_logs_one_debug_line_naming_the_hosts(ctx, caplog):
    hosts = [_local("dut1"), _local("dut2")]

    async def use():
        for h in hosts:
            await h.run("true")
        with caplog.at_level(logging.DEBUG, logger="otto"):
            await ctx.sweep_loop(asyncio.get_running_loop(), label="TestRouter's loop")

    asyncio.run(use())
    ids = ", ".join(h.id for h in hosts)
    assert f"closed 2 hosts at end of TestRouter's loop: {ids}" in caplog.text


def test_a_failed_close_is_a_warning_and_the_rest_still_close(ctx, caplog, monkeypatch):
    bad, good = _local("bad"), _local("good")

    async def boom():
        raise OSError("socket gone")

    monkeypatch.setattr(bad, "_close", boom)

    async def use():
        await bad.run("true")
        await good.run("true")
        closed = await ctx.sweep_loop(asyncio.get_running_loop(), label="x")
        assert bad._owner_loop is None  # close()'s finally released the loop despite the raise
        monkeypatch.undo()
        await bad.close()  # the failed close still released the loop; this frees the shell
        return closed

    with caplog.at_level(logging.WARNING, logger="otto"):
        closed = asyncio.run(use())
    assert good.id in closed
    assert bad.id not in closed
    assert "'bad'" in caplog.text
    assert "socket gone" in caplog.text


def test_a_sweep_that_outlasts_its_deadline_drops_the_stuck_host(ctx, caplog, monkeypatch):
    """The sweep is bounded; a close still running at the deadline is cancelled and dropped.

    The hosts that did close are still returned and named in the debug line.
    """
    slow, quick = _local("slow"), _local("quick")
    real_close = slow._close

    async def hang():
        try:
            await asyncio.sleep(30)
        finally:  # the deadline cancels this; free the shell anyway
            await real_close()

    monkeypatch.setattr(slow, "_close", hang)
    dropped: list[str] = []
    monkeypatch.setattr(slow, "_drop_dead_connections", lambda: dropped.append(slow.id))

    async def use():
        await slow.run("true")
        await quick.run("true")
        return await ctx.sweep_loop(
            asyncio.get_running_loop(), label="TestSlow's loop", deadline=0.1
        )

    with caplog.at_level(logging.DEBUG, logger="otto"):
        assert asyncio.run(use()) == ["quick"]
    assert dropped == ["slow"]
    assert slow._owner_loop is None
    assert quick._owner_loop is None
    assert re.search(
        r"closing hosts at end of TestSlow's loop ran past its deadline; gave up after "
        r"0\.\ds and dropped the connections of: slow$",
        caplog.text,
        re.MULTILINE,
    ), caplog.text
    assert "closed 1 host at end of TestSlow's loop: quick" in caplog.text


def test_a_close_stuck_in_its_own_cleanup_cannot_stretch_the_deadline(ctx):
    """A cancelled close whose ``finally`` hangs too is cancelled again after a short grace.

    ``asyncio.wait_for`` would wait out that ``finally``: a transport close
    that hangs on a dead peer held a 0.2 s deadline to seconds.
    """
    stuck = _local("stuck")
    real_close = stuck._close

    async def hang():
        try:
            await asyncio.sleep(30)
        finally:
            try:
                await asyncio.sleep(30)  # the cleanup in the finally hangs as well
            finally:
                await real_close()  # free the shell

    stuck._close = hang  # type: ignore[method-assign]

    async def use():
        await stuck.run("true")
        started = time.monotonic()
        closed = await ctx.sweep_loop(asyncio.get_running_loop(), label="x", deadline=0.2)
        return closed, time.monotonic() - started

    closed, elapsed = asyncio.run(use())
    assert closed == []
    assert elapsed < 2, f"the sweep took {elapsed:.1f}s against a 0.2s deadline"
    assert stuck._owner_loop is None


def test_the_sweep_leaves_a_host_its_own_code_closed(ctx, caplog):
    """A host closed before the loop ends has nothing left to close; the debug line omits it."""
    mine, left = _local("mine"), _local("left")
    closes: list[str] = []
    for host in (mine, left):
        real = host._close

        async def counted(real=real, host=host):
            closes.append(host.id)
            await real()

        host._close = counted  # type: ignore[method-assign]

    async def use():
        await mine.run("true")
        await left.run("true")
        await mine.close()
        with caplog.at_level(logging.DEBUG, logger="otto"):
            return await ctx.sweep_loop(asyncio.get_running_loop(), label="x")

    assert asyncio.run(use()) == ["left"]
    assert closes == ["mine", "left"]
    assert "closed 1 host at end of x: left" in caplog.text


def test_abandon_closed_loops_drops_what_nobody_swept(ctx, caplog):
    host = _local("dut1")
    asyncio.run(_run_then_free_the_shell(host))
    stale_mgr = host._session_mgr

    with caplog.at_level(logging.DEBUG, logger="otto"):
        assert ctx.abandon_closed_loops() == ["dut1"]
    assert "abandoned 1 host left on closed loops: dut1" in caplog.text
    assert host._owner_loop is None
    assert host._session_mgr is not stale_mgr
    assert ctx.abandon_closed_loops() == []


def test_a_sweep_of_a_loop_with_no_hosts_is_empty_and_silent(ctx, caplog):
    async def use():
        with caplog.at_level(logging.DEBUG, logger="otto"):
            return await ctx.sweep_loop(asyncio.get_running_loop(), label="x")

    assert asyncio.run(use()) == []
    assert "closed" not in caplog.text


def test_abandon_leaves_a_host_that_moved_to_a_live_loop_alone(ctx):
    host = _local("dut1")
    first = asyncio.new_event_loop()
    second = asyncio.new_event_loop()
    try:
        first.run_until_complete(host.run("true"))
        first.run_until_complete(host.close())
        first.close()
        second.run_until_complete(host.run("true"))
        live_mgr = host._session_mgr

        assert ctx.abandon_closed_loops() == []
        assert host._owner_loop is second
        assert host._session_mgr is live_mgr
        assert second.run_until_complete(ctx.sweep_loop(second, label="x")) == ["dut1"]
    finally:
        first.close()
        second.close()


def test_a_sweep_skips_a_host_that_moved_to_another_loop(ctx):
    host = _local("dut1")
    first = asyncio.new_event_loop()
    second = asyncio.new_event_loop()
    try:
        first.run_until_complete(host.run("true"))
        first.run_until_complete(host.close())
        second.run_until_complete(host.run("true"))

        assert first.run_until_complete(ctx.sweep_loop(first, label="first")) == []
        assert host._owner_loop is second
        assert second.run_until_complete(ctx.sweep_loop(second, label="second")) == ["dut1"]
    finally:
        first.close()
        second.close()


def test_current_user_after_an_unswept_loop_is_the_login_user_not_the_dead_sessions(ctx):
    """The dead loop's switch must not decide sudo on the next loop: the read claims first."""
    host = LocalHost()
    login_user = LocalHost().current_user

    async def switch_then_leave():
        await host.run("true")
        # What a persisting `switch_user("root")` records on the default session.
        host._session_mgr._set_current_user("root")
        # Free the shell process; the manager keeps the dead session and its record.
        await host._session_mgr._session.close()

    asyncio.run(switch_then_leave())
    assert host._session_mgr.current_user == "root"

    async def first_read():
        return host.current_user

    assert asyncio.run(first_read()) == login_user
    assert login_user != "root"


def _container() -> DockerContainerHost:
    return DockerContainerHost(
        parent=make_host("test1"),
        container_id="abc123def456",
        project="repo1",
        service="api",
        compose_project="otto-repo1-vagrant",
    )


@pytest.mark.parametrize(
    ("family", "read"),
    [
        ("unix", lambda h: h._ambient_user()),
        ("docker", lambda h: h._ambient_user()),
        ("docker", lambda h: h._run_channel_is_bound),
        ("local", lambda h: h.current_user),
    ],
    ids=[
        "unix-ambient-user",
        "docker-ambient-user",
        "docker-run-channel-bound",
        "local-current-user",
    ],
)
def test_a_sync_read_of_session_state_claims_the_running_loop(ctx, family, read):
    """Every sync reader of loop-bound session state is a gateway, like ``connections``."""
    host = {"unix": lambda: make_host("test1"), "docker": _container, "local": LocalHost}[family]()

    async def claim():
        host._claim_loop()
        return asyncio.get_running_loop()

    first = asyncio.run(claim())  # closes unswept: nothing here sweeps it
    assert host._owner_loop is first
    stale_mgr = host._session_mgr

    async def later():
        read(host)
        return asyncio.get_running_loop()

    second = asyncio.run(later())
    assert host._owner_loop is second
    assert host._session_mgr is not stale_mgr


def test_sweep_loop_refuses_a_loop_that_is_not_the_running_one(ctx):
    """The sweep closes hosts on the RUNNING loop, so it cannot sweep any other."""
    host = _local("dut1")
    other = asyncio.new_event_loop()
    try:
        other.run_until_complete(host.run("true"))

        async def sweep_the_wrong_loop():
            return await ctx.sweep_loop(other, label="other")

        with pytest.raises(RuntimeError, match=r"sweep_loop.*running"):
            asyncio.run(sweep_the_wrong_loop())
        assert ctx.scope_for(other)._hosts == [host]  # refused before draining anything
        assert other.run_until_complete(ctx.sweep_loop(other, label="other")) == ["dut1"]
    finally:
        other.close()


def test_many_loops_under_one_context_do_not_grow_the_scope_table(ctx):
    """``scope_for`` prunes closed loops' entries, so a long-lived context stays bounded."""

    async def touch():
        ctx.scope_for(asyncio.get_running_loop())

    for _ in range(20):
        asyncio.run(touch())
    assert len(ctx._loop_scopes) == 1

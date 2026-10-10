"""Who holds a loop's cleanup boundary, and when its hosts close (spec 2 §3.3 "Boundaries")."""

import asyncio
import signal

import pytest

from otto import invocation as inv
from otto import lifecycle
from otto.config.lab import Lab
from otto.context import open_context
from otto.host import LocalHost
from otto.invocation import RunPolicy, install_policy, reset_binding
from tests._fixtures.registry import DuckHost, registered_ids


def _controller(deadline: float = 10.0) -> lifecycle._CommandRun:
    return lifecycle._CommandRun(teardown_deadline=deadline, install_handlers=False)


class _NoHandlers(lifecycle._CommandRun):
    """What run_command builds when no controller is passed, minus real signal handlers."""

    def __init__(self, *, teardown_deadline, install_handlers=True):
        super().__init__(teardown_deadline=teardown_deadline, install_handlers=False)


async def _noop():
    return None


@pytest.fixture
def lab_without_repos(monkeypatch):
    """A lab for open_context, with bootstrap installed as no repos (as test_context.py does).

    Discovery stays real, so OTTO_TEARDOWN_DEADLINE still reaches the run's policy.
    """
    from otto import bootstrap as bs

    bs._reset()
    monkeypatch.setattr(bs, "_result", bs.BootstrapResult(env=None, repos=[]))  # type: ignore[arg-type]
    return Lab(name="rig")


def test_a_context_less_connect_under_run_command_is_swept_when_the_command_ends():
    seen: dict = {}

    async def body():
        duck = DuckHost("h")
        duck.claim(asyncio.get_running_loop())
        seen["duck"] = duck

    lifecycle.run_command(body(), _controller=_controller())
    assert seen["duck"].closes_finished == 1


def test_a_finished_command_leaves_no_registry_behind():
    seen: dict = {}

    async def body():
        seen["loop"] = asyncio.get_running_loop()
        DuckHost("h").claim(seen["loop"])

    lifecycle.run_command(body(), _controller=_controller())
    assert seen["loop"] not in inv._REGISTRIES


@pytest.mark.asyncio
async def test_an_inner_open_context_exit_leaves_the_outer_contexts_host_open(lab_without_repos):
    loop = asyncio.get_running_loop()
    async with open_context(lab=lab_without_repos):
        shared = DuckHost("shared")
        async with open_context(lab=lab_without_repos):
            shared.claim(loop)
        assert shared.closes_finished == 0
        assert registered_ids(loop) == ["shared"]
    assert shared.closes_finished == 1


def test_a_command_boundary_records_the_explicit_deadline_over_the_policy(monkeypatch):
    recorded: list = []
    real = inv.acquire_boundary

    def spy(loop, *, deadline):
        recorded.append(deadline)
        return real(loop, deadline=deadline)

    monkeypatch.setattr("otto.invocation.acquire_boundary", spy)
    monkeypatch.setattr("otto.lifecycle._CommandRun", _NoHandlers)
    binding = install_policy(RunPolicy(teardown_deadline=9.0))
    try:
        lifecycle.run_command(_noop(), teardown_deadline=2.0)  # how remote completion pins 2.0 s
        lifecycle.run_command(_noop())
    finally:
        reset_binding(binding)
    assert recorded == [2.0, 9.0]


@pytest.mark.asyncio
async def test_a_lone_open_context_records_its_environment_derived_deadline(
    monkeypatch, lab_without_repos
):
    recorded: list = []
    real = inv.acquire_boundary_waiting

    async def spy(loop, *, deadline):
        recorded.append(deadline)
        return await real(loop, deadline=deadline)

    monkeypatch.setattr("otto.invocation.acquire_boundary_waiting", spy)
    monkeypatch.setenv("OTTO_TEARDOWN_DEADLINE", "7.5")
    async with open_context(lab=lab_without_repos):
        pass
    assert recorded == [7.5]


@pytest.mark.asyncio
async def test_sweep_loop_inside_open_context_is_refused(lab_without_repos):
    async with open_context(lab=lab_without_repos) as ctx:
        DuckHost("h").claim(asyncio.get_running_loop())
        with pytest.raises(RuntimeError, match="cleanup boundary holds it"):
            await ctx.sweep_loop(asyncio.get_running_loop(), label="t")


async def _enter_and_use(lab, host, admitted: asyncio.Event, go: asyncio.Event):
    async with open_context(lab=lab):
        admitted.set()
        await go.wait()  # the test looks at the host before this context reuses it
        host._claim_loop()
        return host._owner_loop, host._generation


@pytest.mark.asyncio
async def test_open_context_on_a_draining_loop_waits_then_reconnects_what_the_drain_closed(
    lab_without_repos,
):
    loop = asyncio.get_running_loop()
    blocker = DuckHost("blocker", block=True)
    blocker.claim(loop)
    reused = LocalHost()
    reused._claim_loop()  # generation 1
    drain = asyncio.ensure_future(inv.sweep_unheld(loop, label="t", deadline=5))
    await blocker.entered.wait()
    admitted, go = asyncio.Event(), asyncio.Event()
    entering = asyncio.ensure_future(_enter_and_use(lab_without_repos, reused, admitted, go))
    registry = inv._REGISTRIES[loop]  # the waiter queue is the handshake

    async def queued() -> None:
        while not (registry.waiters or admitted.is_set() or entering.done()):  # noqa: ASYNC110
            await asyncio.sleep(0)  # the queue is a plain list, with no event to wait on

    try:
        # Bounded, so a red never hangs; longer than the drain's 5 s deadline, so a
        # slow drain fails on the admission assertion, not on this bound.
        await asyncio.wait_for(queued(), 10)
        if entering.done():
            entering.result()  # an entry refused outright surfaces its own error
        assert not admitted.is_set()  # queued on the drain, not admitted past it
    except BaseException:
        blocker.gate.set()  # lets everything finish, so a red never hangs the teardown
        go.set()
        raise
    blocker.gate.set()
    await drain
    await asyncio.wait_for(admitted.wait(), 1)
    # closed by the drain, not reused yet
    assert reused._owner_loop is None
    assert reused._generation == 1
    go.set()
    owner, generation = await entering
    # its next use reconnected it, on this loop
    assert owner is loop
    assert generation == 2


@pytest.mark.asyncio
async def test_a_close_that_enters_open_context_mid_sweep_is_refused(
    lab_without_repos, monkeypatch
):  # B6 wiring
    from otto import bootstrap as bs
    from otto.context import try_get_context

    loop = asyncio.get_running_loop()
    refusals: list[str] = []
    bootstraps: list[None] = []
    real_bootstrap = bs.bootstrap

    def counting_bootstrap():
        bootstraps.append(None)
        return real_bootstrap()

    monkeypatch.setattr(bs, "bootstrap", counting_bootstrap)

    async def enter_a_context():
        try:
            async with open_context(lab=lab_without_repos):
                pass
        except RuntimeError as exc:
            refusals.append(str(exc))

    DuckHost("h", on_close=enter_a_context).claim(loop)
    closed = await asyncio.wait_for(inv.sweep_unheld(loop, label="t", deadline=1), 3)
    assert closed == ["h"]  # the sweep still completed within its deadline
    assert len(refusals) == 1
    assert "inside a host sweep" in refusals[0]
    assert bootstraps == []  # refused at entry: no bootstrap, lab build or reservation gate
    assert try_get_context() is None


def test_a_forced_command_leaves_nothing_that_trips_the_next():  # Review Focus 4
    ctrl = _controller()
    background: list = []
    stuck: dict = {}

    async def second_signal_when_the_close_starts():
        await stuck["duck"].entered.wait()
        ctrl._on_signal(signal.SIGINT)

    async def body():
        duck = DuckHost("stuck", block=True)
        duck.claim(asyncio.get_running_loop())
        stuck["duck"] = duck
        background.append(asyncio.ensure_future(second_signal_when_the_close_starts()))
        asyncio.get_running_loop().call_soon(ctrl._on_signal, signal.SIGINT)
        await asyncio.Event().wait()

    with pytest.raises(SystemExit):
        lifecycle.run_command(body(), _controller=ctrl)
    assert ctrl.forced is True
    # asyncio.run cancelled the drain's body as the forced loop closed: the drain cut
    # short and abandoned the stuck host on the way out (R-C1), so the backstop finds nothing
    assert stuck["duck"].abandoned == [1]
    assert inv.abandon_closed_loops() == []

    seen: dict = {}

    async def next_command():
        duck = DuckHost("next")
        duck.claim(asyncio.get_running_loop())
        seen["duck"] = duck

    lifecycle.run_command(next_command(), _controller=_controller())  # nothing raises
    assert seen["duck"].closes_finished == 1


def test_a_runner_shutdown_sweeps_with_no_context_active():
    from types import SimpleNamespace

    from otto.context import try_get_context
    from otto.suite.loops import sweep_runner_loop

    assert try_get_context() is None
    loop = asyncio.new_event_loop()
    try:
        runner = SimpleNamespace(get_loop=lambda: loop, run=loop.run_until_complete)
        boundary = inv.acquire_boundary(loop, deadline=5)
        duck = DuckHost("d")
        duck.claim(loop)
        sweep_runner_loop(runner, "the probe's loop", boundary)
        assert duck.closes_finished == 1
        with pytest.raises(RuntimeError, match="shut"):
            inv.acquire_boundary(loop, deadline=5)
    finally:
        loop.close()
    # The shut registry stays until its loop closes; the next registration prunes it.
    fresh = asyncio.new_event_loop()
    try:
        DuckHost("next").claim(fresh)
        assert loop not in inv._REGISTRIES
    finally:
        fresh.close()
        inv.forget_closed_loops()


@pytest.mark.parametrize("entry", ["acquire_boundary", "shut_down"])
def test_hostless_runners_closed_in_turn_keep_no_earlier_registry(entry):
    """No host ever registers, yet each runner's shut registry goes once its loop closes.

    The next runner's acquisition (or a shutdown alone) prunes it, so a session of
    short-scoped runner loops keeps at most the latest closed one.
    """
    from types import SimpleNamespace

    from otto.suite.loops import sweep_runner_loop

    loops: list[asyncio.AbstractEventLoop] = []
    try:
        for n in range(3):
            loop = asyncio.new_event_loop()
            loops.append(loop)
            runner = SimpleNamespace(get_loop=lambda loop=loop: loop, run=loop.run_until_complete)
            if entry == "acquire_boundary":
                boundary = inv.acquire_boundary(loop, deadline=5)
                assert [lp for lp in loops[:-1] if lp in inv._REGISTRIES] == []
                sweep_runner_loop(runner, f"runner {n}", boundary)
            else:
                runner.run(inv.shut_down(loop, label=f"runner {n}", deadline=5))
                assert [lp for lp in loops[:-1] if lp in inv._REGISTRIES] == []
            assert inv._REGISTRIES[loop].state == "shut"
            loop.close()
        assert [lp for lp in loops if lp in inv._REGISTRIES] == [loops[-1]]
    finally:
        for loop in loops:
            loop.close()
        inv.forget_closed_loops()

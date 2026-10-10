"""Every teardown-deadline reader reads the run's policy; preparation fills it from discovery."""

import asyncio

import pytest

from otto import bootstrap as bs
from otto import lifecycle
from otto.invocation import RunPolicy, install_policy, reset_binding


@pytest.fixture(autouse=True)
def _fresh_bootstrap():
    bs.invalidate()
    yield
    bs.invalidate()


class _Recorder(lifecycle._CommandRun):
    seen: "list[float]" = []  # noqa: RUF012 — reset per test by the recorder fixture

    def __init__(self, *, teardown_deadline, install_handlers=True):
        _Recorder.seen.append(teardown_deadline)
        super().__init__(teardown_deadline=teardown_deadline, install_handlers=False)


async def _nothing():
    return None


@pytest.fixture
def recorder(monkeypatch):
    _Recorder.seen = []
    monkeypatch.setattr("otto.lifecycle._CommandRun", _Recorder)
    return _Recorder.seen


def test_run_command_reads_the_installed_policy(recorder):
    binding = install_policy(RunPolicy(teardown_deadline=3.0))
    try:
        lifecycle.run_command(_nothing())
    finally:
        reset_binding(binding)
    assert recorder == [3.0]


def test_with_no_policy_the_deadline_is_ten_seconds_whatever_the_environment(recorder, monkeypatch):
    monkeypatch.setenv("OTTO_TEARDOWN_DEADLINE", "7.5")
    lifecycle.run_command(_nothing())
    assert recorder == [10.0]


def test_an_explicit_deadline_still_wins(recorder):
    binding = install_policy(RunPolicy(teardown_deadline=3.0))
    try:
        lifecycle.run_command(_nothing(), teardown_deadline=2.0)
    finally:
        reset_binding(binding)
    assert recorder == [2.0]


def test_sync_phase_reads_the_installed_policy():
    binding = install_policy(RunPolicy(teardown_deadline=3.0))
    try:
        with lifecycle.sync_phase(what="probe", install_handlers=False) as guard:
            assert guard._bound == 3.0
    finally:
        reset_binding(binding)


@pytest.mark.asyncio
async def test_compensate_reads_the_installed_policy(caplog):
    hung = asyncio.Event()  # never set

    async def rollback() -> None:
        await hung.wait()

    binding = install_policy(RunPolicy(teardown_deadline=0.0))
    try:
        task = asyncio.ensure_future(lifecycle.compensate(rollback(), what="hung rollback"))
        await asyncio.sleep(0)  # rollback parked
        task.cancel()  # holds the cancel and arms the policy's 0 s deadline
        # Bounded: the policy's 0 s fires on the next loop turn; 10 s or the
        # environment's value would leave the task pending here.
        done, _ = await asyncio.wait({task}, timeout=5.0)
        assert done == {task}
        with pytest.raises(asyncio.CancelledError):
            task.result()
    finally:
        hung.set()
        reset_binding(binding)
    assert any("hung rollback" in r.message for r in caplog.records)


def test_preparation_reads_the_deadline_through_the_env_settings(monkeypatch):
    class _Env:
        teardown_deadline = 3.5

    monkeypatch.setattr("otto.bootstrap.get_env", _Env)
    assert bs.discovered_teardown_deadline() == 3.5


def test_preparation_reads_no_deadline_when_discovery_is_unavailable(monkeypatch):
    def _boom():
        raise FileNotFoundError("no OTTO_SUT_DIRS")

    monkeypatch.setattr("otto.bootstrap.get_env", _boom)
    assert bs.discovered_teardown_deadline() is None


def test_discovery_fills_the_prepared_deadline(monkeypatch):
    from otto.context import fill_teardown_deadline, prepared_policy

    monkeypatch.setenv("OTTO_TEARDOWN_DEADLINE", "7.5")
    assert bs.discovered_teardown_deadline() == 7.5
    policy = prepared_policy()
    assert policy.teardown_deadline == 10.0  # nothing read before the fill
    fill_teardown_deadline(policy)
    assert policy.teardown_deadline == 7.5


def test_failed_discovery_leaves_ten_seconds_even_under_an_ambient_policy(monkeypatch, tmp_path):
    from otto.context import fill_teardown_deadline, prepared_policy

    monkeypatch.setenv("OTTO_SUT_DIRS", str(tmp_path / "missing"))
    ambient = install_policy(RunPolicy(teardown_deadline=3.0))  # an outer run's deadline
    try:
        assert bs.discovered_teardown_deadline() is None
        policy = prepared_policy()
        fill_teardown_deadline(policy)
        assert policy.teardown_deadline == 10.0  # never the ambient 3.0
    finally:
        reset_binding(ambient)


@pytest.mark.asyncio
async def test_a_nested_open_context_with_failed_discovery_runs_under_ten_seconds(monkeypatch):
    from otto import context
    from otto.config.lab import Lab
    from otto.invocation import current_policy

    monkeypatch.setattr("otto.bootstrap.discovered_teardown_deadline", lambda: None)
    bs._reset()
    monkeypatch.setattr(bs, "_result", bs.BootstrapResult(env=None, repos=[]))  # type: ignore[arg-type]
    ambient = install_policy(RunPolicy(teardown_deadline=3.0))
    try:
        async with context.open_context(lab=Lab(name="rig")):
            assert current_policy().teardown_deadline == 10.0
    finally:
        reset_binding(ambient)


def test_library_run_tests_honours_the_environment(monkeypatch, tmp_path):
    from otto.suite.run import _session_context

    monkeypatch.setenv("OTTO_TEARDOWN_DEADLINE", "7.5")
    with _session_context(tmp_path) as ctx:
        assert ctx.policy.teardown_deadline == 7.5

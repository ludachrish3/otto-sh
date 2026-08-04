"""otto test's pytest sessions run under the sync-phase interrupt policy.

The fake ``pytest.main`` delivers a REAL SIGINT to this process; the guard
installed by ``_guarded_pytest_session`` intercepts it, so the signal tests
exercise genuine handler installation and delivery in-process. They request
``real_sync_phase`` (root conftest): the autouse guard otherwise forces an
inert ``sync_phase`` for every test — a real install/restore cycle disarms
the worker's chained SIGINT faulthandler — and the fixture's teardown
re-arms that chain after the genuine cycle these tests need.
"""

import os
import signal
import threading
import time

import pytest

from otto.suite.run import run_suite


class _LibSuite:
    pass


@pytest.fixture(autouse=True)
def _no_repos(monkeypatch):
    import otto.config

    monkeypatch.setattr(otto.config, "get_repos", list)


@pytest.fixture
def post_cov_sentinel(monkeypatch):
    import otto.suite.run as run_mod

    calls: dict = {}

    async def fake_post(*_a, **_k):
        calls["ran"] = True

    monkeypatch.setattr(run_mod, "_post_run_coverage", fake_post)
    return calls


def test_graceful_interrupt_exits_130_and_skips_post_coverage(
    tmp_path, monkeypatch, post_cov_sentinel, real_sync_phase
):
    """pytest catches the KI itself (rc INTERRUPTED); we still exit 128+SIGINT."""

    def fake_main(_args, **_kw):
        try:
            # Positive control: the guard's handler must be live RIGHT NOW —
            # otherwise this kill would terminate the test process.
            assert signal.getsignal(signal.SIGINT) is not signal.default_int_handler
            os.kill(os.getpid(), signal.SIGINT)
            time.sleep(5)
        except KeyboardInterrupt:
            return pytest.ExitCode.INTERRUPTED
        pytest.fail("SIGINT was not delivered into the session")

    monkeypatch.setattr("pytest.main", fake_main)
    with pytest.raises(SystemExit) as excinfo:
        run_suite(_LibSuite, output_dir=tmp_path)
    assert excinfo.value.code == 130
    assert "ran" not in post_cov_sentinel, "interrupt means STOP: no post-coverage"


def test_hard_abort_exits_130(tmp_path, monkeypatch, post_cov_sentinel, real_sync_phase):
    """KI escaping pytest itself (mid-collection abort) still maps to 130."""

    def fake_main(_args, **_kw):
        os.kill(os.getpid(), signal.SIGINT)
        time.sleep(5)
        pytest.fail("SIGINT was not delivered into the session")

    monkeypatch.setattr("pytest.main", fake_main)
    with pytest.raises(SystemExit) as excinfo:
        run_suite(_LibSuite, output_dir=tmp_path)
    assert excinfo.value.code == 130
    assert "ran" not in post_cov_sentinel


def test_undisturbed_session_unchanged(tmp_path, monkeypatch, post_cov_sentinel):
    """No signal: same result contract as before the guard, post-coverage runs.

    Deliberately does NOT opt into real handlers: the wiring must behave
    identically through an inert guard (which is also what every other test
    in the suite gets from the root conftest's autouse patch).
    """
    monkeypatch.setattr("pytest.main", lambda *_a, **_k: pytest.ExitCode.OK)
    result = run_suite(_LibSuite, output_dir=tmp_path)
    assert result.exit_code == 0
    assert post_cov_sentinel.get("ran") is True


def test_off_main_thread_runs_unguarded(tmp_path, monkeypatch, post_cov_sentinel, real_sync_phase):
    """Library callers on a worker thread degrade to an unguarded session.

    Mirrors the async policy (``add_signal_handler`` refusal is swallowed):
    no RuntimeError, the same result contract, post-coverage still runs.

    ``real_sync_phase`` is load-bearing for the assertion, not a formality:
    the autouse conftest patch DISCARDS the caller's ``install_handlers``,
    so under it a broken thread-dispatch in ``_guarded_pytest_session``
    would still pass. With the real function, a wrongly-computed ``True``
    off the main thread raises ``RuntimeError`` and fails this test.
    """
    monkeypatch.setattr("pytest.main", lambda *_a, **_k: pytest.ExitCode.OK)
    holder = {}

    def call():
        try:
            holder["result"] = run_suite(_LibSuite, output_dir=tmp_path)
        except BaseException as exc:  # noqa: BLE001 — the failure surface under test
            holder["error"] = exc

    t = threading.Thread(target=call)
    t.start()
    t.join(timeout=60)
    assert not t.is_alive(), "library call must not hang off the main thread"
    assert "error" not in holder, f"unexpected: {holder.get('error')!r}"
    assert holder["result"].exit_code == 0
    assert post_cov_sentinel.get("ran") is True

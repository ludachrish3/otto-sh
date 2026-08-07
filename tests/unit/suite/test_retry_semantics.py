"""G2 pins: the semantics of ``@pytest.mark.retry(n)`` (otto.suite._retry).

Each test materializes a tiny pytest tree in ``tmp_path`` — its conftest
registers the shared :func:`otto.suite._retry.retry_hookwrapper` exactly the
way the two real sites do — and runs it as a subprocess with its own rootdir,
so no repo ini or plugin state leaks in. The inner ini mirrors the dev
suite's ``timeout_func_only = true`` because that setting is what makes
pytest-timeout's alarm *cancelled* by the time the retry loop runs — the
shape under which the old implementation retried hanging tests unbounded.

Proven red. Against verbatim copies of the pre-wave implementations (old
``tests/conftest.py`` hookwrapper, old ``OttoPlugin`` plain hookimpl):

- evidence pin: old wrapper passes the test but leaves no JUnit property and
  no summary — a silently erased failure;
- fresh-timeout pin: old wrapper sleeps the full hang (unbounded attempt);
- body-count pin: old plain hookimpl lets pytest's default runner re-run the
  body after a successful retry (extra side-effecting run);
- pytest.fail pin: old wrapper's ``except Exception`` misses ``Failed`` and
  aborts the retry loop mid-flight.

Against the first cut of the shared implementation (interim review):

- keyword-timeout pin: ``timeout(timeout=1)`` read as "no budget" — 25s hang
  passed (finding 1); the ``_effective_timeout`` keyword case likewise;
- wiring pins: red against main's plain-hookimpl plugin by construction
  (finding 4 — the subprocess pins alone stay green if the real sites
  regress, because their inner conftest builds a simulated site).

By mutation of the final implementation: skip-evidence (Skipped branch
deleted → evidence gone), protocol-timer restore (restore deleted → 25s
teardown), thread guard (guard deleted → arming ValueError becomes the
attempt's failure).
"""

import ast
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from textwrap import dedent

import pytest

_REPO = Path(__file__).resolve().parents[3]

# filterwarnings=error makes an exception ESCAPING the retry wrapper loud
# (pluggy's PluggyTeardownRaisedWarning) — the dev suite runs that strict,
# so the pins must too (interim review, finding 3).
_INNER_INI = dedent(
    """\
    [pytest]
    timeout_method = signal
    timeout_func_only = true
    asyncio_default_fixture_loop_scope = function
    filterwarnings = error
    markers =
        retry(n): retry the test body up to n times
    """
)

# The same minus func_only: pytest-timeout's PROTOCOL-scoped default, which
# arms in pytest_runtest_protocol and is therefore still ticking during the
# retry loop — the shape under which the outer-timer save/restore runs.
_PROTOCOL_INI = _INNER_INI.replace("timeout_func_only = true\n", "")

# The real registration shape, both sites at once: a module-level conftest
# hookwrapper (tests/conftest.py's site) plus a registered plugin object
# (OttoPlugin's site). Running both in every pin keeps the
# double-registration invariant continuously exercised, not a special case.
_INNER_CONFTEST = dedent(
    '''\
    import pytest

    from otto.suite._retry import report_retries, retry_hookwrapper


    class _PluginSite:
        """Stands in for otto.suite.plugin.OttoPlugin's registration."""

        @pytest.hookimpl(hookwrapper=True)
        def pytest_runtest_call(self, item):
            yield from retry_hookwrapper(item)


    def pytest_configure(config):
        config.pluginmanager.register(_PluginSite(), "otto-plugin-site-sim")


    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_call(item):
        yield from retry_hookwrapper(item)


    def pytest_terminal_summary(terminalreporter):
        report_retries(terminalreporter)
    '''
)

# Counts real executions of the test body across attempts — retry claims to
# re-run the body, so the pins assert on this, not on pytest's summary line.
_COUNTER_PRELUDE = dedent(
    """\
    import pathlib

    import pytest

    _COUNTER = pathlib.Path(__file__).with_name("attempts.count")


    def _attempt() -> int:
        runs = int(_COUNTER.read_text()) + 1 if _COUNTER.exists() else 1
        _COUNTER.write_text(str(runs))
        return runs
    """
)


def _run_inner(tmp_path: Path, test_body: str, *, ini: str = _INNER_INI, junit: bool = False):
    """Run the materialized tree; returns (CompletedProcess, elapsed_seconds)."""
    (tmp_path / "pytest.ini").write_text(ini)
    (tmp_path / "conftest.py").write_text(_INNER_CONFTEST)
    (tmp_path / "test_inner.py").write_text(_COUNTER_PRELUDE + dedent(test_body))
    argv = [
        sys.executable,
        "-m",
        "pytest",
        "test_inner.py",
        "-q",
        "--no-cov",
        "-p",
        "no:cacheprovider",
        # The tach pytest plugin installs a C-level SIGINT handler at import;
        # nothing in this venue needs it (see tests/conftest.py's stub).
        "-p",
        "no:tach",
    ]
    if junit:
        argv.append("--junit-xml=junit.xml")
    # An ambient PYTEST_ADDOPTS (tests/e2e/_otto_subprocess.py sets one for
    # its children) must not reshape the inner session.
    env = {k: v for k, v in os.environ.items() if k != "PYTEST_ADDOPTS"}
    started = time.monotonic()
    result = subprocess.run(
        argv,
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    return result, time.monotonic() - started


def _body_runs(tmp_path: Path) -> int:
    return int((tmp_path / "attempts.count").read_text())


def test_a_pass_after_retries_is_recorded_not_erased(tmp_path):
    """Two failed attempts before a pass must leave evidence in JUnit XML
    (``retry_attempts`` travels through xdist as a user property) and in the
    terminal summary — the old wrapper's ``force_result(None)`` reported a
    clean pass indistinguishable from a healthy test."""
    result, _ = _run_inner(
        tmp_path,
        """\

        @pytest.mark.retry(3)
        def test_flaky():
            assert _attempt() >= 3
        """,
        junit=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed" in result.stdout
    assert _body_runs(tmp_path) == 3
    junit = (tmp_path / "junit.xml").read_text()
    assert '<property name="retry_attempts" value="2"' in junit, junit
    assert "retried tests (1)" in result.stdout, result.stdout
    assert "test_flaky: 3 attempts" in result.stdout


def test_a_retried_attempt_gets_a_fresh_timeout(tmp_path):
    """With ``timeout_func_only = true`` (the dev suite's config),
    pytest-timeout's alarm is already cancelled when the retry loop runs; the
    old implementation then ran retried attempts with no bound at all — this
    tree's hang ran its full 25 seconds and *passed*. The shared
    implementation re-arms the marker's budget per attempt."""
    result, elapsed = _run_inner(
        tmp_path,
        """\
        import time


        @pytest.mark.retry(2)
        @pytest.mark.timeout(1)
        def test_hangs_on_retry():
            if _attempt() == 1:
                raise ValueError("fail fast so the retry path runs")
            time.sleep(25)
        """,
    )
    assert result.returncode != 0, "a hanging retried attempt must fail, not pass"
    assert "1 failed" in result.stdout
    assert "RetryAttemptTimeoutError" in result.stdout, result.stdout
    assert elapsed < 15, f"retried attempt was not bounded by its timeout ({elapsed:.1f}s)"


def test_the_timeout_markers_keyword_form_also_bounds_retries(tmp_path):
    """pytest-timeout honors ``@pytest.mark.timeout(timeout=1)`` — the keyword
    form — so the retry re-arm must read ``marker.kwargs`` too. The first cut
    read only ``marker.args`` and a keyword-marked hanging retry ran unbounded
    (interim review, finding 1: 20s hang PASSED)."""
    result, elapsed = _run_inner(
        tmp_path,
        """\
        import time


        @pytest.mark.retry(2)
        @pytest.mark.timeout(timeout=1)
        def test_hangs_on_retry():
            if _attempt() == 1:
                raise ValueError("fail fast so the retry path runs")
            time.sleep(25)
        """,
    )
    assert result.returncode != 0, "a hanging retried attempt must fail, not pass"
    assert "RetryAttemptTimeoutError" in result.stdout, result.stdout
    assert elapsed < 15, f"keyword-form timeout was not re-armed ({elapsed:.1f}s)"


def test_the_retry_markers_keyword_form_works(tmp_path):
    """``@pytest.mark.retry(n=3)`` must retry — the first cut read only
    ``marker.args``, so the keyword form of otto's OWN marker silently did
    nothing (verification review, NEW-3: the finding-1 shape applied to the
    marker the wave owns)."""
    result, _ = _run_inner(
        tmp_path,
        """\

        @pytest.mark.retry(n=3)
        def test_flaky_keyword_marker():
            assert _attempt() >= 3
        """,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed" in result.stdout
    assert _body_runs(tmp_path) == 3


def test_an_unarmable_timeout_never_becomes_the_attempts_failure(tmp_path):
    """A budget ``setitimer`` cannot represent (``timeout(1e300)`` — the
    plausible "disable it" idiom) must fall through to an unarmed retried
    attempt, not surface ``OverflowError`` as the attempt's failure with the
    body never run (verification review, NEW-2).

    pytest-timeout has the same arming bug in its own wrapper: it eats
    attempt 1 *before the body runs* (that is why the first attempt needs no
    injected failure here, and why ``_body_runs`` is 1 — the body's only
    execution is the retried attempt). That defect is theirs; this pin holds
    ours: the re-arm must not repeat it, so the retried attempt runs the
    body and the test PASSES."""
    result, _ = _run_inner(
        tmp_path,
        """\

        @pytest.mark.retry(2)
        @pytest.mark.timeout(1e300)
        def test_passes_despite_absurd_budget():
            _attempt()
        """,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed" in result.stdout
    assert _body_runs(tmp_path) == 1


def test_an_xfail_on_the_first_attempt_is_not_retried(tmp_path):
    """``pytest.xfail()`` is an outcome decision: the body runs once, the
    item reports xfailed, and no rerun evidence is fabricated (verification
    review, NEW-1 — the first-failure half of the decision set)."""
    result, _ = _run_inner(
        tmp_path,
        """\

        @pytest.mark.retry(3)
        def test_deliberate_xfail():
            _attempt()
            pytest.xfail("known-broken path; a retry would just run it again")
        """,
        junit=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 xfailed" in result.stdout
    assert _body_runs(tmp_path) == 1
    assert "retry_attempts" not in (tmp_path / "junit.xml").read_text()


def test_a_swallowing_body_cannot_eat_its_own_timeout(tmp_path):
    """The alarm must cross a body's ``except Exception:`` — the standard
    shape of exactly the flaky-network tests retry exists for. The first cut
    raised the public ``RetryAttemptTimeoutError`` (an ``Exception``)
    straight from the signal handler, so a swallowing body converted its
    timeout into a pass — the same ``BaseException`` reasoning the module
    docstring applies to ``pytest.fail``, missed for our own signal (fable
    final review, finding 2). The in-flight signal is now
    ``BaseException``-derived and converted to the public error only in the
    retry loop."""
    result, elapsed = _run_inner(
        tmp_path,
        """\
        import time


        @pytest.mark.retry(2)
        @pytest.mark.timeout(1)
        def test_swallows_everything():
            if _attempt() == 1:
                raise ValueError("fail fast so the retry path runs")
            try:
                time.sleep(25)
            except Exception:
                pass  # swallow and carry on — the classic flaky-network body
        """,
    )
    assert result.returncode != 0, "a swallowed alarm must not become a pass"
    assert "1 failed" in result.stdout
    assert "RetryAttemptTimeoutError" in result.stdout, result.stdout
    assert elapsed < 15, f"swallowing body ran past its budget ({elapsed:.1f}s)"


def test_double_registration_cannot_multiply_attempts(tmp_path):
    """Both real sites are registered in this tree (module wrapper + plugin
    object). ``retry(3)`` on an always-failing body must run it exactly three
    times — nested wrappers pass through, and no default-runner re-run
    sneaks in an extra execution."""
    result, _ = _run_inner(
        tmp_path,
        """\

        @pytest.mark.retry(3)
        def test_always_fails():
            raise ValueError(f"attempt {_attempt()} fails")
        """,
    )
    assert result.returncode != 0
    assert "1 failed" in result.stdout
    assert _body_runs(tmp_path) == 3


def test_pytest_fail_retries_like_any_failure(tmp_path):
    """``pytest.fail()`` raises ``Failed`` (a ``BaseException``); the old
    wrapper's ``except Exception`` let it escape mid-loop, aborting the
    remaining attempts. A test that fails twice via ``pytest.fail`` and then
    passes must simply pass."""
    result, _ = _run_inner(
        tmp_path,
        """\

        @pytest.mark.retry(3)
        def test_flaky_via_fail():
            if _attempt() < 3:
                pytest.fail("flaky by explicit fail()")
        """,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed" in result.stdout
    assert _body_runs(tmp_path) == 3


def test_an_async_test_body_can_be_retried(tmp_path):
    """Every real retry user in this repo is a pytest-asyncio test —
    ``item.runtest()`` must re-enter the async machinery (the plugin's sync
    wrapper and its function-scoped loop), not merely re-call a sync body."""
    result, _ = _run_inner(
        tmp_path,
        """\

        @pytest.mark.asyncio
        @pytest.mark.retry(3)
        async def test_flaky_async():
            assert _attempt() >= 3
        """,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed" in result.stdout
    assert _body_runs(tmp_path) == 3


def test_a_skip_on_a_retried_attempt_ends_the_loop(tmp_path):
    """A retried attempt that skips is a decision, not a failure: the loop
    stops, the item reports skipped, and the retried attempt still leaves
    evidence. The evidence half is what makes this pin falsifiable: deleting
    the wrapper's Skipped branch also reports "1 skipped" (the exception
    escapes and pytest converts it), but the escape path records nothing —
    the erasure failure mode again (interim review, finding 3)."""
    result, _ = _run_inner(
        tmp_path,
        """\

        @pytest.mark.retry(3)
        def test_skips_on_retry():
            if _attempt() == 1:
                raise ValueError("first attempt fails")
            pytest.skip("environment gone; do not keep retrying")
        """,
        junit=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 skipped" in result.stdout
    assert _body_runs(tmp_path) == 2
    junit = (tmp_path / "junit.xml").read_text()
    assert '<property name="retry_attempts" value="1"' in junit, junit
    assert "retried tests (1)" in result.stdout, result.stdout


def test_a_retry_does_not_disarm_the_protocol_timer_for_teardown(tmp_path):
    """With pytest-timeout's protocol-scoped default (no ``func_only``), the
    outer timer is live during the retry loop; the per-attempt re-arm must
    SUSPEND and RESTORE it, not clobber it — otherwise a retried test's
    teardown runs with no timeout at all. Here the retried test passes and
    its fixture's teardown hangs: the restored protocol timer must kill the
    hang at ~3s (this tree's 25s sleep running to completion is the clobber
    regression, killed by the elapsed bound)."""
    result, elapsed = _run_inner(
        tmp_path,
        """\
        import time


        @pytest.fixture()
        def hanging_teardown():
            yield
            time.sleep(25)


        @pytest.mark.retry(2)
        @pytest.mark.timeout(3)
        def test_flaky_with_slow_teardown(hanging_teardown):
            if _attempt() == 1:
                raise ValueError("fail fast so the retry path runs")
        """,
        ini=_PROTOCOL_INI,
    )
    assert result.returncode != 0, "the hanging teardown must be killed by the restored timer"
    assert "Timeout" in result.stdout, result.stdout
    assert elapsed < 15, f"protocol timer was not restored after a retry ({elapsed:.1f}s)"
    # The retry must actually have happened — with the loop disabled, the
    # untouched protocol timer would kill the teardown all the same
    # (verification review, NEW-5).
    assert _body_runs(tmp_path) == 2
    # And the kill must come from pytest-timeout's RESTORED handler, not a
    # leaked retry handler firing on the restored timer (NEW-4 — this is
    # the only venue where a leaked handler is observable: everywhere else
    # pytest-timeout reinstalls its own handler on the next arm).
    assert "RetryAttemptTimeoutError" not in result.stdout, result.stdout


def test_otto_plugin_wires_retry_as_a_hookwrapper():
    """The real ``otto test`` site: the motivating defect was OttoPlugin's
    plain hookimpl (pytest's default runner re-ran the body after a
    successful retry). The subprocess pins exercise a SIMULATED site, so
    this pin holds the real one: wrapper registration, delegation to the
    shared implementation, and the terminal-summary wiring (interim review,
    finding 4)."""
    from otto.suite.plugin import OttoPlugin

    opts = getattr(OttoPlugin.pytest_runtest_call, "pytest_impl", None) or {}
    assert opts.get("hookwrapper") is True, (
        "OttoPlugin.pytest_runtest_call must be a hookwrapper — a plain "
        "hookimpl runs alongside pytest's default runner and double-runs "
        "the body after a successful retry"
    )
    assert "retry_hookwrapper" in OttoPlugin.pytest_runtest_call.__code__.co_names
    assert "report_retries" in OttoPlugin.pytest_terminal_summary.__code__.co_names


def test_the_dev_conftest_wires_retry_as_a_hookwrapper():
    """The real bare-``pytest`` site, checked structurally (importing the
    root conftest outside a pytest session has side effects)."""
    tree = ast.parse((_REPO / "tests" / "conftest.py").read_text())
    hooks = {
        node.name: node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name in ("pytest_runtest_call", "pytest_terminal_summary")
    }
    call_hook = hooks.get("pytest_runtest_call")
    assert call_hook is not None, "tests/conftest.py lost its pytest_runtest_call retry site"
    decorators = [ast.unparse(d) for d in call_hook.decorator_list]
    assert any("hookwrapper=True" in d for d in decorators), decorators
    delegations = [
        node
        for node in ast.walk(call_hook)
        if isinstance(node, ast.YieldFrom)
        and isinstance(node.value, ast.Call)
        and getattr(node.value.func, "id", None) == "retry_hookwrapper"
    ]
    assert delegations, "the conftest hook must `yield from retry_hookwrapper(item)`"
    summary_hook = hooks.get("pytest_terminal_summary")
    assert summary_hook is not None
    summary_calls = [
        node
        for node in ast.walk(summary_hook)
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "report_retries"
    ]
    assert summary_calls, "the conftest terminal summary must call report_retries"


class _StubConfig:
    def __init__(self, ini):
        self._ini = ini

    def getini(self, name):
        if self._ini is None:
            raise ValueError(f"unknown configuration value: {name!r}")
        return self._ini


class _StubMarker:
    def __init__(self, args=(), kwargs=None):
        self.args = tuple(args)
        self.kwargs = dict(kwargs or {})


class _StubItem:
    """Duck-typed stand-in: exactly the item surface _effective_timeout reads."""

    def __init__(self, marker=None, ini=None):
        self._marker = marker
        self.config = _StubConfig(ini)

    def get_closest_marker(self, name):
        return self._marker if name == "timeout" else None


@pytest.mark.parametrize(
    ("marker", "ini", "expected"),
    [
        (_StubMarker(args=(5,)), None, 5.0),
        (_StubMarker(kwargs={"timeout": 3}), None, 3.0),  # review finding 1
        (_StubMarker(kwargs={"func_only": True, "timeout": "2.5"}), None, 2.5),
        (_StubMarker(kwargs={"method": "signal"}), "7", 7.0),  # budgetless marker → ini
        (None, "7", 7.0),
        (None, None, None),
        (None, "not-a-number", None),
        (_StubMarker(args=(0,)), None, None),
        (_StubMarker(args=(-1,)), None, None),
    ],
    ids=[
        "positional",
        "keyword",
        "keyword-string-among-others",
        "budgetless-marker-falls-to-ini",
        "ini-only",
        "nothing",
        "garbage-ini",
        "zero",
        "negative",
    ],
)
def test_effective_timeout_reads_both_marker_forms_and_the_ini(marker, ini, expected):
    from otto.suite._retry import _effective_timeout

    assert _effective_timeout(_StubItem(marker, ini)) == expected


def test_attempt_alarm_is_inert_off_the_main_thread():
    """``run_suite`` as a library call from a worker thread is a supported
    mode; ``signal.signal`` raises ValueError there. The alarm must skip
    arming — an arming error surfacing as the attempt's failure both hides
    the real result and makes the rerun evidence lie (interim review,
    finding 2)."""
    from otto.suite._retry import _attempt_alarm

    item = _StubItem(_StubMarker(args=(1,)), None)
    ran, errors = [], []

    def _body():
        try:
            with _attempt_alarm(item):
                ran.append(True)
        except BaseException as exc:  # noqa: BLE001 — the pin's whole point is that nothing may escape
            errors.append(exc)

    worker = threading.Thread(target=_body)
    worker.start()
    worker.join(timeout=10)
    assert not worker.is_alive()
    assert ran == [True]
    assert errors == [], f"arming leaked into the attempt off-main-thread: {errors!r}"

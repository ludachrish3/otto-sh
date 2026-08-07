"""The e2e resource-marker rule fails the offender — it must not crash the controller.

``tests/e2e/conftest.py`` enforces the exactly-one-primary-resource-marker rule.
It originally raised ``pytest.UsageError`` from ``pytest_collection_modifyitems``,
which under this repo's default ``-n auto`` is an xdist **controller crash**, not
a clean failure: any exception from a hook that fires after a worker's
``pytest_sessionstart`` — which every collection-time hook does — kills the
controller with an ``INTERNALERROR`` (``AssertionError`` / ``RuntimeError`` in
``xdist/dsession.py``; the empirical write-up lives with the web-dist guard in
``tests/e2e/monitor/dashboard/conftest.py``). One mistagged test file and the
whole session's diagnosis is a distributed-session traceback instead of the
rule's own message.

The enforcement is therefore *deferred*: collection stamps each violation on the
offending item (and re-appends offenders that ``-m``/``-k`` filtering removed),
and the tryfirst ``pytest_runtest_setup`` hook fails that item at setup with the
violation text. These tests run a real nested pytest session (subprocess, so the
outer session's config cannot leak in — see
``reference: in-process Config inherits otto's addopts``) against a copy of the
REAL ``tests/e2e/conftest.py`` and pin three legs of the contract:

1. Under ``-n 2``, a mistagged test FAILS BY NAME with the rule's message while
   its well-tagged neighbour passes — and the session shows no INTERNALERROR.
   This test, run against the pre-fix conftest, is the standing reproduction of
   the controller crash (it was proven red exactly that way).
2. ``--collect-only`` exits clean: a run precondition must not fire when
   nothing runs — the doctrine from issue #196, where ``--collect-only``
   tripped the browser build gate.
3. A marker expression cannot deselect an offender into silence — the
   collection wrapper re-appends filtered offenders, so the stamp travels
   with the item onto every lane that runs anything.

The conftest is copied verbatim at runtime (not re-implemented) so the pin
follows the real hook through refactors. The probe directory is deliberately
NOT named ``tests/`` — ``python -m pytest`` puts the subprocess cwd on
``sys.path``, and a ``tests/`` namespace package there would shadow the real
``tests`` package that the copied conftest imports ``_ambient_env`` from.
"""

from pathlib import Path

import pytest

from tests._fixtures.paths import PROJECT_ROOT, TESTS_ROOT

pytestmark = pytest.mark.hostless

pytest_plugins = ["pytester"]

REAL_E2E_CONFTEST = TESTS_ROOT / "e2e" / "conftest.py"

PROBE_SRC = """\
import pytest


@pytest.mark.hostless
def test_well_tagged():
    assert True


def test_mistagged():
    assert True
"""

# Registers the marker axes the copied conftest works with, nothing else: the
# probe session must stay inert (no addopts, no plugins beyond what the args
# pass) so the behaviour under test is the conftest's, not this repo's config.
PROBE_INI = """\
[pytest]
markers =
    hostless: probe
    integration: probe
    embedded: probe
    e2e: probe
    browser: probe
    hops: probe
"""

# `-p no:tach`: in a venv that carries the lint group, tach's pytest11 plugin
# auto-loads and its Rust Ctrl-C handler panics on repeated in-tree sessions
# (issue #193) — same guard the repo addopts and the e2e child env use.
INNER_ARGS = ("-p", "no:tach", "-p", "no:cacheprovider")


@pytest.fixture
def probe_tree(pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A throwaway pytest rootdir holding the real e2e conftest + one mistagged test."""
    # The inner session must not inherit the outer one's env-based config: no
    # repo addopts, no live pytest-cov subprocess hooks writing into the outer
    # datafile, no stale xdist worker identity.
    monkeypatch.delenv("PYTEST_ADDOPTS", raising=False)
    for var in (
        "COV_CORE_SOURCE",
        "COV_CORE_CONFIG",
        "COV_CORE_DATAFILE",
        "COV_CORE_CONTEXT",
        "PYTEST_XDIST_WORKER",
        "PYTEST_XDIST_WORKER_COUNT",
        "PYTEST_XDIST_TESTRUNUID",
    ):
        monkeypatch.delenv(var, raising=False)
    # The copied conftest does `from tests._ambient_env import ambient`; the
    # subprocess resolves that against the real repo root.
    monkeypatch.setenv("PYTHONPATH", str(PROJECT_ROOT))

    pytester.makeini(PROBE_INI)
    probe = pytester.path / "e2eprobe"
    probe.mkdir()
    (probe / "conftest.py").write_text(REAL_E2E_CONFTEST.read_text())
    (probe / "test_probe.py").write_text(PROBE_SRC)
    return probe


def test_mistagged_offender_fails_itself_not_the_controller(
    pytester: pytest.Pytester, probe_tree: Path
) -> None:
    """Under xdist, a marker-rule violation is the offender's own failure."""
    result = pytester.runpytest_subprocess(str(probe_tree), "-n", "2", *INNER_ARGS, timeout=180)
    combined = str(result.stdout) + str(result.stderr)
    assert "INTERNALERROR" not in combined, (
        "the marker rule crashed the xdist controller instead of failing the "
        f"offending test:\n{combined}"
    )
    result.assert_outcomes(passed=1, errors=1)
    assert "test_mistagged" in combined
    assert "resource-marker rule" in combined
    assert "need exactly one of" in combined


def test_collect_only_is_clean_when_nothing_runs(
    pytester: pytest.Pytester, probe_tree: Path
) -> None:
    """#196 doctrine: a run precondition must not fire when nothing runs."""
    result = pytester.runpytest_subprocess(
        str(probe_tree), "--collect-only", *INNER_ARGS, timeout=180
    )
    combined = str(result.stdout) + str(result.stderr)
    assert result.ret == 0, f"--collect-only failed on a mistagged item:\n{combined}"
    # Not vacuous: prove the copied conftest actually collected the probe —
    # a broken copy (import failure, wrong root) would also exit 0.
    assert "test_mistagged" in combined
    assert "2 tests collected" in combined


# The hostless CI lane's marker expression (noxfile tests_hostless / Makefile
# M_HOSTLESS). Copied literally: this test pins that the lane CANNOT deselect
# an offender into silence, so it must break if either side changes shape.
M_HOSTLESS = "not integration and not embedded and not stability and not browser"

DUAL_PROBE_SRC = """\
import pytest


@pytest.mark.hostless
@pytest.mark.integration
def test_double_tagged():
    assert True
"""


def test_marker_expression_cannot_hide_an_offender(
    pytester: pytest.Pytester, probe_tree: Path
) -> None:
    """A deselected offender is re-appended and still fails on the lane.

    The trap this pins: a test mistagged ``hostless`` + ``integration`` is
    DESELECTED by the hostless lane's ``-m`` expression — the only lane CI
    runs — so a purely run-time report would never fire there and the mistag
    would ship green forever. Collection therefore re-appends stamped
    offenders after the marker expression has done its filtering: an offender
    anywhere in tests/e2e fails every session, whatever the ``-m``.
    """
    (probe_tree / "test_probe_dual.py").write_text(DUAL_PROBE_SRC)
    result = pytester.runpytest_subprocess(
        str(probe_tree), "-m", M_HOSTLESS, *INNER_ARGS, timeout=180
    )
    combined = str(result.stdout) + str(result.stderr)
    # well_tagged passes; the unmarked offender runs and errors; the
    # double-tagged offender is deselected by -m yet still errors.
    result.assert_outcomes(passed=1, errors=2)
    assert "test_double_tagged" in combined
    assert "resource-marker rule" in combined

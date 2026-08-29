"""Root test conftest — shared fixtures across every test tree.

WHERE A FIXTURE BELONGS
-----------------------
A guard that protects **process-global state** belongs HERE, never in a package
conftest. The trees are not separate processes: ``tests_hostless`` runs
``tests/unit`` and ``tests/e2e`` in one pytest session (``make coverage`` adds
``tests/integration``), and one xdist worker runs tests from several trees in
one process. So a guard parked in ``tests/unit/conftest.py`` leaves every other
tree exposed to a hazard that is not remotely unit-specific.

This is not hypothetical — it is the single most repeated defect in this suite:

* #132 — the hermetic web dist lived in ``tests/unit/monitor/conftest.py``, so
  tests booting a ``MonitorServer`` from ``tests/unit/suite`` and ``tests/e2e/cli``
  demanded a real ``make web`` build that CI never produces. Green locally
  (every checkout has a dist), red in CI, by construction.
* #133 — the issue-#110 CliRunner shield lived in ``tests/unit/conftest.py``, so
  ``tests/e2e/cli`` drove the same runner unprotected and died on the same
  "I/O operation on closed file" the shield exists to prevent.

The rule, then:

* State owned by the PROCESS (global registries, the ``otto`` logger, the
  OttoContext ContextVar, click's captured streams, the built web dist,
  ``sys.modules`` identity, the collection tree itself) → root conftest. Every
  tree gets it.
* Setup owned by a RESOURCE or a local technique (docker stacks, the lab, a
  Playwright page, a package's own ``sys.modules`` delitem trick) → that
  package's conftest.

If a guard must NOT apply somewhere (the Playwright lane serves the REAL dist,
so it must never be handed the hermetic marker), express that as an explicit
opt-out where the exception lives — an opt-in fixture, or a same-named override
in that package's conftest — never by narrowing the guard's home.

``tests/e2e/cli/test_registry_isolation_e2e.py`` pins this: it asserts the
process-global guards are actually active in the e2e tree, and fails if one is
moved back into a package conftest.
"""

# ---------------------------------------------------------------------------
# xdist dispatch front-loading (Phase-3 spike: KEEP decision)
#
# ``LoadGroupScheduling`` builds its workqueue by iterating the canonical
# collected list in order (``OrderedDict`` insertion order = dispatch order).
# Sorting heavy serial groups to the front guarantees those groups are
# dispatched to workers *before* the unit-test bulk begins, so slow tests
# (docker-up/down/build, zephyr fanout) run in parallel with unit tests
# rather than after them.
#
# Spike findings: docs/superpowers/specs/2026-06-23-frontload-spike-findings.md
# Median wall improvement: ~79.67s → 73.53s (6.14s, 0% overlap across 6 runs).
#
# Hook execution ordering: pytest fires ``pytest_collection_modifyitems`` LIFO
# (deeper conftest files registered first → run first). The embedded-grouping
# hook in ``tests/integration/host/conftest.py:150`` runs *before* this root
# hook, stamping xdist_group markers onto embedded test items first. This root
# hook then sees all markers fully applied — the ordering is correct by
# construction.
# ---------------------------------------------------------------------------

_FRONTLOAD_GROUPS: frozenset[str] = frozenset(
    {"zephyr37_llext", "docker_e2e", "coverage_e2e", "zephyr_fanout"}
)


def _frontload_key(group: "str | None") -> int:
    """Return 0 for heavy xdist groups (dispatch first) and 1 for all others.

    Pure helper — no pytest dependency — so it can be imported and tested
    directly in ``tests/unit/test_frontload_ordering.py`` without spinning up
    a VM.
    """
    return 0 if group in _FRONTLOAD_GROUPS else 1


def pytest_collection_modifyitems(config, items) -> None:  # type: ignore[no-untyped-def]
    """Sort heavy xdist_group items to the front of the collected list.

    Uses ``list.sort`` (stable) so relative order within each tier (heavy vs
    light) is preserved — non-heavy items stay in their original collection
    order relative to each other.

    Runs *after* the deeper ``tests/integration/host/conftest.py`` hook that
    stamps embedded xdist_group markers, so all markers are applied before the
    reorder (LIFO conftest registration guarantees this).

    NOTE: this ROOT hook cannot stamp xdist_group markers of its own — the
    root conftest registers at config load, so under LIFO it runs *after*
    pytest-xdist's worker plugin has already read the markers and annotated
    the test ids with their ``@group`` suffixes (a stamp landing here is
    silently invisible to the loadgroup scheduler). Group-stamping policies
    live in deeper conftests, which register during collection and therefore
    run before xdist's annotation — see tests/e2e/conftest.py's browser-suite
    grouping policy and tests/integration/host/conftest.py's per-device
    embedded groups.
    """

    def _group_of(item):
        m = item.get_closest_marker("xdist_group")
        return m.args[0] if (m and m.args) else None

    items.sort(key=lambda it: _frontload_key(_group_of(it)))


import os

# Disable colored CLI output before typer/click/rich are imported anywhere.
# CI runners (e.g. GitHub Actions) set FORCE_COLOR, which causes Rich to embed
# ANSI escapes in help/error text and breaks substring assertions like
# `'--flag' in result.output`.
os.environ["NO_COLOR"] = "1"
os.environ["TERM"] = "dumb"
for _var in ("FORCE_COLOR", "CLICOLOR_FORCE", "PY_COLORS", "CLICOLOR"):
    os.environ.pop(_var, None)

# Hermetic otto env: ambient otto configuration must never leak into the
# suite. A developer shell with OTTO_SUT_DIRS exported (e.g. pointing at
# another checkout's tests/repo1) makes every ambient-env bootstrap() in a
# CLI test register that repo's suites under foreign file paths, which later
# collide with the real tests/repo1 imports in test_repo.py's bootstrap test
# ("test suite ... is already registered", xdist worker-order dependent).
# Strip everything OTTO_-prefixed at import time — this runs in the
# controller and every xdist worker before any test code. Tests that need
# otto env set their own values (monkeypatch / explicit subprocess env
# dicts), which happens after this and is unaffected.
#
# Harness opt-ins — knobs a Makefile target, a CI job, or a developer sets to
# steer the harness rather than otto — are exempt, and are declared ONCE in
# tests/_ambient_env.py alongside what each one drives. That module is the
# allowlist; do not restate it here. A second copy is what let issue #192
# through: the copy agreed with itself while the strip was missing an entry,
# so nightly's `OTTO_CHAOS_DOCKER=loopback` job silently ran against the bed
# host instead. Pinned by tests/unit/test_env_hermeticity.py.
from tests._ambient_env import AMBIENT_OPT_INS, ambient

for _var in [k for k in os.environ if k.startswith("OTTO_") and k not in AMBIENT_OPT_INS]:
    os.environ.pop(_var, None)

import asyncio
import atexit
import contextlib
import dataclasses
import errno
import gc
import ipaddress
import logging
import logging.handlers
import sys
import types
import weakref
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
import pytest_asyncio

from otto.config.lab import Lab
from otto.context import OttoContext, reset_context, set_context
from otto.host.factory import create_host_from_dict
from otto.host.local_host import LocalHost
from otto.host.login_proxy import Cred
from otto.host.remote_host import make_host_id
from otto.host.unix_host import UnixHost
from otto.registry import Registry
from otto.suite._retry import report_retries, retry_hookwrapper
from tests._fixtures import _conftest_rebind
from tests._fixtures._coverage_preinit import (
    PREINIT_OUTCOME,
    PreinitOutcome,
    active_pytest_cov,
    force_coverage_schema_init,
    preinit_failure_message,
)
from tests._fixtures._loop_reaper import classify_loop_origin, reap_or_raise
from tests._fixtures._transport_leaks import (
    describe_referrers,
    install_transport_tracker,
    scan_leaked_transports,
)

# ---------------------------------------------------------------------------
# tach pytest-plugin stub (process-global sys.modules identity → root conftest)
#
# tach ships a `pytest11` entry-point plugin whose Rust extension installs a
# C-level Ctrl-C handler (ctrlc crate) at import and PANICS (MultipleHandlers)
# if the module is ever re-imported in one process (issue #193). The harness
# addopts' `-p no:tach` blocks it for the OUTER run, but pytester's
# `runpytest_inprocess` sessions parse their own isolated rootdir config (no
# addopts) and pytester's SysModulesSnapshot evicts the module between tests —
# so in any venv that carries the `lint` dependency group, the second pytester
# test would re-import the extension and panic. Pre-seeding an empty stub at
# conftest import makes every nested `import tach.pytest_plugin` resolve to a
# hookless module: no ctrlc handler, no re-init, and the stub predates every
# pytester snapshot so restores keep it. Harmless when tach isn't installed.
# ---------------------------------------------------------------------------
if "tach.pytest_plugin" not in sys.modules:
    sys.modules["tach.pytest_plugin"] = types.ModuleType("tach.pytest_plugin")


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item: pytest.Item):
    """Implement ``@pytest.mark.retry(n)`` for dev pytest runs.

    Provides the marker under bare ``pytest`` — ``otto.suite.plugin.OttoPlugin``
    only registers under ``otto test``. All retry semantics (per-attempt
    timeout re-arm, JUnit/terminal rerun evidence, double-registration
    safety) live in :func:`otto.suite._retry.retry_hookwrapper`, pinned by
    ``tests/unit/suite/test_retry_semantics.py``.
    """
    yield from retry_hookwrapper(item)


def pytest_configure(config):  # type: ignore[no-untyped-def]
    _install_sigint_traceback_dump()
    _install_loop_origin_tracker()
    install_transport_tracker(lambda: _current_test)
    # Registered as a PLUGIN, never re-exported as a conftest hook:
    # pytest_collectstart is dispatched through a PATH-FILTERED proxy that
    # strips conftest hookimpls for non-anchor directories — a conftest-hosted
    # copy of this hook never fires for exactly the duplicate Directory nodes
    # it must repair (see the module docstring; interim-review find).
    if not config.pluginmanager.has_plugin("otto-conftest-rebind"):
        config.pluginmanager.register(_conftest_rebind, name="otto-conftest-rebind")


def pytest_collection_finish(session):  # type: ignore[no-untyped-def]
    """Pre-initialize this worker's coverage SQLite schema, single-threaded.

    Runs once per process after collection and before the first test — the last
    moment when only the main thread exists. Closes coverage's
    ``no such table: context`` schema-init race, an intermittent, whole-worker
    failure that has repeatedly aborted ``make release`` at ``make nox`` on the
    newest-Python ``tests_all`` leg (see
    :mod:`tests._fixtures._coverage_preinit` for the mechanism). A no-op when
    pytest-cov has not started coverage in this process (bare ``pytest``,
    ``--no-cov``, or a distributed run's in-process controller) — which is also
    where no per-test context writes happen, so there is no race to close.

    The outcome is stashed so ``test_coverage_schema_preinit`` can prove the
    hook actually ran and armed (a "schema exists" check alone can't — coverage
    would lazily build the same schema by the first test regardless), and so
    ``_coverage_preinit_failure_is_loud`` below can ACT on a failure: a raised
    pre-init used to become ``False`` and nothing read it, silently re-opening
    the race in exactly the ``make release`` runs it exists for (review §5.4).
    The hook itself must stay exception-free — an exception from a
    post-sessionstart hook under xdist is an INTERNALERROR that crashes the
    controller blaming an innocent item (Wave 12).

    Lives in the ROOT conftest per the process-global-state rule: the per-worker
    coverage data file is owned by the process, so every test tree measured
    under ``--cov`` needs the guard, not just one.
    """
    cov = active_pytest_cov(session.config)
    if cov is None:
        outcome = PreinitOutcome(armed=False)
    else:
        outcome = PreinitOutcome(armed=True, error=force_coverage_schema_init(cov))
    session.config.stash[PREINIT_OUTCOME] = outcome


@pytest.fixture(scope="session", autouse=True)
def _coverage_preinit_failure_is_loud(request):
    """Fail this worker's tests, by name, when the coverage pre-init failed.

    Deferred to the first test's setup rather than raised in the collection
    hook (xdist-safe: a hook exception is a controller INTERNALERROR blaming
    an innocent item — Wave 12), and fixture-based so it cannot fire when
    nothing runs (``--collect-only``, the #196 lesson). Session-scoped: the
    first test errors with the recorded traceback and every later test on the
    worker reuses that error, so the whole worker is loudly invalid instead
    of silently racing ``no such table: context`` (warn-vs-error house
    ruling: FAIL LOUD). The decision half is
    :func:`tests._fixtures._coverage_preinit.preinit_failure_message`, whose
    truth table is unit-tested.
    """
    message = preinit_failure_message(request.config.stash.get(PREINIT_OUTCOME, None))
    if message is not None:
        pytest.fail(message, pytrace=False)


def _install_sigint_traceback_dump() -> None:
    """Dump every thread's stack on the first Ctrl-C, then fall through to
    pytest's normal interrupt handling so the JUnit report is still emitted.

    ``pytest-timeout`` (configured in ``pyproject.toml``: 180s, signal method)
    already covers *hung* tests — it fails the test and lets the session reach
    sessionfinish. This covers the third case: *you* decide to bail early.
    Without it, a Ctrl-C while a worker is wedged in a blocking C call gives no
    diagnostics and, under xdist, often no report.

    ``chain=True`` runs faulthandler's C-level dump and then the previous
    SIGINT handler (CPython's, which raises ``KeyboardInterrupt``), so pytest
    still unwinds to ``pytest_sessionfinish`` and the junitxml plugin writes
    its file. Registered in the controller and every xdist worker (conftest is
    imported in each), so the dump shows the worker actually stuck, not just
    the controller. Stacks go to the real stderr fd.
    """
    import faulthandler
    import signal
    import sys

    if not hasattr(faulthandler, "register"):  # not available on Windows
        return
    # Unregister-first makes this a true RE-ARM: `register` on an
    # already-registered signal only updates options — it does NOT re-install
    # the C-level handler after a `signal.signal` cycle clobbered it (proven
    # empirically: a bare re-register left the dump disarmed; unregister
    # clears the enabled flag so register re-saves and re-installs, and the
    # chain still fires). `real_sync_phase`'s teardown depends on this.
    # Unregistering a never-registered signal returns False, no error.
    faulthandler.unregister(signal.SIGINT)
    faulthandler.register(
        signal.SIGINT,
        file=sys.stderr,
        all_threads=True,
        chain=True,
    )


def pytest_unconfigure(config):  # type: ignore[no-untyped-def]
    import faulthandler
    import signal

    if hasattr(faulthandler, "unregister"):
        faulthandler.unregister(signal.SIGINT)


# ---------------------------------------------------------------------------
# Orphaned-event-loop reaper (always on — including CI)
#
# Closes leaked pytest-asyncio (harness) function loops at each test boundary
# so their unclosed-loop ``ResourceWarning`` never gets gc-finalized inside an
# unrelated later test and escalated by ``filterwarnings=["error"]`` into a
# flaky, misattributed ``ExceptionGroup`` failure (the usual scapegoat is a
# Hypothesis ``@given`` test, whose ``register_random`` calls ``gc.collect()``).
#
# A loop created by ``otto/`` product code is NEVER closed here — it is
# reported via :class:`LeakedProductLoopError`, so a genuine product resource
# leak surfaces loudly with attribution instead of being masked. Product code
# only ever creates loops via ``asyncio.run()`` (which always closes them), so
# such a loop never sits open at a boundary today; the raise is a regression
# guard. See ``tests/_loop_reaper.py`` for the full rationale and evidence.
#
# Loops owned by a still-live *wider-than-function* pytest-asyncio runner
# (``loop_scope`` of class/module/package/session) are ALSO never closed here:
# they are open-but-idle between tests by design and pytest-asyncio closes them
# itself at scope end. Reaping one mid-scope closes the loop out from under the
# next test in that scope, which then dies with ``RuntimeError: Event loop is
# closed`` (and orphans its coroutines into later unrelated tests). The reaper
# only ever targets *leaked function* loops, so ``_live_scoped_runner_loops``
# excludes the wider-scoped runner loops from the reap set.
# ---------------------------------------------------------------------------

# loop -> (origin, creating-test nodeid). Weak keys: dead loops drop out on
# their own and loop-id reuse can't produce a stale lookup.
_LOOP_INFO: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, tuple[str, str]]" = (
    weakref.WeakKeyDictionary()
)
_current_test = "(session setup/collection)"
_loops_reaped = 0
_tracker_installed = False


def _frame_filenames(frame):
    """Yield ``co_filename`` for ``frame`` and every caller above it.

    Cheap (no line formatting); ``classify_loop_origin`` short-circuits on the
    first ``otto/`` frame, so the full walk only happens for harness loops.
    """
    while frame is not None:
        yield frame.f_code.co_filename
        frame = frame.f_back


def _install_loop_origin_tracker() -> None:
    """Tag every event loop with its origin at creation time.

    Wraps ``BaseEventLoop.__init__`` — the single chokepoint every asyncio loop
    passes through — to record whether the loop was built by ``otto/`` product
    code or by the harness, plus the test running when it was created.
    Test-only; runs in the controller and every xdist worker (this conftest is
    imported in each).
    """
    global _tracker_installed  # noqa: PLW0603 — module-level singleton/cache
    if _tracker_installed:
        return
    from asyncio import base_events

    orig_init = base_events.BaseEventLoop.__init__

    def _tracking_init(self, *args, **kwargs):
        orig_init(self, *args, **kwargs)
        origin = classify_loop_origin(_frame_filenames(sys._getframe(1)))
        with contextlib.suppress(
            TypeError
        ):  # not weak-referenceable (shouldn't happen for real loops)
            _LOOP_INFO[self] = (origin, _current_test)

    base_events.BaseEventLoop.__init__ = _tracking_init
    _tracker_installed = True


def pytest_runtest_setup(item):  # type: ignore[no-untyped-def]
    global _current_test  # noqa: PLW0603 — module-level singleton/cache
    _current_test = item.nodeid
    # serial_timing tests reject the slow (deadline) path by elapsed time, and
    # sibling xdist workers loading the machine can counterfeit that path as a
    # false red (three loaded-gate sightings). Excluding the marker from a
    # parallel lane is easy to forget when adding a lane, so enforce it where
    # the process-global state lives: a marked test inside a worker is a
    # deterministic failure, never a load-dependent flake. The paired `-n0`
    # legs (Makefile / noxfile; pinned by tests/unit/test_lane_invariants.py)
    # re-append what the parallel legs exclude, so nothing goes CI-invisible.
    if item.get_closest_marker("serial_timing") and os.environ.get("PYTEST_XDIST_WORKER"):
        pytest.fail(
            "serial_timing test collected into an xdist worker — sibling-worker "
            "load can counterfeit its slow arm as a false red. This lane must "
            "exclude `-m serial_timing` from its parallel run and re-append it "
            "in a dedicated -n0 leg (see the marker's pyproject entry).",
            pytrace=False,
        )


# pytest-asyncio backs each non-function ``loop_scope`` with a fixture named
# ``_{scope}_scoped_runner`` whose value is an ``asyncio.Runner`` holding the
# scope's persistent loop. The function runner is intentionally omitted — its
# leaked loops are exactly what the reaper exists to close.
_SCOPED_RUNNER_FIXTURES = (
    "_class_scoped_runner",
    "_module_scoped_runner",
    "_package_scoped_runner",
    "_session_scoped_runner",
)


def _live_scoped_runner_loops(item) -> set:
    """Loops owned by a still-live wider-than-function pytest-asyncio runner.

    pytest-asyncio requests the runner fixture dynamically inside ``runtest``
    (``request.getfixturevalue``), so it is not in the item's fixture closure;
    look each runner fixturedef up in the session-wide registry and read its
    cached value. A fixturedef with a live ``cached_result`` means its scope
    has not ended, so its loop must be left alone. Defensive throughout: a
    pytest/pytest-asyncio internals change degrades to "reap as before" rather
    than crashing teardown.
    """
    owned: set = set()
    fm = getattr(item.session, "_fixturemanager", None)
    registry = getattr(fm, "_arg2fixturedefs", None) or {}
    for name in _SCOPED_RUNNER_FIXTURES:
        for fixturedef in registry.get(name, ()):
            cached = getattr(fixturedef, "cached_result", None)
            if not cached:  # never set up, or already finalized -> scope ended
                continue
            with contextlib.suppress(Exception):
                owned.add(cached[0].get_loop())
    return owned


@pytest.hookimpl(wrapper=True)
def pytest_runtest_teardown(item):
    """After the test and all its fixtures finalize, reap orphaned harness
    loops. Raises :class:`LeakedProductLoopError` if a product loop leaked.

    Loops still owned by a live wider-than-function runner are excluded so the
    reaper never closes a class/module/package/session loop out from under the
    next test in that scope (see :func:`_live_scoped_runner_loops`).
    """
    result = yield
    global _loops_reaped  # noqa: PLW0603 — module-level singleton/cache

    def origin_of(loop):
        info = _LOOP_INFO.get(loop)
        return info[0] if info else "harness"

    def describe(loop):
        info = _LOOP_INFO.get(loop)
        return f"{loop!r} (created during {info[1] if info else '?'})"

    owned = _live_scoped_runner_loops(item)
    # A product (otto/-created) loop must ALWAYS reach reap_or_raise so a real
    # leak still raises LeakedProductLoopError — never let the scoped-runner
    # exclusion swallow one. ``owned`` only ever holds harness runner loops, so
    # this guard is belt-and-suspenders, but it keeps the "product leaks are
    # never masked" invariant local and self-evident.
    reapable = [loop for loop in _LOOP_INFO if loop not in owned or origin_of(loop) == "product"]
    _loops_reaped += reap_or_raise(reapable, origin_of, describe=describe)
    # After the reap, so transports bound to a just-reaped function loop are
    # flagged at this very boundary instead of one test later.
    _report_leaked_transports(item)
    return result


def pytest_terminal_summary(terminalreporter):  # type: ignore[no-untyped-def]
    report_retries(terminalreporter)
    if _loops_reaped:
        terminalreporter.write_line(
            f"loop-reaper: closed {_loops_reaped} orphaned pytest-asyncio event "
            "loop(s) at test boundaries (harness teardown race; see "
            "tests/_loop_reaper.py)"
        )


# ---------------------------------------------------------------------------
# Asyncio transport-leak detector (diagnostic; reporting gated by
# OTTO_DETECT_ASYNCIO_LEAKS=1, e.g. `make release` / stability targets)
# ---------------------------------------------------------------------------


def _report_leaked_transports(item) -> None:  # type: ignore[no-untyped-def]
    """Attribute leaked asyncio transports at the test boundary.

    A transport left open on a closed loop fires ``ResourceWarning`` from
    ``__del__`` at GC time and is escalated by pytest's ``[unraisable]``
    plugin into a ``PytestUnraisableExceptionWarning`` on whichever *later*
    test happens to be running — the source of the xdist-flake symptom.
    :mod:`tests._fixtures._transport_leaks` records every transport at
    creation (originally a per-test whole-heap gc scan, replaced 2026-07-25
    after it measured at 2.3-3.4x suite CPU), so this check costs nothing
    when no transport leaked. Print rather than raise: we want to *attribute*
    the leak, not fail the test that detected it.
    """
    if not ambient("OTTO_DETECT_ASYNCIO_LEAKS"):
        return
    leaks = scan_leaked_transports()
    if not leaks:
        return
    print(  # noqa: T201 — test diagnostic output
        f"\nLEAK after {item.nodeid}: {len(leaks)} live transport(s) bound to closed loop:"
    )
    for transport, description in leaks:
        print(  # noqa: T201 — test diagnostic output
            f"  {description}\n    referrers: {describe_referrers(transport)}"
        )
    # Flush the leaked transports' ResourceWarnings right here (rather than at
    # some later organic gc point inside an innocent test) so the unraisable
    # escalation lands on the leaking test, next to the report above. Our own
    # strong refs must go first or the collect can't finalize them. Only ever
    # runs on the leak path — the steady-state check stays gc-free.
    del transport, leaks
    gc.collect()


# ---------------------------------------------------------------------------
# active_context: test helper for installing an OttoContext in a block.
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def active_context(lab=None, **kwargs):
    """Install an OttoContext for the duration of the block (test helper)."""
    token = set_context(OttoContext(lab=lab if lab is not None else Lab(name="test"), **kwargs))
    try:
        yield
    finally:
        reset_context(token)


@pytest.fixture(autouse=True)
def _reset_otto_context():
    """Restore the OttoContext ContextVar to its pre-test value after every test.

    The main() callback (and some fixtures) call set_context(), which persists in
    the ContextVar. We snapshot the value at test start and restore it at
    teardown, so a test that sets a context can't leak into later tests run on
    the same (long-lived) xdist worker. We do NOT force the var to None during
    the test — that would wipe a module/session scoped context a fixture
    installed for the test to use (e.g. the hop integration suite's
    module-scoped lab).

    Lives in the *root* conftest so it covers the integration tree too: under
    ``make coverage`` the whole suite runs in one process and ungrouped unit
    tests can land on a worker that previously ran integration tests.
    """
    from otto.context import _active

    snapshot = _active.get()
    try:
        yield
    finally:
        _active.set(snapshot)


@pytest.fixture(autouse=True)
def _restore_otto_logger_state():
    """Snapshot-restore otto's logging-management state around every test.

    Restores the pre-test state rather than resetting to defaults (the old
    ``_reset_otto_logger_retention`` / ``management.reset()`` teardown): a
    reset-to-default destroys state that EXISTED BEFORE the test — a
    module-scoped fixture's logging setup died with its first test's teardown
    while later tests in the module silently ran against defaults (review
    §5.5). Restoring the snapshot keeps the original isolation win (retention
    / output-dir config from one test can't leak into the next — the old
    test_cov ENOTDIR flakes) because by induction every test starts from, and
    therefore hands back, the worker's pre-test state.

    What restore means here: a listener the TEST created is stopped and its
    handlers closed (its thread must not outlive the test), and the
    ``_stop_listener`` atexit hook it registered unconditionally is
    unregistered with it; other atexit hooks the test registered are
    unregistered; ``management._state`` is rebound to the snapshot copy;
    handlers/level/propagate on the ``'otto'`` logger and any test-added
    external-logger captures are put back exactly as found (membership AND
    order). Stated limits (interim review), none with a live venue today —
    each would be a new harness design, not a leak: a listener LIVE at
    snapshot time that the test itself stops cannot be resurrected (pytest
    never wires otto logging before tests); a handler that was in the
    snapshot AND got fanned into a test-created listener is closed with that
    listener yet re-attached here; test-added external-logger captures are
    detached to ``NOTSET`` (their pre-test level is not snapshotted), and a
    pre-existing captured prefix that a test re-captures through a NEW
    QueueHandler keeps that second handler. The restore is wrapped so a
    raising stop/close cannot skip the state rebind — under the old single
    ``reset()`` that skip healed next test, but a skipped SNAPSHOT restore
    would be permanent for the worker (the next test would snapshot the
    polluted state as its baseline).
    """
    from otto.logger import management

    otto_logger = logging.getLogger("otto")
    saved = dataclasses.replace(
        management._state,
        capture_prefixes=list(management._state.capture_prefixes),
        captured_prefixes=list(management._state.captured_prefixes),
    )
    saved_handlers = list(otto_logger.handlers)
    saved_level = otto_logger.level
    saved_propagate = otto_logger.propagate
    yield
    state = management._state
    try:
        # Stop and close a listener the test created; a pre-existing one
        # (none in any current venue — see docstring) is left running for
        # restore. _add_log_handlers registers the _stop_listener atexit hook
        # unconditionally with each wired listener — drop it with the
        # listener it belongs to (idempotent at exit anyway, but the registry
        # should describe reality).
        if state.listener is not None and state.listener is not saved.listener:
            if getattr(state.listener, "_thread", None) is not None:
                state.listener.stop()
            for handler in state.listener.handlers:
                handler.close()
            if saved.listener is None:
                atexit.unregister(management._stop_listener)
        # Unregister the flag-guarded atexit hook the test registered
        # (restore, don't reset: hooks registered before the test stay).
        if state.atexit_registered and not saved.atexit_registered:
            atexit.unregister(management._stop_listener)
            atexit.unregister(management._print_output_dir)
        # Detach the shared QueueHandler from external loggers captured
        # DURING the test; captures from before the test are part of the
        # snapshot and stay.
        for prefix in state.captured_prefixes:
            if prefix in saved.captured_prefixes:
                continue
            prefix_logger = logging.getLogger(prefix)
            for handler in list(prefix_logger.handlers):
                if isinstance(handler, logging.handlers.QueueHandler):
                    prefix_logger.removeHandler(handler)
            prefix_logger.setLevel(logging.NOTSET)
    finally:
        # The rebind and logger restore must happen even if a stop/close
        # above raised — see docstring: a skipped snapshot restore is
        # permanent pollution, not a one-test glitch.
        management._state = saved
        for handler in list(otto_logger.handlers):
            if handler not in saved_handlers:
                otto_logger.removeHandler(handler)
        for handler in saved_handlers:
            if handler not in otto_logger.handlers:
                otto_logger.addHandler(handler)
        if otto_logger.handlers != saved_handlers:
            # Same membership, wrong order (a test removed + re-added a
            # snapshot handler): restore order too — handler order decides
            # emit order.
            otto_logger.handlers[:] = list(saved_handlers)
        otto_logger.setLevel(saved_level)
        otto_logger.propagate = saved_propagate


@pytest.fixture(autouse=True)
def _restore_bootstrap_state():
    """Snapshot-restore ``otto.bootstrap``'s discovery/registration caches.

    ``bootstrap()`` memoizes into three module globals; discovery errors ride
    the cached ``_discovered`` :class:`~otto.bootstrap.DiscoveryResult` itself
    (the old append-only ``_discovery_errors`` global is gone). So one test
    that drives the CLI with ``OTTO_SUT_DIRS`` pointing at a scratch repo —
    ``test_init_prompts``'s epilogue tests do exactly that — records a framed
    "no .otto/settings.toml" error that outlives the ``monkeypatch.setenv``
    restoring the var. Every later test on that worker then trips
    ``fail_loud_on_bootstrap_errors()``, which exits **1** before Click can
    report the missing ``--lab``, so the ``TestArgumentValidation`` /
    ``TestLabFreeFlags`` "must exit 2" tests fail with a bare
    ``SystemExit(1)`` and no usage message. It only reproduced under xdist
    (~1 run in 3, load-dependent) because it needs the poisoning
    test and the victims to land on the same worker in that order.

    Snapshot-restore, not the old ``bootstrap._reset()`` teardown (review
    §5.5): reset-to-empty also destroyed caches that existed BEFORE the test,
    so a module-scoped fixture priming bootstrap died with its first test.
    Restoring the snapshot keeps the poisoning fix — by induction each test
    starts from the worker's pre-test caches, so what the poisoner left is
    swapped back out — while pre-test state survives.
    (``tests/unit/test_env_hermeticity.py`` re-runs the historical
    poisoner/victim pair to prove the isolation half still holds.) Stated
    limit: the restore is reference-only — a test that mutates the cached
    ``DiscoveryResult``/dict IN PLACE (instead of rebinding, as every real
    path does) leaks that mutation; the old wholesale reset would not have.
    Note (interim review): ``parsefactories`` orders same-scope autouse
    fixtures alphabetically, so the ``_reset_*`` → ``_restore_*`` rename also
    moved both restores after ``_reset_otto_context`` / ``_reset_tunnel_add_
    locks`` in setup order — verified interaction-free; nothing here may
    depend on its neighbors' ordering.

    Root conftest, not ``tests/unit/cli``: these are process-global module
    caches, and under ``make coverage`` the whole suite shares one process —
    the same #132/#133 rule that put ``_isolate_registries`` and the
    ``sys.path`` guard here.
    """
    from otto import bootstrap

    saved = (
        bootstrap._discovered,
        bootstrap._result,
        bootstrap._in_progress,
        bootstrap._completion_names,
    )
    yield
    # Direct rebinds, same access the _ADD_LOCKS guard below uses. A rename of
    # these globals fails loudly at the snapshot READ above (setup raises
    # AttributeError); this teardown setattr alone would silently mint new
    # attributes, so the read is the guard (fable's final-review find).
    (
        bootstrap._discovered,
        bootstrap._result,
        bootstrap._in_progress,
        bootstrap._completion_names,
    ) = saved


@pytest.fixture(autouse=True)
def _reset_tunnel_add_locks():
    """Clear ``otto.tunnel.manage._ADD_LOCKS`` between tests.

    ``add_tunnel`` serializes racing adds for the same tunnel id with a
    per-id ``asyncio.Lock`` cached in this module-global dict (tunnel-
    stability-suite Task 6). A lock that survives past the test that first
    contended it is a hazard across the whole supported CPython range
    (3.10-3.12+): ``Lock`` only binds to an event loop on genuinely
    *contended* acquire (the uncontended fast path never touches
    ``self._loop``), but once contended
    it is pinned to that loop forever — a later contention on the SAME id
    from a DIFFERENT event loop (a fresh loop per test, via
    ``asyncio_default_fixture_loop_scope = "function"``) raises
    ``RuntimeError: ... bound to a different event loop`` instead of the
    intended ``ValueError``. A single CI pass never re-contends the same id,
    so this only surfaces under the ``tests_unit_repeat`` nox session's
    ``--count=2 --repeat-scope=session`` single-process repeat.

    Lives in the ROOT conftest per the process-global-state rule: the dict
    is module-global in ``otto.tunnel.manage``, not local to any one test
    tree. Uses a lazy ``sys.modules.get`` check so tests that never import
    the tunnel package don't pay for (or trigger) the import.
    """
    yield
    manage = sys.modules.get("otto.tunnel.manage")
    if manage is not None:
        manage._ADD_LOCKS.clear()


@pytest.fixture(autouse=True)
def _reset_inventory_stale_warnings():
    """Clear ``otto.inventory.cache._warned_snapshots`` between tests.

    A ``SnapshotCache`` serving a snapshot because its backend is unreachable
    warns ONCE PER PROCESS, memoized by snapshot path in a module-global set
    (host-inventory spec §9.5) — one otto command resolves the inventory
    several times over, and the operator should be told once, not once per
    resolution. Left uncleared, the first test to warn for a given path
    silences every later one, and a test asserting the warning would pass or
    fail on collection ORDER.

    Lives in the ROOT conftest per the process-global-state rule: the set is
    module-global in ``otto.inventory.cache``, not local to any one test tree.
    Uses a lazy ``sys.modules.get`` check so tests that never import the
    inventory package don't pay for (or trigger) the import, and the module's
    own public ``reset_stale_warnings()`` rather than reaching into the
    private set, so the module keeps owning what "reset" means.
    """
    yield
    cache = sys.modules.get("otto.inventory.cache")
    if cache is not None:
        cache.reset_stale_warnings()


# The provider seams, as ``(module, attribute)``. Plain lists rather than
# ``otto.registry.Registry`` singletons, which is exactly why ``_isolate_registries``
# cannot see them: its discovery scans for ``Registry`` instances.
_PROVIDER_REGISTRIES = (
    ("otto.host.product", "_PRODUCT_PROVIDERS"),
    ("otto.host.dev_tool", "_DEV_TOOL_PROVIDERS"),
)


def _provider_snapshot() -> "list[tuple[str, str, list | None]]":
    """Copy each provider list, recording ``None`` when its module is not loaded.

    ``None`` is not "empty" — it is "there was nothing to snapshot", which is
    the case :func:`_restore_provider_snapshot` has to treat specially. Both
    modules define their list as ``[]`` at import, so a module the TEST
    imported has an import-time baseline of empty and nothing else.
    """
    out: "list[tuple[str, str, list | None]]" = []
    for mod_name, attr in _PROVIDER_REGISTRIES:
        mod = sys.modules.get(mod_name)
        # `getattr` with no default: a rename of either global fails loudly
        # here, at the snapshot READ, rather than silently minting a new
        # attribute in the restore below (the `_restore_bootstrap_state` rule).
        out.append((mod_name, attr, None if mod is None else list(getattr(mod, attr))))
    return out


def _restore_provider_snapshot(snapshot: "list[tuple[str, str, list | None]]") -> None:
    """Put each provider list back to what *snapshot* recorded.

    IN PLACE (``[:] =``), never a rebind: ``otto.bootstrap`` binds
    ``_PRODUCT_PROVIDERS``/``_DEV_TOOL_PROVIDERS`` by ``from … import`` at call
    time, so a rebound module attribute would leave that reader holding the
    leaked list.
    """
    for mod_name, attr, saved in snapshot:
        mod = sys.modules.get(mod_name)
        if mod is None:
            continue
        getattr(mod, attr)[:] = [] if saved is None else saved


@pytest.fixture(autouse=True)
def _restore_provider_registries():
    """Snapshot-restore the product and dev-tool PROVIDER lists around every test.

    ``register_product_provider`` / ``register_dev_tool_provider`` append to two
    module-global lists, and nothing ever unregisters. Every ``bootstrap()``
    that imports a repo init module registering one leaves it there for the rest
    of the process — and both lists are read at the single lab-ingest chokepoint
    (``create_host_from_dict``), so a leaked provider hangs products or dev tools
    on hosts in every later test that builds one. ``bootstrap`` also re-reads
    them for its D2 refusal, so a leaked provider owned by a repo NAME a later
    test happens to reuse fails that test's bootstrap over a registration it
    never made.

    ``_isolate_registries`` does not cover these: it discovers state by scanning
    loaded ``otto.*`` modules for ``otto.registry.Registry`` instances, and these
    are plain lists. Five test files carry their own local copy of this
    snapshot/restore (``tests/unit/host/test_product_providers.py``,
    ``test_dev_tool_providers.py``, ``test_factory.py``,
    ``tests/unit/project/test_composed_lab.py``, ``tests/unit/bootstrap/
    test_bootstrap.py``) — one of them says in so many words that the list is
    "a plain list the root guard cannot see". That is the tell: the state is
    process-global, so a per-file guard is scoped narrower than the hazard, and
    the next file to register a provider inherits no protection at all. Root
    conftest, per the #132/#133 rule that put ``_isolate_registries`` here.

    Lazy ``sys.modules.get``, like ``_reset_tunnel_add_locks``: a test that never
    imports the host package must not be made to. That laziness is also why the
    snapshot distinguishes "not loaded" from "loaded and empty" — a test that
    imports the module ITSELF and registers into it would otherwise leak
    everything it added, which is precisely the hole that let the first-party
    instructions escape ``_isolate_registries`` (see
    ``tests/unit/config/test_completion_cache_unit.py``).
    """
    snapshot = _provider_snapshot()
    yield
    _restore_provider_snapshot(snapshot)


@pytest.fixture(autouse=True)
def _no_real_signal_handlers():
    """Force every in-process ``run_command`` call to skip real signal handlers.

    ``run_command`` (``otto.lifecycle``) builds its own
    ``_CommandRun(install_handlers=True)`` whenever a caller doesn't pass
    ``_controller=`` explicitly — and plenty of tests reach it that way
    without knowing it: any ``CliRunner.invoke`` of a command whose Typer
    callback bottoms out in bare ``run_command(...)`` (e.g.
    tests/unit/cli/test_cov.py, tests/unit/tunnel/test_cli.py) installs REAL
    ``loop.add_signal_handler(SIGINT/SIGTERM, ...)`` for the duration of the
    call. One add/remove cycle permanently disarms this pytest worker's
    chained SIGINT faulthandler (``_install_sigint_traceback_dump`` above)
    for every test that runs afterward on it:
    ``loop.remove_signal_handler`` restores Python's plain
    ``signal.default_int_handler``, never the chain conftest installed —
    there is no way back once that happens. tests/unit/test_lifecycle.py's
    own ``_controller()`` helper documents this exact trap for the tests
    that drive ``_CommandRun`` through the explicit ``_controller=`` seam;
    this fixture closes the same hole for every test that goes through
    ``run_command``'s default path instead.

    Patches ``otto.lifecycle._CommandRun`` itself, since ``run_command``
    looks that name up as a module global at call time: a factory forcing
    ``install_handlers=False`` intercepts every DEFAULT construction. Tests
    that inject their own controller (``_controller=_CommandRun(...)``) or
    that patch ``otto.lifecycle.run_command`` wholesale (e.g.
    tests/unit/cli/test_monitor.py) are unaffected — neither path calls this
    factory.

    Real signal installation/delivery is intentionally NOT covered here —
    that is tier-2 subprocess e2e coverage (chaos plan 3), which runs in a
    fresh process this in-process monkeypatch can't reach and shouldn't need
    to.

    Imports ``otto.lifecycle`` lazily, inside the fixture body rather than at
    conftest import time, so this file's own import cost is unchanged for
    the (large) fraction of test runs that never touch a command path.

    Restores ``_CommandRun`` by hand (plain assignment in a ``finally``)
    rather than via the shared ``monkeypatch`` fixture ON PURPOSE: as a ROOT
    autouse fixture, pulling in ``monkeypatch`` here changes WHEN that shared
    instance gets built relative to other fixtures that also request it —
    e.g. tests/unit/cli/conftest.py's ``no_logger_output_dir``, an
    autouse ``with patch("otto.logger.management.create_output_dir"): yield``
    fixture. ``monkeypatch.setattr`` records "restore to the CURRENT value"
    at the point it's called; if that call lands while
    ``no_logger_output_dir``'s ``mock.patch`` is already active (order
    dependent on fixture resolution), monkeypatch captures the *mock* as the
    value to restore, not the real function. Once torn down, monkeypatch's
    generic restore can then run *after* ``no_logger_output_dir`` exits its
    own ``with patch(...)`` block, clobbering that correct restoration back
    to a stale ``MagicMock`` — corrupting ``create_output_dir`` for every
    test that runs afterward in the same process. This is exactly the kind
    of accidental cross-fixture entanglement a shared mutable fixture
    invites; a private, self-contained save/restore has no such surface.

    ``sync_phase`` (the synchronous sibling policy) opens the SAME door
    through a different frame: ``suite.run._guarded_pytest_session`` resolves
    ``otto.lifecycle.sync_phase`` lazily and installs real SIGINT/SIGTERM
    handlers by default, so any test that reaches ``run_suite``/
    ``run_selection`` in-process would disarm the chained faulthandler
    exactly like a bare ``run_command`` — hence the second patch below,
    forcing ``install_handlers=False`` (an inert guard). Tests that must
    prove REAL installation opt back in via the ``real_sync_phase`` fixture,
    which re-arms the chain in teardown.
    """
    from otto import lifecycle

    real_command_run = lifecycle._CommandRun

    def _factory(*, teardown_deadline, install_handlers=True):
        return real_command_run(teardown_deadline=teardown_deadline, install_handlers=False)

    real_sync = lifecycle.sync_phase

    def _inert_sync_phase(*, install_handlers=True, **kwargs):
        return real_sync(install_handlers=False, **kwargs)

    _inert_sync_phase._otto_real = real_sync  # real_sync_phase's road back

    lifecycle._CommandRun = _factory
    lifecycle.sync_phase = _inert_sync_phase
    try:
        yield
    finally:
        lifecycle._CommandRun = real_command_run
        lifecycle.sync_phase = real_sync


@pytest.fixture
def real_sync_phase():
    """Opt-in: real ``sync_phase`` handler installation for THIS test.

    Undoes ``_no_real_signal_handlers``' inerting patch (module attribute
    only — test modules that imported ``sync_phase`` directly already hold
    the real one) and, in teardown, re-arms this worker's chained SIGINT
    faulthandler: a real install/restore cycle replaces faulthandler's
    C-level registration with the plain Python-level restore, and only
    re-running ``_install_sigint_traceback_dump`` re-arms the chain. Request
    this fixture from ANY test that lets ``sync_phase`` install real
    handlers in-process, whichever way it reaches it.

    Yields the real ``sync_phase`` for tests that go through the lazy
    module-attribute lookup (``suite.run._guarded_pytest_session``).
    """
    from otto import lifecycle

    patched = lifecycle.sync_phase
    real = getattr(patched, "_otto_real", patched)
    lifecycle.sync_phase = real
    try:
        yield real
    finally:
        lifecycle.sync_phase = patched
        _install_sigint_traceback_dump()


# ---------------------------------------------------------------------------
# Lab-data helpers
# ---------------------------------------------------------------------------

from tests._fixtures._host_pool import lease_unix_host
from tests._fixtures.labdata import (  # noqa: F401
    flat_hosts,
    flatten_lab_doc,
    host_data,
    lab_data_path,
    lab_json_v2,
    make_host,
    write_lab_json,
)

# ---------------------------------------------------------------------------
# Parameterized host fixtures (driven by @pytest.mark.parametrize + indirect)
#
# These are shared by both unit-tree integration tests (currently in
# tests/unit/host/) and the new tests/integration/host/ tree. They yield real
# host objects backed by the Vagrant test bed — tests must be gated by
# @pytest.mark.integration (Unix) or @pytest.mark.embedded (Zephyr).
# ---------------------------------------------------------------------------

# Mapping from `host1` parametrize value -> the embedded host's lab `ne` name.
# Lets host1 construct an EmbeddedHost directly via the factory without
# special-casing each Zephyr config in the fixture body.
#
# The matrix is anchored on Zephyr 3.7 LTS, which carries the full
# {FAT-on-RAM, LittleFS, no-FS} set; 2.7 and 4.4 each contribute a single fs
# cell — five QEMU instances on the `zephyr` Vagrant VM. The trim is
# deliberate: otto's exercised device surface is only `fs read` / `fs write` /
# `fs rm` plus the command-frame retcode parse (perf/disk metrics ride SNMP,
# not the shell), and none of the per-version `fs` divergences (cp/mv, the
# `ls` size column, the `kernel thread` rename) are touched. So fat-vs-lfs is
# a version-independent EmbeddedFileSystem distinction fully covered once on
# 3.7, and a second fs cell on another version would only re-smoke that
# firmware's identical fs surface. 2.7 stays because its command frame is
# genuinely different (ZephyrInlineRetcodeFrame, inline retcode); 4.4 stays as
# a newest-LTS firmware-drift sentinel; no-FS likewise needs only one backend
# (the transfer gate short-circuits before any frame). The 3.7 ids are
# unversioned for backwards compatibility (they predate the multi-version
# matrix and are referenced by name in the unit tree); 2.7 and 4.4 carry an
# explicit version token. This dict is the single source of truth for the
# embedded backend list — the integration test files import
# :data:`EMBEDDED_BACKENDS` rather than re-listing the ids, so a new row here
# flows into every parametrized contract suite without touching the test files.
_ZEPHYR_BACKEND_NE: dict[str, str] = {
    # Zephyr 3.7 LTS — primary version: full {FAT, LittleFS, no-FS}. ids kept
    # unversioned (predate the matrix; referenced by name in the unit tree).
    "zephyr_fat": "zephyr37_fat",
    "zephyr_lfs": "zephyr37_lfs",
    "zephyr_no_fs": "zephyr37_nofs",
    # Zephyr 2.7 LTS — distinct command frame (inline retcode); one fs cell.
    "zephyr_27_fat": "zephyr27_fat",
    # Zephyr 4.4 LTS — newest-LTS firmware-drift sentinel; one fs cell.
    "zephyr_44_lfs": "zephyr44_lfs",
}

# Ordered list of embedded backend ids — imported by the integration contract
# suites so the parametrize lists stay in lockstep with the lab matrix.
EMBEDDED_BACKENDS: list[str] = list(_ZEPHYR_BACKEND_NE)

# Mapping from `host1` parametrize value -> the BusyBox bed guest's lab `ne`.
# First-party parity (spec 2026-08-20 §5): the guests ride the same
# backend machinery as the Zephyr matrix. One id per pinned milestone
# version; the ne encodes the version (bb1161 = 1.16.1).
_BUSYBOX_BACKEND_NE: dict[str, str] = {
    "busybox_1161": "bb1161",
    "busybox_1211": "bb1211",
    "busybox_1281": "bb1281",
    "busybox_1310": "bb1310",
    "busybox_1350": "bb1350",
}

# Ordered list of BusyBox bed backend ids — imported by the integration
# suites (and the xdist family grouping) exactly as EMBEDDED_BACKENDS is, so
# the guest matrix is defined here and nowhere else.
BUSYBOX_BACKENDS: list[str] = list(_BUSYBOX_BACKEND_NE)

# Ordered list of the guests' lab `ne` values — the OTHER spelling a test
# parameter can name a guest by, and the one `transfer_host` takes (a
# transfer's param says which BOX, not which host1 backend id, so the `ne`
# reads as what it is). Same order as BUSYBOX_BACKENDS, from the same dict.
BUSYBOX_GUEST_NES: list[str] = list(_BUSYBOX_BACKEND_NE.values())

# Every string by which a test parameter can name a BusyBox bed guest, in one
# place. The xdist family grouping in `tests/integration/host/conftest.py`
# searches param VALUES against this rather than against either spelling
# alone: `host1` rows arrive as backend ids (`busybox_1161`) and
# `transfer_host` rows arrive as `ne`s inside a tuple (`("shell", "bb1161")`),
# and a row that matched neither would silently leave the family group and put
# a second worker on the two TCG cores all five guests share.
BUSYBOX_PARAM_TOKENS: frozenset[str] = frozenset(BUSYBOX_BACKENDS) | frozenset(BUSYBOX_GUEST_NES)

# The one xdist group every item that drives a guest joins — the bed suite
# (`tests/integration/busybox_bed/conftest.py` stamps it by directory), the
# parametrized rows in the generic host suites
# (`tests/integration/host/conftest.py` stamps those by param value), and the
# guard that fails an item which reaches the guests outside it
# (`tests/integration/conftest.py`). ONE family group, not one per guest: the
# five guests are TCG on `test1`'s two cores, so a second worker buys no
# parallelism and takes cycles the guests already lose to emulation.
#
# Spelled here rather than in each of those three files because two spellings
# would be two groups, and two groups can run at once — which is the exact
# failure the group exists to prevent.
BUSYBOX_BED_GROUP = "busybox_bed"


def embedded_param_id(backend_id: str) -> str:
    """Descriptive test id for an embedded backend, derived from lab data.

    Returns ``"{os_name}-{os_version}-{fs}"`` so a new entry in
    ``lab_data/tech1/lab.json`` (e.g. a future Zephyr 4.x or a different
    RTOS) surfaces its identity in test output without test-code edits.
    Non-embedded backend ids pass through unchanged so the same helper can
    be used by parametrize callers that mix unix and embedded backends.
    """
    if backend_id not in _ZEPHYR_BACKEND_NE:
        return backend_id
    data = host_data(_ZEPHYR_BACKEND_NE[backend_id])
    osname = str(data.get("os_name", "embedded"))
    osver = str(data.get("os_version", ""))
    # Filesystem token from the declared `filesystem` variant — the source of
    # truth in lab data (``default_dest_dir`` is usually unset, defaulting to
    # the FS mount at construction time). Maps the lab string to a short tag.
    fs = {
        "fat-ram": "fat",
        "littlefs": "lfs",
        "none": "nofs",
    }.get(str(data.get("filesystem", "none")), str(data.get("filesystem")))
    parts = [p for p in (osname, osver, fs) if p]
    return "-".join(parts).lower().replace(" ", "")


def remote_name(worker_id: str, basename: str) -> str:
    """Namespace a remote transfer filename by the running xdist worker.

    The host-contract and stability tests transfer to fixed names under a
    shared remote dir, and ``ssh``+``telnet`` share one host (``test1:/tmp``)
    while ``local`` shares the runner's ``/tmp``. Under ``-n auto`` — and the
    ``COUNT`` soak repeats — different workers would otherwise race the same
    remote path, one worker's delete/overwrite corrupting another's get
    (surfacing as ``scp: No such file`` or ``content corrupt``). Tests run
    sequentially within a worker, so the worker id is a sufficient key. Under
    a non-xdist run ``worker_id`` is ``"master"``, which is equally fine.
    """
    return f"{worker_id}_{basename}"


@pytest_asyncio.fixture
async def host1(request):
    """Integration host parameterized by backend id.

    Accepted values:

    - ``"ssh"`` / ``"telnet"`` -> UnixHost on `test1`, with the matching term.
    - ``"local"``              -> LocalHost.
    - any id in :data:`EMBEDDED_BACKENDS` -> EmbeddedHost on the matching
      Zephyr QEMU target, built via the host factory from its lab-data
      entry. The matrix anchors the full {FAT-on-RAM, LittleFS, no-FS} set on
      3.7, with a single fs cell on 2.7 and 4.4; see :data:`_ZEPHYR_BACKEND_NE`
      for the id -> `ne` mapping and the trim rationale.
    - any id in :data:`BUSYBOX_BACKENDS` -> UnixHost on the matching BusyBox
      QEMU guest (five pinned userland versions on the ``test1`` VM, reached
      over telnet through the ``test1`` hop), built via the host factory from
      its lab-data entry; see :data:`_BUSYBOX_BACKEND_NE` for the id -> `ne`
      mapping.
    """
    backend = request.param
    if backend == "local":
        h = LocalHost()
        yield h
        await h.close()
        return
    if backend in _ZEPHYR_BACKEND_NE:
        # Embedded backends round-trip through the factory so the same lab-data
        # entry tests target as `otto host` / `EmbeddedHost(...)` users do.
        data = host_data(_ZEPHYR_BACKEND_NE[backend])
        h = create_host_from_dict(data)
        yield h
        await h.close()
        return
    if backend in _BUSYBOX_BACKEND_NE:
        # BusyBox bed guests round-trip through the factory: term/transfer
        # resolve from the entry's menus (telnet/shell), hop from test1.
        data = host_data(_BUSYBOX_BACKEND_NE[backend])
        h = create_host_from_dict(data)
        yield h
        await h.close()
        return
    # Unix terms ("ssh" / "telnet").
    kwargs: dict[str, str] = {"term": backend}
    if backend == "telnet":
        kwargs["transfer"] = "ftp"
    h = make_host("test1", **kwargs)
    yield h
    await h.close()


@pytest_asyncio.fixture
async def host2(request):
    """Integration host2, parameterized by term type ('ssh' or 'telnet')."""
    term = request.param
    kwargs: dict[str, str] = {"term": term}
    if term == "telnet":
        kwargs["transfer"] = "ftp"
    h = make_host("test2", **kwargs)
    yield h
    await h.close()


@pytest_asyncio.fixture
async def host3(request):
    """Integration host3, parameterized by term type ('ssh' or 'telnet')."""
    term = request.param
    kwargs: dict[str, str] = {"term": term}
    if term == "ssh":
        kwargs["transfer"] = "scp"
    h = make_host("test3", **kwargs)
    yield h
    await h.close()


@pytest_asyncio.fixture
async def hop_host(request):
    """Integration host reached through one or two SSH hops.

    Parameterized by ``(ne, hop_ne, term, transfer)`` tuples — e.g.
    ``("test2", "test1", "ssh", "scp")`` means "reach test2 through test1".

    For two-hop chains, *hop_ne* is the first hop and the intermediate host
    must itself have a hop configured at fixture construction time.
    """
    ne, hop_ne, term, transfer = request.param
    target_data = host_data(ne)
    hop_data = host_data(hop_ne)
    hop_id = make_host_id(hop_data["element"], None, hop_data.get("board"), None)
    h = UnixHost(
        ip=target_data["ip"],
        element=target_data["element"],
        creds=[Cred(**c) for c in target_data["creds"]],
        board=target_data.get("board"),
        is_virtual=target_data.get("is_virtual", False),
        term=term,
        transfer=transfer,
        hop=hop_id,
    )
    yield h
    await h.close()


@pytest_asyncio.fixture
async def transfer_host(request, tmp_path_factory):
    """Integration host parameterized by transfer type.

    Three param shapes:

    - a transfer name (``"scp"`` / ``"sftp"`` / ``"ftp"`` / ``"nc"``) — a host
      leased from ``UNIX_POOL`` on its default term;
    - ``(transfer, term)`` — the same lease with the term pinned
      (``("nc", "telnet")``);
    - ``(transfer, ne)`` where *ne* names a BusyBox bed guest
      (:data:`BUSYBOX_GUEST_NES`) — THAT guest, built through the host factory
      from its lab entry so term/transfer resolve from the entry's menus and
      the hop from test1, exactly as ``host1``'s busybox branch does.

    The first two lease a free host from ``UNIX_POOL`` instead of always using
    test1, so the transfer tests spread across the unix-lab peers
    (test1/test2/test3) rather than serializing on one VM.

    **A guest is never leased, and never joins the pool.** The pool's premise
    is that its members are interchangeable — a test asks for "a unix host" and
    gets whichever is free. The five guests are the opposite of interchangeable:
    each one IS a specific pinned BusyBox milestone, which is the whole point of
    the tier, and they are TCG-emulated on a two-core VM. Putting them in
    ``UNIX_POOL`` would hand an unrelated scp/sftp/ftp test a slow guest with no
    scp applet and a userland it never meant to ask about. So the busybox shape
    returns before ``lease_unix_host`` is ever entered: it takes no lease, and
    ``UNIX_POOL`` stays the three interchangeable Unix VMs it has always held.
    """
    param = request.param
    if isinstance(param, tuple) and len(param) == 2 and param[1] in BUSYBOX_GUEST_NES:
        transfer, ne = param
        h = create_host_from_dict({**host_data(ne), "transfer": transfer})
        try:
            yield h
        finally:
            await h.close()
        return
    lock_dir = tmp_path_factory.getbasetemp().parent
    with lease_unix_host(lock_dir) as element:
        if isinstance(param, tuple):
            transfer, term = param
            h = make_host(element, transfer=transfer, term=term)
        else:
            h = make_host(element, transfer=param)
        try:
            yield h
        finally:
            await h.close()


# ---------------------------------------------------------------------------
# OS-agnostic host kits — backend-appropriate command strings for the
# parametrized contract suite. There is no command both Unix and Zephyr can
# run (Zephyr has no `echo` builtin), so the contract asserts on otto
# behavior while each backend's kit supplies the actual commands.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HostKit:
    """Backend-appropriate command strings for OS-agnostic contract tests.

    A test using ``host1_kit`` should treat all fields as opaque shell
    fragments — no Unix-isms, no Zephyr-isms — and assert only on the otto
    behavior they trigger (status, retcode shape, output presence).
    """

    successful_cmd: str
    """A command that exits 0 with non-empty stdout."""

    failing_cmd: str
    """A command that produces a non-zero retcode (Status.Failed)."""

    temp_remote_dir: str | None
    """Writable directory on the target for get/put round-trips, or None
    when the target has no filesystem (graceful-degradation case)."""

    send_line_ending: str
    """Line ending the backend's shell accepts to commit a sent command.

    The Zephyr telnet shell takes ``\\r`` (the framing seam writes ``\\r``-
    separated lines); bash shells (Unix, local) take ``\\n``.
    """

    expect_in_output: str
    """A stable substring of ``successful_cmd``'s output that
    :meth:`Host.expect` can match against. Used by the send/expect
    contract case so the test stays OS-agnostic — the kit provides both
    the command and what to look for in its echo."""

    stability_iterations: int = 20
    """Number of sequential ``run`` iterations the cross-OS stability
    contract performs. Embedded backends keep this modest because the
    Zephyr telnet console is slow; unix backends can comfortably run
    higher counts."""

    stability_cycle_count: int = 10
    """Number of sequential put/get/verify/delete cycles. Set to ``0`` for
    backends without a filesystem so the cycle test self-skips."""

    stability_large_size: int = 0
    """Size in bytes for the large-file stability transfer. Embedded
    backends keep this orders of magnitude smaller than unix because the
    console transfer encodes 32 hex chars per shell invoke
    (see :mod:`otto.host.embedded_transfer`). Set to ``0`` to skip the
    large-file test on backends without a filesystem."""


_UNIX_KIT = HostKit(
    successful_cmd="echo hello",
    failing_cmd="ls /this_path_does_not_exist_otto_contract",
    temp_remote_dir="/tmp",
    send_line_ending="\n",
    expect_in_output="hello",
    stability_iterations=50,
    stability_cycle_count=20,
    stability_large_size=5 * 1024 * 1024,
)

# Zephyr has no echo builtin — pick a stock command that prints non-empty
# output and exits 0. `version` is universally available on the Zephyr
# shell and prints "Zephyr version X.Y.Z" — both the command name and
# "Zephyr" appear in the output, so either is a fine expect-fragment.
_ZEPHYR_COMMON = {
    "successful_cmd": "version",
    "failing_cmd": "bogus_otto_contract_cmd",
    "send_line_ending": "\r",
    "expect_in_output": "Zephyr",
}

# Embedded backends share these stability numbers — the slow per-invoke
# console encoding dominates wall time, so we keep iteration counts modest
# and large transfers in the tens of KiB rather than MiB.
_ZEPHYR_STABILITY = {
    "stability_iterations": 20,
    "stability_cycle_count": 10,
    "stability_large_size": 32 * 1024,
}


def _zephyr_kit(backend_id: str) -> HostKit:
    """Build the contract kit for a Zephyr backend from its lab data.

    ``temp_remote_dir`` is the on-device mount path, resolved from the host's
    declared ``filesystem`` variant via :func:`build_filesystem` — one source
    of truth for "where does this FS live on the device", shared with the
    production factory. A no-filesystem target (mount ``None``) self-skips the
    file-transfer stability cycles by zeroing their counts.

    Deriving the kit from lab data (rather than a hand-written table per
    backend) means a new Zephyr version added to :data:`_ZEPHYR_BACKEND_NE`
    and ``lab.json`` gets a correct kit for free.
    """
    from otto.host.embedded_filesystem import build_filesystem

    data = host_data(_ZEPHYR_BACKEND_NE[backend_id])
    fs = build_filesystem(data.get("filesystem", "none"))
    if fs.mount is None:
        return HostKit(
            temp_remote_dir=None,
            **_ZEPHYR_COMMON,
            stability_iterations=20,
            stability_cycle_count=0,
            stability_large_size=0,
        )
    return HostKit(temp_remote_dir=fs.mount, **_ZEPHYR_COMMON, **_ZEPHYR_STABILITY)


# BusyBox guests: same command shapes as _UNIX_KIT (ash has echo/ls and
# the contract's failing path), but soak sizes trimmed for five TCG
# guests sharing two host cores — a 5 MiB large transfer over telnet
# base64 on TCG is minutes, not seconds. 256 KiB still spans dozens of
# _SHELL_CHUNK_BYTES chunks, which is the property the soak exercises.
_BUSYBOX_KIT = HostKit(
    successful_cmd="echo hello",
    failing_cmd="ls /this_path_does_not_exist_otto_contract",
    temp_remote_dir="/tmp",
    send_line_ending="\n",
    expect_in_output="hello",
    stability_iterations=15,
    stability_cycle_count=6,
    stability_large_size=256 * 1024,
)


_KITS: dict[str, HostKit] = {
    "ssh": _UNIX_KIT,
    "telnet": _UNIX_KIT,
    "local": _UNIX_KIT,
    **{b: _zephyr_kit(b) for b in EMBEDDED_BACKENDS},
    # One kit object shared by all five guests — unlike the Zephyr kits,
    # nothing here is derived per backend, and HostKit is frozen.
    **dict.fromkeys(BUSYBOX_BACKENDS, _BUSYBOX_KIT),
}


@pytest.fixture
def host1_kit(request) -> HostKit:
    """Backend-appropriate command kit for the parametrized host1 fixture.

    Indirect-parametrize ``host1_kit`` alongside ``host1`` with the same
    backend id so the kit lines up with whichever host is built::

        @pytest.mark.parametrize(
            "host1, host1_kit",
            [(b, b) for b in ALL_BACKENDS],
            indirect=True,
        )
    """
    return _KITS[request.param]


@pytest.fixture
def hermetic_monitor_dist(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Stand in a throwaway React dist so a test can boot a ``MonitorServer``.

    ``MonitorServer`` construction hard-requires a real ``dist/index.html``
    under ``otto.monitor.server._STATIC_DIR`` (see ``_dist_index_path``) — a
    deliberate fail-fast for deployments that skipped ``make web``. That check
    is a trap for tests: **pytest never builds the web dist, but every
    developer checkout has one**, so a test that boots a server passes locally
    and fails in CI's ``tests_hostless``/``unit-repeat`` jobs, which run pytest
    without ``make web``. Request this fixture from any test that boots a
    server to exercise something other than the bundle itself (archive
    persistence, console logging, port binding, ...).

    Tests that serve the *real* bundle — the Playwright lane under
    ``tests/e2e/monitor/dashboard`` — must NOT use this: a marker page would
    silently certify the wrong artifact. That package keeps its own
    real-and-fresh dist guard instead.
    """
    from otto.monitor import server as server_module

    static_dir = tmp_path / "_hermetic_static"
    dist_dir = static_dir / "dist"
    dist_dir.mkdir(parents=True)
    (dist_dir / "index.html").write_text("<html>HERMETIC_TEST_DIST_MARKER</html>")
    monkeypatch.setattr(server_module, "_STATIC_DIR", static_dir)
    return static_dir


@pytest.fixture
def neutralized_webassets(tmp_path: Path) -> Iterator[Path]:
    """Point every registered webasset consumer at a nonexistent directory.

    ``otto._webassets.ALL`` is the single registry of in-package build
    artifacts (``make web`` output, gitignored, embedded in the wheel); every
    module-global that resolves one — ``otto.monitor.server._STATIC_DIR``,
    ``otto.coverage.renderer.spa_renderer.STATIC_DIR`` — is listed in
    ``tests/_fixtures/webassets.py``'s ``CONSUMERS``. This fixture monkeypatches
    every entry there to a path under ``tmp_path`` that is guaranteed absent, so
    a test that requests it exercises exactly the "no build artifact" path
    regardless of whether this checkout happens to have run ``make web``.

    Requested directly by a test that wants an explicit void; activated
    unconditionally for the whole unit tree by ``tests/unit/conftest.py``'s
    autouse ``_no_ambient_webassets`` (issue #175) — see that fixture's
    docstring for the ordering contract with package-level hermetic-dist
    fixtures such as ``hermetic_monitor_dist`` / ``_hermetic_static_dir``.

    Lives in the ROOT conftest per the #132 process-global-state rule: the
    ``otto._webassets`` paths are process-global module attributes, not local
    to ``tests/unit`` — a guard confined to one tree would leave every other
    tree exposed to whichever real (or absent) dist that process happens to
    see, exactly the defect class #132/#133 already fixed for other guards in
    this file.

    Uses a PRIVATE ``pytest.MonkeyPatch`` instance, never the shared
    ``monkeypatch`` fixture. Requesting the shared fixture from an autouse
    fixture this early would promote its instantiation ahead of every later
    fixture's ``mock.patch`` context (e.g. ``tests/unit/cli``'s autouse
    ``no_logger_output_dir``), inverting the teardown LIFO those tests rely
    on: a test-body ``monkeypatch.setattr`` on an attribute that a still-open
    ``mock.patch`` owns records the MOCK as "previous", and with the shared
    instance now finalizing last it re-installs that mock AFTER the patch
    restored the real function — leaking a MagicMock into the module for the
    rest of the worker's life (surfaced as mix-dependent logger-test failures
    the first time this fixture went tree-wide).
    """
    import importlib

    from tests._fixtures.webassets import CONSUMERS

    mp = pytest.MonkeyPatch()
    void = tmp_path / "_absent_webassets"
    for module_name, attr in CONSUMERS:
        module = importlib.import_module(module_name)
        mp.setattr(module, attr, void / module_name.rpartition(".")[2])
    yield void
    mp.undo()


_LOOPBACK_NAMES = frozenset({"localhost", "localhost.localdomain"})


def _is_loopback_dial(host: object) -> bool:
    """True for the targets a hostless test may dial: loopback, or no host at all.

    ``host=None`` is the pre-connected ``sock=`` form (the caller already
    owns a socket; nothing is dialed here). Literals are judged by
    :mod:`ipaddress` — ``127.0.0.0/8``, ``::1``, ``::ffff:127.0.0.1``, and
    the unspecified addresses (``0.0.0.0`` / ``::``, which Linux routes to
    localhost on connect) — so ``127.0.0.1.evil.example`` is a name, not a
    prefix match. Names: only the two spellings of localhost, case-folded.
    asyncssh hands over the RAW host, so an /etc/hosts alias for loopback is
    refused; a hostless test should say ``127.0.0.1``.
    """
    if host is None:
        return True
    text = (host.decode() if isinstance(host, bytes) else str(host)).strip("[]")
    try:
        address = ipaddress.ip_address(text)
    except ValueError:
        return text.lower() in _LOOPBACK_NAMES
    return address.is_loopback or address.is_unspecified


@pytest.fixture
def refuse_off_loopback_dials() -> Iterator[list[str]]:
    """An in-process asyncio dial off loopback fails at once, and its test fails at teardown.

    Every asyncio client otto uses — asyncssh, telnetlib3, aioftp, httpx —
    reaches the network through ``loop.create_connection`` (asyncssh's own
    direct path is that call with ``host, port``; its ``sock=`` form only
    wraps a socket the caller already connected), so this one seam covers
    them all without importing any of them: they are runtime dependencies
    loaded at connect time, and a conftest-level import would defeat that
    laziness for every test process. The refusal is
    ``OSError(errno.ECONNREFUSED, ...)``, which Python promotes to
    ``ConnectionRefusedError``, so code under test takes exactly the branch
    it takes against a dead host instead of waiting out a connect timeout.

    SCOPE: in-process asyncio dials. A blocking ``urllib`` fetch, or a child
    process (``test_support_matrix.py``'s inner conformance run, which does
    fetch the BusyBox artifacts on a cold cache), is out of this seam's
    reach — those are the artifact tier's concern, not this fixture's.

    The refusal alone is NOT the guard, and the reason is the defect that
    motivated it: ``test_power.py::test_unix_shutdown_issues_shutdown_sudo``
    spent 30 s on three real SSH dials to 10.0.0.1 because ``shutdown()``
    resolves its userland before the mocked ``run`` and ``Userland._send``
    swallows the connect error and probes again. Under a bare refusal that
    test passes, faster, with the defect intact. So every off-loopback dial
    is recorded and the test fails at teardown by name: a hostless test that
    reaches for a routable address is a red run, not a slow one.

    Lives in the ROOT conftest per the #132 process-global-state rule (the
    patched attribute is a class attribute of every event loop in the
    process); ``tests/unit/conftest.py`` activates it for that tree only.
    Uses a PRIVATE ``pytest.MonkeyPatch`` for the reason
    ``neutralized_webassets`` spells out above.
    """
    dialed: list[str] = []
    original = asyncio.BaseEventLoop.create_connection

    async def guarded(self, protocol_factory, host=None, port=None, *args, **kwargs):  # type: ignore[no-untyped-def]
        if not _is_loopback_dial(host):
            dialed.append(f"{host}:{port}")
            raise OSError(
                errno.ECONNREFUSED,
                f"hostless lane: refusing to dial {host}:{port} — a test that declares no "
                f"host may reach loopback only (tests/conftest.py refuse_off_loopback_dials)",
            )
        return await original(self, protocol_factory, host, port, *args, **kwargs)

    mp = pytest.MonkeyPatch()
    mp.setattr(asyncio.BaseEventLoop, "create_connection", guarded)
    try:
        yield dialed
    finally:
        mp.undo()
    assert not dialed, (
        f"this hostless test dialed off loopback: {sorted(set(dialed))} "
        f"({len(dialed)} attempt(s)). Mock the seam the code reaches the network "
        f"through, or build the object in its resolved shape — a unit test that "
        f"opens a socket to a routable address is measuring the network, not the code"
    )


@pytest.fixture
def hermetic_covapp_bundle(tmp_path: Path) -> Iterator[Path]:
    """Stand in a throwaway covapp bundle so a test can exercise the
    present-bundle paths of ``SpaRenderer`` without a real ``make web`` build.

    Mirrors ``hermetic_monitor_dist`` for the covapp coverage-SPA artifact:
    since the unit lane now defaults to blind (``neutralized_webassets``,
    autouse in ``tests/unit/conftest.py``, issue #175), any test asserting on
    the "bundle present" behavior — the index.html/dist copy, the *.map
    exclusion, the no-warning path — must request this fixture explicitly
    rather than rely on an ambient dev-box build.

    Seeds ``dist/covapp.js.map`` deliberately: the *.map-exclusion pin becomes
    unconditional instead of depending on what vite happened to emit in this
    checkout — a test asserting "no .map copied" is meaningful precisely
    because one was there to exclude.

    Lives in the ROOT conftest per the #132 process-global-state rule, same as
    ``neutralized_webassets`` above: ``spa_renderer.STATIC_DIR`` is a
    process-global module attribute, not local to ``tests/unit``, so any tree
    that wants the real-bundle-present behavior (not just the unit lane) can
    request this fixture without import gymnastics.

    Private ``pytest.MonkeyPatch`` instance for the same LIFO-inversion reason
    as ``neutralized_webassets`` above. Ordering with the neutralizer is
    plain fixture LIFO either way: this fixture sets up after it (explicit
    request beats autouse) and undoes before it, so the void is restored, then
    the real path.
    """
    from otto.coverage.renderer import spa_renderer

    mp = pytest.MonkeyPatch()
    bundle = tmp_path / "_hermetic_covapp"
    dist = bundle / "dist"
    dist.mkdir(parents=True)
    (bundle / "index.html").write_text("<html>HERMETIC_COVAPP_MARKER</html>")
    (dist / "covapp.js").write_text("// hermetic bundle js")
    (dist / "covapp.css").write_text("/* hermetic bundle css */")
    (dist / "covapp.js.map").write_text("{}")
    mp.setattr(spa_renderer, "STATIC_DIR", bundle)
    yield bundle
    mp.undo()


def _clirunner_guard_impl():
    """Detach pytest's live-log handlers from the root logger during every ``CliRunner.invoke``.

    With ``log_cli = true`` (our pyproject default), any log record reaching the
    ROOT logger *while a ``CliRunner.invoke`` is in flight* makes pytest's
    ``_LiveLoggingStreamHandler`` suspend stdout capture to print the line.
    Suspending capture drops click's isolated ``_NamedTextIOWrapper``, whose
    ``TextIOWrapper.__del__`` closes the underlying ``BytesIOCopy`` — so typer's
    finally-block ``outstreams[0].getvalue()`` raises ``ValueError: I/O
    operation on closed file``. The close is GC-timing-dependent, which is why it
    surfaced just once, on the nightly ``--repeat-scope=session`` 3.12 job
    (issue #110), and never in a single ``make coverage`` pass.

    This guard lives in the ROOT conftest, not the unit tree's, ON PURPOSE: it
    must cover every tree that drives a ``CliRunner``. It was originally scoped
    to ``tests/unit`` and so never reached ``tests/e2e/cli``, which invokes the
    same runner against commands (``otto monitor``) whose non-otto loggers
    (uvicorn, asyncio) log mid-invoke — issue #133, two e2e tests dying on this
    exact #110 signature once ``otto monitor``'s review branch began really
    booting a server. ``tests/e2e/cli/test_clirunner_capture_guard_e2e.py`` and
    ``tests/unit/cli/test_clirunner_capture_guard.py`` each pin the guard's
    reach into their own tree.

    ``tests/unit/cli``'s ``no_logger_output_dir`` sets ``otto.propagate=False``,
    but that only blocks the ``otto`` hierarchy; a record from ANY other logger
    (a third-party lib, ``asyncio``, ``py.warnings``) still reaches root and
    trips it. Removing only the live-log handlers for the invoke window closes
    the whole class without changing observable behavior: ``caplog``'s separate
    ``LogCaptureHandler`` and otto's console handler are left attached, so log
    capture and console output during the invoke still work.

    The patch is applied/restored manually rather than via the ``monkeypatch``
    fixture ON PURPOSE: depending on ``monkeypatch`` here would pull its setup
    earlier than ``no_logger_output_dir`` and thus flip their teardown order, so
    a cli/suite test that ``monkeypatch.setattr``s ``create_output_dir`` would
    have that undo run *after* ``no_logger_output_dir`` restores it — re-leaking
    the mock into later (e.g. logger) tests.
    """
    from typer.testing import CliRunner

    try:
        from _pytest.logging import _LiveLoggingNullHandler, _LiveLoggingStreamHandler
    except ImportError:
        # FAIL LOUD, never degrade (review §5.4): a pytest rename of these
        # private classes would otherwise inertly disarm this guard for every
        # CliRunner site while ``log_cli = true`` keeps the #110/#133 hazard
        # live — and the two pin tests assert the guard's REACH, not its
        # liveness, so nothing else would notice. One pytest upgrade turns
        # every test red with this message instead.
        pytest.fail(
            "pytest moved/renamed _pytest.logging._LiveLoggingNullHandler / "
            "_LiveLoggingStreamHandler — the CliRunner live-log capture guard "
            "(issues #110/#133) cannot arm. Update the import in "
            "_clirunner_guard_impl (tests/conftest.py) to pytest's "
            "new spelling; do NOT fall back to yielding, or the "
            "GC-timing-dependent 'I/O operation on closed file' flake returns "
            "silently.",
            pytrace=False,
        )

    real_invoke = CliRunner.invoke

    def _invoke_without_live_log(self, *args, **kwargs):
        root = logging.getLogger()
        live = [
            h
            for h in root.handlers
            if isinstance(h, (_LiveLoggingNullHandler, _LiveLoggingStreamHandler))
        ]
        for handler in live:
            root.removeHandler(handler)
        try:
            return real_invoke(self, *args, **kwargs)
        finally:
            for handler in live:
                root.addHandler(handler)

    CliRunner.invoke = _invoke_without_live_log
    try:
        yield
    finally:
        CliRunner.invoke = real_invoke


@pytest.fixture(autouse=True)
def _clirunner_live_log_capture_guard():
    """Autouse wrapper over :func:`_clirunner_guard_impl` — see its docstring.

    Split so ``tests/unit/cli/test_clirunner_capture_guard.py`` can drive the
    generator body directly and prove the fail-loud arm (a pytest rename of
    the live-log handler classes) without a nested pytest session.
    """
    yield from _clirunner_guard_impl()


def _loaded_registries() -> list[Registry]:
    """Return every ``Registry`` reachable from a loaded ``otto.*`` module.

    Discovery is dynamic (scans ``sys.modules``) rather than a hand-maintained
    list, so a registry added in the future is isolated automatically without a
    matching test-side edit. Instances are de-duplicated by ``id`` because a
    single registry is often re-exported from several modules.

    Every call re-scans, DELIBERATELY uncached. The scan measures 0.2 ms with
    all of otto imported (121 otto modules of 488 total) — noise against any
    test body. The memo this replaces was keyed on ``len(sys.modules)``,
    which is identity-blind: a test that imports one module and evicts
    another leaves the count unchanged, so a brand-new ``Registry`` was
    silently never isolated. ``test_registry_isolation_e2e.py`` pins the
    completeness this bought (proven red against the cached version first).
    """
    found: dict[int, Registry] = {}
    for module in list(sys.modules.values()):
        mod_name = getattr(module, "__name__", "")
        if mod_name != "otto" and not mod_name.startswith("otto."):
            continue
        try:
            members = vars(module)
        except TypeError:  # pragma: no cover - namespace without __dict__
            continue
        for value in members.values():
            if isinstance(value, Registry):
                found[id(value)] = value
    return list(found.values())


@pytest.fixture(autouse=True)
def _isolate_sys_path():
    """Snapshot ``sys.path`` before each test; restore it in place after.

    ``Repo.add_libs_to_pythonpath()`` appends each repo's ``libs`` dirs to
    ``sys.path`` with no cleanup of its own — a one-shot, startup-time call in
    production, fine there. In-process tests trigger it from several trees:
    ``bootstrap()`` registration (``tests/unit/bootstrap``,
    ``tests/e2e/suite``) and direct calls (``tests/unit/config/test_repo.py``,
    ``tests/unit/suite/test_import_and_register.py``). When tests reuse
    generic repo/init-module names (two tests both writing a repo named ``b``
    with ``init = ["b_init"]``), an earlier test's still-on-disk ``tmp_path``
    entry sits ahead of the current test's own entry, and the import machinery
    resolves the freshly-purged module name against the *earlier* directory —
    a working module shadowing a deliberately broken one (order-dependent
    flake, found in the dependency-management work).

    Lives in the ROOT conftest per the #132/#133 rule (see
    ``_isolate_registries`` below): ``sys.path`` is process-global and its
    mutators span trees. Higher-scoped fixtures that extend ``sys.path``
    (e.g. ``tests/repo1/conftest.py``'s import-time append) run before each
    function-scoped snapshot, so their entries are captured and survive the
    restore — only additions made during a test body are rolled back.
    """
    snapshot = list(sys.path)
    yield
    sys.path[:] = snapshot


@pytest.fixture(autouse=True)
def _drop_richs_cached_console():
    """Discard ``rich``'s global ``Console`` around each test — it CACHES the width.

    ``rich.print`` renders through one lazily-built module singleton
    (``rich._console``), and ``Console.__init__`` resolves ``COLUMNS`` ONCE, into
    ``self._width``. Only a console built with no ``COLUMNS`` set leaves
    ``_width`` unset and re-reads the environment per render. So the FIRST test
    in a process to render through ``rich.print`` while ``COLUMNS`` is pinned
    freezes that width for every later test, and every later
    ``monkeypatch.setenv("COLUMNS", …)`` becomes a silent no-op.

    Several modules pin the width deliberately — ``test_error_render.py`` and
    ``test_main.py`` widen it so a WRAP cannot masquerade as an eaten message,
    and ``test_instruction_ownership.py`` narrows one case to prove a copyable
    hint survives an 80-column terminal. Without this fixture those pins hold or
    not depending on execution ORDER, which pytest-randomly varies per seed: the
    narrow case was observed passing on seed 1 and failing on seed 3, purely
    because a different test built the console first.

    Dropping the reference is the whole fix; ``get_console()`` rebuilds on next
    use. Done on BOTH sides so a test is neither poisoned by its predecessors
    nor able to poison its successors.

    Lives in the ROOT conftest per the #132/#133 rule (see ``_isolate_registries``
    below): the singleton is process-global, and the tests that pin ``COLUMNS``
    span ``tests/unit`` and ``tests/e2e``.
    """
    import rich

    rich._console = None
    yield
    rich._console = None


@pytest.fixture(autouse=True)
def _isolate_registries():
    """Snapshot every global otto ``Registry`` before each test; restore after.

    The ``otto.registry.Registry`` singletons (``INSTRUCTIONS``,
    ``LOADER_CLASSES``, ``FRAME_CLASSES``, ``CLI_COMMANDS``, …) live for the
    whole process. Tests that register entries into them — via ``@instruction``,
    ``register_binary_loader``, ``register_cli_command``, etc. — never clean up,
    so under the nightly ``--count=N --repeat-scope=session`` repeat the second
    pass re-registers the same name and the registry's loud collision guard
    raises ``ValueError: already registered`` (issue #108). A single CI pass
    registers each name exactly once and never trips this, which is why the leak
    only surfaces in the nightly repeat job — never in ``make nox`` /
    ``make coverage`` (both single-pass by default).

    Snapshotting each registry's entries before the test and, on teardown,
    dropping anything the test added and restoring the originals keeps every
    registry byte-for-byte stable across tests and across repeat iterations of
    the same test in one process. Built-in registrations (present at import)
    survive because they are part of the snapshot.

    The ``tests/unit/suite`` package additionally keeps its own
    ``_isolate_suites`` fixture: it *clears* ``SUITES`` to an empty baseline that
    those tests assert against, which this snapshot/restore alone does not do.

    This guard lives in the ROOT conftest, not the unit tree's, ON PURPOSE. The
    registries are process-global, so the hazard is too: ``tests_hostless`` runs
    ``tests/unit`` and ``tests/e2e`` in ONE session (and ``make coverage`` adds
    ``tests/integration``), so a single xdist worker runs tests from several
    trees in one process — an entry leaked by an e2e test lands in the very
    registry the next unit test asserts against. Scoped to ``tests/unit`` the
    guard could not see any of that.

    The move is PREVENTIVE, not a fix for an observed leak: no e2e test today
    happens to register into a global registry (measured, baselining after
    collection so that import-time registrations are not miscounted as leaks).
    But nothing stops the next one from doing so, and a guard scoped narrower
    than the state it guards is exactly the defect behind issues #132 and #133.
    ``tests/e2e/cli/test_registry_isolation_e2e.py`` pins the guard's reach into
    the e2e tree.

    Higher-scoped fixtures are unaffected: pytest sets up module- and
    session-scoped fixtures BEFORE function-scoped ones, so anything they
    register is already inside every per-test snapshot and survives the restore.
    """
    snapshots = [
        (reg, {name: (reg.get(name), reg.origin(name)) for name in reg.names()})
        for reg in _loaded_registries()
    ]
    modules_before = frozenset(sys.modules)

    yield

    _restore_registries(snapshots, modules_before)


def _restore_registries(
    snapshots: list[tuple[Registry, dict[str, tuple[object, str]]]],
    modules_before: frozenset[str],
) -> None:
    """Drop entries a test added, restore the snapshot, evict side-effect origins.

    A test that imports an extension module listed in a repo's ``init`` (e.g.
    ``custom_hosts``, which calls ``register_command_frame`` at import) registers
    into an isolated registry as an **import side effect**. Dropping the entry
    on teardown is not enough: the origin module stays in ``sys.modules``, so a
    later ``importlib.import_module`` of it is a no-op and never re-runs the
    registration — leaving the module imported but its registry entry gone. A
    downstream test that relies on re-import to re-register (e.g.
    ``Repo.import_init_modules`` mirroring bootstrap order) then fails with
    ``... is not a registered frame``. This surfaces only single-process
    (``-n0``); ``-n auto`` scatters the importer and the victim across workers.

    So after restoring each registry, evict from ``sys.modules`` the origin
    module of every entry the test added — but ONLY origins the test itself
    imported (absent from *modules_before*), mirroring ``purge_tmp_imports``.
    A module already loaded before the test (a pytest-collected test module
    registering a locally-defined class via ``register_suite_class``, or a core
    ``otto`` module) must never be evicted: it isn't a re-importable extension,
    and dropping the running test file breaks ``inspect.getfile`` for every
    later registration in it.
    """
    evict_origins: set[str] = set()

    def _drop_added(reg: Registry, name: str) -> None:
        """Unregister *name*, evicting its origin when a re-import can restore it."""
        origin = reg.origin(name)
        if (
            origin
            and origin not in modules_before
            and origin != "otto"
            and not origin.startswith("otto.")
        ):
            evict_origins.add(origin)
        reg.unregister(name)

    for reg, parked in snapshots:
        for name in list(reg.names()):
            if name not in parked:
                _drop_added(reg, name)
        for name, (entry, origin) in parked.items():
            reg.register(name, entry, overwrite=True, origin=origin)

    # Registries that did not EXIST at snapshot time. The snapshot can only
    # cover what was reachable when the test started, so a registry living in
    # an ``otto.*`` module the test itself imported has no entry above — and
    # iterating snapshots alone left everything the test registered there
    # standing for the next test to trip over.
    #
    # Their baseline cannot be recovered by re-import (the module stays in
    # ``sys.modules``, so a second import is a no-op), which is why the rule
    # here is by ORIGIN rather than wholesale: an entry registered by otto's
    # own module as an import side effect IS the process's state now, and
    # dropping it would leave otto missing its own defaults for every later
    # test — a worse failure than the leak. Anything else in a brand-new
    # registry arrived from the test, and goes.
    snapshotted = {id(reg) for reg, _ in snapshots}
    for reg in _loaded_registries():
        if id(reg) in snapshotted:
            continue
        for name in list(reg.names()):
            origin = reg.origin(name)
            if origin == "otto" or origin.startswith("otto."):
                continue
            _drop_added(reg, name)

    for origin in evict_origins:
        sys.modules.pop(origin, None)

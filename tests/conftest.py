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
  ``sys.modules`` identity) → root conftest. Every tree gets it.
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
    {"sprout_cov", "docker_e2e", "coverage_e2e", "zephyr_fanout"}
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
# dicts), which happens after this and is unaffected. Harness opt-ins
# legitimately read from the ambient environment are exempt. Pinned by
# tests/unit/test_env_hermeticity.py.
# OTTO_TS_COVERAGE is a harness opt-in like OTTO_DETECT_ASYNCIO_LEAKS: `make
# dashboard` sets it to arm the browser suites' CDP coverage collection
# (tests/_fixtures/_ts_coverage.py), so it must survive this strip to reach the
# fixture. OTTO_BROWSER_SHARD is the same kind of opt-in: CI's dashboard jobs
# set it to relax the browser suites' single-worker pin to per-file xdist
# groups (tests/e2e/conftest.py's grouping policy reads it at collection,
# which happens after this strip). Keep in sync with
# tests/unit/test_env_hermeticity.py's ALLOWED_AMBIENT.
_OTTO_AMBIENT_ALLOWED = {"OTTO_DETECT_ASYNCIO_LEAKS", "OTTO_TS_COVERAGE", "OTTO_BROWSER_SHARD"}
for _var in [k for k in os.environ if k.startswith("OTTO_") and k not in _OTTO_AMBIENT_ALLOWED]:
    os.environ.pop(_var, None)

import asyncio
import contextlib
import logging
import sys
import weakref
from dataclasses import dataclass
from pathlib import Path

import pytest
import pytest_asyncio

from otto.config.lab import Lab
from otto.context import OttoContext, reset_context, set_context
from otto.host.factory import create_host_from_dict
from otto.host.local_host import LocalHost
from otto.host.login_proxy import Cred
from otto.host.unix_host import UnixHost
from otto.registry import Registry
from tests._fixtures._loop_reaper import classify_loop_origin, reap_or_raise

_logger = logging.getLogger(__name__)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item: pytest.Item):
    """Implement ``@pytest.mark.retry(n)`` for dev pytest runs.

    Provides the marker under bare ``pytest`` — ``otto.suite.plugin.OttoPlugin``
    only registers under ``otto test``. Used to gate known-flaky integration
    tests (nc transfers through an SSH hop) — see
    ``todo/hop_nc_transfer_flake.md`` for the underlying issue.

    Implemented as a hookwrapper so the first attempt runs through the default
    hook and retries override the outcome on success — a plain ``tryfirst``
    impl would let the default re-run (and possibly fail) the test after a
    retry succeeded.
    """
    outcome = yield
    retry_marker = item.get_closest_marker("retry")
    if retry_marker is None or outcome.excinfo is None:
        return
    n = int(retry_marker.args[0]) if retry_marker.args else 1
    first_exc = outcome.excinfo[1]
    _logger.warning(f"retry: {item.nodeid} attempt 1/{n} failed: {first_exc}")
    for attempt in range(1, n):
        try:
            item.runtest()
        except Exception as exc:  # noqa: BLE001 — retry hook must catch any test exception to report and retry
            _logger.warning(f"retry: {item.nodeid} attempt {attempt + 1}/{n} failed: {exc}")
            outcome.force_exception(exc)
            continue
        outcome.force_result(None)
        return


def pytest_configure(config):  # type: ignore[no-untyped-def]
    _install_sigint_traceback_dump()
    _install_loop_origin_tracker()


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
    return result


def pytest_terminal_summary(terminalreporter):  # type: ignore[no-untyped-def]
    if _loops_reaped:
        terminalreporter.write_line(
            f"loop-reaper: closed {_loops_reaped} orphaned pytest-asyncio event "
            "loop(s) at test boundaries (harness teardown race; see "
            "tests/_loop_reaper.py)"
        )


# ---------------------------------------------------------------------------
# Asyncio leak detector (diagnostic, autouse on host tests)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _detect_asyncio_leaks(request):
    """Attribute leaked asyncio transports to the test that created them.

    Recipe from ``~/wiki/inbox/2026-04-24-detect-asyncio-leaks-at-source.md``:
    after each test, look for live transports whose ``_loop`` is closed.
    Those are the things that fire ``ResourceWarning`` from ``__del__`` at
    GC time and are then escalated by pytest's ``[unraisable]`` plugin into
    a ``PytestUnraisableExceptionWarning`` on whichever *next* test happens
    to be running — the source of the xdist-flake symptom.

    Only enabled by setting ``OTTO_DETECT_ASYNCIO_LEAKS=1`` in the env so
    it doesn't slow the regular run with the per-test ``gc.collect()``.
    """
    yield
    import os

    if not os.environ.get("OTTO_DETECT_ASYNCIO_LEAKS"):
        return
    import gc
    from asyncio.base_subprocess import BaseSubprocessTransport
    from asyncio.selector_events import _SelectorTransport

    gc.collect()
    leaks = []
    for o in gc.get_objects():
        if not isinstance(o, (BaseSubprocessTransport, _SelectorTransport)):
            continue
        loop = getattr(o, "_loop", None)
        if loop is None or not loop.is_closed():
            continue
        # Filter to ones that would actually emit a ResourceWarning from
        # __del__: i.e., the transport is still "open" (closing flag unset).
        # Already-closed transports don't warn even if they linger in GC.
        closing = getattr(o, "_closing", None)
        sock = getattr(o, "_sock", None)
        details = f" closing={closing} sock={sock!r}"
        # Show what's referencing this transport so we can find the leak.
        referrers = gc.get_referrers(o)
        ref_summary = ", ".join(
            f"{type(r).__module__}.{type(r).__name__}"
            for r in referrers[:5]
            if r is not gc.get_referrers and r is not leaks
        )
        leaks.append(f"{o!r}{details}\n    referrers: {ref_summary}")
    if leaks:
        # Print rather than raise: we want to *attribute* the leak, not
        # fail the test that detected it.
        print(  # noqa: T201 — test diagnostic output
            f"\nLEAK after {request.node.nodeid}: "
            f"{len(leaks)} live transport(s) bound to closed loop:"
        )
        for leak in leaks:
            print(f"  {leak}")  # noqa: T201 — test diagnostic output


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
def _reset_otto_logger_retention():
    """Reset otto's logging-management state between tests so log retention /
    output-dir config can't leak across tests in the same xdist worker (the
    root cause of the old test_cov ENOTDIR flakes)."""
    yield
    from otto.logger import management

    management.reset()


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


# ---------------------------------------------------------------------------
# Lab-data helpers
# ---------------------------------------------------------------------------

from tests._fixtures._host_pool import lease_unix_host
from tests._fixtures.labdata import host_data, lab_data_path, make_host  # noqa: F401

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
    "zephyr_fat": "sprout",
    "zephyr_lfs": "sprout_lfs",
    "zephyr_no_fs": "sprout_no_fs",
    # Zephyr 2.7 LTS — distinct command frame (inline retcode); one fs cell.
    "zephyr_27_fat": "sprout27",
    # Zephyr 4.4 LTS — newest-LTS firmware-drift sentinel; one fs cell.
    "zephyr_44_lfs": "sprout44_lfs",
}

# Ordered list of embedded backend ids — imported by the integration contract
# suites so the parametrize lists stay in lockstep with the lab matrix.
EMBEDDED_BACKENDS: list[str] = list(_ZEPHYR_BACKEND_NE)


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
    shared remote dir, and ``ssh``+``telnet`` share one host (``carrot:/tmp``)
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

    - ``"ssh"`` / ``"telnet"`` -> UnixHost on `carrot`, with the matching term.
    - ``"local"``              -> LocalHost.
    - any id in :data:`EMBEDDED_BACKENDS` -> EmbeddedHost on the matching
      Zephyr QEMU target, built via the host factory from its lab-data
      entry. The matrix anchors the full {FAT-on-RAM, LittleFS, no-FS} set on
      3.7, with a single fs cell on 2.7 and 4.4; see :data:`_ZEPHYR_BACKEND_NE`
      for the id -> `ne` mapping and the trim rationale.
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
    # Unix terms ("ssh" / "telnet").
    kwargs: dict[str, str] = {"term": backend}
    if backend == "telnet":
        kwargs["transfer"] = "ftp"
    h = make_host("carrot", **kwargs)
    yield h
    await h.close()


@pytest_asyncio.fixture
async def host2(request):
    """Integration host2, parameterized by term type ('ssh' or 'telnet')."""
    term = request.param
    kwargs: dict[str, str] = {"term": term}
    if term == "telnet":
        kwargs["transfer"] = "ftp"
    h = make_host("tomato", **kwargs)
    yield h
    await h.close()


@pytest_asyncio.fixture
async def host3(request):
    """Integration host3, parameterized by term type ('ssh' or 'telnet')."""
    term = request.param
    kwargs: dict[str, str] = {"term": term}
    if term == "ssh":
        kwargs["transfer"] = "scp"
    h = make_host("pepper", **kwargs)
    yield h
    await h.close()


@pytest_asyncio.fixture
async def hop_host(request):
    """Integration host reached through one or two SSH hops.

    Parameterized by ``(ne, hop_ne, term, transfer)`` tuples — e.g.
    ``("tomato", "carrot", "ssh", "scp")`` means "reach tomato through carrot".

    For two-hop chains, *hop_ne* is the first hop and the intermediate host
    must itself have a hop configured at fixture construction time.
    """
    ne, hop_ne, term, transfer = request.param
    target_data = host_data(ne)
    hop_data = host_data(hop_ne)
    hop_id = f"{hop_data['element']}_{hop_data.get('board', 'seed')}"
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
    """Integration host leased from the Unix pool, parameterized by transfer
    type ('scp', 'sftp', 'ftp', 'nc') or a ``(transfer, term)`` tuple.

    Leases a free host from ``UNIX_POOL`` instead of always using carrot, so
    the transfer tests spread across the veggies-lab peers (carrot/tomato/pepper)
    rather than serializing on one VM.
    """
    param = request.param
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


_KITS: dict[str, HostKit] = {
    "ssh": _UNIX_KIT,
    "telnet": _UNIX_KIT,
    "local": _UNIX_KIT,
    **{b: _zephyr_kit(b) for b in EMBEDDED_BACKENDS},
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


@pytest.fixture(autouse=True)
def _clirunner_live_log_capture_guard():
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
    except ImportError:  # pragma: no cover - pytest renamed its live-log handlers
        # Guard is best-effort: if pytest's internal handler classes move, skip
        # it rather than error every unit test. The flake reappears but is rare.
        yield
        return

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


_cached_registries: list[Registry] = []
_cached_module_count: int = -1


def _loaded_registries() -> list[Registry]:
    """Return every ``Registry`` reachable from a loaded ``otto.*`` module.

    Discovery is dynamic (scans ``sys.modules``) rather than a hand-maintained
    list, so a registry added in the future is isolated automatically without a
    matching test-side edit. Instances are de-duplicated by ``id`` because a
    single registry is often re-exported from several modules. The result is
    memoized and only re-scanned when ``sys.modules`` grows (a new module —
    possibly carrying a new registry — was imported); registries are import-time
    singletons that are never torn down, so this keeps the per-test snapshot cost
    negligible across the ~15k-test unit run.
    """
    global _cached_registries, _cached_module_count  # noqa: PLW0603 — memoized discovery
    module_count = len(sys.modules)
    if module_count != _cached_module_count:
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
        _cached_module_count = module_count
        _cached_registries = list(found.values())
    return _cached_registries


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
    for reg, parked in snapshots:
        for name in list(reg.names()):
            if name not in parked:
                origin = reg.origin(name)
                if (
                    origin
                    and origin not in modules_before
                    and origin != "otto"
                    and not origin.startswith("otto.")
                ):
                    evict_origins.add(origin)
                reg.unregister(name)
        for name, (entry, origin) in parked.items():
            reg.register(name, entry, overwrite=True, origin=origin)
    for origin in evict_origins:
        sys.modules.pop(origin, None)

"""Root test conftest — shared fixtures across unit and integration tests."""

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

import asyncio
import sys
import weakref
from dataclasses import dataclass

import pytest
import pytest_asyncio

from otto.configmodule.lab import Lab
from otto.context import OttoContext, reset_context, set_context
from otto.host.local_host import LocalHost
from otto.host.unix_host import UnixHost
from otto.logger import get_otto_logger
from otto.storage.factory import create_host_from_dict
from tests._fixtures._loop_reaper import classify_loop_origin, reap_or_raise

_logger = get_otto_logger()


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
        except Exception as exc:
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
    global _tracker_installed
    if _tracker_installed:
        return
    from asyncio import base_events

    orig_init = base_events.BaseEventLoop.__init__

    def _tracking_init(self, *args, **kwargs):
        orig_init(self, *args, **kwargs)
        origin = classify_loop_origin(_frame_filenames(sys._getframe(1)))
        try:
            _LOOP_INFO[self] = (origin, _current_test)
        except TypeError:
            pass  # not weak-referenceable (shouldn't happen for real loops)

    base_events.BaseEventLoop.__init__ = _tracking_init
    _tracker_installed = True


def pytest_runtest_setup(item):  # type: ignore[no-untyped-def]
    global _current_test
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
            try:
                owned.add(cached[0].get_loop())
            except Exception:
                pass
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
    global _loops_reaped

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
    reapable = [
        loop for loop in _LOOP_INFO.keys() if loop not in owned or origin_of(loop) == "product"
    ]
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
        print(
            f"\nLEAK after {request.node.nodeid}: {len(leaks)} live transport(s) bound to closed loop:"
        )
        for l in leaks:
            print(f"  {l}")


# ---------------------------------------------------------------------------
# active_context: test helper for installing an OttoContext in a block.
# ---------------------------------------------------------------------------

import contextlib


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
    ``lab_data/tech1/hosts.json`` (e.g. a future Zephyr 4.x or a different
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
      Zephyr QEMU target, built via the storage factory from its lab-data
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
        creds=target_data["creds"],
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
    and ``hosts.json`` gets a correct kit for free.
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

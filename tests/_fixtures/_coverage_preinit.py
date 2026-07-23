"""Pre-initialize each xdist worker's coverage SQLite schema single-threaded.

Closes coverage's ``no such table: context`` schema-init race, which has
repeatedly aborted ``make release`` at ``make nox`` on the newest-Python
``tests_all`` leg.

**The race.** Under ``parallel = true`` (see ``.coveragerc``) every worker and
subprocess writes to its own ``reports/coverage/.coverage.<host>.<pid>.<rand>``
data file, created lazily on the first write. With ``--cov-context=test`` every
test triggers such a write. Coverage builds the schema with one multi-statement
``executescript`` that creates ``coverage_schema`` first and ``context`` several
statements later; in SQLite autocommit each ``CREATE TABLE`` commits on its own.
Coverage's connections are thread-local (``self._dbs[threading.get_ident()]``)
but share the one file, and ``_read_db`` decides whether to initialize by probing
a single table (``select version from coverage_schema``). So if a test spawns a
second thread that flushes coverage concurrently with that first write, the
second thread can observe ``coverage_schema`` already committed, skip
initialization, and then write a context into a DB whose ``context`` table does
not exist yet -> ``sqlite3.OperationalError: no such table: context``.

Once a worker's DB loses ``context`` this way, every later test on that worker
fails setup *and* teardown, cascading into thousands of errors and a pytest
INTERNALERROR at ``cov.save()``.

**The fix.** Force the full schema to be created on the MAIN thread once per
worker, before any test — and therefore before any test-spawned thread — runs.
The root conftest's ``pytest_collection_finish`` calls
:func:`force_coverage_schema_init`. After that, every later ``_read_db`` on any
thread sees the finished schema and never re-initializes, so the window is gone.
Grouping subprocess-cov tests only bounds concurrency; it never closed this
window, which is why the race kept recurring.

Kept as an importable helper (not inlined in ``conftest.py``) so
``tests/unit/test_coverage_schema_preinit.py`` can drive it directly against a
real ``coverage.Coverage`` with no VM.
"""

import warnings

import coverage
import pytest

# Set by the collection-finish hook to what force_coverage_schema_init returned
# (False when coverage is not active in this process). The end-to-end test reads
# it to prove the hook actually ran and armed — a plain "the schema exists"
# check can't, because coverage would lazily create the same schema by the first
# test regardless of the hook.
PREINIT_OUTCOME: "pytest.StashKey[bool]" = pytest.StashKey()


def active_pytest_cov(config) -> "coverage.Coverage | None":
    """Return the ``Coverage`` object pytest-cov is measuring with, or ``None``.

    ``None`` whenever pytest-cov has not marked coverage *started* in this
    process: a bare ``pytest`` run without ``--cov``, ``--no-cov``, or a
    distributed run's in-process controller (which builds a ``Coverage`` but
    never flips ``started``). That ``started`` flag is the same one pytest-cov's
    per-test context switch checks, so a ``None`` here means no per-test context
    writes happen either — the pre-init is skipped exactly where there is no
    schema-init race to close.
    """
    manager = config.pluginmanager
    if not manager.hasplugin("_cov"):
        return None
    plugin = manager.getplugin("_cov")
    controller = getattr(plugin, "cov_controller", None)
    if controller is None or not getattr(controller, "started", False):
        return None
    return getattr(controller, "cov", None)


def force_coverage_schema_init(cov) -> bool:
    """Create ``cov``'s data-file schema on the current thread, in full.

    Returns ``True`` when the schema is in place afterwards, ``False`` when
    coverage's internals have moved and the pre-init was skipped. This is
    best-effort by design: the collection hook must never abort the whole
    session over a coverage upgrade — a skip only re-exposes the intermittent
    race, and ``tests/unit/test_coverage_schema_preinit.py`` fails loudly if the
    real path stops working, so a regression is caught in CI rather than in a
    release.

    Must be called single-threaded, before any test spawns a second thread.
    """
    try:
        # get_data() flushes the collector and runs _post_save_work, which emits
        # CoverageWarning("No data was collected") when nothing has been measured
        # yet — always the case at collection-finish before any test runs. Under
        # the suite's filterwarnings=["error"] that warning becomes an exception,
        # so silence warnings for just this call (a real no-data problem still
        # surfaces at pytest-cov's own end-of-session save). Without this the
        # whole pre-init was silently a no-op on every xdist worker.
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            data = cov.get_data()
        # _start_using() FIRST: it sets _have_used, so a later first real
        # add_lines() won't run coverage's lazy erase() (drop + recreate the
        # file) and wipe the schema we built here — which would re-open the race.
        # _connect() then reads-or-inits, creating coverage_schema AND context
        # together on this one thread.
        data._start_using()
        data._connect()
    except Exception:  # noqa: BLE001 — best-effort hardening; never fail the run
        return False
    return True

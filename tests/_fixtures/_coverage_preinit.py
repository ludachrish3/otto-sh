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

import traceback
import warnings
from dataclasses import dataclass

import coverage
import pytest


@dataclass(frozen=True)
class PreinitOutcome:
    """What the collection-finish hook did on this worker.

    ``armed`` is True when pytest-cov is measuring in this process (so the
    pre-init was attempted); ``error`` carries the full traceback text when
    the attempt raised — the state in which the ``no such table: context``
    race is silently back.
    """

    armed: bool
    error: "str | None" = None


# Set by the collection-finish hook. The end-to-end test reads it to prove the
# hook actually ran and armed — a plain "the schema exists" check can't,
# because coverage would lazily create the same schema by the first test
# regardless of the hook — and ``_coverage_preinit_failure_is_loud`` (root
# conftest) acts on a recorded failure instead of letting the run race.
PREINIT_OUTCOME: "pytest.StashKey[PreinitOutcome]" = pytest.StashKey()


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


def force_coverage_schema_init(cov) -> "str | None":
    """Create ``cov``'s data-file schema on the current thread, in full.

    Returns ``None`` when the schema is in place afterwards; when coverage's
    internals have moved (or the data file is unwritable) it returns the full
    traceback text instead of raising. The hook must not raise — an exception
    from a post-sessionstart hook under xdist is an INTERNALERROR that crashes
    the controller blaming an innocent item (the Wave 12 lesson) — but the
    failure is NOT silent either: the collection hook stashes this detail and
    ``_coverage_preinit_failure_is_loud`` (root conftest) fails the worker's
    tests naming it, because a skipped pre-init re-exposes the intermittent
    ``no such table: context`` release race with no other signal (review
    §5.4 — this guard has already failed silently once, see the warnings
    note below).

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
    except Exception:  # noqa: BLE001 — the hook must not raise (xdist INTERNALERROR); the detail is preserved and acted on
        return traceback.format_exc()
    return None


def preinit_failure_message(outcome: "PreinitOutcome | None") -> "str | None":
    """The loud-failure text for a worker whose pre-init failed, else ``None``.

    Pure decision half of ``_coverage_preinit_failure_is_loud`` (root
    conftest), so the truth table is unit-testable:

    * ``None`` (hook never stashed — the collection-finish hook is gone or
      broken): fail; that is the silent-disarm shape.
    * not armed (bare ``pytest`` / ``--no-cov`` / in-process controller): no
      context writes happen, so there is no race to close — quiet.
    * armed, no error: the schema is in place — quiet.
    * armed with an error: fail, carrying the recorded traceback.
    """
    if outcome is None:
        return (
            "coverage schema pre-init never ran on this worker — the "
            "pytest_collection_finish hook in tests/conftest.py is missing or "
            "was renamed. Without it the 'no such table: context' schema-init "
            "race (see tests/_fixtures/_coverage_preinit.py) is back."
        )
    if not outcome.armed or outcome.error is None:
        return None
    return (
        "coverage schema pre-init FAILED on this worker; every test here runs "
        "with the 'no such table: context' schema-init race re-opened (the "
        "race that repeatedly aborted `make release`, see "
        "tests/_fixtures/_coverage_preinit.py), so this worker's tests fail "
        "loudly instead of racing. Recorded failure:\n" + outcome.error
    )

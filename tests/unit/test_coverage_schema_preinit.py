"""Unit tests for the per-worker coverage schema pre-init (race fix).

These pin the fix for coverage's ``no such table: context`` schema-init race,
which repeatedly broke ``make release`` on the newest-Python ``tests_all`` leg
(see :mod:`tests._fixtures._coverage_preinit` and the release-failure history).

No VM and no tracer: each test drives a real :class:`coverage.Coverage` over a
throwaway ``tmp_path`` data file. Coverage is deliberately never ``start()``-ed
so these tests can't nest a second tracer inside the suite's own coverage run —
the pre-init path exercises only the data-file/SQLite machinery, which needs no
collector.
"""

import sqlite3
from pathlib import Path

import coverage
import pytest

from tests._fixtures._coverage_preinit import (
    PREINIT_OUTCOME,
    active_pytest_cov,
    force_coverage_schema_init,
)


def _fresh_cov(tmp_path: Path) -> coverage.Coverage:
    """A brand-new, never-started Coverage over a unique tmp data file."""
    return coverage.Coverage(data_file=str(tmp_path / ".coverage"))


def _context_table_usable(db_path: Path) -> bool:
    """True if ``db_path`` is a coverage DB whose ``context`` table exists."""
    # timeout > 0 so the read waits out any brief write lock when the DB is the
    # live worker file coverage is still writing to (the end-to-end test below).
    con = sqlite3.connect(db_path, timeout=5)
    try:
        con.execute("SELECT * FROM context").fetchall()
    except sqlite3.OperationalError:
        return False
    finally:
        con.close()
    return True


def test_data_file_absent_before_preinit(tmp_path: Path) -> None:
    """Baseline: a freshly-started coverage has not created its DB yet.

    This is exactly the window the race exploits — the schema (and its
    ``context`` table) does not exist until the first context write, and that
    lazy first write is what two threads can race.
    """
    cov = _fresh_cov(tmp_path)
    data = cov.get_data()
    assert not Path(data.data_filename()).exists()


def test_preinit_creates_context_table(tmp_path: Path) -> None:
    """After the pre-init, the data file exists with a usable ``context`` table."""
    cov = _fresh_cov(tmp_path)

    assert force_coverage_schema_init(cov) is True

    db_path = Path(cov.get_data().data_filename())
    assert db_path.exists()
    assert _context_table_usable(db_path)


def test_preinit_marks_data_used_so_first_write_wont_erase(tmp_path: Path) -> None:
    """Pre-init must mark the data "used" so the first write can't erase it.

    Coverage's ``_start_using`` runs a lazy ``erase()`` (drop + recreate the
    file) the first time it sees ``_have_used`` still False. If pre-init created
    the schema but left ``_have_used`` False, the first real write would erase
    and recreate the DB — re-opening exactly the init window this fix closes.
    Pre-init therefore has to flip ``_have_used`` itself; assert it did, then
    confirm a real write leaves the schema intact.
    """
    cov = _fresh_cov(tmp_path)
    force_coverage_schema_init(cov)

    data = cov.get_data()
    assert data._have_used is True

    data.set_context("t")
    data.add_lines({"otto/x.py": [1, 2, 3]})
    assert _context_table_usable(Path(data.data_filename()))


def test_active_pytest_cov_none_without_plugin() -> None:
    """With no ``_cov`` plugin registered, the accessor returns None (skip)."""

    class _Manager:
        def hasplugin(self, name: str) -> bool:
            return False

    class _Config:
        pluginmanager = _Manager()

    assert active_pytest_cov(_Config()) is None


def _fake_config(controller: object) -> object:
    """A config whose ``_cov`` plugin exposes ``controller`` as cov_controller."""

    class _Plugin:
        cov_controller = controller

    class _Manager:
        def hasplugin(self, name: str) -> bool:
            return name == "_cov"

        def getplugin(self, name: str) -> object:
            return _Plugin()

    class _Config:
        pluginmanager = _Manager()

    return _Config()


def test_active_pytest_cov_returns_started_controller_cov() -> None:
    """A started controller yields its ``Coverage`` object.

    Pins the attribute walk (``cov_controller.started`` / ``.cov``); a bad edit
    to it would silently disarm the pre-init, and this fails if the walk breaks.
    """
    sentinel = object()

    class _Controller:
        started = True
        cov = sentinel

    assert active_pytest_cov(_fake_config(_Controller())) is sentinel


def test_active_pytest_cov_none_when_controller_not_started() -> None:
    """An unstarted controller (a distributed run's in-process master) yields None.

    ``started`` is False there, and that is the same flag pytest-cov's per-test
    context switch checks — so skipping the pre-init is correct: no context
    writes happen, so there is no schema-init race in that process.
    """

    class _Controller:
        started = False
        cov = object()

    assert active_pytest_cov(_fake_config(_Controller())) is None


def test_worker_data_file_preinited_under_real_coverage(request: pytest.FixtureRequest) -> None:
    """End-to-end: the root-conftest hook pre-inited THIS process's coverage DB.

    Skips when coverage is not measuring in this process (bare ``pytest`` /
    ``--no-cov``). Under the ``make release`` / ``make nox`` ``--cov`` run it
    proves ``pytest_collection_finish`` fired on this worker and left the schema
    — ``context`` table included — in place before any test ran. This is the
    piece the isolated helper tests above can't cover: the live hook wiring on a
    real xdist worker, which is exactly what kept breaking releases.
    """
    cov = active_pytest_cov(request.config)
    if cov is None:
        pytest.skip("coverage not active in this process")
    # Prove the hook itself ran and armed — not merely that a schema exists
    # (coverage would lazily build one by the first test regardless). A missing
    # or False outcome means the collection-finish hook was removed or its
    # force-init returned False: exactly the silent disarm this guards against.
    assert request.config.stash.get(PREINIT_OUTCOME, False) is True
    db_path = Path(cov.get_data().data_filename())
    assert db_path.exists()
    assert _context_table_usable(db_path)


def test_force_returns_false_when_coverage_internals_break() -> None:
    """Best-effort: a raising cov object yields False, never propagates.

    The collection hook must never abort the whole session if a coverage
    upgrade moves the internals it pokes — it degrades to "no pre-init"
    (the intermittent race returns) rather than failing every run.
    """

    class _Broken:
        def get_data(self):
            raise RuntimeError("coverage internals moved")

    assert force_coverage_schema_init(_Broken()) is False

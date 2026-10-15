"""Every in-process pytest session runs with the outer heap frozen, and the heap is thawed after.

See :mod:`tests._fixtures._gc_freeze` for why a window, and what a window
delays (never drops).
"""

import gc
import importlib.util
import os
import subprocess
import sys
from collections.abc import Callable

import _pytest.pytester
import pytest

from tests._fixtures import _gc_freeze
from tests._fixtures._gc_freeze import FrozenWindow, frozen_outer_heap, install, window_open
from tests._fixtures.paths import PROJECT_ROOT
from tests.unit.suite._inner import INNER_ARGS

pytest_plugins = ["pytester"]

STARTUP_FREEZE_COUNT = gc.get_freeze_count()
"""What the interpreter itself froze, read at collection, before any window opened.

CPython 3.12 freezes its startup heap (about 375 objects); 3.10, 3.11, 3.13
and 3.14 freeze nothing. A window's thaw drops 3.12's count to 0, so
"unfrozen" is a count no greater than this, never a count of 0. On 3.12,
once any earlier window has thawed the count to 0, the ``<=`` half of
:func:`_assert_unfrozen` is trivially true and ``window_open()`` does the
real work.
"""


def _assert_unfrozen(baseline: int) -> None:
    """No window of ours is open, and nothing beyond *baseline* (the interpreter's) is frozen."""
    assert not window_open(), "a window is still open"
    assert gc.get_freeze_count() <= baseline, "objects beyond the baseline are still frozen"


class _RecordingGc:
    """Stands in for :mod:`gc`: records each call, and counts what a freeze holds.

    *frozen* is what was frozen before any window (the interpreter's own); a
    thaw empties the permanent generation, as :func:`gc.unfreeze` does.
    """

    def __init__(self, frozen: int) -> None:
        self.frozen = frozen
        self.calls: list[str] = []

    def get_freeze_count(self) -> int:
        """What the real :mod:`gc` reports; unrecorded, since a window need not read it.

        Kept so a guard that read the count (as one once did) sees *frozen*,
        and the interpreter-frozen baseline test fails for that reason.
        """
        return self.frozen

    def freeze(self) -> None:
        self.calls.append("freeze")
        self.frozen += 1

    def unfreeze(self) -> None:
        self.calls.append("unfreeze")
        self.frozen = 0

    def collect(self) -> int:
        self.calls.append("collect")
        return 0


class _FailingCollect(_RecordingGc):
    """A collector whose closing collection raises."""

    def collect(self) -> int:
        super().collect()
        raise RuntimeError("the closing collection failed")


class _InterruptedAfterFreeze(_RecordingGc):
    """A collector whose freeze completes, then an interrupt lands before the window is marked."""

    def freeze(self) -> None:
        super().freeze()
        raise KeyboardInterrupt


class _CleanupRecorder:
    """Stands in for a ``pytest.Config``: keeps the cleanups ``install`` registers."""

    def __init__(self) -> None:
        self.cleanups: list[Callable[[], None]] = []

    def add_cleanup(self, func: Callable[[], None]) -> None:
        self.cleanups.append(func)


def test_a_window_freezes_for_its_block_then_thaws_and_collects():
    """The closing collection runs what the freeze delayed, before the caller resumes."""
    recording = _RecordingGc(frozen=0)
    with frozen_outer_heap(recording) as froze:
        recording.calls.append("block")
    assert froze is True
    assert recording.calls == ["freeze", "block", "unfreeze", "collect"]
    assert not window_open(recording)


def test_a_window_freezes_over_a_heap_the_interpreter_froze_at_startup():
    """CPython 3.12 starts with its startup heap frozen; that is no window of ours."""
    recording = _RecordingGc(frozen=375)
    with frozen_outer_heap(recording) as froze:
        recording.calls.append("block")
    assert froze is True
    assert recording.calls == ["freeze", "block", "unfreeze", "collect"]
    assert recording.frozen == 0
    assert not window_open(recording)


def test_a_window_thaws_and_collects_when_its_block_raises():
    recording = _RecordingGc(frozen=0)
    with pytest.raises(RuntimeError, match="nested session blew up"), frozen_outer_heap(recording):
        raise RuntimeError("nested session blew up")
    assert recording.calls[-2:] == ["unfreeze", "collect"]
    assert recording.frozen == 0
    assert not window_open(recording)


def test_a_failing_closing_collection_never_hides_the_blocks_exception():
    """The nested session's own failure is what the caller sees; the heap is still thawed."""
    failing = _FailingCollect(frozen=0)
    with (
        pytest.raises(ValueError, match="the nested session failed"),
        frozen_outer_heap(failing),
    ):
        raise ValueError("the nested session failed")
    assert failing.calls[-2:] == ["unfreeze", "collect"]
    assert failing.frozen == 0
    assert not window_open(failing)


def test_a_failing_closing_collection_after_a_clean_block_is_raised():
    """With nothing else to report, the collection's own failure is not swallowed."""
    failing = _FailingCollect(frozen=0)
    with (
        pytest.raises(RuntimeError, match="the closing collection failed"),
        frozen_outer_heap(failing),
    ):
        pass
    assert failing.frozen == 0
    assert not window_open(failing)


def test_an_interrupt_during_the_freeze_leaves_no_window_open():
    """A stuck open mark would silently turn every later window into a no-op."""
    interrupted = _InterruptedAfterFreeze(frozen=0)
    with pytest.raises(KeyboardInterrupt), frozen_outer_heap(interrupted):
        pytest.fail("the block ran although the freeze was interrupted")
    assert interrupted.calls == ["freeze", "unfreeze", "collect"]
    assert interrupted.frozen == 0
    assert not window_open(interrupted)


def test_a_window_inside_a_window_leaves_the_freeze_to_the_outer_one():
    """A thaw here would unfreeze the outer window's heap halfway through its session."""
    recording = _RecordingGc(frozen=0)
    with frozen_outer_heap(recording) as outer:
        with frozen_outer_heap(recording) as inner:
            recording.calls.append("block")
        assert window_open(recording), "the inner window closed the outer one"
    assert (outer, inner) == (True, False)
    assert recording.calls == ["freeze", "block", "unfreeze", "collect"]


def test_a_fresh_copy_of_the_module_sees_the_open_window():
    """A nested session may import its own copy of the module; its window must still nest."""
    spec = importlib.util.spec_from_file_location("_gc_freeze_fresh_copy", _gc_freeze.__file__)
    assert spec is not None
    assert spec.loader is not None
    fresh = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fresh)
    assert fresh.frozen_outer_heap is not frozen_outer_heap
    recording = _RecordingGc(frozen=0)
    with frozen_outer_heap(recording) as outer:
        with fresh.frozen_outer_heap(recording) as inner:
            recording.calls.append("block")
        assert fresh.window_open(recording)
    assert (outer, inner) == (True, False)
    assert recording.calls == ["freeze", "block", "unfreeze", "collect"]


def test_the_outer_session_runs_unfrozen():
    """Only nested sessions run frozen; this session's own objects are collected as ever."""
    _assert_unfrozen(STARTUP_FREEZE_COUNT)


def test_pytest_main_runs_inside_a_window(monkeypatch):
    window = pytest.main
    assert getattr(window, "freezes_the_outer_heap", False)
    before = gc.get_freeze_count()
    seen: list[tuple[bool, int]] = []

    def inside(*_a: object, **_k: object) -> int:
        seen.append((window_open(), gc.get_freeze_count()))
        return 0

    monkeypatch.setattr(window, "run", inside)
    assert pytest.main([]) == 0
    assert len(seen) == 1
    assert seen[0][0], "pytest.main ran outside a window"
    assert seen[0][1] > before, "the window froze nothing"
    _assert_unfrozen(before)


def test_a_cycle_dropped_inside_a_window_is_finalized_before_pytest_main_returns(monkeypatch):
    """An object from before the window, dropped inside it: its ``__del__`` runs in this test."""
    fired: list[str] = []

    class Finalizes:
        def __del__(self) -> None:
            fired.append("del")

    held = [Finalizes()]
    held[0].self = held[0]
    window = pytest.main
    monkeypatch.setattr(window, "run", lambda *_a, **_k: held.clear() or 0)
    assert pytest.main([]) == 0
    assert fired == ["del"]


def test_a_nested_config_neither_wraps_again_nor_restores():
    """This session installed the windows; a nested session's config must leave them be."""
    before = (pytest.main, _pytest.pytester.main)
    nested = _CleanupRecorder()
    install(nested)
    assert nested.cleanups == []
    assert (pytest.main, _pytest.pytester.main) == before


def test_the_installing_config_puts_the_entry_points_back(monkeypatch):
    raw_main, raw_pytester = object(), object()
    monkeypatch.setattr(pytest, "main", raw_main)
    monkeypatch.setattr(_pytest.pytester, "main", raw_pytester)
    owner = _CleanupRecorder()
    install(owner)
    assert isinstance(pytest.main, FrozenWindow)
    assert isinstance(_pytest.pytester.main, FrozenWindow)
    for cleanup in owner.cleanups:
        cleanup()
    assert pytest.main is raw_main
    assert _pytest.pytester.main is raw_pytester


def test_a_restore_leaves_a_later_replacement_alone(monkeypatch):
    """A test that replaced ``pytest.main`` after the install keeps its replacement."""
    monkeypatch.setattr(pytest, "main", object())
    monkeypatch.setattr(_pytest.pytester, "main", object())
    owner = _CleanupRecorder()
    install(owner)
    replacement = object()
    monkeypatch.setattr(pytest, "main", replacement)
    for cleanup in owner.cleanups:
        cleanup()
    assert pytest.main is replacement, "the restore clobbered a later replacement"


_SEQUENTIAL_DRIVER = """\
import sys

import pytest

args = [sys.argv[1], "-q", "-p", "no:cacheprovider", "-p", "no:randomly"]
codes = [int(pytest.main(args)) for _ in range(2)]
print("CODES", codes)
sys.exit(1 if any(codes) else 0)
"""


def test_a_second_top_level_session_in_one_process_runs_unfrozen(tmp_path):
    """Two sessions, one after the other, in one process: neither runs inside a window."""
    project = tmp_path / "project"
    project.mkdir()
    (project / "pytest.ini").write_text("[pytest]\n")
    (project / "conftest.py").write_text(
        "from tests._fixtures._gc_freeze import install\n\n\n"
        "def pytest_configure(config):\n    install(config)\n"
    )
    (project / "test_unfrozen.py").write_text(
        "import gc\n\n"
        "from tests._fixtures._gc_freeze import window_open\n\n"
        "STARTUP_FREEZE_COUNT = gc.get_freeze_count()\n\n\n"
        "def test_unfrozen():\n"
        "    assert not window_open()\n"
        "    assert gc.get_freeze_count() <= STARTUP_FREEZE_COUNT\n"
    )
    driver = tmp_path / "driver.py"
    driver.write_text(_SEQUENTIAL_DRIVER)
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(PROJECT_ROOT), *filter(None, [env.get("PYTHONPATH")])])
    run = subprocess.run(
        [sys.executable, str(driver), str(project)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert "CODES [0, 0]" in run.stdout, run.stdout + run.stderr


def test_a_pytester_session_runs_frozen_and_the_heap_is_thawed_after(pytester):
    assert getattr(_pytest.pytester.main, "freezes_the_outer_heap", False)
    before = gc.get_freeze_count()
    pytester.makepyfile(
        "import gc\n\n"
        "from tests._fixtures._gc_freeze import window_open\n\n\n"
        "def test_inner():\n"
        "    assert window_open()\n"
        f"    assert gc.get_freeze_count() > {before}\n"
    )
    result = pytester.runpytest_inprocess(*INNER_ARGS)
    result.assert_outcomes(passed=1)
    _assert_unfrozen(before)

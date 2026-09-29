"""The check behind a TAB: the shim starts the collect child detached, after its answer.

Design 2026-09-27 §9.2, as Chris revised it on 2026-09-28. A tests-site TAB
whose table was last checked more than ``CHECK_WINDOW_SECONDS`` ago answers
with the last-known names, then starts the same collect child the full path
starts (``otto.config.completion_cache.spawn_collect_child``), never waiting for
it; the child does the stat pass. A fresh collect lock or a cooldown stamp
means no child and leaves the check marker alone; starting one touches it, so
a burst of TABs starts one.
"""

import io
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from otto import _shim_complete as sc
from otto.config import completion_cache as cc

pytestmark = pytest.mark.interpreter_agnostic


@pytest.fixture
def popen(monkeypatch) -> list[tuple[tuple, dict]]:
    """Every ``subprocess.Popen`` call, recorded instead of made."""
    calls: list[tuple[tuple, dict]] = []

    def record(*args, **kwargs):
        calls.append((args, kwargs))
        return object()

    monkeypatch.setattr(subprocess, "Popen", record)
    return calls


@pytest.fixture
def cache_dir(tmp_path, monkeypatch) -> Path:
    """A workspace home with a relative ``OTTO_SUT_DIRS``, as a shell may export it."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "sut").mkdir()
    monkeypatch.setenv("OTTO_SUT_DIRS", "sut")
    monkeypatch.setenv("OTTO_HOME", "home")
    monkeypatch.setenv("OTTO_XDIR", "out,more")
    home = cc._cache_path().parent
    home.mkdir(parents=True)
    return home


def _tab_environ() -> dict[str, str]:
    environ = dict(os.environ)
    environ.update(_OTTO_COMPLETE="complete_bash", COMP_WORDS="otto test ", COMP_CWORD="2")
    return environ


def _without_owner(env: dict[str, str]) -> dict[str, str]:
    return {k: v for k, v in env.items() if k != cc.COLLECT_OWNER_ENV_VAR}


def test_the_shim_starts_the_child_the_full_path_starts_detached(cache_dir, popen, monkeypatch):
    """Same argv, environment and directory as the product's child, and never waited for."""
    for var in ("_OTTO_COMPLETE", "COMP_WORDS", "COMP_CWORD"):
        monkeypatch.delenv(var, raising=False)
    expected = cc._collect_child_command(cache_dir)
    assert expected is not None, "the venv has an otto console script"

    assert sc.spawn_refresh(str(cache_dir), _tab_environ(), time.time()) is True

    [(args, kwargs)] = popen
    assert list(args[0]) == expected.argv
    assert _without_owner(kwargs["env"]) == _without_owner(expected.env)
    assert kwargs["env"][cc.COLLECT_OWNER_ENV_VAR], "the child writes an owner into its lock"
    assert kwargs["env"]["OTTO_SUT_DIRS"] == str(Path.cwd() / "sut"), "made absolute"
    assert Path(kwargs["cwd"]) == expected.cwd
    assert kwargs["start_new_session"] is True
    assert kwargs["close_fds"] is True
    for stream in ("stdin", "stdout", "stderr"):
        assert kwargs[stream] is subprocess.DEVNULL


def _check_marker(cache_dir: Path) -> Path:
    return cache_dir / sc.MARKER_FILENAMES["tests"]


def test_a_started_child_restarts_the_check_window(cache_dir, popen):
    assert sc.spawn_refresh(str(cache_dir), _tab_environ(), time.time()) is True
    assert _check_marker(cache_dir).is_file()


def test_a_held_lock_starts_no_child(cache_dir, popen):
    (cache_dir / cc.COLLECT_LOCK_FILENAME).write_text("another")
    assert sc.spawn_refresh(str(cache_dir), _tab_environ(), time.time()) is False
    assert popen == []
    assert not _check_marker(cache_dir).exists(), "the running child records its own check"


def test_a_lock_its_holder_left_behind_does_not_block_the_child(cache_dir, popen):
    lock = cache_dir / cc.COLLECT_LOCK_FILENAME
    lock.write_text("dead")
    old = time.time() - cc.COLLECT_LOCK_STALE_SECONDS - 5
    os.utime(lock, (old, old))
    assert sc.spawn_refresh(str(cache_dir), _tab_environ(), time.time()) is True
    assert len(popen) == 1


def test_a_cooldown_after_a_failure_starts_no_child(cache_dir, popen):
    (cache_dir / cc.COLLECT_COOLDOWN_FILENAME).write_text("timed out\n")
    assert sc.spawn_refresh(str(cache_dir), _tab_environ(), time.time()) is False
    assert popen == []
    assert not _check_marker(cache_dir).exists()


def test_a_check_that_cannot_be_recorded_starts_no_child(cache_dir, popen, monkeypatch):
    """An unwritable check marker would start a child on every TAB of a burst: none starts."""
    _check_marker(cache_dir).write_text("")

    def refuse(*_args, **_kwargs):
        raise PermissionError("read-only")

    monkeypatch.setattr(sc.os, "utime", refuse)
    assert sc.spawn_refresh(str(cache_dir), _tab_environ(), time.time()) is False
    assert popen == []


def test_a_child_that_cannot_start_is_not_an_error(cache_dir, monkeypatch):
    def refuse(*_args, **_kwargs):
        raise OSError("no more processes")

    monkeypatch.setattr(subprocess, "Popen", refuse)
    assert sc.spawn_refresh(str(cache_dir), _tab_environ(), time.time()) is False


def test_the_lock_and_cooldown_names_track_the_product():
    assert sc.COLLECT_LOCK_FILENAME == cc.COLLECT_LOCK_FILENAME
    assert sc.COLLECT_LOCK_STALE_SECONDS == cc.COLLECT_LOCK_STALE_SECONDS
    assert sc.COLLECT_COOLDOWN_FILENAME == cc.COLLECT_COOLDOWN_FILENAME
    assert sc.COLLECT_COOLDOWN_SECONDS == cc.COLLECT_COOLDOWN_SECONDS
    assert sc.DUMP_TESTS_ENV_VAR == cc.DUMP_TESTS_ENV_VAR
    assert sc.COLLECT_OWNER_ENV_VAR == cc.COLLECT_OWNER_ENV_VAR


def test_the_entry_starts_the_refresh_only_after_the_answer_is_written(monkeypatch):
    """The shell has its candidates before any child exists."""
    from otto import _shim

    stdout = io.StringIO()
    order: list[str] = []
    monkeypatch.setattr(sys, "argv", ["otto"])
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setenv("_OTTO_COMPLETE", "complete_bash")
    monkeypatch.setattr(
        sc, "answer_or_reason", lambda environ: sc.Outcome(["test_a"], refresh="/cache")
    )

    def spawn(cache_dir, environ, now=None):
        order.append(f"spawn {cache_dir} after {stdout.getvalue()!r}")
        return True

    monkeypatch.setattr(sc, "spawn_refresh", spawn)
    with pytest.raises(SystemExit) as done:
        _shim.main()
    assert done.value.code == 0
    assert order == ["spawn /cache after 'test_a\\n'"]


def test_an_answer_that_asks_for_no_refresh_starts_nothing(monkeypatch):
    from otto import _shim

    monkeypatch.setattr(sys, "argv", ["otto"])
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    monkeypatch.setenv("_OTTO_COMPLETE", "complete_bash")
    monkeypatch.setattr(sc, "answer_or_reason", lambda environ: sc.Outcome(["host"]))
    monkeypatch.setattr(sc, "spawn_refresh", lambda *a: pytest.fail("no refresh was asked for"))
    with pytest.raises(SystemExit):
        _shim.main()

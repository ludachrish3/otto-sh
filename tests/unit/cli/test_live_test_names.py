"""With the cache off, a test-name TAB still offers names: collected live, stored nowhere.

Off bypasses the cache: nothing reads, writes or deletes a completion file. A
test-name or ``-m`` TAB then starts the collect child in its live mode, waits
for it (bounded by the collect cap) and offers what it prints. The child
collects in a process of its own because the completer's stdout is the
shell's. Any failure is an empty answer, never an exception, and with no
cooldown: there is nowhere to stamp one.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from otto import bootstrap
from otto.config import collected_tests as ct
from otto.config import completion_cache as cc
from otto.config.cache_maintenance import CACHE_FILE_NAMES, MARKER_FILENAMES
from otto.config.env import is_otto_env_var
from otto.config.user_settings import clear_user_settings_memo
from tests._fixtures.generated_project import generate_project

_OTTO = Path(sys.executable).with_name("otto")

_LIVE_TESTS = (
    "import pytest\n\n\n"
    "@pytest.mark.live_only_mark\n"
    "def test_live_one():\n    pass\n\n\n"
    "class TestLive:\n    def test_live_two(self):\n        pass\n"
)

_EVERY_COMPLETION_FILE = {
    *CACHE_FILE_NAMES,
    *MARKER_FILENAMES.values(),
    cc.COLLECT_LOCK_FILENAME,
    cc.COLLECT_COOLDOWN_FILENAME,
}


@pytest.fixture
def off_project(tmp_path, monkeypatch):
    """A generated project, its own home, and ``[cache] enabled = false`` in that home."""
    project = generate_project(tmp_path, files=3, dirs=1)
    (project / "tests" / "test_live.py").write_text(_LIVE_TESTS)
    home = tmp_path / "home"
    home.mkdir()
    (home / "settings.toml").write_text("[cache]\nenabled = false\n")  # scaffold-exempt: user-level
    monkeypatch.setenv("OTTOPATH", str(project))
    monkeypatch.setenv("OTTO_HOME", str(home))
    clear_user_settings_memo()
    bootstrap.invalidate()
    assert cc._cache_path() is None
    yield project, home
    bootstrap.invalidate()


def _projects():
    return bootstrap.get_projects()


def _completion_files(home: Path) -> list[Path]:
    return [p for p in home.rglob("*") if p.name in _EVERY_COMPLETION_FILE]


# ── (a) the real child answers ────────────────────────────────────────────────


def test_off_offers_live_names_and_markers_from_a_real_child(off_project):
    _project, home = off_project
    view = ct.completion_view(_projects())
    assert {"test_live_one", "TestLive", "TestLive::test_live_two", "test_x"} <= set(view.names)
    assert "live_only_mark" in view.markers
    assert _completion_files(home) == []


def test_the_live_child_prints_only_its_view(off_project):
    """Live mode's stdout is the parent's channel: one JSON object, and nothing else.

    The project's init module prints at import, as user code may.
    """
    project, _home = off_project
    init = project / "pylib" / "genproject_instructions.py"
    assert init.is_file()
    init.write_text(init.read_text() + "\nprint('noise from an init module')\n")
    done = subprocess.run(
        [str(_OTTO)],
        env={**os.environ, cc.DUMP_TESTS_ENV_VAR: cc.DUMP_TESTS_LIVE},
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert done.returncode == 0, done.stderr
    printed = json.loads(done.stdout)
    assert set(printed) == {"names", "markers"}
    assert "test_live_one" in printed["names"]
    assert "live_only_mark" in printed["markers"]


def test_a_live_child_that_cannot_collect_exits_1_and_prints_nothing(off_project):
    project, home = off_project
    (project / "tests" / "conftest.py").write_text("raise RuntimeError('broken conftest')\n")
    done = subprocess.run(
        [str(_OTTO)],
        env={**os.environ, cc.DUMP_TESTS_ENV_VAR: cc.DUMP_TESTS_LIVE},
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert done.returncode == 1
    assert done.stdout == ""
    assert _completion_files(home) == []


# ── (b) nothing is written for the cache ──────────────────────────────────────


def _env(project: Path, home: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not is_otto_env_var(k)}
    env.update(OTTOPATH=str(project), OTTO_HOME=str(home))
    return env


def _real_tab(env: dict[str, str], words: str, cword: int) -> list[str]:
    done = subprocess.run(
        [str(_OTTO)],
        env={
            **env,
            "_OTTO_COMPLETE": "complete_bash",
            "COMP_WORDS": words,
            "COMP_CWORD": str(cword),
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert done.returncode == 0, done.stderr
    return done.stdout.split()


def test_a_real_test_name_tab_off_offers_live_names_and_writes_no_completion_file(off_project):
    project, home = off_project
    env = _env(project, home)
    assert "test_live_one" in _real_tab(env, "otto test ", 2)
    assert "live_only_mark" in _real_tab(env, "otto test -m ", 3)
    help_ = subprocess.run(
        [str(_OTTO), "-h"], env=env, capture_output=True, text=True, check=False, timeout=120
    )
    assert help_.returncode == 0, help_.stderr
    assert _completion_files(home) == []


# ── (c) a child that fails is an empty answer ─────────────────────────────────


def _raises(exc):
    def run(argv, **kwargs):
        raise exc(argv, kwargs["timeout"]) if exc is subprocess.TimeoutExpired else exc("boom")

    return run


@pytest.mark.parametrize(
    "run",
    [
        _raises(subprocess.TimeoutExpired),
        _raises(OSError),
        lambda argv, **_k: subprocess.CompletedProcess(argv, 1, stdout='{"names": ["x"]}'),
        lambda argv, **_k: subprocess.CompletedProcess(argv, 0, stdout="not json"),
        lambda argv, **_k: subprocess.CompletedProcess(argv, 0, stdout='["a list"]'),
        lambda argv, **_k: subprocess.CompletedProcess(argv, 0, stdout='{"names": "abc"}'),
    ],
    ids=["timeout", "spawn", "exit-1", "bad-json", "not-an-object", "not-a-list"],
)
def test_a_live_child_that_fails_is_an_empty_view(off_project, monkeypatch, run):
    _project, home = off_project
    seen: list[dict] = []

    def spy(argv, **kwargs):
        seen.append(kwargs)
        return run(argv, **kwargs)

    monkeypatch.setattr(subprocess, "run", spy)
    view = ct.completion_view(_projects())
    assert (view.names, view.markers) == ([], [])
    assert len(seen) == 1
    assert _completion_files(home) == [], "no cooldown is stamped: off writes nothing"


def test_the_live_child_is_bounded_captured_and_started_in_a_scratch_dir(off_project, monkeypatch):
    _project, home = off_project
    seen: list[dict] = []

    def run(argv, **kwargs):
        seen.append({**kwargs, "cwd_existed": Path(kwargs["cwd"]).is_dir()})
        return subprocess.CompletedProcess(argv, 0, stdout='{"names": ["n"], "markers": ["m"]}')

    monkeypatch.setattr(subprocess, "run", run)
    view = ct.completion_view(_projects())
    assert (view.names, view.markers) == (["n"], ["m"])
    [call] = seen
    assert call["env"][cc.DUMP_TESTS_ENV_VAR] == cc.DUMP_TESTS_LIVE
    assert call["timeout"] == cc.COLLECT_TIMEOUT_SECONDS + cc._COLLECT_KILL_GRACE_SECONDS
    assert call["stdout"] == subprocess.PIPE
    assert call["stderr"] == subprocess.DEVNULL
    assert call["cwd_existed"]
    assert not Path(call["cwd"]).exists(), "the scratch cwd is removed afterwards"
    assert home not in Path(call["cwd"]).parents

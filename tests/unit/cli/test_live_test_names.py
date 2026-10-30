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
from tests._fixtures.generated_project import generate_project
from tests._fixtures.user_settings import set_cache_enabled

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
    monkeypatch.setenv("OTTOPATH", str(project))
    monkeypatch.setenv("OTTO_HOME", str(home))
    set_cache_enabled(False)
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


@pytest.fixture
def two_projects(tmp_path, monkeypatch):
    """A healthy project and one whose conftest fails to load, on one ``OTTOPATH``."""
    healthy = generate_project(tmp_path / "a", files=2, dirs=1, name="healthy")
    (healthy / "tests" / "test_live.py").write_text(_LIVE_TESTS)
    broken = generate_project(tmp_path / "b", files=2, dirs=1, name="broken")
    (broken / "tests" / "conftest.py").write_text("raise RuntimeError('broken conftest')\n")
    (broken / "tests" / "test_only_broken.py").write_text("def test_only_in_broken():\n    pass\n")
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("OTTOPATH", os.pathsep.join([str(healthy), str(broken)]))
    monkeypatch.setenv("OTTO_HOME", str(home))
    bootstrap.invalidate()
    yield home
    bootstrap.invalidate()


def _settle(*, on: bool) -> ct.CompletionView:
    """The answer a test-name TAB settles on: off is live every time; on, the TAB after the seed."""
    set_cache_enabled(on)
    assert (cc._cache_path() is not None) is on
    view = ct.completion_view(_projects())
    return ct.completion_view(_projects()) if on else view


def test_output_at_exit_never_reaches_the_live_childs_answer(off_project):
    """An init module that prints at interpreter exit: the TAB still offers the names.

    The child's stdout stays on ``/dev/null`` until it exits; only its answer is
    written to the parent's pipe, through the descriptor it saved.
    """
    project, home = off_project
    init = project / "pylib" / "genproject_instructions.py"
    init.write_text(
        init.read_text() + "\nimport atexit\natexit.register(lambda: print('bye from atexit'))\n"
    )
    done = subprocess.run(
        [str(_OTTO)],
        env={**os.environ, cc.DUMP_TESTS_ENV_VAR: cc.DUMP_TESTS_LIVE},
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert done.returncode == 0, done.stderr
    assert "test_live_one" in json.loads(done.stdout)["names"]
    assert "test_live_one" in _real_tab(_env(project, home), "otto test ", 2)


@pytest.mark.parametrize("on", [False, True], ids=["off", "on-cold"])
def test_output_from_an_init_module_never_reaches_a_full_path_tab(off_project, on):
    """An init module that prints on import and at exit: the shell reads only the words.

    Off, every TAB loads the init modules; on, a cold cache does. The words go
    out through the descriptor saved before the load; the process's own
    stdout is ``/dev/null`` from then until it exits.
    """
    project, home = off_project
    init = project / "pylib" / "genproject_instructions.py"
    init.write_text(
        init.read_text()
        + "\nimport atexit\nprint('hello at import')\n"
        + "atexit.register(lambda: print('bye from atexit'))\n"
    )
    set_cache_enabled(on)
    env = _env(project, home)
    for words, cword, expected in (("otto ", 1, "test"), ("otto test ", 2, "test_live_one")):
        offered = _real_tab(env, words, cword)
        assert expected in offered
        assert not {"hello", "bye", "atexit", "import"} & set(offered), offered


_NOISY_COMMAND = """
from typing import Annotated

import typer

from otto.cli.registry import cli_command


def _noisy_words(incomplete: str) -> list[str]:
    print("noisy completer")
    return ["alpha", "beta"]


@cli_command(name="noisy")
async def noisy(word: Annotated[str, typer.Argument(autocompletion=_noisy_words)] = "") -> None:
    pass
"""


def test_a_completer_that_prints_offers_only_its_words(off_project):
    """User code that runs during the TAB itself, a completer here, prints to ``/dev/null`` too."""
    project, home = off_project
    init = project / "pylib" / "genproject_instructions.py"
    init.write_text(init.read_text() + _NOISY_COMMAND)
    assert _real_tab(_env(project, home), "otto noisy ", 2) == ["alpha", "beta"]


def test_a_project_that_cannot_collect_costs_only_its_own_names_on_and_off(two_projects):
    """Off offers what on settles on: the healthy project's names, without the broken one's."""
    off = _settle(on=False)
    assert {"test_live_one", "TestLive::test_live_two"} <= set(off.names)
    assert "test_only_in_broken" not in off.names
    assert "live_only_mark" in off.markers
    assert _completion_files(two_projects) == [], "off wrote nothing"
    on = _settle(on=True)
    assert (on.names, on.markers) == (off.names, off.markers)


def test_a_live_child_that_raises_exits_1_and_prints_nothing(off_project, monkeypatch, capfd):
    """``entry()``'s child branch in live mode: an exception is the one failure with no output."""
    from otto.cli.main import entry

    _project, home = off_project

    def broken_bootstrap():
        raise RuntimeError("an init module is broken")

    monkeypatch.setattr(bootstrap, "bootstrap", broken_bootstrap)
    monkeypatch.setenv(cc.DUMP_TESTS_ENV_VAR, cc.DUMP_TESTS_LIVE)
    saved = os.dup(1)  # the child never gives fd 1 back: it is a process of its own
    try:
        with pytest.raises(SystemExit) as exited:
            entry()
    finally:
        os.dup2(saved, 1)
        os.close(saved)
    assert exited.value.code == 1
    assert capfd.readouterr().out == ""
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


def test_a_scratch_dir_that_cannot_be_removed_keeps_the_childs_answer(off_project, monkeypatch):
    """The child answered; removing its scratch cwd then fails (``EBUSY``, an NFS silly-rename).

    The answer is good and the leftover directory harmless, so the names are offered.
    """
    import errno
    import shutil

    real_rmtree = shutil.rmtree

    def busy(path) -> None:
        raise OSError(errno.EBUSY, "Device or resource busy", str(path))

    def busy_rmtree(path, *args, onerror=None, onexc=None, **kwargs):
        if not Path(path).name.startswith("otto-collect-"):
            return real_rmtree(path, *args, onerror=onerror, onexc=onexc, **kwargs)
        real_rmtree(path)
        try:  # as rmtree does: the handler runs while the error is being handled
            busy(path)
        except OSError as err:
            if onexc is not None:
                return onexc(os.rmdir, path, err)
            if onerror is not None:
                return onerror(os.rmdir, path, (OSError, err, None))
            raise

    monkeypatch.setattr(shutil, "rmtree", busy_rmtree)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda argv, **_k: subprocess.CompletedProcess(
            argv, 0, stdout='{"names": ["n"], "markers": ["m"]}'
        ),
    )
    view = ct.completion_view(_projects())
    assert (view.names, view.markers) == (["n"], ["m"])

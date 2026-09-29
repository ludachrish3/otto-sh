"""``otto test <TAB>`` offers what pytest collected: seeded once, checked behind the answer.

The first TAB with no table waits once for the collect child, which seeds the
whole tree. Every later TAB answers at once with what each file held when
last collected, stat'ing nothing the tables track, exactly as the
console-script shim does; once the check window has lapsed it asks for the
child, detached, once the answer is written, and the child re-collects
exactly what moved. Design:
``docs/superpowers/specs/2026-09-27-test-name-cache-design.md`` §9.1-§9.2 and §13.

The child's process boundary is replaced by a fake ``subprocess.run`` that
runs :func:`~otto.config.collected_tests.collect_child_main` in this process,
so every pytest session it starts is a real one over a generated repo whose
modules log their import. The end-to-end test at the bottom runs the real
``otto`` console script, detached child included.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from otto.cli.test import _markers_completer, _names_completer
from otto.config import collected_tests as tr
from otto.config import completion_cache as cc
from otto.suite import run_tests
from otto.suite.run import _refresh_tables
from tests._fixtures.import_log_repo import ImportLogRepo, logged_test

_A = "class TestA:\n" + logged_test("test_a1", "    ") + "\n" + logged_test("test_a2", "    ")
_B = logged_test("test_b1")


def _bump(path: Path, seconds: int = 5) -> None:
    """Move *path*'s mtime forward, as the seconds between two real edits would."""
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + seconds * 1_000_000_000))


def _repos():
    from otto.config import get_repos

    return get_repos()


def _tab(incomplete: str = "") -> list[str]:
    return _names_completer(None, incomplete)  # type: ignore[arg-type]  # ctx is unused


@pytest.fixture(autouse=True)
def _own_home(tmp_path, monkeypatch):
    """A home of this test's own: the collect lock and cooldown are per workspace home."""
    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))


@pytest.fixture
def repo(sut_repo, tmp_path) -> ImportLogRepo:
    shell = ImportLogRepo(tmp_path / "sut")
    shell.root = sut_repo(
        files={
            "tests/test_a.py": shell.module("test_a", _A),
            "tests/test_b.py": shell.module("test_b", _B),
            "tests/sub/conftest.py": (
                "def pytest_configure(config):\n"
                "    config.addinivalue_line('markers', 'nested_mine: from tests/sub')\n"
            ),
            "tests/sub/test_c.py": shell.module(
                "test_c", "import pytest\n\n\n@pytest.mark.nested_mine\n" + logged_test("test_c1")
            ),
        }
    )
    return shell


@pytest.fixture
def sessions(monkeypatch) -> list:
    """Every pytest session started: its files (``None`` = whole tree), directories, kind."""
    import otto.suite.run as run_module

    seen: list = []
    real = run_module._run_pytest_session

    def spy(repo, selection, run=None, **kwargs):
        files = selection.candidates
        seen.append(
            {
                "files": None if files is None else sorted(Path(c).name for c in files),
                "dirs": sorted(Path(d).name for d in selection.candidate_dirs),
                "collect_only": run is None,
            }
        )
        return real(repo, selection, run, **kwargs)

    monkeypatch.setattr(run_module, "_run_pytest_session", spy)
    return seen


@pytest.fixture
def child(monkeypatch) -> list:
    """The waited-for collect child, run in this process; each call's argv is recorded."""
    calls: list = []

    def run(argv, **kwargs):
        calls.append({"argv": argv, **kwargs})
        assert kwargs["env"][cc.DUMP_TESTS_ENV_VAR] == "1"
        code = tr.collect_child_main(_repos, _refresh_tables)
        return subprocess.CompletedProcess(argv, code)

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(cc, "_collect_refresh_requested", False)
    return calls


@pytest.fixture
def popen(monkeypatch) -> list:
    """``subprocess.Popen`` spy: records each spawn and every method called on the process."""
    spawned: list = []

    class _Process:
        def __init__(self, argv, **kwargs) -> None:
            self.calls: list[str] = []
            spawned.append({"argv": argv, "kwargs": kwargs, "process": self})

        def __getattr__(self, name):
            def record(*_a, **_k):
                self.calls.append(name)

            return record

    monkeypatch.setattr(subprocess, "Popen", _Process)
    return spawned


# ── a cold table: the one TAB that waits ──────────────────────────────────────


def test_a_cold_tab_seeds_the_whole_tree_once_and_answers(repo, sessions, child, popen):
    answer = _tab()

    assert len(child) == 1, "one waited-for child"
    assert child[0]["timeout"] == cc.COLLECT_TIMEOUT_SECONDS + cc._COLLECT_KILL_GRACE_SECONDS
    assert sessions == [{"files": None, "dirs": [], "collect_only": True}]
    assert repo.imported() == ["test_a", "test_b", "test_c"]
    assert repo.ran_tests() == []
    assert {"TestA", "TestA::test_a1", "test_a2", "test_b1", "test_c1"} <= set(answer)
    assert _tab("test_b") == ["test_b1"]

    repo.next_run()
    sessions.clear()
    assert _tab() == answer
    assert len(child) == 1, "a warm table never waits again"
    assert sessions == []
    assert not cc._collect_refresh_requested, "nothing moved: nothing to refresh"
    cc.spawn_requested_refresh()
    assert popen == []


def test_a_child_that_times_out_leaves_an_empty_answer_and_a_cooldown(repo, sessions, monkeypatch):
    calls: list = []

    def run(argv, **kwargs):
        calls.append(argv)
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    monkeypatch.setattr(subprocess, "run", run)

    assert _tab() == []
    assert cc.collect_cooldown_active()
    assert "timed out" in (cc._cache_path().parent / cc.COLLECT_COOLDOWN_FILENAME).read_text()

    assert _tab() == []
    assert len(calls) == 1, "the cooldown keeps the next TAB from waiting again"
    assert sessions == []


def test_a_failing_child_stamps_the_cooldown(repo, monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda argv, **_k: subprocess.CompletedProcess(argv, 1))

    assert _tab() == []
    assert cc.collect_cooldown_active()


def test_the_child_past_its_cap_stamps_the_cooldown_and_frees_the_lock(repo, monkeypatch):
    lock = cc._cache_path().parent / cc.COLLECT_LOCK_FILENAME
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("held")

    def exit_(code):
        raise SystemExit(code)

    monkeypatch.setattr(tr.os, "_exit", exit_)
    with pytest.raises(SystemExit) as exited:
        tr._expire_collect_child(lock)

    assert exited.value.code == 1
    assert not lock.exists()
    assert cc.collect_cooldown_active()


def _lock() -> Path:
    """The collect lock's path, its directory made (the child makes it; these fakes stand in)."""
    home = cc._cache_path().parent
    home.mkdir(parents=True, exist_ok=True)
    return home / cc.COLLECT_LOCK_FILENAME


def _exit_raises(monkeypatch) -> None:
    """``os._exit`` raises ``SystemExit`` instead, so an expiry ends only the test's call."""

    def exit_(code):
        raise SystemExit(code)

    monkeypatch.setattr(tr.os, "_exit", exit_)


def test_a_hanging_bootstrap_is_stopped_by_the_deadline(repo, monkeypatch):
    """The lock and the deadline come before the repos are loaded: an init module that hangs
    is stopped like a collection that hangs, and the next TABs cool down."""
    _exit_raises(monkeypatch)
    monkeypatch.setattr(cc, "COLLECT_TIMEOUT_SECONDS", 0.2)
    held: list[bool] = []

    def hanging_bootstrap():
        held.append(_lock().exists())
        time.sleep(30)
        return _repos()

    started = time.monotonic()
    with pytest.raises(SystemExit) as exited:
        tr.collect_child_main(hanging_bootstrap, _refresh_tables)

    assert time.monotonic() - started < 10
    assert exited.value.code == 1
    assert held == [True], "the lock was taken before the slow part"
    assert not _lock().exists()
    assert cc.collect_cooldown_active()


def test_a_bootstrap_that_raises_in_the_child_stamps_the_cooldown(repo, monkeypatch):
    """``entry()``'s child branch: the lock is held while bootstrap runs; its failure cools down."""
    import otto.bootstrap as bs
    from otto.cli.main import entry

    held: list[bool] = []

    def broken_bootstrap():
        held.append(_lock().exists())
        raise RuntimeError("an init module is broken")

    monkeypatch.setattr(bs, "bootstrap", broken_bootstrap)
    monkeypatch.setenv(cc.DUMP_TESTS_ENV_VAR, "1")

    with pytest.raises(SystemExit) as exited:
        entry()

    assert exited.value.code == 1
    assert held == [True]
    assert not _lock().exists()
    assert cc.collect_cooldown_active()
    assert "RuntimeError" in (cc._cache_path().parent / cc.COLLECT_COOLDOWN_FILENAME).read_text()


def test_a_timed_out_child_leaves_a_lock_it_never_took_alone(repo, monkeypatch):
    """Another process took the lock while the child ran; the child's timeout is not its release."""

    def run(argv, **kwargs):
        _lock().write_text("another process")
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    monkeypatch.setattr(subprocess, "run", run)

    assert _tab() == []
    assert _lock().read_text() == "another process"
    assert cc.collect_cooldown_active()


def test_a_timed_out_child_that_held_the_lock_has_it_freed(repo, monkeypatch):
    """The child took the lock and was killed before it could free it: the waiting TAB frees it."""

    def run(argv, **kwargs):
        assert cc._acquire_collect_lock(_lock(), owner=kwargs["env"][cc.COLLECT_OWNER_ENV_VAR])
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    monkeypatch.setattr(subprocess, "run", run)

    assert _tab() == []
    assert not _lock().exists()


def test_an_expiry_during_a_cache_write_leaves_no_temporary_file(repo, monkeypatch):
    _exit_raises(monkeypatch)
    home = cc._cache_path().parent
    home.mkdir(parents=True, exist_ok=True)
    real_dump = cc.json.dump

    def dump_then_expire(obj, fp, *args, **kwargs):
        real_dump(obj, fp, *args, **kwargs)
        tr._expire_collect_child(_lock())

    monkeypatch.setattr(cc.json, "dump", dump_then_expire)
    with pytest.raises(SystemExit):
        cc._atomic_write_json(home / "completion_cache.json", {"x": 1})

    assert list(home.glob(".completion_cache_*.tmp")) == []


def test_a_table_another_env_wrote_offers_nothing_when_the_seed_fails(
    repo, tmp_path, sessions, monkeypatch
):
    """An outdated table is cold: its names may be wrong in every file, so none are offered."""
    assert run_tests(["test_b1"], output_dir=tmp_path / "warm").exit_code == 0
    repo.write("pytest.ini", "[pytest]\n")
    monkeypatch.setattr(subprocess, "run", lambda argv, **_k: subprocess.CompletedProcess(argv, 1))

    assert _tab() == []
    assert cc.collect_cooldown_active()
    assert _tab() == [], "and during the cooldown"


def test_a_refresh_that_leaves_the_table_behind_cools_down(repo, tmp_path, sessions, child, popen):
    """A directory whose conftest breaks is still unlisted after the child: no respawn every TAB."""
    _warm(repo, tmp_path, sessions)
    repo.write("tests/new/conftest.py", "raise RuntimeError('broken conftest')\n")
    repo.write("tests/new/test_n.py", repo.module("test_n", logged_test("test_n1")))
    _bump(repo.root / "tests")
    _age(_check_marker(), 3600)

    _tab()
    assert tr.collect_child_main(_repos, _refresh_tables) == 1
    assert cc.collect_cooldown_active()

    _tab()
    assert cc._collect_refresh_requested, "no check was recorded"
    cc.spawn_requested_refresh()
    assert popen == [], "the cooldown holds the next refresh back"


def test_a_refresh_that_finds_a_new_dependency_succeeds(sut_repo, tmp_path, monkeypatch):
    """A record's first-seen dependency has no stat until the next read: that is not a failure.

    The table is one refresh short of current by design; the child exits 0,
    no cooldown is stamped, and the names are right.
    """
    root = sut_repo(
        files={
            "tests/base.py": "class Base:\n    def test_x(self):\n        pass\n",
            "tests/helper2.py": "class Other:\n    def test_z(self):\n        pass\n",
            "tests/test_d.py": "from base import Base\n\n\nclass TestD(Base):\n    pass\n",
        }
    )
    assert tr.collect_child_main(_repos, _refresh_tables) == 0
    test_d = root / "tests" / "test_d.py"
    test_d.write_text(
        "from base import Base\nfrom helper2 import Other\n\n\n"
        "class TestD(Base):\n    pass\n\n\nclass TestE(Other):\n    pass\n"
    )
    _bump(test_d)
    for name in ["base", "helper2", "test_d"]:
        sys.modules.pop(name, None)

    assert tr.collect_child_main(_repos, _refresh_tables) == 0

    assert not cc.collect_cooldown_active()
    [repo] = _repos()
    table = tr.read_table(repo)
    assert table is not None
    assert {"TestD::test_x", "TestE::test_z"} <= set(table.names)


@pytest.mark.parametrize("start", ["run_collect_child", "spawn_collect_child"])
def test_a_collect_child_starts_in_the_workspace_home_whatever_the_shell_cwd(
    repo, child, popen, tmp_path, monkeypatch, start
):
    """Never the shell's cwd: the child starts in the workspace home, its OTTO_* paths absolute."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OTTO_SUT_DIRS", str(repo.root.relative_to(tmp_path)))
    monkeypatch.setenv("OTTO_HOME", "home")
    cache_path = cc._cache_path()
    assert cache_path is not None
    home = cache_path.parent.absolute()

    assert getattr(cc, start)()

    [kwargs] = child or [spawn["kwargs"] for spawn in popen]
    assert Path(kwargs["cwd"]) == home
    assert home.is_dir()
    assert kwargs["env"]["OTTO_SUT_DIRS"] == str(repo.root)
    assert kwargs["env"]["OTTO_HOME"] == str(tmp_path / "home")


def test_a_check_that_cannot_be_recorded_starts_no_child(repo, popen, monkeypatch):
    """As in the shim: an unwritable check marker would start a child on every late TAB."""
    cache_dir = cc._cache_path().parent
    cache_dir.mkdir(parents=True, exist_ok=True)
    _check_marker().write_text("")

    def refuse(*_args, **_kwargs):
        raise PermissionError("read-only")

    monkeypatch.setattr(os, "utime", refuse)
    assert not cc.spawn_collect_child()
    assert popen == []


def test_a_spawn_lets_go_of_children_that_finished(repo, popen, monkeypatch):
    """The handles held for detached children do not pile up in a long-lived process."""

    class _Finished:
        def poll(self):
            return 0

    finished = _Finished()
    monkeypatch.setattr(cc, "_detached_children", [finished])

    assert cc.spawn_collect_child()

    [spawn] = popen
    assert cc._detached_children == [spawn["process"]]


def test_two_detached_children_in_a_row_collect_once(tmp_path, monkeypatch):
    """A burst of TABs: the first child takes the lock before it bootstraps; the next one leaves.

    The first child is held inside its bootstrap (its init module waits for a
    file this test writes), so the lock is certainly held when the second
    child starts: it exits 0 without bootstrapping, and a TAB spawns none.
    """
    from tests._fixtures.generated_repo import generate_repo

    repo = generate_repo(tmp_path, files=4, dirs=2)
    loads = tmp_path / "bootstraps.log"
    release = tmp_path / "release"
    init = repo / "pylib" / "genrepo_instructions.py"
    init.write_text(
        init.read_text()
        + "\nimport pathlib, time\n"
        + f"with open({str(loads)!r}, 'a') as _f:\n    _f.write('x\\n')\n"
        + "_until = time.monotonic() + 10\n"
        + f"while not pathlib.Path({str(release)!r}).exists() and time.monotonic() < _until:\n"
        + "    time.sleep(0.05)\n"
    )
    for key in [k for k in os.environ if k.startswith("OTTO_")]:
        monkeypatch.delenv(key)
    monkeypatch.setenv("OTTO_SUT_DIRS", str(repo))
    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
    for var in (cc.COMPLETION_ENV_VAR, "COMP_WORDS", "COMP_CWORD"):
        monkeypatch.delenv(var, raising=False)
    cache_dir = cc._cache_path().parent
    cache_dir.mkdir(parents=True, exist_ok=True)
    lock = cache_dir / cc.COLLECT_LOCK_FILENAME

    assert cc.spawn_collect_child()
    _wait_for(loads.exists)  # the first child is in its bootstrap
    assert lock.exists(), "the lock was taken before the bootstrap"

    assert not cc.spawn_collect_child(), "a TAB sees the lock and spawns none"
    command = cc._collect_child_command(cache_dir)
    assert command is not None
    second = subprocess.run(  # a child that started anyway, before the lock existed
        command.argv, env=command.env, capture_output=True, text=True, timeout=60, check=False
    )
    assert second.returncode == 0
    assert loads.read_text().split() == ["x"], "the second child never bootstrapped"

    release.touch()
    for spawned in cc._detached_children:  # the CLI exits; this test process reaps them
        spawned.wait(timeout=60)
    cc._detached_children.clear()
    cache = cache_dir / "completion_cache.json"
    assert str(repo) in json.loads(cache.read_text())[cc.COLLECTED_TESTS_KEY]
    assert loads.read_text().split() == ["x"]
    assert not lock.exists()


# ── a warm table: answer at once, refresh behind ──────────────────────────────


def _warm(repo: ImportLogRepo, tmp_path: Path, sessions: list) -> None:
    """Run once, so the table holds what pytest collected; then forget the run."""
    assert run_tests(["test_b1"], output_dir=tmp_path / "warm").exit_code == 0
    repo.next_run()
    sessions.clear()


def _save(repo: ImportLogRepo, rel: str, source: str) -> Path:
    path = repo.write(rel, source)
    _bump(path)
    return path


def test_a_tab_after_a_save_answers_at_once_and_checks_behind(
    repo, tmp_path, sessions, child, popen
):
    _warm(repo, tmp_path, sessions)
    _save(repo, "tests/test_b.py", repo.module("test_b", _B + "\n\n" + logged_test("test_b2")))
    _age(_check_marker(), 3600)

    answer = _tab("test_b")

    assert answer == ["test_b1"], "the file's last-known names, at once"
    assert child == [], "nothing waited on"
    assert sessions == [], "nothing collected before the answer"
    assert popen == [], "nothing spawned before the answer is written"
    assert cc._collect_refresh_requested

    cc.spawn_requested_refresh()

    [spawn] = popen
    assert spawn["kwargs"]["start_new_session"] is True
    for stream in ("stdin", "stdout", "stderr"):
        assert spawn["kwargs"][stream] is subprocess.DEVNULL
    assert spawn["kwargs"]["close_fds"] is True
    assert spawn["kwargs"]["env"][cc.DUMP_TESTS_ENV_VAR] == "1"
    assert cc.COMPLETION_ENV_VAR not in spawn["kwargs"]["env"]
    assert spawn["process"].calls == [], "never waited on"
    assert not cc._collect_refresh_requested, "one request, one spawn"
    assert not tr.tables_check_due(_check_marker().parent), "the spawn restarted the window"

    # The child, as the detached process runs it: exactly the saved file.
    assert tr.collect_child_main(_repos, _refresh_tables) == 0
    assert sessions == [{"files": ["test_b.py"], "dirs": [], "collect_only": True}]
    assert repo.imported() == ["test_b"]

    assert _tab("test_b") == ["test_b1", "test_b2"]
    assert not cc._collect_refresh_requested


def test_a_new_file_is_found_by_its_directory(repo, tmp_path, sessions, child, popen):
    _warm(repo, tmp_path, sessions)
    repo.write("tests/sub/test_d.py", repo.module("test_d", logged_test("test_d1")))
    _bump(repo.root / "tests" / "sub")
    _age(_check_marker(), 3600)

    assert "test_d1" not in _tab(), "an omission until the refresh, never a phantom"
    assert cc._collect_refresh_requested
    assert tr.collect_child_main(_repos, _refresh_tables) == 0

    assert sessions == [{"files": [], "dirs": ["sub"], "collect_only": True}]
    assert repo.imported() == ["test_c", "test_d"]
    assert "test_d1" in _tab()


def test_a_deleted_files_names_stay_until_the_check(repo, tmp_path, sessions, child):
    """Last-known, like every other record: the TAB does not stat the file to find it gone."""
    _warm(repo, tmp_path, sessions)
    (repo.root / "tests" / "test_b.py").unlink()
    _bump(repo.root / "tests")

    assert "test_b1" in _tab()
    assert not cc._collect_refresh_requested, "inside the check window"

    _age(_check_marker(), 3600)
    assert "test_b1" in _tab()
    assert cc._collect_refresh_requested
    assert tr.collect_child_main(_repos, _refresh_tables) == 0

    assert "test_b1" not in _tab()
    assert child == []


def test_a_warm_tab_stats_nothing_the_table_tracks(repo, tmp_path, sessions, child, monkeypatch):
    """O(1) in the corpus: the table's ``env`` paths are stat'ed, no test file or directory."""
    _warm(repo, tmp_path, sessions)
    tests = str(repo.root / "tests")
    stats: list[str] = []
    real = os.stat

    def spy(path, *args, **kwargs):
        stats.append(os.fspath(path))
        return real(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", spy)

    assert "test_b1" in _tab()

    assert stats, "the spy sees the TAB's stat calls"
    assert [p for p in stats if p == tests or p.startswith(tests + os.sep)] == []
    assert child == []


def test_a_held_lock_starts_no_child(repo, tmp_path, sessions, child, popen):
    _warm(repo, tmp_path, sessions)
    _save(repo, "tests/test_b.py", repo.module("test_b", _B + "\n\n" + logged_test("test_b2")))
    _age(_check_marker(), 3600)
    lock = cc._cache_path().parent / cc.COLLECT_LOCK_FILENAME
    lock.write_text(str(time.time()))

    assert _tab("test_b") == ["test_b1"]
    assert cc._collect_refresh_requested
    cc.spawn_requested_refresh()

    assert popen == [], "another collect child is at work"


def test_a_request_left_from_before_a_completion_starts_no_child(repo, popen, monkeypatch, capsys):
    """Only this completion's own completers ask for the child: an older request is dropped.

    A completer called outside ``entry()`` (a library caller, an earlier test)
    leaves its request behind; the next completion, one that asks for
    nothing, must not act on it.
    """
    from otto.cli.main import entry

    monkeypatch.setattr(cc, "_collect_refresh_requested", True)
    monkeypatch.setattr(sys, "argv", ["otto"])
    monkeypatch.setenv(cc.COMPLETION_ENV_VAR, "complete_bash")
    monkeypatch.setenv("COMP_WORDS", "otto --vers")
    monkeypatch.setenv("COMP_CWORD", "1")

    with pytest.raises(SystemExit):
        entry()

    assert "--version" in capsys.readouterr().out
    assert popen == []


def test_a_cold_tab_while_the_lock_is_held_does_not_wait(repo, sessions, child):
    lock = cc._cache_path().parent / cc.COLLECT_LOCK_FILENAME
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(str(time.time()))

    assert _tab() == []
    assert child == []


def test_a_run_before_the_refresh_collects_the_saved_file_itself(
    repo, tmp_path, sessions, child, popen
):
    _warm(repo, tmp_path, sessions)
    _save(repo, "tests/test_b.py", repo.module("test_b", _B + "\n\n" + logged_test("test_b_new")))
    _age(_check_marker(), 3600)
    assert "test_b_new" not in _tab()
    assert cc._collect_refresh_requested  # ...but the child has not run yet

    result = run_tests(["test_b_new"], output_dir=tmp_path / "out")

    assert result.exit_code == 0
    assert repo.ran_tests() == ["test_b_new"]
    assert sessions == [{"files": ["test_b.py"], "dirs": [], "collect_only": False}]
    assert "test_b_new" in _tab()


def _check_marker() -> Path:
    from otto.config.cache_maintenance import MARKER_FILENAMES

    return cc._cache_path().parent / MARKER_FILENAMES["tests"]


def _age(path: Path, seconds: float) -> None:
    then = time.time() - seconds
    os.utime(path, (then, then))


def test_a_run_restarts_the_check_window_but_not_the_ttl(repo, tmp_path, sessions):
    """A run classified the whole table before it wrote it, so the next TAB starts no
    check; only a whole-tree collection dates the table, so the 24 h TTL still runs."""
    _warm(repo, tmp_path, sessions)
    cache = cc._cache_path()
    data = json.loads(cache.read_text())
    [key] = data[cc.COLLECTED_TESTS_KEY]
    dated = int(time.time()) - 3600
    data[cc.COLLECTED_TESTS_KEY][key]["generated_at"] = dated
    cache.write_text(json.dumps(data))
    _age(_check_marker(), 3600)
    _save(repo, "tests/test_b.py", repo.module("test_b", _B + "\n\n" + logged_test("test_b2")))

    assert run_tests(["test_b2"], output_dir=tmp_path / "out").exit_code == 0

    assert sessions == [{"files": ["test_b.py"], "dirs": [], "collect_only": False}]
    assert time.time() - _check_marker().stat().st_mtime < 60, "the window restarted"
    [repo_] = _repos()
    table = tr.read_table(repo_)
    assert table is not None
    assert table.generated_at == dated, "a run that collected part of the tree dates nothing"


# ── the check behind a TAB: the collect child does the stat pass ──────────────

_BASE = "class Base:\n    def test_x(self):\n        pass\n"


@pytest.fixture
def checked(sut_repo, tmp_path, sessions) -> Path:
    """A repo whose table is current, the check marker aged past its window, sessions cleared.

    ``tests/test_d.py``'s tests come from ``tests/base.py``: a dependency.
    """
    root = sut_repo(
        files={
            "tests/base.py": _BASE,
            "tests/test_a.py": "def test_a1():\n    pass\n",
            "tests/test_d.py": "from base import Base\n\n\nclass TestD(Base):\n    pass\n",
            "tests/sub/test_c.py": "def test_c1():\n    pass\n",
        }
    )
    for _ in range(3):  # the seed, then the read that stamps the first-seen dependency
        _forget_test_modules()
        assert tr.collect_child_main(_repos, _refresh_tables) == 0
        [repo_] = _repos()
        if tr.classify(repo_, tr.read_table(repo_)).is_current:
            break
    else:
        raise AssertionError("the table never became current")
    _age(_check_marker(), 3600)
    _forget_test_modules()
    sessions.clear()
    return root


def _forget_test_modules() -> None:
    for name in ["base", "test_a", "test_c", "test_d", "test_e"]:
        sys.modules.pop(name, None)


def _check(sessions) -> tuple[list, "tr.RepoTable"]:
    assert tr.collect_child_main(_repos, _refresh_tables) == 0
    [repo_] = _repos()
    table = tr.read_table(repo_)
    assert table is not None
    assert tr.classify(repo_, table).is_current, "the check leaves the table current"
    return list(sessions), table


def test_a_check_of_a_current_table_collects_and_writes_nothing(checked, sessions):
    cache = cc._cache_path()
    before = cache.stat().st_mtime_ns

    found, _table = _check(sessions)

    assert found == []
    assert cache.stat().st_mtime_ns == before, "the table is not rewritten"
    assert time.time() - _check_marker().stat().st_mtime < 60, "but it was checked just now"


def _edit(path: Path, text: str) -> None:
    path.write_text(text)
    _bump(path)


@pytest.mark.parametrize(
    ("change", "collected", "offered", "gone"),
    [
        (
            lambda root: _edit(
                root / "tests/test_a.py", "def test_a1():\n    pass\n\n\ndef test_a2():\n    pass\n"
            ),
            [{"files": ["test_a.py"], "dirs": [], "collect_only": True}],
            "test_a2",
            None,
        ),
        (
            lambda root: (
                (root / "tests/sub/test_e.py").write_text("def test_e1():\n    pass\n"),
                _bump(root / "tests/sub"),
            ),
            [{"files": [], "dirs": ["sub"], "collect_only": True}],
            "test_e1",
            None,
        ),
        (
            lambda root: (
                (root / "tests/sub/test_c.py").unlink(),
                _bump(root / "tests/sub"),
            ),
            None,
            None,
            "test_c1",
        ),
        (
            lambda root: _edit(
                root / "tests/base.py", _BASE + "\n    def test_y(self):\n        pass\n"
            ),
            [{"files": ["test_d.py"], "dirs": [], "collect_only": True}],
            "TestD::test_y",
            None,
        ),
        (
            lambda root: (root / "pytest.ini").write_text("[pytest]\n"),
            [{"files": None, "dirs": [], "collect_only": True}],
            "test_a1",
            None,
        ),
    ],
    ids=["edited-file", "new-file", "deleted-file", "edited-dependency", "added-config"],
)
def test_a_check_refreshes_exactly_what_moved(checked, sessions, change, collected, offered, gone):
    """Each change, on its own, makes the child re-read the right files, and nothing else."""
    change(checked)

    found, table = _check(sessions)

    if collected is not None:
        assert found == collected
    if offered is not None:
        assert offered in table.names
    if gone is not None:
        assert gone not in table.names
        assert not [k for k in table.files if k.endswith("test_c.py")], "its record is dropped"
    assert time.time() - _check_marker().stat().st_mtime < 60


# ── markers ──────────────────────────────────────────────────────────────────


def test_the_markers_are_what_pytest_registered_and_applied(repo, sessions, child):
    from otto.suite.markers import OTTO_MARKERS

    offered = _markers_completer(None, "")  # type: ignore[arg-type]  # ctx is unused

    assert "nested_mine" in offered, "registered by a nested conftest"
    assert "asyncio" in offered, "registered by a plugin"
    assert set(OTTO_MARKERS) <= set(offered)
    assert _markers_completer(None, "slow or nest") == ["slow or nested_mine"]  # type: ignore[arg-type]
    assert len(child) == 1


@pytest.mark.filterwarnings("ignore::pytest.PytestUnknownMarkWarning")
def test_a_marker_only_applied_is_offered(repo, sessions, child):
    repo.write(
        "tests/test_e.py",
        repo.module(
            "test_e", "import pytest\n\n\n@pytest.mark.applied_only\n" + logged_test("test_e1")
        ),
    )

    assert "applied_only" in _markers_completer(None, "app")  # type: ignore[arg-type]


# ── the real console script ───────────────────────────────────────────────────

_OTTO = Path(sys.executable).with_name("otto")


def _env(repo: Path, home: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("OTTO_")}
    env.update(OTTO_SUT_DIRS=str(repo), OTTO_HOME=str(home))
    return env


def _real_tab(env: dict[str, str]) -> list[str]:
    done = subprocess.run(
        [str(_OTTO)],
        env={
            **env,
            "_OTTO_COMPLETE": "complete_bash",
            "COMP_WORDS": "otto test ",
            "COMP_CWORD": "2",
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert done.returncode == 0, done.stderr
    return done.stdout.split()


def _wait_for(predicate, seconds: float = 60) -> None:
    deadline = time.monotonic() + seconds
    while not predicate():
        assert time.monotonic() < deadline, "the detached child never finished"
        time.sleep(0.1)


def test_the_console_script_seeds_once_then_checks_behind_the_answer(tmp_path):
    from tests._fixtures.generated_repo import generate_repo

    repo = generate_repo(tmp_path, files=4, dirs=2)
    env = _env(repo, tmp_path / "home")
    cache = tmp_path / "home"

    assert "test_x" in _real_tab(env), "the first TAB waited for the seed"

    nested = next(repo.rglob("sub*/test_*.py"))
    nested.write_text("def test_x():\n    pass\n\ndef test_saved():\n    pass\n")
    _bump(nested)
    cache_file = next(cache.rglob("completion_cache.json"))
    lock = cache_file.parent / cc.COLLECT_LOCK_FILENAME
    from otto.config.cache_maintenance import MARKER_FILENAMES

    # The seed started the check window; let it lapse, as ten minutes would.
    _age(cache_file.parent / MARKER_FILENAMES["tests"], 3600)
    answer = _real_tab(env)
    assert "test_x" in answer
    assert "test_saved" not in answer, "last-known names: the check is behind the answer"

    def refreshed() -> bool:
        if lock.exists():
            return False
        table = json.loads(cache_file.read_text())[cc.COLLECTED_TESTS_KEY].get(str(repo), {})
        return any(
            name == "test_saved"
            for record in table.get("files", {}).values()
            for _classes, name in record.get("tests", [])
        )

    _wait_for(refreshed)
    assert "test_saved" in _real_tab(env)

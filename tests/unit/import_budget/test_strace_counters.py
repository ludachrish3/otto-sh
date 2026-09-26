"""Unit tests for the import budget's strace counters: pure text in, counts out."""

import pytest

from tests._fixtures.budget_harness import load_harness

harness = load_harness()

WS = "/tmp/fixture-root/"


def _counts(text: str, excluded: frozenset[str] = frozenset()) -> dict[str, int]:
    return harness.parse_strace(text, workspace_prefixes=[WS], excluded_paths=excluded)


def test_counts_stat_family_calls_of_the_measured_process():
    text = (
        '100 execve("/usr/bin/python3", ["python3", "-c", "..."], 0x0 /* 3 vars */) = 0\n'
        '100 newfstatat(AT_FDCWD, "/tmp/fixture-root/repo/tests", {st_mode=S_IFDIR}, 0) = 0\n'
        '100 statx(AT_FDCWD, "/usr/lib/python3.10/os.py", AT_STATX_SYNC_AS_STAT, 0x0, {}) = 0\n'
        '100 openat(AT_FDCWD, "/tmp/fixture-root/repo/a.py", O_RDONLY) = 3\n'
        '100 access("/tmp/fixture-root/x", R_OK) = -1 ENOENT (No such file or directory)\n'
    )
    assert _counts(text) == {"stat_workspace": 2, "stat_total": 3}


def test_a_child_that_execs_is_excluded_threads_are_not():
    text = (
        '100 execve("/usr/bin/python3", ["python3"], 0x0) = 0\n'
        '101 newfstatat(AT_FDCWD, "/tmp/fixture-root/thread-side", {}, 0) = 0\n'
        '102 execve("/usr/bin/git", ["git", "log"], 0x0) = 0\n'
        '102 newfstatat(AT_FDCWD, "/tmp/fixture-root/.git", {}, 0) = 0\n'
    )
    # 101 never execs, so it is a thread of the Python process and counts.
    # 102 execs git, so it is a child command and is excluded.
    assert _counts(text) == {"stat_workspace": 1, "stat_total": 1}


def test_unfinished_counts_once_and_resumed_is_skipped():
    text = (
        '100 execve("/usr/bin/python3", ["python3"], 0x0) = 0\n'
        '100 newfstatat(AT_FDCWD, "/tmp/fixture-root/a" <unfinished ...>\n'
        "100 <... newfstatat resumed>{st_mode=S_IFREG}, 0) = 0\n"
    )
    assert _counts(text) == {"stat_workspace": 1, "stat_total": 1}


def test_excluded_lib_dir_probe_counts_in_total_only():
    lib = "/tmp/fixture-root/repo/pylib"
    text = (
        '100 execve("/usr/bin/python3", ["python3"], 0x0) = 0\n'
        f'100 newfstatat(AT_FDCWD, "{lib}", {{st_mode=S_IFDIR}}, 0) = 0\n'
        f'100 newfstatat(AT_FDCWD, "{lib}/genrepo_instructions.py", {{}}, 0) = 0\n'
    )
    # The dir itself is the import system's freshness probe. A file inside it is not.
    assert _counts(text, frozenset({lib})) == {"stat_workspace": 1, "stat_total": 2}


def test_fd_relative_stat_has_no_workspace_path():
    text = (
        '100 execve("/usr/bin/python3", ["python3"], 0x0) = 0\n'
        '100 newfstatat(3, "", {st_mode=S_IFREG}, AT_EMPTY_PATH) = 0\n'
    )
    assert _counts(text) == {"stat_workspace": 0, "stat_total": 1}


def test_missing_strace_fails_by_name(monkeypatch):
    monkeypatch.setattr(harness.shutil, "which", lambda name: None)
    with pytest.raises(RuntimeError, match="apt-get install strace"):
        harness.strace_executable()


def test_ceiling_counter_allows_headroom_and_refuses_beyond_it():
    assert harness.within_ceiling(measured=110, baseline=100)
    assert not harness.within_ceiling(measured=111, baseline=100)

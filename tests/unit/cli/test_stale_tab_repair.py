"""A stale bash TAB rebuilds the cache, so the next TAB is served by the shim again."""

import os
import subprocess
import sys
from pathlib import Path

from tests._fixtures.generated_repo import generate_repo

_OTTO = Path(sys.executable).with_name("otto")


def _env(repo: Path, home: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("OTTO_")}
    env.update(OTTO_SUT_DIRS=str(repo), OTTO_HOME=str(home), PYTHONDONTWRITEBYTECODE="1")
    return env


def _tab(env: dict[str, str], words: str, cword: int) -> subprocess.CompletedProcess:
    complete_env = {
        **env,
        "_OTTO_COMPLETE": "complete_bash",
        "COMP_WORDS": words,
        "COMP_CWORD": str(cword),
    }
    return subprocess.run(
        [str(_OTTO)],
        env=complete_env,
        capture_output=True,
        text=True,
        check=False,
    )


def _tests_site_reason(env: dict[str, str]) -> str:
    from otto import _shim_complete as sc

    environ = {
        **env,
        "_OTTO_COMPLETE": "complete_bash",
        "COMP_WORDS": "otto test --tests ",
        "COMP_CWORD": "3",
    }
    for marker in Path(env["OTTO_HOME"]).rglob("completion_cache.*.ok"):
        marker.unlink()  # force the stat pass; the 60 s marker window would otherwise vouch
    return sc.answer_or_reason(environ).reason


def _arm_bootstrap_sentinel(repo: Path) -> Path:
    """Make the repo's init module touch a sentinel file when bootstrap imports it.

    Only ``otto.bootstrap.discover()``'s registration pass imports init
    modules; the completion names fast path's ``_cached_names_payload``
    reads the cached ``names`` section and never runs any repo code at all.
    So the sentinel reappearing is proof a FULL bootstrap ran, not merely
    that a cache was consulted — a cache write's mtime moving is not enough
    on its own, since a cache that was already valid is never rewritten
    even when bootstrap runs. Anchored on the INIT module rather than a
    top-level test file: a later task in this plan makes test files load
    lazily, and bootstrap will keep importing init modules regardless.
    """
    marker = repo / "bootstrap-ran"
    (repo / "pylib" / "genrepo_instructions.py").write_text(f"open({str(marker)!r}, 'a').close()\n")
    return marker


def test_a_stale_tests_tab_rebuilds_the_cache(tmp_path):
    repo = generate_repo(tmp_path, files=12, dirs=3)
    # Without a pytest config file at the SUT root, pytest's own rootdir
    # search (walked by the `--tests` completer's real collection warm --
    # see `maybe_warm_collected_tests`) lands INSIDE `repo/tests` -- the only
    # ini-like file otherwise found is none at all. Its `.pytest_cache` write
    # there retouches the very directory this test tracks for staleness --
    # not a race (the warm runs synchronously, inside this same TAB, right
    # after `write_cache`), but a deterministic extra invalidation: left
    # alone, the FIRST `--tests` TAB after an edit would rebuild and then
    # immediately re-dirty itself, costing a second full-path TAB before the
    # shim serves again. Pinning `cache_dir` outside the repo is what a real
    # workspace's own pytest config already does implicitly by giving pytest
    # a rootdir to settle on; the underlying `Repo.collect_tests` behavior is
    # tracked as a separate follow-up, not this task's product change.
    (repo / "pytest.ini").write_text(f"[pytest]\ncache_dir = {tmp_path / 'pytest_cache_out'}\n")
    marker = _arm_bootstrap_sentinel(repo)
    env = _env(repo, tmp_path / "home")
    seeded = subprocess.run([str(_OTTO), "--help"], env=env, capture_output=True, check=False)
    assert seeded.returncode == 0, seeded.stderr
    assert marker.exists(), "the seeding --help call never bootstrapped"
    marker.unlink()

    nested = next(repo.rglob("sub*/test_*.py"))
    nested.write_text("def test_x():\n    pass\n\ndef test_new():\n    pass\n")
    assert _tests_site_reason(env).startswith("stale: "), "the edit was not seen as stale"

    tab = _tab(env, "otto test --tests ", 3)
    assert tab.returncode == 0, tab.stderr

    assert marker.exists(), "the stale TAB never bootstrapped"
    assert not _tests_site_reason(env).startswith("stale"), "the stale TAB did not rebuild"


def test_a_non_stale_handover_does_not_bootstrap(tmp_path):
    """``otto nope <TAB>`` is a resolution handover (unknown command) the shim itself
    cannot answer, but the cache is fine: `entry()` must serve the fallback from the
    installed ``names`` snapshot alone, never running bootstrap.

    ``otto tunnel remove <TAB>`` would NOT prove this: its own completer
    (``_tunnel_id_completer``) calls ``get_repos()`` unconditionally to list live
    tunnels, so it bootstraps regardless of `cache_stale` — that is a property of
    a LIVE source, not of this task's repair mechanism. ``otto nope <TAB>``
    instead falls back to the top-level command LISTING, which
    ``_OttoGroup.list_commands``/``_cached_stub`` serve from the very ``names``
    snapshot `entry()`'s fast path installs (see ``_cached_names_payload``), with
    no live completer and so no bootstrap on a cache hit.
    """
    repo = generate_repo(tmp_path, files=4, dirs=2)
    marker = _arm_bootstrap_sentinel(repo)
    env = _env(repo, tmp_path / "home")
    subprocess.run([str(_OTTO), "--help"], env=env, capture_output=True, check=True)
    assert marker.exists(), "the seeding --help call never bootstrapped"
    marker.unlink()

    cache = next(Path(env["OTTO_HOME"]).rglob("completion_cache.json"))
    before = cache.stat().st_mtime_ns
    tab = _tab(env, "otto nope ", 2)
    assert tab.returncode == 0, tab.stderr
    assert cache.stat().st_mtime_ns == before
    assert not marker.exists(), "a non-stale handover must not bootstrap"


def test_a_stale_tab_that_writes_bytecode_stays_repaired(tmp_path):
    """The repairing TAB's own suites load must not re-stale the entry it writes.

    ``_env`` pins ``PYTHONDONTWRITEBYTECODE``; a real shell does not. With
    bytecode on (see ``argv_writing_test_file_bytecode``), the rebuild's
    suites load creates ``tests/__pycache__``, moving the mtime of the tests
    dir the ``tests`` key set stats. The rebuild must import before it
    snapshots the tree, or the entry it stores is stale on arrival and the
    next ``--tests`` TAB rebuilds again.
    """
    import shutil

    from tests._fixtures.generated_repo import argv_writing_test_file_bytecode

    repo = generate_repo(tmp_path, files=12, dirs=3)
    # Same reason as test_a_stale_tests_tab_rebuilds_the_cache: keep pytest's
    # own cache write out of the tracked tree.
    (repo / "pytest.ini").write_text(f"[pytest]\ncache_dir = {tmp_path / 'pytest_cache_out'}\n")
    env = {k: v for k, v in _env(repo, tmp_path / "home").items() if k != "PYTHONDONTWRITEBYTECODE"}
    argv = argv_writing_test_file_bytecode("from otto._shim import main; main()")
    seeded = subprocess.run([*argv, "--help"], env=env, capture_output=True, check=False)
    assert seeded.returncode == 0, seeded.stderr

    nested = next(repo.rglob("sub*/test_*.py"))
    nested.write_text("def test_x():\n    pass\n\ndef test_new():\n    pass\n")
    for pycache in (repo / "tests").rglob("__pycache__"):
        shutil.rmtree(pycache)
    assert _tests_site_reason(env).startswith("stale: "), "the edit was not seen as stale"

    tab_env = {
        **env,
        "_OTTO_COMPLETE": "complete_bash",
        "COMP_WORDS": "otto test --tests ",
        "COMP_CWORD": "3",
    }
    tab = subprocess.run(argv, env=tab_env, capture_output=True, text=True, check=False)
    assert tab.returncode == 0, tab.stderr
    assert (repo / "tests" / "__pycache__").is_dir(), "the repairing TAB wrote no bytecode"

    reason = _tests_site_reason(env)
    assert not reason.startswith("stale"), f"the repaired entry was stale on arrival: {reason}"

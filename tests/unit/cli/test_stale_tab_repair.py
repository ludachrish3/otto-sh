"""A stale bash TAB repairs what it read, so the next TAB is served by the shim again."""

import os
import subprocess
import sys
from pathlib import Path

from tests._fixtures.generated_repo import generate_repo

_OTTO = Path(sys.executable).with_name("otto")


def _env(repo: Path, home: Path) -> dict[str, str]:
    # Bytecode writing stays ON, as in a real shell, whatever the parent's
    # environment says: the repair must hold without anyone switching it off.
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("OTTO_") and k != "PYTHONDONTWRITEBYTECODE"
    }
    env.update(OTTO_SUT_DIRS=str(repo), OTTO_HOME=str(home))
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


def _tests_site_outcome(env: dict[str, str]):
    from otto import _shim_complete as sc

    environ = {
        **env,
        "_OTTO_COMPLETE": "complete_bash",
        "COMP_WORDS": "otto test ",
        "COMP_CWORD": "2",
    }
    for marker in Path(env["OTTO_HOME"]).rglob(sc.MARKER_FILENAMES["names"]):
        marker.unlink()  # force the names stat pass; the 60 s marker window would otherwise vouch
    return sc.answer_or_reason(environ)


def _table_is_current(repo: Path, home: Path) -> bool:
    """What the collect child's check would find: nothing the table tracks has moved."""
    from otto.config import collected_tests as ct
    from otto.config.repo import Repo

    [cache] = home.rglob("completion_cache.json")
    live = Repo(sut_dir=repo)
    table = ct.read_tables([live], home=cache.parent).get(str(repo))
    return table is not None and ct.classify(live, table).is_current


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


def test_a_stale_tests_tab_seeds_the_table(tmp_path):
    """A workspace with no test table hands a test-name TAB over as stale;
    that TAB seeds the table, and the next one is the shim's to answer.

    No pytest config of its own, as a fresh workspace has: the collect child
    (`run_collect_child`) must not write `.pytest_cache/` or `__pycache__/`
    into the tracked tests dir, or the table it writes has moved on arrival
    (#456).
    """
    repo = generate_repo(tmp_path, files=12, dirs=3)
    env = _env(repo, tmp_path / "home")
    seeded = subprocess.run([str(_OTTO), "--help"], env=env, capture_output=True, check=False)
    assert seeded.returncode == 0, seeded.stderr

    cold = _tests_site_outcome(env)
    assert cold.items is None, f"no table yet, yet: {cold}"
    assert cold.stale, f"no table yet, yet: {cold}"

    tab = _tab(env, "otto test ", 2)
    assert tab.returncode == 0, tab.stderr
    assert "test_x" in tab.stdout.split()

    warm = _tests_site_outcome(env)
    assert warm.items is not None, f"the stale TAB did not seed the table: {warm.reason}"
    assert warm.refresh is None, "the seed started the check window"
    assert _table_is_current(repo, tmp_path / "home"), "the table the TAB wrote moved on arrival"


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


def test_a_seeding_tab_that_writes_bytecode_stays_current(tmp_path):
    """A seeding TAB with bytecode on must not move the table it writes.

    With bytecode on, as in a real shell (see
    ``argv_writing_test_file_bytecode``), importing a test file would create
    ``tests/__pycache__``, moving the mtime of a tests dir the table stats.
    The collect child writes no bytecode, so the table it stores is current
    on arrival and the next test-name TAB asks for no refresh.
    """
    from tests._fixtures.generated_repo import argv_writing_test_file_bytecode

    repo = generate_repo(tmp_path, files=12, dirs=3)
    env = _env(repo, tmp_path / "home")
    argv = argv_writing_test_file_bytecode("from otto._shim import main; main()")
    seeded = subprocess.run([*argv, "--help"], env=env, capture_output=True, check=False)
    assert seeded.returncode == 0, seeded.stderr

    tab_env = {
        **env,
        "_OTTO_COMPLETE": "complete_bash",
        "COMP_WORDS": "otto test ",
        "COMP_CWORD": "2",
    }
    tab = subprocess.run(argv, env=tab_env, capture_output=True, text=True, check=False)
    assert tab.returncode == 0, tab.stderr
    assert not list((repo / "tests").rglob("__pycache__")), "the seeding TAB wrote bytecode"

    outcome = _tests_site_outcome(env)
    assert outcome.items is not None, f"the seeded table was not served: {outcome.reason}"
    assert _table_is_current(repo, tmp_path / "home"), "the seeded table moved on arrival"

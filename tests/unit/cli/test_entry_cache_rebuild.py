import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests._fixtures.generated_repo import generate_repo

_ENTRY = [sys.executable, "-c", "from otto.cli.main import entry; entry()"]

# `otto host --help`, not `otto --help`. THE ARGV IS LOAD-BEARING: root help
# and completion are the only readers of the completion cache (spec §4.2), so
# they are the only paths that check its validity or rebuild it. Ordinary
# dispatch — this subcommand's help included — never touches the cache at
# all: it neither reads nor writes it, valid or stale. A top-level edit is
# repaired by the next root `--help`; a nested one by a stale bash TAB (see
# `test_stale_tab_repair.py`) — never by ordinary dispatch.
_SUBCOMMAND_HELP = ["host", "--help"]
_ROOT_HELP = ["--help"]


def _run(env, argv=None) -> "subprocess.CompletedProcess[str]":
    p = subprocess.run(
        [*_ENTRY, *(_SUBCOMMAND_HELP if argv is None else argv)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert p.returncode == 0, p.stderr
    return p


@pytest.fixture
def repo_env(tmp_path):
    repo = generate_repo(tmp_path, files=30, dirs=3)
    env = {k: v for k, v in os.environ.items() if not k.startswith("OTTO_")}
    env["OTTO_SUT_DIRS"] = str(repo)
    env["OTTO_HOME"] = str(tmp_path / "home")
    return repo, env


def _cache_file(env) -> Path:
    caches = list(Path(env["OTTO_HOME"]).rglob("completion_cache.json"))
    assert caches, "the first invocation wrote no cache at all"
    return caches[0]


def test_ordinary_dispatch_writes_no_cache(repo_env):
    """Ordinary dispatch (spec §4.2) performs no completion-cache I/O at all.

    Migrated from ``test_second_invocation_does_not_rewrite_the_cache``: that
    test's premise — an ordinary command's SECOND run must not rewrite the
    cache the first run wrote — no longer holds, because an ordinary command
    never writes the cache in the first place. This asserts the stronger
    invariant directly: a single ordinary dispatch leaves no cache file at
    all.
    """
    _repo, env = repo_env
    _run(env)  # `otto host --help`: a subcommand, not root help
    assert not list(Path(env["OTTO_HOME"]).rglob("completion_cache.json")), (
        "ordinary dispatch wrote the completion cache"
    )


def test_ordinary_dispatch_leaves_a_stale_cache_alone(repo_env):
    """Ordinary dispatch never repairs a stale cache either.

    Migrated from ``test_editing_a_test_file_still_rebuilds_the_cache``: that
    test's premise — an ordinary command repairs the cache it just staled —
    no longer holds, since ordinary dispatch never checks or rebuilds it.
    Root help seeds the cache here; the edit stales the ``names`` section too
    (a top-level test file), so any read of the cache would have to notice.
    Ordinary dispatch reads nothing, so the entry is untouched. Repair for a
    nested edit is a stale bash TAB (``test_stale_tab_repair.py``); repair for
    a top-level edit like this one is the next root ``--help``.
    """
    repo, env = repo_env
    _run(env, argv=_ROOT_HELP)
    cache = _cache_file(env)
    first = cache.stat().st_mtime_ns
    top = next((repo / "tests").glob("test_*.py"))
    top.write_text(top.read_text() + "\n# edit\n")  # names is now stale too
    _run(env)
    assert cache.stat().st_mtime_ns == first, "ordinary dispatch rebuilt the cache"


def test_second_root_help_does_not_rewrite_the_cache(repo_env):
    """A valid entry read by root help must not be rebuilt.

    Migrated from ``test_second_invocation_does_not_rewrite_the_cache``,
    which drove the seeding invocation through ordinary dispatch (subcommand
    help). Ordinary dispatch is no longer a cache reader at all, so this now
    seeds and re-checks through root ``--help`` instead, one of the two
    surviving readers (spec §4.2).
    """
    _repo, env = repo_env
    _run(env, argv=_ROOT_HELP)
    cache = _cache_file(env)
    first = cache.stat().st_mtime_ns
    _run(env, argv=_ROOT_HELP)
    assert cache.stat().st_mtime_ns == first, "cache was rewritten on a valid hit"


def test_root_help_does_not_rebuild_after_a_nested_corpus_edit(repo_env):
    """Root help is O(names): a nested test file is not its business.

    The counterpart to the test above, and the reason that one had to move off
    ``otto --help``. A file under ``tests/sub*/`` cannot register a command —
    only top-level test files are imported — so it keys the ``tests`` section
    and not the ``names`` one. Root help reads ``names``, hits, and neither
    walks the corpus nor rewrites the entry. The ``tests`` section is
    refreshed by the next full-path invocation, or by the ``--tests``
    completer, which reads that section itself.
    """
    repo, env = repo_env
    _run(env, _ROOT_HELP)
    cache = _cache_file(env)
    first = cache.stat().st_mtime_ns

    edited = next(repo.rglob("sub*/test_*.py"))
    edited.write_text("def test_x():\n    pass\n\ndef test_added():\n    pass\n")

    _run(env, _ROOT_HELP)
    assert cache.stat().st_mtime_ns == first, "root help rebuilt for a corpus it never reads"


def test_editing_a_top_level_test_file_rebuilds_even_for_root_help(repo_env):
    """...and the other edge: a TOP-LEVEL test file does key ``names``.

    Without this, "root help ignores the corpus" would be indistinguishable
    from "root help ignores every edit". Top-level files are imported during
    registration, so one of them changing can change the command list — the
    ``names`` digest moves and root help falls back to the full load.
    """
    repo, env = repo_env
    _run(env, _ROOT_HELP)
    cache = _cache_file(env)
    first = cache.stat().st_mtime_ns

    top = repo / "tests" / "test_top0.py"
    top.write_text("def test_x():\n    pass\n\ndef test_added():\n    pass\n")

    _run(env, _ROOT_HELP)
    assert cache.stat().st_mtime_ns != first, "a top-level test edit did not invalidate names"


def test_a_cold_rebuild_is_tainted_by_a_broken_test_file(tmp_path):
    """The rebuild reads SUITES, which loads test files, and taint is computed after collection.

    ``bootstrap()`` no longer imports test files, so its result carries no error
    when the rebuild starts; the broken file is found by the suites load the
    collection triggers. A taint computed before collection would store the
    partial picture as trustworthy.
    """
    import json

    from tests._fixtures.paths import PROJECT_ROOT

    env = {k: v for k, v in os.environ.items() if not k.startswith("OTTO_")}
    env.update(
        OTTO_SUT_DIRS=str(PROJECT_ROOT / "tests" / "repo_broken"),
        OTTO_HOME=str(tmp_path / "home"),
    )
    p = _run(env, argv=_ROOT_HELP)
    assert p.stderr.count("failed to load test_syntax_error.py") == 1, p.stderr
    data = json.loads(_cache_file(env).read_text())
    assert data["sections"]["names"]["tainted"] is True


def test_a_rebuild_that_writes_bytecode_does_not_stale_itself(repo_env):
    """A root-help rebuild that creates ``tests/__pycache__`` must still store a valid entry.

    The rebuild reads SUITES, and loading them imports the top-level test
    files; with bytecode writing on (a real shell; see
    ``argv_writing_test_file_bytecode``) Python creates ``tests/__pycache__``,
    which moves the mtime of the tests dir — a directory the ``names`` key set
    stats. If that happens after the validity check memoized the stat,
    ``write_cache`` stores a digest that is already stale, and the next root
    ``--help`` pays for a full rebuild again.
    """
    import shutil

    from tests._fixtures.generated_repo import argv_writing_test_file_bytecode

    repo, env = repo_env
    env = {k: v for k, v in env.items() if k != "PYTHONDONTWRITEBYTECODE"}
    entry = "from otto.cli.main import entry; entry()"
    argv = [*argv_writing_test_file_bytecode(entry), *_ROOT_HELP]

    def root_help() -> None:
        p = subprocess.run(argv, env=env, capture_output=True, text=True, check=False)
        assert p.returncode == 0, p.stderr

    root_help()
    cache = _cache_file(env)

    top = repo / "tests" / "test_top0.py"
    top.write_text("def test_x():\n    pass\n\ndef test_added():\n    pass\n")
    # A tree nobody has imported yet: the rebuild below is the one to create it.
    for pycache in (repo / "tests").rglob("__pycache__"):
        shutil.rmtree(pycache)

    root_help()  # stale names: rebuilds, importing the test files
    assert (repo / "tests" / "__pycache__").is_dir(), "the rebuild wrote no bytecode"
    rebuilt = cache.stat().st_mtime_ns

    root_help()
    assert cache.stat().st_mtime_ns == rebuilt, "the rebuild stored an entry it had already staled"


_UNCACHEABLE_INIT = '''\
from otto.inventory import InventoryRecord, register_inventory_backend


class Uncacheable:
    """Cannot report freshness: the networked-CMDB case, so no entry can be stored."""

    def __init__(self, **kwargs):
        self.label = "uncacheable:test"
        self.supplies = frozenset({"ip"})

    def lookup(self, key):
        return InventoryRecord(ip="10.0.0.1")

    def list_keys(self):
        return ["dut-1"]

    def fingerprint(self):
        return None


register_inventory_backend("uncacheable", Uncacheable)
'''


@pytest.mark.parametrize("cacheable", [True, False], ids=["cacheable", "uncacheable"])
def test_root_help_imports_test_files_only_when_a_rebuild_can_be_stored(tmp_path, cacheable):
    """No entry can be stored → root help must not import the test files for one.

    With an inventory that cannot report freshness, no cache entry is ever
    written, so EVERY root help and TAB takes the full path. Loading the suites
    there — the rebuild's first step — would import every test file (and pytest)
    on each of them for a rebuild that cannot happen. The cacheable twin is the
    control: the same sentinel proves a real rebuild does import them.
    """
    from tests._fixtures.sutrepo import make_sut_repo

    sentinel = tmp_path / "imported"
    extra = 'libs = ["pylib"]\ninit = ["uninit"]\n'
    if not cacheable:
        extra += '\n[inventory]\nbackend = "uncacheable"\n'
    repo = make_sut_repo(
        tmp_path / "unrepo",
        name="unrepo",
        version="0.1.0",
        tests=["tests"],
        extra=extra,
        files={
            "pylib/uninit.py": _UNCACHEABLE_INIT,
            "tests/test_top0.py": (
                f"from pathlib import Path\nPath({str(sentinel)!r}).write_text('x')\n"
                "def test_x():\n    pass\n"
            ),
        },
    )
    env = {k: v for k, v in os.environ.items() if not k.startswith("OTTO_")}
    env["OTTO_SUT_DIRS"] = str(repo)
    env["OTTO_HOME"] = str(tmp_path / "home")

    _run(env, argv=_ROOT_HELP)
    assert sentinel.exists() is cacheable, (
        "a root help that can store a rebuild must import the test files for it"
        if cacheable
        else "root help imported the test files for a rebuild no entry could store"
    )

import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests._fixtures.generated_repo import generate_repo

_ENTRY = [sys.executable, "-c", "from otto.cli.main import entry; entry()"]

# `otto host --help`, not `otto --help`. THE ARGV IS LOAD-BEARING SINCE THE
# NAMES CONSUMER LANDED: root help is served from the `names` section alone
# and deliberately never validates the corpus, so it is the wrong probe for a
# question about corpus invalidation. A subcommand's help takes the full path,
# where the cache-validity check these tests are about actually runs.
_SUBCOMMAND_HELP = ["host", "--help"]
_ROOT_HELP = ["--help"]


def _run(env, argv=None):
    p = subprocess.run(
        [*_ENTRY, *(_SUBCOMMAND_HELP if argv is None else argv)],
        env=env,
        capture_output=True,
        check=False,
    )
    assert p.returncode == 0, p.stderr


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


def test_second_invocation_does_not_rewrite_the_cache(repo_env):
    """A valid entry must not be rebuilt.

    Rebuilding reads and ast.parses the whole corpus and then writes; on a
    network filesystem the write needs a commit and invalidates client cache.
    """
    _repo, env = repo_env
    _run(env)
    cache = _cache_file(env)
    first = cache.stat().st_mtime_ns
    _run(env)
    assert cache.stat().st_mtime_ns == first, "cache was rewritten on a valid hit"


def test_editing_a_test_file_still_rebuilds_the_cache(repo_env):
    """The skip must not defeat invalidation."""
    repo, env = repo_env
    _run(env)
    cache = _cache_file(env)
    first = cache.stat().st_mtime_ns

    edited = next(repo.rglob("sub*/test_*.py"))
    edited.write_text("def test_x():\n    pass\n\ndef test_added():\n    pass\n")

    _run(env)
    assert cache.stat().st_mtime_ns != first, "cache went stale after a corpus edit"


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

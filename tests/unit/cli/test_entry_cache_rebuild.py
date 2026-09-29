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
    Root help seeds the cache here; the edit stales the ``tests`` section,
    so any read of the whole cache would have to notice. Ordinary dispatch
    reads nothing, so the entry is untouched. Repair for a test-file edit is a
    stale bash TAB (``test_stale_tab_repair.py``); repair for an init-module
    edit is the next root ``--help``.
    """
    repo, env = repo_env
    _run(env, argv=_ROOT_HELP)
    cache = _cache_file(env)
    first = cache.stat().st_mtime_ns
    top = next((repo / "tests").glob("test_*.py"))
    top.write_text(top.read_text() + "\n# edit\n")  # the tests section is now stale
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
    ``otto --help``. A test file cannot register a command, so it keys the
    ``tests`` section and not the ``names`` one. Root help reads ``names``,
    hits, and neither walks the corpus nor rewrites the entry. The ``tests``
    section is refreshed by a stale TAB on ``otto test``'s NAMES, whose
    completer reads that section itself.
    """
    repo, env = repo_env
    _run(env, _ROOT_HELP)
    cache = _cache_file(env)
    first = cache.stat().st_mtime_ns

    edited = next(repo.rglob("sub*/test_*.py"))
    edited.write_text("def test_x():\n    pass\n\ndef test_added():\n    pass\n")

    _run(env, _ROOT_HELP)
    assert cache.stat().st_mtime_ns == first, "root help rebuilt for a corpus it never reads"


def test_editing_a_top_level_test_file_does_not_rebuild_for_root_help(repo_env):
    """...and a TOP-LEVEL test file is no different: no test file keys ``names``.

    Test files are never imported to register anything, so no edit to one can
    change the command list. The control is an init-module edit, which does
    move the ``names`` digest: without it, "root help ignores a test edit"
    would be indistinguishable from "root help ignores every edit".
    """
    repo, env = repo_env
    _run(env, _ROOT_HELP)
    cache = _cache_file(env)
    first = cache.stat().st_mtime_ns

    top = repo / "tests" / "test_top0.py"
    top.write_text("def test_x():\n    pass\n\ndef test_added():\n    pass\n")
    _run(env, _ROOT_HELP)
    assert cache.stat().st_mtime_ns == first, "a top-level test edit invalidated names"

    init = repo / "pylib" / "genrepo_instructions.py"
    init.write_text(init.read_text() + "\n# edited\n")
    _run(env, _ROOT_HELP)
    assert cache.stat().st_mtime_ns != first, "an init-module edit did not invalidate names"


def test_a_rebuild_writes_no_test_table(repo_env):
    """Root help's rebuild never reads a test file: the per-file test table stays unwritten.

    Only a pytest collection writes the table (a run, or the collect child a
    test-name TAB starts), so the rebuild neither seeds nor refreshes it.
    """
    import json

    from otto.config.completion_cache import COLLECTED_TESTS_KEY

    repo_dir, env = repo_env
    _run(env, argv=_ROOT_HELP)
    namespace = json.loads(_cache_file(env).read_text()).get(COLLECTED_TESTS_KEY, {})
    assert str(repo_dir) not in namespace


def test_the_collect_child_seeds_a_table_this_process_finds_current(repo_env):
    """The collect child's table is one any other otto process on this install trusts.

    The ``env`` it stored (python, pytest and otto versions, the
    site-packages stat) is the one a ``classify`` in THIS process computes,
    and the child writes nothing to stdout, the shell's channel.
    """
    import json

    from otto.config import collected_tests as ct
    from otto.config.completion_cache import COLLECTED_TESTS_KEY, DUMP_TESTS_ENV_VAR
    from otto.config.repo import Repo

    repo_dir, env = repo_env
    _run(env, argv=_ROOT_HELP)  # a cache home for the child to write into
    child = _run({**env, DUMP_TESTS_ENV_VAR: "1"}, argv=[])
    assert child.stdout == ""
    raw = json.loads(_cache_file(env).read_text())[COLLECTED_TESTS_KEY][str(repo_dir)]
    repo = Repo(sut_dir=repo_dir)
    table = ct.table_from_json(repo.sut_dir, raw)
    assert table is not None
    test_files = sorted(k for k in table.files if Path(k).name != "conftest.py")
    assert test_files == sorted(str(p) for p in (repo_dir / "tests").rglob("test_*.py"))
    assert ct.classify(repo, table).is_current


def test_the_collect_child_collects_as_a_run_does(repo_env):
    """The child sets the repo up as ``otto test`` does before collecting.

    A test module that imports a module from the repo's ``libs`` is collected
    like any other, not recorded as a ``ModuleNotFoundError``: the table must
    hold what a run would find.
    """
    import json

    from otto.config.completion_cache import COLLECTED_TESTS_KEY, DUMP_TESTS_ENV_VAR

    repo_dir, env = repo_env
    (repo_dir / "pylib" / "genrepo_helpers.py").write_text("GREETING = 'hi'\n")
    uses_lib = repo_dir / "tests" / "test_uses_lib.py"
    uses_lib.write_text("from genrepo_helpers import GREETING\n\n\ndef test_greets():\n    pass\n")
    _run(env, argv=_ROOT_HELP)
    _run({**env, DUMP_TESTS_ENV_VAR: "1"}, argv=[])
    raw = json.loads(_cache_file(env).read_text())[COLLECTED_TESTS_KEY][str(repo_dir)]
    record = raw["files"][str(uses_lib)]
    assert record["error"] is None
    assert [name for _classes, name in record["tests"]] == ["test_greets"]


def test_a_cold_rebuild_never_imports_a_broken_test_file(tmp_path):
    """A rebuild parses test files statically; a broken one neither warns nor taints.

    The rebuild used to import the top-level test files to register suites,
    so a file that failed to import tainted the ``names`` section. Nothing
    registers from a test file any more, and the static parse skips a file it
    cannot parse, so the entry is stored clean.
    """
    import json

    from tests._fixtures.paths import PROJECT_ROOT

    env = {k: v for k, v in os.environ.items() if not k.startswith("OTTO_")}
    env.update(
        OTTO_SUT_DIRS=str(PROJECT_ROOT / "tests" / "repo_broken"),
        OTTO_HOME=str(tmp_path / "home"),
    )
    p = _run(env, argv=_ROOT_HELP)
    assert "test_syntax_error.py" not in p.stderr, p.stderr
    data = json.loads(_cache_file(env).read_text())
    assert data["sections"]["names"]["tainted"] is False


def test_a_rebuild_writes_no_bytecode_into_the_tests_tree(repo_env):
    """A root-help rebuild never imports a test file, so it creates no ``tests/__pycache__``.

    With bytecode writing on (a real shell; see
    ``argv_writing_test_file_bytecode``) an import of a test file would create
    ``tests/__pycache__``, moving the mtime of a directory the ``tests`` key
    set stats, and the entry just stored would be stale on arrival. The
    rebuild is triggered by an init-module edit; the control is that it did
    rebuild.
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
    first = cache.stat().st_mtime_ns
    for pycache in (repo / "tests").rglob("__pycache__"):
        shutil.rmtree(pycache)

    init = repo / "pylib" / "genrepo_instructions.py"
    init.write_text(init.read_text() + "\n# edited\n")
    root_help()  # stale names: rebuilds
    assert cache.stat().st_mtime_ns != first, "the init edit did not rebuild the cache"
    assert not (repo / "tests" / "__pycache__").exists(), "the rebuild imported a test file"
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
def test_root_help_never_imports_test_files(tmp_path, cacheable):
    """Root help imports no test file, whether or not a rebuild can be stored.

    With an inventory that cannot report freshness, no cache entry is ever
    written, so EVERY root help and TAB takes the full path; with one that
    can, root help rebuilds. Neither imports a test file (or pytest): names
    come from a static parse, and nothing registers from a test file.
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
    assert not sentinel.exists(), "root help imported a test file"
    if cacheable:
        _cache_file(env)  # the control: this root help did rebuild the cache


_AUDITED_ENTRY = """\
import sys
seen = []
def hook(event, args):
    if event == "open" and isinstance(args[0], str):
        seen.append(("open", args[0]))
    elif event in ("os.scandir", "os.listdir") and args and isinstance(args[0], str):
        seen.append((event, args[0]))
sys.addaudithook(hook)
from otto.cli.main import entry
try:
    entry()
finally:
    with open({out!r}, "w") as fh:
        fh.write("\\n".join(f"{{e}} {{p}}" for e, p in seen))
"""


def test_a_cold_rebuild_opens_and_lists_nothing_under_the_tests_dirs(repo_env, tmp_path):
    """The rebuild does no corpus I/O: test names come only from pytest's collections.

    Root help on a cold home rebuilds every section; not one of them reads a
    test file or lists a tests directory. (Stats are not audited; the tests
    site's cost is pinned in the import-budget tests.)
    """
    repo, env = repo_env
    seen_file = tmp_path / "audit.txt"
    argv = [sys.executable, "-c", _AUDITED_ENTRY.format(out=str(seen_file)), *_ROOT_HELP]
    p = subprocess.run(argv, env=env, capture_output=True, text=True, check=False)
    assert p.returncode == 0, p.stderr
    _cache_file(env)  # the control: this root help did rebuild the cache
    tests_dir = str(repo / "tests")
    under = [line for line in seen_file.read_text().splitlines() if tests_dir in line]
    assert under == [], under[:10]

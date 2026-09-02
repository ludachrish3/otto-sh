"""Generate a synthetic sut-dir repo of a requested shape.

Deterministic by construction: the harness creates what it measures, so a
repo-bearing import-budget surface stays as host-independent as the rest of the
table.

`dirs` scales independently of `files` because a stat-only walk has no per-file
audit signal — it is observable only per directory, via `os.scandir`. `top_level`
is separate because only top-level test files can register (`iter_test_files` is
non-recursive), which is the distinction the names cache section rests on.
"""

from pathlib import Path

from tests._fixtures.sutrepo import make_sut_repo

_EXTRA = """\
libs = ["pylib"]
init = ["{name}_instructions"]
"""

_TEST_BODY = "def test_x():\n    pass\n"


def generate_repo(
    root: Path,
    *,
    files: int,
    dirs: int,
    top_level: int = 2,
    name: str = "genrepo",
) -> Path:
    """Write a sut-dir repo under *root*; return the path for ``OTTO_SUT_DIRS``.

    *files* nested test files spread across *dirs* subdirectories, plus
    *top_level* test files directly in the tests dir.
    """
    # Through `make_sut_repo`, not a hand-rolled write: `.otto/settings.toml`
    # has exactly one spelling in the suite (tests/_fixtures/sutrepo.py), and
    # `tests/unit/test_sutrepo_scaffold_policy.py` enforces it.
    repo = make_sut_repo(
        root / name,
        name=name,
        version="0.1.0",
        tests=["tests"],
        extra=_EXTRA.format(name=name),
        files={f"pylib/{name}_instructions.py": ""},
    )

    tests_root = repo / "tests"
    tests_root.mkdir(parents=True, exist_ok=True)
    for d in range(dirs):
        (tests_root / f"sub{d}").mkdir(exist_ok=True)
    for i in range(top_level):
        (tests_root / f"test_top{i}.py").write_text(_TEST_BODY)
    for i in range(files):
        subdir = tests_root / f"sub{i % dirs}"
        (subdir / f"test_{i}.py").write_text(_TEST_BODY)
    return repo

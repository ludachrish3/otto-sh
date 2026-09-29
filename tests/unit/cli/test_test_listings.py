"""``otto test --list-tests``, ``--list-markers`` and ``otto -n test``: what pytest collects.

The listings and the dry run come from the collection path a run takes
(``otto.suite.run``), with ``--collect-only``: pytest is the only thing that
knows which tests exist, so what they print is what it collected, and
nothing runs. Design: ``docs/superpowers/specs/2026-09-27-test-name-cache-design.md``
§9.4 and §11.5.
"""

import itertools
import logging
import re
import sys

import pytest

from otto.config.collected_tests import classify, read_table
from tests._fixtures.import_log_repo import ImportLogRepo, logged_test

_TREE_GLYPHS = "│├└─ "


def _lines(output: str) -> list[str]:
    return [line.strip(_TREE_GLYPHS) for line in output.splitlines()]


def _the_repo():
    import otto.config

    [only] = otto.config.get_repos()
    return only


@pytest.fixture
def sessions(monkeypatch) -> list[list[str]]:
    """The argv of every pytest session started from here on."""
    seen: list[list[str]] = []
    real = pytest.main

    def counting(args, *rest, **kwargs):
        seen.append(list(args))
        return real(args, *rest, **kwargs)

    monkeypatch.setattr(pytest, "main", counting)
    return seen


# ── The dry run ──────────────────────────────────────────────────────────────


def test_a_cold_dry_run_is_one_collection_that_prints_what_it_found_and_seeds_the_table(
    otto_test_cli, sut_repo, sessions
):
    sut_repo(
        files={
            "tests/test_a.py": "class TestA:\n    def test_a1(self): pass\n",
            "tests/test_b.py": "def test_b1(): pass\n",
        }
    )

    result = otto_test_cli(["-n", "test", "TestA"])

    assert result.exit_code == 0, result.output
    assert len(sessions) == 1
    assert "--collect-only" in sessions[0]
    lines = _lines(result.output)
    assert "dry run: pytest collected these tests; nothing ran" in lines
    assert lines.index("TestA") < lines.index("test_a1")
    assert "test_b1" not in lines
    table = read_table(_the_repo())
    assert table is not None
    assert {"TestA", "test_a1", "test_b1"} <= set(table.names)


def test_a_warm_dry_run_imports_only_the_file_holding_the_name(
    otto_test_cli, sut_repo, tmp_path, sessions
):
    shell = ImportLogRepo(tmp_path / "sut")
    shell.root = sut_repo(
        files={
            "tests/test_a.py": shell.module(
                "test_a", "class TestA:\n" + logged_test("test_a1", "    ")
            ),
            "tests/test_b.py": shell.module("test_b", logged_test("test_b1")),
        }
    )
    assert otto_test_cli(["test", "--list-tests"], lab=None).exit_code == 0
    shell.next_run()
    sessions.clear()

    result = otto_test_cli(["-n", "test", "TestA"])

    assert result.exit_code == 0, result.output
    assert shell.imported() == ["test_a"]
    assert shell.ran_tests() == []
    assert [s for s in sessions if "--collect-only" not in s] == []
    assert "test_a1" in _lines(result.output)


def test_the_dry_run_expands_parametrizations_and_files_an_inherited_test_under_its_subclass(
    otto_test_cli, sut_repo
):
    sut_repo(
        files={
            "tests/test_p.py": (
                "import pytest\n"
                "class TestBase:\n    def test_x(self): pass\n"
                "class TestChild(TestBase):\n    pass\n"
                "@pytest.mark.parametrize('n', [1, 2])\n"
                "def test_p(n): pass\n"
            )
        }
    )

    result = otto_test_cli(["-n", "test", "TestChild", "test_p"])

    assert result.exit_code == 0, result.output
    lines = _lines(result.output)
    assert lines.index("TestChild") < lines.index("test_x")
    assert "TestBase" not in lines
    assert "test_p[1]" in lines
    assert "test_p[2]" in lines


def test_the_dry_run_evaluates_the_marker_expression(otto_test_cli, sut_repo):
    sut_repo(
        files={
            "tests/test_h.py": (
                "import pytest\n"
                "class TestH:\n"
                "    @pytest.mark.slow\n"
                "    def test_heavy(self): pass\n"
                "    def test_light(self): pass\n"
            ),
            "pyproject.toml": "[tool.pytest.ini_options]\nmarkers = ['slow']\n",
        }
    )

    result = otto_test_cli(["-n", "test", "TestH", "-m", "slow"])

    assert result.exit_code == 0, result.output
    lines = _lines(result.output)
    assert "test_heavy" in lines
    assert "test_light" not in lines


def test_the_dry_run_refuses_a_typo_with_did_you_mean(otto_test_cli, sut_repo):
    sut_repo(files={"tests/test_h.py": "def test_alpha(): pass\n"})

    result = otto_test_cli(["-n", "test", "test_alhpa"])

    assert result.exit_code == 2
    flat = " ".join(result.output.replace("│", " ").split())
    assert "'test_alhpa' (did you mean: test_alpha" in flat


def test_the_dry_run_runs_nothing(otto_test_cli, sut_repo, tmp_path):
    witness = tmp_path / "ran"
    sut_repo(
        files={
            "tests/test_w.py": (
                "import pathlib\n"
                f"def test_w():\n    pathlib.Path({str(witness)!r}).write_text('x')\n"
            )
        }
    )

    result = otto_test_cli(["-n", "test", "test_w"])

    assert result.exit_code == 0, result.output
    assert "test_w" in _lines(result.output)
    assert not witness.exists()


def test_a_dry_run_that_matches_nothing_says_so_like_a_run(otto_test_cli, sut_repo):
    sut_repo(files={"tests/test_h.py": "def test_light(): pass\n"})

    result = otto_test_cli(["-n", "test", "test_light", "-m", "slow"])

    assert result.exit_code == 1
    assert "No tests matched the selection." in result.output
    assert "pytest collected these tests" not in result.output


# ── --list-tests ─────────────────────────────────────────────────────────────


def test_an_unfiltered_listing_writes_the_per_file_table(otto_test_cli, sut_repo):
    sut_repo(files={"tests/test_g.py": "def test_g(): pass\n"})

    assert otto_test_cli(["test", "--list-tests"], lab=None).exit_code == 0

    repo = _the_repo()
    table = read_table(repo)
    assert table is not None
    assert "test_g" in table.names
    assert classify(repo, table).is_current


def test_an_unfiltered_listing_rereads_a_file_no_stat_says_changed(
    otto_test_cli, sut_repo, sessions
):
    """The remedy the docs give for a test decided by a bare imported value: list them all."""
    sut = sut_repo(
        files={
            "tests/cfg.py": "FLAG = False\n",
            "tests/test_f.py": (
                "from cfg import FLAG\n"
                "if FLAG:\n    def test_flagged(): pass\n"
                "def test_plain(): pass\n"
            ),
        }
    )
    assert otto_test_cli(["test", "--list-tests"], lab=None).exit_code == 0
    (sut / "tests" / "cfg.py").write_text("FLAG = True\n")
    # A new otto process would import cfg afresh. This one kept it: a session
    # evicts what its record names, and a bare value is what no record follows.
    sys.modules.pop("cfg", None)
    repo = _the_repo()
    before = read_table(repo)
    assert before is not None
    assert "test_flagged" not in before.names
    assert classify(repo, before).is_current, "the hole: nothing the table follows moved"

    result = otto_test_cli(["test", "--list-tests"], lab=None)

    assert result.exit_code == 0, result.output
    assert "test_flagged" in _lines(result.output)
    after = read_table(repo)
    assert after is not None
    assert "test_flagged" in after.names


def test_a_listing_collects_in_source_order_and_writes_no_pytest_cache(
    otto_test_cli, sut_repo, sessions
):
    """Collection order is the order a ``--no-random`` run follows; the repo gains nothing."""
    sut = sut_repo(files={"tests/test_o.py": "def test_z(): pass\ndef test_a(): pass\n"})

    result = otto_test_cli(["test", "--list-tests"], lab=None)

    assert result.exit_code == 0, result.output
    [argv] = sessions
    pairs = list(itertools.pairwise(argv))
    assert ("-p", "no:randomly") in pairs
    assert ("-p", "no:cacheprovider") in pairs
    lines = _lines(result.output)
    assert lines.index("test_z") < lines.index("test_a")
    assert not list(sut.rglob(".pytest_cache"))


def test_a_listing_from_beside_the_repo_loads_no_conftest_above_it(
    otto_test_cli, sut_repo, tmp_path, monkeypatch
):
    """Collection stops at the repo root, whatever directory otto runs from.

    With no pytest config, pytest roots a session at the common ancestor of
    the cwd and the test directories, and would load that directory's
    conftest (and scan every entry below it).
    """
    sut_repo(files={"tests/test_a.py": "def test_ok(): pass\n"})
    (tmp_path / "conftest.py").write_text("raise RuntimeError('outside the repo')\n")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    result = otto_test_cli(["test", "--list-tests"], lab=None)

    assert result.exit_code == 0, result.output
    assert "test_ok" in _lines(result.output)


def test_a_listing_whose_collection_cannot_finish_says_why_and_fails(
    otto_test_cli, sut_repo, caplog
):
    sut_repo(
        files={
            "tests/conftest.py": "raise RuntimeError('boom')\n",
            "tests/test_a.py": "def test_a(): pass\n",
        }
    )

    with caplog.at_level(logging.ERROR):
        result = otto_test_cli(["test", "--list-tests"], lab=None)

    assert result.exit_code != 0
    assert "Could not collect the tests of repo 'sut'" in caplog.text
    assert "boom" in caplog.text


# ── --list-markers ───────────────────────────────────────────────────────────

_MARKED = {
    "pyproject.toml": "[tool.pytest.ini_options]\nmarkers = ['declared_only: declared, unused']\n",
    "tests/test_m.py": "import pytest\n@pytest.mark.applied_only\ndef test_m(): pass\n",
}


# An applied marker no config declares is what pytest warns about, and this suite
# turns warnings into errors: a user's session only warns.
_APPLIED_ONLY_WARNS = pytest.mark.filterwarnings("ignore::pytest.PytestUnknownMarkWarning")


@_APPLIED_ONLY_WARNS
def test_list_markers_on_a_cold_table_is_one_collection(otto_test_cli, sut_repo, sessions):
    sut_repo(files=_MARKED)

    result = otto_test_cli(["test", "--list-markers"], lab=None)

    assert result.exit_code == 0, result.output
    assert len(sessions) == 1
    assert "--collect-only" in sessions[0]
    assert "applied_only" in result.output


@_APPLIED_ONLY_WARNS
def test_list_markers_on_a_warm_table_collects_nothing_and_shows_what_pytest_knows(
    otto_test_cli, sut_repo, sessions
):
    """Declared in the config, registered by a plugin, applied without either, and otto's."""
    sut = sut_repo(files=_MARKED)
    assert otto_test_cli(["test", "--list-tests"], lab=None).exit_code == 0
    # A file saved since: still no collection; its markers are the last-known ones.
    (sut / "tests" / "test_m.py").write_text(_MARKED["tests/test_m.py"] + "\n\n")
    sessions.clear()

    result = otto_test_cli(["test", "--list-markers"], lab=None)

    assert result.exit_code == 0, result.output
    assert sessions == []
    for marker in ("declared_only", "asyncio", "applied_only"):
        assert marker in result.output
    assert "otto (built in)" in result.output
    # otto's markers once, in otto's panel: never bare, as a repo's panel lists names.
    assert not re.search(r"• (ensure|retry)\s", result.output)
    assert "• retry(n)" in result.output

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
from tests._fixtures.import_log_project import ImportLogProject, logged_test
from tests.unit.cli.conftest import _flat

_TREE_GLYPHS = "│├└─ "


def _lines(output: str) -> list[str]:
    return [line.strip(_TREE_GLYPHS) for line in output.splitlines()]


def _the_project():
    import otto.bootstrap

    [only] = otto.bootstrap.get_projects()
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
    otto_test_cli, one_project, sessions
):
    one_project(
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
    table = read_table(_the_project())
    assert table is not None
    assert {"TestA", "test_a1", "test_b1"} <= set(table.names)


def test_a_warm_dry_run_collects_the_whole_tree_and_lists_only_the_name(
    otto_test_cli, one_project, tmp_path, sessions
):
    shell = ImportLogProject(tmp_path / "sut")
    shell.root = one_project(
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
    assert shell.imported() == ["test_a", "test_b"]
    assert shell.ran_tests() == []
    assert [s for s in sessions if "--collect-only" not in s] == []
    assert "test_a1" in _lines(result.output)
    assert "test_b1" not in _lines(result.output)


def test_the_dry_run_expands_parametrizations_and_files_an_inherited_test_under_its_subclass(
    otto_test_cli, one_project
):
    one_project(
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


def test_the_dry_run_evaluates_the_marker_expression(otto_test_cli, one_project):
    one_project(
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


def test_the_dry_run_refuses_a_typo_with_did_you_mean(otto_test_cli, one_project):
    one_project(files={"tests/test_h.py": "def test_alpha(): pass\n"})

    result = otto_test_cli(["-n", "test", "test_alhpa"])

    assert result.exit_code == 2
    flat = " ".join(result.output.replace("│", " ").split())
    assert "'test_alhpa' (did you mean: test_alpha" in flat


def test_the_dry_run_runs_nothing(otto_test_cli, one_project, tmp_path):
    witness = tmp_path / "ran"
    one_project(
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


def test_a_dry_run_that_matches_nothing_says_so_like_a_run(otto_test_cli, one_project):
    one_project(files={"tests/test_h.py": "def test_light(): pass\n"})

    result = otto_test_cli(["-n", "test", "test_light", "-m", "slow"])

    assert result.exit_code == 1
    assert "No tests matched the selection." in result.output
    assert "pytest collected these tests" not in result.output


# ── --list-tests ─────────────────────────────────────────────────────────────


def test_an_unfiltered_listing_writes_the_per_file_table(otto_test_cli, one_project):
    one_project(files={"tests/test_g.py": "def test_g(): pass\n"})

    assert otto_test_cli(["test", "--list-tests"], lab=None).exit_code == 0

    project = _the_project()
    table = read_table(project)
    assert table is not None
    assert "test_g" in table.names
    assert classify(project, table).is_current


def test_an_unfiltered_listing_rereads_a_file_no_stat_says_changed(
    otto_test_cli, one_project, sessions
):
    """The remedy the docs give for a test decided by a bare imported value: list them all."""
    sut = one_project(
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
    project = _the_project()
    before = read_table(project)
    assert before is not None
    assert "test_flagged" not in before.names
    assert classify(project, before).is_current, "the hole: nothing the table follows moved"

    result = otto_test_cli(["test", "--list-tests"], lab=None)

    assert result.exit_code == 0, result.output
    assert "test_flagged" in _lines(result.output)
    after = read_table(project)
    assert after is not None
    assert "test_flagged" in after.names


def test_a_listing_collects_in_source_order_and_writes_no_pytest_cache(
    otto_test_cli, one_project, sessions
):
    """Collection order is the order a ``--no-random`` run follows; the project gains nothing."""
    sut = one_project(files={"tests/test_o.py": "def test_z(): pass\ndef test_a(): pass\n"})

    result = otto_test_cli(["test", "--list-tests"], lab=None)

    assert result.exit_code == 0, result.output
    [argv] = sessions
    pairs = list(itertools.pairwise(argv))
    assert ("-p", "no:randomly") in pairs
    assert ("-p", "no:cacheprovider") in pairs
    lines = _lines(result.output)
    assert lines.index("test_z") < lines.index("test_a")
    assert not list(sut.rglob(".pytest_cache"))


def test_a_listing_from_beside_the_project_loads_no_conftest_above_it(
    otto_test_cli, one_project, tmp_path, monkeypatch
):
    """Collection stops at the project root, whatever directory otto runs from.

    With no pytest config, pytest roots a session at the common ancestor of
    the cwd and the test directories, and would load that directory's
    conftest (and scan every entry below it).
    """
    one_project(files={"tests/test_a.py": "def test_ok(): pass\n"})
    (tmp_path / "conftest.py").write_text("raise RuntimeError('outside the project')\n")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    result = otto_test_cli(["test", "--list-tests"], lab=None)

    assert result.exit_code == 0, result.output
    assert "test_ok" in _lines(result.output)


def test_a_listing_whose_collection_cannot_finish_says_why_and_fails(
    otto_test_cli, one_project, caplog
):
    one_project(
        files={
            "tests/conftest.py": "raise RuntimeError('boom')\n",
            "tests/test_a.py": "def test_a(): pass\n",
        }
    )

    with caplog.at_level(logging.ERROR):
        result = otto_test_cli(["test", "--list-tests"], lab=None)

    assert result.exit_code != 0
    assert "Could not collect the tests of project 'sut'" in caplog.text
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
def test_list_markers_on_a_cold_table_is_one_collection(otto_test_cli, one_project, sessions):
    one_project(files=_MARKED)

    result = otto_test_cli(["test", "--list-markers"], lab=None)

    assert result.exit_code == 0, result.output
    assert len(sessions) == 1
    assert "--collect-only" in sessions[0]
    assert "applied_only" in result.output


@_APPLIED_ONLY_WARNS
def test_list_markers_on_a_warm_table_collects_nothing_and_shows_what_pytest_knows(
    otto_test_cli, one_project, sessions
):
    """Declared in the config, registered by a plugin, applied without either, and otto's."""
    sut = one_project(files=_MARKED)
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
    # otto's markers once, in otto's panel: never bare, as a project's panel lists names.
    assert not re.search(r"• (ensure|retry)\s", result.output)
    assert "• retry(n)" in result.output


# ── scope: only the projects that take part ──────────────────────────────────

_SCOPED_B = '[project]\nlab_patterns = ["other_lab"]\nhost_patterns = [".*"]\n'


@pytest.fixture
def scoped_projects(project_root, monkeypatch, _generated_modules_evicted):
    """``project_a`` is always active; ``project_b`` applies to ``other_lab`` only."""
    from tests._fixtures.project_scaffold import make_project
    from tests._fixtures.run_projects import _wire_projects

    a = make_project(
        project_root / "project_a",
        name="project_a",
        tests=["tests"],
        files={"tests/test_a.py": "import pytest\n\n\ndef test_in_a(): pass\n"},
    )
    b = make_project(
        project_root / "project_b",
        name="project_b",
        tests=["tests"],
        extra=_SCOPED_B,
        files={"tests/test_b.py": "def test_in_b(): pass\ndef test_only_b(): pass\n"},
    )
    _wire_projects(monkeypatch, [a, b])
    # The lab load is the lab slice's, not under test: the installed context stands in.
    monkeypatch.setattr("otto.cli.invoke.ensure_inline_lab", lambda ctx: None)
    return a, b


def _root(monkeypatch, **fields):
    """Stand in for the root's parse: the ``-l`` / ``-I`` / ``-E`` it read, and their record."""
    from otto.cli import invoke
    from otto.config.scope import ScopeSelection
    from tests._fixtures.rootoptions import make_root_options

    opts = make_root_options(**fields)
    selection = ScopeSelection(
        labs=tuple(opts.labs or ()), include=opts.include_projects, exclude=opts.exclude_projects
    )
    monkeypatch.setattr(invoke, "maybe_root_options", lambda ctx: opts)
    monkeypatch.setattr(invoke, "scope_selection", lambda ctx: selection)


def test_list_tests_with_lab_omits_inactive_tree(otto_test_cli, scoped_projects, monkeypatch):
    _root(monkeypatch, labs=["unix"])
    result = otto_test_cli(["test", "--list-tests"], lab="unix")

    assert result.exit_code == 0, result.output
    assert "test_in_a" in result.output
    assert "project_b" not in result.output
    assert "test_in_b" not in result.output


def test_list_tests_with_lab_makes_no_run_dir(
    otto_test_cli, scoped_projects, tmp_path, monkeypatch
):
    """The listing exits at parse time: no run directory is made."""
    _root(monkeypatch, labs=["unix"])
    result = otto_test_cli(["test", "--list-tests"], lab="unix")

    assert result.exit_code == 0, result.output
    assert list((tmp_path / "otto-out").iterdir()) == []


def test_list_tests_exclude_without_lab(otto_test_cli, scoped_projects, monkeypatch):
    """The root ``-E`` reaches the library call (the e2e test drives it end to end)."""
    from otto.suite import run
    from otto.suite.run import Listing

    seen = {}

    def fake(names, **kwargs):
        seen.update(kwargs)
        return Listing(tests={}, exit_code=0)

    monkeypatch.setattr(run, "selected_tests", fake)
    # The root callback hands the switches over PEP-503-normalised.
    _root(monkeypatch, exclude_projects=("project-a",), include_projects=("project-b",))
    result = otto_test_cli(["test", "--list-tests"], lab=None)

    assert result.exit_code == 0, result.output
    assert "no lab selected, showing every project" in result.output
    assert (seen["include"], seen["exclude"]) == (("project-b",), ("project-a",))


def test_list_tests_without_lab_says_so(otto_test_cli, scoped_projects, monkeypatch):
    # The autouse CLI fixture installs a stub lab context whenever none is; with a
    # lab loaded its verdicts rightly drop project_b. A real no-lab listing has none.
    monkeypatch.setattr("otto.run_context.try_get_context", lambda: None)
    result = otto_test_cli(["test", "--list-tests"], lab=None)

    assert result.exit_code == 0, result.output
    assert "no lab selected, showing every project" in result.output
    # No lab: every project not switched off takes part, one declaring lab_patterns too.
    assert "test_in_a" in result.output
    assert "test_in_b" in result.output


def test_list_tests_with_every_project_left_out_says_why(
    otto_test_cli, scoped_projects, monkeypatch
):
    """Nothing to list: the not-searched lines say why, and the listing still exits 0."""
    monkeypatch.setattr("otto.run_context.try_get_context", lambda: None)
    # The root callback hands the switches over PEP-503-normalised.
    _root(monkeypatch, exclude_projects=("project-a", "project-b"))
    result = otto_test_cli(["test", "--list-tests"], lab=None)

    assert result.exit_code == 0, result.output
    assert "project project_a was not searched" in result.output
    assert "project project_b was not searched" in result.output
    assert "No tests matched" not in result.output


_READS_OPTIONS = (
    "import dataclasses\n"
    "import os\n"
    "from pathlib import Path\n\n"
    "import otto\n\n\n"
    "@dataclasses.dataclass\n"
    "class Knobs:\n"
    "    depth: int = 3\n\n\n"
    'Path(os.environ["OTTO_TEST_OPTIONS_READ"]).write_text(repr(otto.get_options(Knobs)))\n\n\n'
    "def test_reads_options(): pass\n"
)


@pytest.mark.parametrize("flag", ["--list-tests", "--list-markers"])
@pytest.mark.usefixtures("_generated_modules_evicted")
def test_a_lab_listing_collects_with_option_defaults(
    otto_test_cli, project_root, tmp_path, monkeypatch, flag
):
    """A listing records no options: a module that reads one while it collects gets the defaults.

    The lab is installed under the command's pending mark (``ensure_lab_context``);
    the listing clears it once the lab is in, so the read is not refused.
    """
    from otto.cli.invoke import pending_options
    from otto.run_context import get_context, record_options
    from tests._fixtures.project_scaffold import make_project
    from tests._fixtures.run_projects import _wire_projects

    a = make_project(
        project_root / "project_a",
        name="project_a",
        tests=["tests"],
        files={"tests/test_reads.py": _READS_OPTIONS},
    )
    _wire_projects(monkeypatch, [a])

    def marked_lab(ctx):
        # What the real lab load leaves behind: the command's pending mark on the context.
        recorded = pending_options(ctx)
        assert recorded is not None
        assert recorded.pending
        record_options(get_context(), recorded)

    monkeypatch.setattr("otto.cli.invoke.ensure_inline_lab", marked_lab)
    read = tmp_path / "read.txt"
    monkeypatch.setenv("OTTO_TEST_OPTIONS_READ", str(read))
    _root(monkeypatch, labs=["unix"])

    result = otto_test_cli(["test", flag], lab="unix")

    assert result.exit_code == 0, result.output
    assert read.read_text() == "Knobs(depth=3)"


def test_list_tests_typo_exclude_fails(otto_test_cli, scoped_projects, monkeypatch):
    _root(monkeypatch, exclude_projects=("projectt_a",))
    result = otto_test_cli(["test", "--list-tests"], lab=None)

    assert result.exit_code == 2, result.output
    assert "test_in_a" not in result.output


def test_dry_run_omits_inactive_tree(otto_test_cli, scoped_projects):
    result = otto_test_cli(["-n", "test", "test_in_a"], lab="unix")

    assert result.exit_code == 0, result.output
    assert "pytest collected these tests" in result.output
    assert "project_a" in result.output
    assert "project_b" not in result.output


def test_dry_run_empty_marker_selection_prints_not_searched(otto_test_cli, scoped_projects):
    result = otto_test_cli(["-n", "test", "-m", "no_such_marker_anywhere"], lab="unix")

    assert result.exit_code == 1, result.output
    assert "No tests matched the selection." in result.output
    assert "project project_b was not searched" in result.output
    assert "pytest collected these tests" not in result.output


def test_dry_run_empty_selection_prints_not_searched(otto_test_cli, scoped_projects):
    result = otto_test_cli(["-n", "test", "test_only_b"], lab="unix")

    # A name only the inactive project holds is an unknown name, with the reason.
    assert result.exit_code == 2, result.output
    # The usage error is a wrapped panel, so compare single-spaced text.
    out = _flat(result.output)
    assert "project project_b was not searched" in out
    assert "-I project_b to search it anyway." in out
    assert "pytest collected these tests" not in out


def test_list_markers_skips_inactive_tables(otto_test_cli, scoped_projects, sessions, monkeypatch):
    _root(monkeypatch, labs=["unix"])
    result = otto_test_cli(["test", "--list-markers"], lab="unix")

    assert result.exit_code == 0, result.output
    assert "project_a" in result.output
    assert "project_b" not in result.output
    # One cold collection, of the project that takes part.
    assert len(sessions) == 1


@pytest.mark.usefixtures("_generated_modules_evicted")
def test_list_markers_with_lab_skips_host_starved_project(otto_test_cli, project_root, monkeypatch):
    from tests._fixtures.project_scaffold import make_project
    from tests._fixtures.run_projects import _wire_projects

    a = make_project(
        project_root / "project_a",
        name="project_a",
        tests=["tests"],
        files={"tests/test_a.py": "def test_in_a(): pass\n"},
    )
    c = make_project(
        project_root / "project_c",
        name="project_c",
        tests=["tests"],
        extra='[project]\nhost_patterns = ["^no-such-host$"]\n',
        files={"tests/test_c.py": "def test_in_c(): pass\n"},
    )
    _wire_projects(monkeypatch, [a, c])
    monkeypatch.setattr("otto.cli.invoke.ensure_inline_lab", lambda ctx: None)
    _root(monkeypatch, labs=["unix"])

    result = otto_test_cli(["test", "--list-markers"], lab="unix")

    assert result.exit_code == 0, result.output
    assert "project_a" in result.output
    assert "project_c" not in result.output


def test_list_markers_without_lab_shows_every_project(otto_test_cli, scoped_projects):
    result = otto_test_cli(["test", "--list-markers"], lab=None)

    assert result.exit_code == 0, result.output
    assert "project_a" in result.output
    assert "project_b" in result.output


def test_list_tests_help_text():
    import typing

    from otto.cli import test as cli_test

    annotation = cli_test._run_flags.__annotations__["list_tests"]
    [info] = [m for m in typing.get_args(annotation)[1:] if hasattr(m, "help")]
    assert "Needs no lab" not in info.help
    assert "With --lab, lists only the projects active for that lab; contacts no host." in info.help

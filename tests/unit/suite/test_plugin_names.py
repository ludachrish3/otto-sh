"""``OttoPlugin`` prunes the files to collect, selects tests by name, and records what it collected.

The run's own pytest session does all three (design
``docs/superpowers/specs/2026-09-27-test-name-cache-design.md`` §3.3): the
targets stay the repo's test directories, ``pytest_ignore_collect`` narrows
them to the candidate files, ``pytest_collection_modifyitems`` records every
collected item per file and keeps the ones a requested name selects, and a
failed module report becomes that file's ``error``.

Every session here is a real in-process pytest session (pytester). A module
that must never be imported writes a sentinel file on import, so "not
imported" is checked on disk, not inferred from an outcome count.
"""

import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from otto.config.collected_tests import RecordedTest
from otto.suite.plugin import OttoPlugin
from tests.unit.suite._inner import INNER_ARGS

pytest_plugins = ["pytester"]


def _sentinel_module(sentinel: str, body: str = "def test_decoy():\n    pass\n") -> str:
    """Source of a test module that leaves *sentinel* beside itself when imported."""
    return (
        "from pathlib import Path\n"
        f"Path(__file__).with_name({sentinel!r}).write_text('imported')\n\n" + body
    )


def _tests_dir(pytester: pytest.Pytester, files: dict[str, str]) -> Path:
    """Write *files* (paths relative to ``tests/``) into a fresh tests dir; return the dir."""
    tests = pytester.path / "tests"
    for rel, source in files.items():
        path = tests / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source)
    return tests


def _run(
    pytester: pytest.Pytester, tests: Path, plugin: OttoPlugin, *args: str
) -> pytest.RunResult:
    """One inner session over the tests DIR (never file args), as ``otto test`` runs it."""
    return pytester.runpytest_inprocess(*INNER_ARGS, str(tests), *args, plugins=[plugin])


def _plugin(tests: Path, candidates: list[str] | None, names: list[str] | None) -> OttoPlugin:
    return OttoPlugin(
        sut_test_dirs=[tests],
        candidates=None if candidates is None else [tests / c for c in candidates],
        names=names,
    )


_TARGET = """\
class TestTarget:
    def test_one(self):
        pass

    def test_two(self):
        pass


def test_other():
    pass
"""


# ── pruning ──────────────────────────────────────────────────────────────────


def test_a_named_class_runs_and_no_other_file_is_imported(pytester: pytest.Pytester) -> None:
    """Only the candidate file imports: not a sibling decoy, not a directory holding no candidate.

    The directory's own conftest never loads either: pytest skips the whole
    directory, so nothing the cache did not name is imported.
    """
    tests = _tests_dir(
        pytester,
        {
            "test_target.py": _TARGET,
            "test_decoy.py": _sentinel_module("decoy.imported"),
            "sub/conftest.py": _sentinel_module("conftest.imported", body=""),
            "sub/test_deep.py": _sentinel_module("deep.imported"),
        },
    )
    plugin = _plugin(tests, ["test_target.py"], ["TestTarget"])

    result = _run(pytester, tests, plugin)

    result.assert_outcomes(passed=2, deselected=1)
    assert not (tests / "decoy.imported").exists()
    assert not (tests / "sub" / "conftest.imported").exists()
    assert not (tests / "sub" / "deep.imported").exists()
    assert plugin.unmatched_names == []


def test_a_candidate_in_a_subdirectory_is_reached(pytester: pytest.Pytester) -> None:
    """A directory holding a candidate is walked; its other files are still pruned."""
    tests = _tests_dir(
        pytester,
        {
            "test_decoy.py": _sentinel_module("decoy.imported"),
            "a/b/test_target.py": _TARGET,
            "a/b/test_sibling.py": _sentinel_module("sibling.imported"),
            "a/c/test_other_dir.py": _sentinel_module("other_dir.imported"),
        },
    )
    plugin = _plugin(tests, ["a/b/test_target.py"], ["test_one"])

    result = _run(pytester, tests, plugin)

    result.assert_outcomes(passed=1, deselected=2)
    assert list(tests.rglob("*.imported")) == []


def test_without_names_every_test_of_the_candidates_runs(pytester: pytest.Pytester) -> None:
    """Pruning alone deselects nothing: the candidate files' tests all run."""
    tests = _tests_dir(
        pytester, {"test_target.py": _TARGET, "test_decoy.py": _sentinel_module("decoy.imported")}
    )
    plugin = _plugin(tests, ["test_target.py"], None)

    result = _run(pytester, tests, plugin)

    result.assert_outcomes(passed=3)
    assert not (tests / "decoy.imported").exists()


def test_no_candidate_set_collects_the_whole_tree(pytester: pytest.Pytester) -> None:
    """``candidates=None`` prunes nothing: every file under the test dirs is collected."""
    tests = _tests_dir(
        pytester,
        {"test_target.py": _TARGET, "sub/test_deep.py": _sentinel_module("deep.imported")},
    )
    plugin = _plugin(tests, None, ["test_decoy"])

    result = _run(pytester, tests, plugin)

    result.assert_outcomes(passed=1, deselected=3)
    assert (tests / "sub" / "deep.imported").exists()


def test_a_collect_ignored_candidate_stays_ignored_with_the_dirs_as_targets(
    pytester: pytest.Pytester,
) -> None:
    """A conftest's ``collect_ignore`` still applies to a candidate (design §2, B5).

    The targets are the test dirs, never the candidate files: an explicit file
    argument would bypass ``collect_ignore``. The ignored candidate still gets
    an (empty) record — it was considered, and it holds nothing pytest would
    run — so the next ``classify`` does not call it changed forever.
    """
    tests = _tests_dir(
        pytester,
        {
            "conftest.py": 'collect_ignore = ["test_hidden.py"]\n',
            "test_hidden.py": _sentinel_module("hidden.imported"),
            "test_target.py": _TARGET,
        },
    )
    plugin = _plugin(tests, ["test_hidden.py", "test_target.py"], None)

    result = _run(pytester, tests, plugin)

    result.assert_outcomes(passed=3)
    assert not (tests / "hidden.imported").exists()
    assert plugin.records[str(tests / "test_hidden.py")].tests == []


def test_candidate_pruning_decides_by_path_alone() -> None:
    """The hook's answers, path by path, with no stat of the path itself."""
    tests = Path("/sut/repo/tests")
    plugin = OttoPlugin(
        sut_test_dirs=[tests],
        candidates=[tests / "a" / "test_x.py", tests / "conftest.py"],
    )

    def ignored(path: Path) -> bool | None:
        return plugin.pytest_ignore_collect(path, MagicMock())

    assert ignored(tests / "a" / "test_x.py") is None
    assert ignored(tests / "a") is None
    assert ignored(tests / "conftest.py") is None
    assert ignored(tests) is None
    assert ignored(tests.parent) is None  # above the test dir: the SUT-dir rule's call
    assert ignored(tests / "a" / "test_y.py") is True
    assert ignored(tests / "b") is True
    assert ignored(tests / "b" / "test_x.py") is True
    assert ignored(Path("/elsewhere/test_x.py")) is True  # the SUT-dir rule still applies


# ── matching ─────────────────────────────────────────────────────────────────


def test_inherited_and_parametrized_tests_match_by_base_name(pytester: pytest.Pytester) -> None:
    """An inherited method answers to its class; every parametrization to its base name."""
    tests = _tests_dir(
        pytester,
        {
            "base.py": "class BaseTests:\n    def test_inherited(self):\n        pass\n",
            "test_derived.py": (
                "import pytest\n"
                "from base import BaseTests\n\n\n"
                "class TestDerived(BaseTests):\n"
                "    def test_own(self):\n"
                "        pass\n\n\n"
                "@pytest.mark.parametrize('n', [1, 2, 3])\n"
                "def test_p(n):\n"
                "    pass\n"
            ),
        },
    )
    plugin = _plugin(tests, ["test_derived.py"], ["TestDerived::test_inherited", "test_p"])

    result = _run(pytester, tests, plugin)

    result.assert_outcomes(passed=4, deselected=1)
    assert plugin.unmatched_names == []
    assert plugin.records[str(tests / "test_derived.py")].tests == [
        RecordedTest(classes=["TestDerived"], name="test_inherited"),
        RecordedTest(classes=["TestDerived"], name="test_own"),
        RecordedTest(classes=[], name="test_p"),
    ]


def test_names_matching_nothing_are_reported(pytester: pytest.Pytester) -> None:
    """A name no collected item answers to is left on the plugin; the rest still run."""
    tests = _tests_dir(pytester, {"test_target.py": _TARGET})
    plugin = _plugin(tests, ["test_target.py"], ["test_nope", "test_other", "TestNope"])

    result = _run(pytester, tests, plugin)

    result.assert_outcomes(passed=1, deselected=2)
    assert plugin.unmatched_names == ["test_nope", "TestNope"]


def test_selected_tests_keep_collection_order(pytester: pytest.Pytester) -> None:
    """The kept items are in pytest's collection order, whatever order the names came in."""
    tests = _tests_dir(pytester, {"test_target.py": _TARGET})
    plugin = _plugin(tests, None, ["test_other", "test_two", "test_one"])

    result = _run(pytester, tests, plugin, "--collect-only", "-q")

    ids = [line for line in result.outlines if "::" in line and "test_target.py" in line]
    assert [i.rsplit("::", 1)[1] for i in ids] == ["test_one", "test_two", "test_other"]


def test_marker_expression_applies_after_name_matching(pytester: pytest.Pytester) -> None:
    """``-m`` deselects among the named tests; a name ``-m`` excludes is still a known name."""
    tests = _tests_dir(
        pytester,
        {
            "test_marked.py": (
                "import pytest\n\n\n"
                "class TestA:\n"
                "    @pytest.mark.slow\n"
                "    def test_slow(self):\n"
                "        pass\n\n"
                "    def test_fast(self):\n"
                "        pass\n\n\n"
                "def test_elsewhere():\n"
                "    pass\n"
            ),
        },
    )
    (pytester.path / "pytest.ini").write_text("[pytest]\nmarkers =\n    slow: slow\n")
    plugin = _plugin(tests, None, ["TestA", "test_elsewhere"])

    result = _run(pytester, tests, plugin, "-m", "not slow")

    result.assert_outcomes(passed=2, deselected=1)
    assert plugin.unmatched_names == []
    record = plugin.records[str(tests / "test_marked.py")]
    assert [t.name for t in record.tests] == ["test_slow", "test_fast", "test_elsewhere"]
    assert "slow" in record.markers


# ── recording ────────────────────────────────────────────────────────────────


def test_every_considered_file_gets_a_record_in_the_table_shape(
    pytester: pytest.Pytester,
) -> None:
    """Collected, empty and pruned files: a record for the first two, none for the last."""
    tests = _tests_dir(
        pytester,
        {
            "test_target.py": "import pytest\n\npytestmark = pytest.mark.hw\n\n" + _TARGET,
            "test_empty.py": "HELPER = 1\n",
            "test_decoy.py": _sentinel_module("decoy.imported"),
        },
    )
    (pytester.path / "pytest.ini").write_text("[pytest]\nmarkers =\n    hw: needs hardware\n")
    plugin = _plugin(tests, ["test_target.py", "test_empty.py"], ["test_one"])

    st = (tests / "test_target.py").stat()
    before_import = [st.st_mtime_ns, st.st_size]

    _run(pytester, tests, plugin)

    assert plugin.collection_finished is True
    assert sorted(plugin.records) == [str(tests / "test_empty.py"), str(tests / "test_target.py")]
    target = plugin.records[str(tests / "test_target.py")]
    assert target.stat == before_import  # read before pytest imported it, never after
    assert target.collected_at > 0
    assert target.error is None
    assert target.tests == [
        RecordedTest(classes=["TestTarget"], name="test_one"),
        RecordedTest(classes=["TestTarget"], name="test_two"),
        RecordedTest(classes=[], name="test_other"),
    ]
    assert target.markers == ["hw"]
    empty = plugin.records[str(tests / "test_empty.py")]
    assert (empty.tests, empty.markers, empty.error) == ([], [], None)


def test_a_whole_tree_session_records_every_module(pytester: pytest.Pytester) -> None:
    """With no candidate set, every collected module is recorded, nested ones included."""
    tests = _tests_dir(
        pytester, {"test_target.py": _TARGET, "sub/test_deep.py": "def test_deep():\n    pass\n"}
    )
    plugin = _plugin(tests, None, None)

    _run(pytester, tests, plugin)

    assert sorted(plugin.records) == [
        str(tests / "sub" / "test_deep.py"),
        str(tests / "test_target.py"),
    ]
    assert plugin.records[str(tests / "sub" / "test_deep.py")].tests == [
        RecordedTest(classes=[], name="test_deep")
    ]


@pytest.mark.parametrize("keep_going", [True, False], ids=["continue", "interrupted"])
def test_a_broken_candidate_records_its_error(
    pytester: pytest.Pytester, *, keep_going: bool
) -> None:
    """A module that fails to import is recorded with the error; the others are recorded too.

    Without ``--continue-on-collection-errors`` pytest runs nothing (exit 2),
    but collection completed, so the records are as good as with it.
    """
    tests = _tests_dir(
        pytester, {"test_broken.py": "def test_x(:\n    pass\n", "test_target.py": _TARGET}
    )
    plugin = _plugin(tests, ["test_broken.py", "test_target.py"], ["test_one"])
    args = ["--continue-on-collection-errors"] if keep_going else []

    result = _run(pytester, tests, plugin, *args)

    if keep_going:
        result.assert_outcomes(passed=1, errors=1, deselected=2)
    else:
        assert result.ret == pytest.ExitCode.INTERRUPTED
    broken = plugin.records[str(tests / "test_broken.py")]
    assert broken.error is not None
    assert broken.error.startswith("SyntaxError")
    assert broken.tests == []
    assert len(plugin.records[str(tests / "test_target.py")].tests) == 3


def test_a_candidate_under_a_broken_conftest_gets_no_record(pytester: pytest.Pytester) -> None:
    """A file its directory's failure kept pytest from reaching is not recorded as empty.

    An empty record would read as fresh with no tests; left out, the file stays
    changed and is collected again next time.
    """
    tests = _tests_dir(
        pytester,
        {
            "sub/conftest.py": "raise RuntimeError('conftest boom')\n",
            "sub/test_under.py": "def test_under():\n    pass\n",
            "test_target.py": _TARGET,
        },
    )
    plugin = _plugin(tests, ["sub/test_under.py", "test_target.py"], None)

    _run(pytester, tests, plugin, "--continue-on-collection-errors")

    assert sorted(plugin.records) == [str(tests / "test_target.py")]


def test_records_wait_for_the_end_of_collection() -> None:
    """Before (or without) a finished collection the plugin claims nothing."""
    plugin = OttoPlugin(names=["test_x"])

    assert plugin.collection_finished is False
    assert plugin.records == {}
    assert plugin.registered_markers == []
    assert plugin.unmatched_names == ["test_x"]


def test_registered_markers_are_every_marker_pytest_knows(pytester: pytest.Pytester) -> None:
    """Declared, conftest-registered (nested ones too), plugin-registered and pytest's own."""
    tests = _tests_dir(
        pytester,
        {
            "conftest.py": (
                "def pytest_configure(config):\n"
                "    config.addinivalue_line('markers', 'top_mine: from the top conftest')\n"
            ),
            "sub/conftest.py": (
                "def pytest_configure(config):\n"
                "    config.addinivalue_line('markers', 'nested_mine(arg): from a nested one')\n"
            ),
            "sub/test_deep.py": "def test_deep():\n    pass\n",
        },
    )
    (pytester.path / "pytest.ini").write_text("[pytest]\nmarkers =\n    slow: a slow test\n")
    plugin = _plugin(tests, ["sub/test_deep.py"], None)

    _run(pytester, tests, plugin)

    markers = plugin.registered_markers
    assert {"slow", "top_mine", "nested_mine", "asyncio", "timeout", "skip"} <= set(markers)
    assert markers == sorted(set(markers))
    assert all(":" not in m and "(" not in m for m in markers)


# ── hook order, combined deselection ─────────────────────────────────────────


def test_a_user_tryfirst_deselection_still_leaves_the_item_in_the_record(
    pytester: pytest.Pytester,
) -> None:
    """The record is taken before any plain ``modifyitems``, a user's ``tryfirst`` one included.

    The conftest registers after the plugin, and a plain impl registered later
    runs first among its peers; only the plugin's wrapper half runs before it
    whatever the order.
    """
    tests = _tests_dir(
        pytester,
        {
            "conftest.py": (
                "import pytest\n\n\n"
                "@pytest.hookimpl(tryfirst=True)\n"
                "def pytest_collection_modifyitems(config, items):\n"
                "    dropped = [i for i in items if i.name == 'test_two']\n"
                "    items[:] = [i for i in items if i.name != 'test_two']\n"
                "    config.hook.pytest_deselected(items=dropped)\n"
            ),
            "test_target.py": _TARGET,
        },
    )
    plugin = _plugin(tests, ["test_target.py"], None)

    result = _run(pytester, tests, plugin)

    result.assert_outcomes(passed=2, deselected=1)
    names = [t.name for t in plugin.records[str(tests / "test_target.py")].tests]
    assert names == ["test_one", "test_two", "test_other"]


def test_the_record_keeps_collection_order_whatever_reorders_the_items(
    pytester: pytest.Pytester,
) -> None:
    """A wrapper running before the plugin's (pytest-randomly's does) cannot reorder the record."""
    tests = _tests_dir(
        pytester,
        {
            "conftest.py": (
                "import pytest\n\n\n"
                "@pytest.hookimpl(wrapper=True, tryfirst=True)\n"
                "def pytest_collection_modifyitems(items):\n"
                "    items.reverse()\n"
                "    return (yield)\n"
            ),
            "test_target.py": _TARGET,
        },
    )
    plugin = _plugin(tests, None, ["TestTarget"])

    result = _run(pytester, tests, plugin)

    result.assert_outcomes(passed=2, deselected=1)
    names = [t.name for t in plugin.records[str(tests / "test_target.py")].tests]
    assert names == ["test_one", "test_two", "test_other"]


def test_a_name_and_a_marker_each_deselect_their_own_item_once(pytester: pytest.Pytester) -> None:
    """A name drops one item and ``-m`` another: two deselected, each reported once."""
    tests = _tests_dir(
        pytester,
        {
            "test_marked.py": (
                "import pytest\n\n\n"
                "class TestA:\n"
                "    @pytest.mark.slow\n"
                "    def test_slow(self):\n"
                "        pass\n\n"
                "    def test_fast(self):\n"
                "        pass\n\n\n"
                "def test_elsewhere():\n"
                "    pass\n"
            ),
        },
    )
    (pytester.path / "pytest.ini").write_text("[pytest]\nmarkers =\n    slow: slow\n")
    plugin = _plugin(tests, None, ["TestA"])

    result = _run(pytester, tests, plugin, "-m", "not slow")

    result.assert_outcomes(passed=1, deselected=2)


# ── a session that must match every name before it runs ──────────────────────


def test_a_session_missing_a_required_name_stops_before_any_test(
    pytester: pytest.Pytester,
) -> None:
    """With ``must_match``, one unmatched required name is a usage error: nothing runs.

    The records stand (the caller writes them and says which name is
    unknown); pytest is given nothing to print for the error.
    """
    tests = _tests_dir(pytester, {"test_target.py": _TARGET})
    plugin = OttoPlugin(
        sut_test_dirs=[tests],
        candidates=[tests / "test_target.py"],
        names=["test_one", "test_nope"],
        must_match=["test_one", "test_nope"],
    )

    result = _run(pytester, tests, plugin)

    assert result.ret == pytest.ExitCode.USAGE_ERROR
    result.assert_outcomes()
    assert "ERROR" not in result.stderr.str()
    assert plugin.collection_finished is True
    assert plugin.unmatched_names == ["test_nope"]
    assert len(plugin.records[str(tests / "test_target.py")].tests) == 3


def test_a_session_matching_every_required_name_runs(pytester: pytest.Pytester) -> None:
    """A matched required name lets the session run; an unrequired unmatched one doesn't stop it."""
    tests = _tests_dir(pytester, {"test_target.py": _TARGET})
    plugin = OttoPlugin(
        sut_test_dirs=[tests],
        names=["test_one", "test_elsewhere"],
        must_match=["test_one"],
    )

    result = _run(pytester, tests, plugin)

    result.assert_outcomes(passed=1, deselected=2)
    assert plugin.unmatched_names == ["test_elsewhere"]


# ── registrations refused while test files load ──────────────────────────────


_REGISTERING = (
    "from otto.registry import RegistrationRefused\n"
    "raise RegistrationRefused('register from an init module')\n"
)


@pytest.mark.parametrize("where", ["test_reg.py", "conftest.py"])
def test_a_refused_registration_is_kept_and_nothing_runs(
    pytester: pytest.Pytester, where: str
) -> None:
    """A test file or conftest whose import is refused: the refusal is kept, no test runs."""
    files = {where: _REGISTERING, "test_target.py": _TARGET}
    tests = _tests_dir(pytester, files)
    plugin = _plugin(tests, None, ["test_one"])

    result = _run(pytester, tests, plugin, "--continue-on-collection-errors")

    assert [type(r).__name__ for r in plugin.refusals] == ["RegistrationRefused"]
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    assert " passed" not in result.stdout.str()


def test_a_marker_a_conftest_adds_while_modifying_items_is_recorded(
    pytester: pytest.Pytester,
) -> None:
    tests = _tests_dir(
        pytester,
        {
            "conftest.py": (
                "def pytest_collection_modifyitems(items):\n"
                "    for item in items:\n"
                "        item.add_marker('added_late')\n"
            ),
            "test_target.py": _TARGET,
        },
    )
    (pytester.path / "pytest.ini").write_text("[pytest]\nmarkers =\n    added_late: late\n")
    plugin = _plugin(tests, ["test_target.py"], None)

    _run(pytester, tests, plugin)

    assert "added_late" in plugin.records[str(tests / "test_target.py")].markers


def test_a_test_inherited_from_another_module_names_it_as_a_dependency(
    pytester: pytest.Pytester,
) -> None:
    tests = _tests_dir(
        pytester,
        {
            "helpers/__init__.py": "",
            "helpers/base.py": "class Base:\n    def test_inherited(self):\n        pass\n",
            "test_derived.py": (
                "from helpers.base import Base\n\n\n"
                "class TestDerived(Base):\n"
                "    def test_own(self):\n"
                "        pass\n\n\n"
                "def test_plain():\n"
                "    pass\n"
            ),
            "test_alone.py": "def test_alone():\n    pass\n",
        },
    )
    plugin = _plugin(tests, None, None)

    _run(pytester, tests, plugin)

    # The namespace also names what pytest's assertion rewriting put there
    # (site-packages): the table drops those, see _tracked_dependency.
    assert _own(plugin.records[str(tests / "test_derived.py")].deps, pytester) == [
        str(tests / "helpers" / "base.py")
    ]
    assert _own(plugin.records[str(tests / "test_alone.py")].deps, pytester) == []


def test_a_module_names_what_its_namespace_came_from_as_dependencies(
    pytester: pytest.Pytester,
) -> None:
    """A class that collected nothing, and what a star-import brought in, are dependencies too."""
    tests = _tests_dir(
        pytester,
        {
            "helpers/__init__.py": "",
            "helpers/base.py": "class Base:\n    pass\n",
            "helpers/common.py": "def helper():\n    pass\n",
            "test_derived.py": (
                "from helpers.base import Base\n"
                "from helpers.common import *  # noqa: F403\n\n\n"
                "class TestDerived(Base):\n"
                "    pass\n\n\n"
                "def test_own():\n"
                "    pass\n"
            ),
            "test_empty.py": (
                "from helpers.base import Base\n\n\nclass TestEmpty(Base):\n    pass\n"
            ),
        },
    )
    plugin = _plugin(tests, None, None)

    _run(pytester, tests, plugin)

    helpers = tests / "helpers"
    derived = plugin.records[str(tests / "test_derived.py")].deps
    assert {str(helpers / "base.py"), str(helpers / "common.py")} <= set(derived)
    assert str(tests / "test_derived.py") not in derived
    assert str(helpers / "base.py") in plugin.records[str(tests / "test_empty.py")].deps


def test_a_later_modifyitems_that_raises_leaves_the_session_unrecorded(
    pytester: pytest.Pytester,
) -> None:
    """Its markers were never read, so its records are not written with none."""
    tests = _tests_dir(
        pytester,
        {
            "conftest.py": (
                "import pytest\n\n\n"
                "def pytest_collection_modifyitems(items):\n"
                "    raise pytest.UsageError('refused by a conftest')\n"
            ),
            "test_target.py": _TARGET,
        },
    )
    plugin = _plugin(tests, None, None)

    _run(pytester, tests, plugin)

    assert plugin.collection_finished is False


def _own(deps: list[str], pytester: pytest.Pytester) -> list[str]:
    """The dependencies under the test's own directory."""
    return [d for d in deps if Path(d).is_relative_to(pytester.path)]


def test_the_namespace_walk_never_evaluates_a_global() -> None:
    """Only ``type(value)`` is read: a lazy proxy is not set up, a hostile value is skipped.

    Django's ``settings`` sets itself up on its first attribute access, and
    ``isinstance`` reads ``__class__``. A metaclass can make ``__mro__`` or
    hashing the class raise.
    """
    import types

    touched: list[str] = []

    class Lazy:
        def __getattribute__(self, name: str) -> object:
            touched.append(name)
            raise RuntimeError("set up on first use")

    class HostileMeta(type):
        @property
        def __mro__(cls) -> tuple[type, ...]:  # type: ignore[override]
            raise RuntimeError("no hierarchy")

    class Hostile(metaclass=HostileMeta):
        pass

    class UnhashableMeta(type):
        def __hash__(cls) -> int:
            raise RuntimeError("no hash")

    class Unhashable(metaclass=UnhashableMeta):
        pass

    def helper() -> None:
        pass

    namespace = types.SimpleNamespace(
        settings=Lazy(), hostile=Hostile, unhashable=Unhashable, helper=helper, os=os
    )
    plugin = OttoPlugin(sut_test_dirs=[])

    found = plugin._namespace_sources(namespace)

    assert touched == []
    assert found == {__file__, os.__file__}

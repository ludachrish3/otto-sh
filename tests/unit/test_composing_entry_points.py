"""otto.suite's and otto.coverage's composing entry points compose as they always did.

A public function whose call can reach a read of the run's repos with no run in hand
composes them: ``prepare_run`` from ``otto.suite`` and ``clean_coverage``,
``collect_coverage`` and ``get_coverage`` from ``otto.coverage`` read them when no
repos are given, and ``run_tests`` reads them through the context it opens (spec
2026-10-11 placement-and-phases §3.12). Each composes once, on its call, and then
answers as it answered before the composed state moved below the root; resolving a
name composes nothing and loads no composition root.

Every case runs in a fresh interpreter that has not imported the root, its
compositions counted by their dependency passes (``tests/_fixtures/fresh_composition.py``).
Every case that counts one pass also asserts the counting hook armed, so those cases
are the controls that prove the hook works: the resolving cases' 0 is a count the
same hook would have made, not a hook that never armed.
"""

from pathlib import Path

import pytest

from tests._fixtures.fresh_composition import armed, compositions, run_fresh

_RESOLVE = """
import json, sys
import {facade} as facade
before = 'otto.bootstrap' in sys.modules
value = getattr(facade, {name!r})
print(json.dumps([before, 'otto.bootstrap' in sys.modules, callable(value)]))
"""

_CALL = """
import asyncio, json, sys
from pathlib import Path
{body}
"""

_PREPARE_RUN = """
from otto.suite import RunOptions, prepare_run
try:
    prepare_run(RunOptions(cov_tickets_json=Path({tickets!r})), dry_run=True)
except Exception as exc:
    print(json.dumps([type(exc).__name__, str(exc)]))
"""

_RUN_TESTS = """
from otto.suite import run_tests
try:
    run_tests(["no_such_test"], output_dir=Path({out!r}))
except Exception as exc:
    print(json.dumps([type(exc).__name__, str(exc)]))
"""

_CLEAN = """
from otto.coverage import clean_coverage
try:
    asyncio.run(clean_coverage())
except Exception as exc:
    print(json.dumps([type(exc).__name__, str(exc)]))
"""

_COLLECT = """
from otto.coverage import collect_coverage
try:
    asyncio.run(collect_coverage(Path({out!r})))
except Exception as exc:
    print(json.dumps([type(exc).__name__, str(exc)]))
"""

_GET = """
from otto.coverage import get_coverage
try:
    asyncio.run(get_coverage(Path({out!r})))
except Exception as exc:
    print(json.dumps([type(exc).__name__, str(exc)]))
"""

_NO_COVERAGE = ["CoverageConfigError", "No [coverage] section found in .otto/settings.toml"]


@pytest.mark.parametrize(
    ("facade", "name"),
    [
        ("otto.suite", "run_tests"),
        ("otto.suite", "prepare_run"),
        ("otto.suite", "RunOptions"),
        ("otto.suite", "NoTestsMatchedError"),
        ("otto.coverage", "clean_coverage"),
        ("otto.coverage", "collect_coverage"),
        ("otto.coverage", "get_coverage"),
        ("otto.coverage", "TierConfig"),
        ("otto.coverage", "CoverageConfigError"),
    ],
)
def test_resolving_a_name_loads_no_root_and_composes_nothing(
    facade: str, name: str, tmp_path: Path
) -> None:
    before, after, is_callable = run_fresh(tmp_path, _RESOLVE.format(facade=facade, name=name))
    assert [before, after, is_callable] == [False, False, True]
    assert compositions(tmp_path) == 0, "resolving a name composed"


def test_prepare_run_composes_once_then_refuses_the_tickets_as_today(tmp_path: Path) -> None:
    source = _CALL.format(body=_PREPARE_RUN.format(tickets=str(tmp_path / "tickets.json")))
    assert run_fresh(tmp_path, source) == [
        "OptionsValidationError",
        "cov_tickets_json requires [coverage.tickets] to be configured",
    ]
    assert armed(tmp_path)
    assert compositions(tmp_path) == 1


def test_run_tests_composes_once_then_finds_nothing_as_today(tmp_path: Path) -> None:
    source = _CALL.format(body=_RUN_TESTS.format(out=str(tmp_path / "out")))
    assert run_fresh(tmp_path, source) == ["NoTestsMatchedError", "No tests matched the selection."]
    assert armed(tmp_path)
    assert compositions(tmp_path) == 1


@pytest.mark.parametrize(
    "body",
    [_CLEAN, _COLLECT, _GET],
    ids=["clean_coverage", "collect_coverage", "get_coverage"],
)
def test_a_coverage_entry_point_with_no_repos_composes_once_then_refuses_as_today(
    body: str, tmp_path: Path
) -> None:
    source = _CALL.format(body=body.format(out=str(tmp_path / "cov")))
    assert run_fresh(tmp_path, source) == _NO_COVERAGE
    assert armed(tmp_path)
    assert compositions(tmp_path) == 1

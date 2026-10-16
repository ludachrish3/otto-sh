"""otto.suite's and otto.coverage's composing entry points compose as they always did.

A public function whose call can reach a read of the run's repos with no run in hand
composes them: ``prepare_run`` from ``otto.suite`` and ``clean_coverage``,
``collect_coverage`` and ``get_coverage`` from ``otto.coverage`` read them when no
repos are given, and ``run_tests`` reads them through the context it opens (spec
2026-10-11 placement-and-phases §3.12). Each composes once, on its call, and then
answers as it answered before the composed state moved below the root; resolving a
name composes nothing and loads no composition root.

Every case runs in a fresh interpreter that has not imported the root: in-process, a
root conftest fixture or an earlier test may already have loaded it. A composition is
counted by its dependency pass, which every composition runs exactly once: the child
wraps ``otto.config.dependencies.resolve_dependencies`` the moment that module is
loaded (an import hook, so the wrapping itself loads nothing) and logs each call. An
init module's run would not do: Python caches the imported module, so a second
composition in the same process runs no init module and leaves no trace there. The
hook logs ``armed`` when it wraps the module, and every case that counts one pass
asserts it, so those cases are the controls that prove the hook works: the
resolving cases' 0 is a count the same hook would have made, not a hook that never
armed.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests._fixtures.sutrepo import make_sut_repo

_INIT = "compose_marker"

_COUNT = """
import importlib.abc, importlib.machinery, sys

class _CountPasses(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path, target=None):
        if name != "otto.config.dependencies":
            return None
        spec = importlib.machinery.PathFinder.find_spec(name, path)
        run = spec.loader.exec_module

        def exec_module(module):
            run(module)
            real = module.resolve_dependencies

            def counted(repos):
                with open({log!r}, "a") as f:
                    f.write("pass\\n")
                return real(repos)

            module.resolve_dependencies = counted
            with open({log!r}, "a") as f:
                f.write("armed\\n")

        spec.loader.exec_module = exec_module
        return spec

sys.meta_path.insert(0, _CountPasses())
"""

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


def _repo(tmp_path: Path) -> Path:
    """The scratch repo, with one init module, so a composition imports user code."""
    return make_sut_repo(
        tmp_path / "repo",
        name="repo",
        extra=f'libs = ["lib"]\ninit = ["{_INIT}"]\n',
        files={f"lib/{_INIT}.py": "MARKED = True\n"},
    )


def _fresh(tmp_path: Path, source: str) -> "list[object]":
    """Run *source* in a new interpreter over the scratch repo, compositions counted.

    Returns the source's last JSON line.
    """
    repo = _repo(tmp_path)
    source = _COUNT.format(log=str(tmp_path / "passes.log")) + source
    env = {
        **os.environ,
        "OTTO_SUT_DIRS": str(repo),
        "OTTO_HOME": str(tmp_path / "home"),
    }
    env.pop("PYTEST_ADDOPTS", None)
    out = subprocess.run(
        [sys.executable, "-c", source],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
        env=env,
        cwd=tmp_path,
    )
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


def _log(tmp_path: Path) -> "list[str]":
    """The child's hook log: ``armed`` once the hook wraps the module, ``pass`` per pass."""
    log = tmp_path / "passes.log"
    return log.read_text().split() if log.exists() else []


def _compositions(tmp_path: Path) -> int:
    """How many dependency passes, so compositions, the child ran."""
    return _log(tmp_path).count("pass")


def _armed(tmp_path: Path) -> bool:
    """Whether the child's hook wrapped the dependency pass, so could count it."""
    return "armed" in _log(tmp_path)


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
    before, after, is_callable = _fresh(tmp_path, _RESOLVE.format(facade=facade, name=name))
    assert [before, after, is_callable] == [False, False, True]
    assert _compositions(tmp_path) == 0, "resolving a name composed"


def test_prepare_run_composes_once_then_refuses_the_tickets_as_today(tmp_path: Path) -> None:
    source = _CALL.format(body=_PREPARE_RUN.format(tickets=str(tmp_path / "tickets.json")))
    assert _fresh(tmp_path, source) == [
        "OptionsValidationError",
        "cov_tickets_json requires [coverage.tickets] to be configured",
    ]
    assert _armed(tmp_path)
    assert _compositions(tmp_path) == 1


def test_run_tests_composes_once_then_finds_nothing_as_today(tmp_path: Path) -> None:
    source = _CALL.format(body=_RUN_TESTS.format(out=str(tmp_path / "out")))
    assert _fresh(tmp_path, source) == ["NoTestsMatchedError", "No tests matched the selection."]
    assert _armed(tmp_path)
    assert _compositions(tmp_path) == 1


@pytest.mark.parametrize(
    "body",
    [_CLEAN, _COLLECT, _GET],
    ids=["clean_coverage", "collect_coverage", "get_coverage"],
)
def test_a_coverage_entry_point_with_no_repos_composes_once_then_refuses_as_today(
    body: str, tmp_path: Path
) -> None:
    source = _CALL.format(body=body.format(out=str(tmp_path / "cov")))
    assert _fresh(tmp_path, source) == _NO_COVERAGE
    assert _armed(tmp_path)
    assert _compositions(tmp_path) == 1

"""Every public route to composition composes as it always did, the root never loaded first.

The composition root, ``otto.bootstrap``, composes the repos; the layers below it read
what it published and start it only through the initiator that importing the root
installs. So a public route that can reach a read of the repos with no run in hand
must have the root loaded by the time it reads (spec 2026-10-11 placement-and-phases
§3.9, §3.12, §3.13). Each case here runs in a fresh interpreter over a scratch repo
that has not imported the root, and counts compositions by their dependency passes
(``tests/_fixtures/fresh_composition.py``): a context opened, a hand-built context's
first read, the library ``run_tests`` reached through the root facade, the repo-aware
logging install, the conformance harness's dry-run context. Each composes once and
answers as it always did; a context handed a result, a name resolved and a discovery
read compose nothing. Every case that counts nothing arms the counting hook
afterwards, so its 0 is a count the hook would have made.
"""

from pathlib import Path

import pytest

from tests._fixtures.fresh_composition import INIT, armed, compositions, run_fresh

_ARM = """
from otto import bootstrap as _arm
_arm.bootstrap()
"""
"""Compose once at the end of a case that counted nothing, so the hook proves it counts."""

_COLD = """
import json, sys
import otto
print(json.dumps(['otto.bootstrap' in sys.modules, 'otto.declared' in sys.modules]))
"""

_NO_ROOT_NAME = """
import json, sys
from {facade} import {name}
print(json.dumps(['otto.bootstrap' in sys.modules, {name} is not None]))
"""

_RESOLVE = """
import json
import {facade} as facade
value = getattr(facade, {name!r})
print(json.dumps([callable(value)]))
"""

_OPEN = """
import asyncio, json, os
os.environ["OTTO_TEARDOWN_DEADLINE"] = "7.5"
{spelling}
from otto.lab import Lab

async def main():
    async with open_context(lab=Lab(name="pin")) as ctx:
        return [
            [r.name for r in ctx.projects],
            [r.name for r in ctx.ordered_projects],
            ctx.policy.teardown_deadline,
        ]

print(json.dumps(asyncio.run(main())))
"""

_HARNESS = """
import json
from otto.context import get_context
from otto.testing.conformance_host import _dry_run
with _dry_run():
    print(json.dumps([r.name for r in get_context().projects]))
"""

_ROOT_RUN_TESTS = """
import json
from pathlib import Path
import otto
try:
    otto.run_tests(["no_such_test"], output_dir=Path({out!r}))
except Exception as exc:
    print(json.dumps([type(exc).__name__, str(exc)]))
else:
    print(json.dumps(["no error", "run_tests returned"]))
"""

_HAND_BUILT = """
import json
{spelling}
from otto.lab import Lab
ctx = OttoContext(lab=Lab(name="pin"))
first = [r.name for r in ctx.projects]
again = [r.name for r in ctx.projects]
ordered = [r.name for r in ctx.ordered_projects]
print(json.dumps([first, again, ordered]))
"""

_REFUSED = """
import json
from otto.context import OttoContext
from otto.lab import Lab
ctx = OttoContext(lab=Lab(name="pin"))
raised = []
for _ in range(2):
    try:
        ctx.projects
    except Exception as exc:
        raised.append(exc)
print(json.dumps([type(raised[0]).__name__, len(raised), raised[0] is raised[1]]))
"""

_GIVEN = """
import json
from otto.bootstrap import BootstrapResult
from otto.context import OttoContext
from otto.lab import Lab
ctx = OttoContext(lab=Lab(name="pin"), bootstrap=BootstrapResult(env=None, projects=[]))
print(json.dumps([ctx.projects, ctx.ordered_projects]))
"""

_LOGGING = """
import json, sys
from otto.session import install_logging
install_logging()
print(json.dumps([{init!r} in sys.modules]))
"""

_DISCOVERY = """
import json, sys
from otto import bootstrap
dirs = [d.name for d in bootstrap.get_env().ottopath]
print(json.dumps([dirs, bootstrap.is_bootstrapped(), {init!r} in sys.modules]))
"""

_NO_ROOT_READ = """
import json
from otto.declared import get_projects
try:
    get_projects()
except Exception as exc:
    print(json.dumps([type(exc).__name__, str(exc)]))
else:
    print(json.dumps(["no error", "get_repos returned"]))
"""

# Settings that make composition refuse: a declared product entry with no
# `[project] lab_patterns` (spec 2026-08-18 D2), which the root raises after the
# repos' init modules ran.
_UNSCOPED_PROVIDER = '\n[[products]]\nname = "pin-entry"\nkind = "noop"\n'


def test_import_otto_loads_neither_the_root_nor_the_catalog(tmp_path: Path) -> None:
    assert run_fresh(tmp_path, _COLD + _ARM) == [False, False]
    assert armed(tmp_path)
    assert compositions(tmp_path) == 1, "only the control's bootstrap() composes"


@pytest.mark.parametrize(
    ("facade", "name"),
    [
        ("otto.context", "get_context"),
        ("otto.context", "try_get_context"),
        ("otto.context", "set_context"),
        ("otto.context", "ProjectContextView"),
        ("otto.context", "RunPolicy"),
        ("otto", "get_context"),
        ("otto", "try_get_context"),
    ],
)
def test_a_context_name_that_needs_a_context_in_hand_loads_no_root(
    facade: str, name: str, tmp_path: Path
) -> None:
    assert run_fresh(tmp_path, _NO_ROOT_NAME.format(facade=facade, name=name) + _ARM) == [
        False,
        True,
    ]
    assert armed(tmp_path)
    assert compositions(tmp_path) == 1, "only the control's bootstrap() composes"


@pytest.mark.parametrize(
    ("facade", "name"),
    [
        ("otto.context", "OttoContext"),
        ("otto.context", "open_context"),
        ("otto", "OttoContext"),
        ("otto", "open_context"),
        ("otto", "run_tests"),
        ("otto.session", "install_logging"),
    ],
)
def test_resolving_a_composing_name_composes_nothing(
    facade: str, name: str, tmp_path: Path
) -> None:
    assert run_fresh(tmp_path, _RESOLVE.format(facade=facade, name=name) + _ARM) == [True]
    assert armed(tmp_path)
    assert compositions(tmp_path) == 1, "only the control's bootstrap() composes"


@pytest.mark.parametrize(
    "spelling",
    ["from otto.context import open_context", "from otto import open_context"],
    ids=["otto.context", "otto"],
)
def test_open_context_composes_once_and_opens_the_run_on_its_repos(
    spelling: str, tmp_path: Path
) -> None:
    assert run_fresh(tmp_path, _OPEN.format(spelling=spelling)) == [["repo"], ["repo"], 7.5]
    assert armed(tmp_path)
    assert compositions(tmp_path) == 1


def test_the_conformance_harness_context_composes_on_its_first_read(tmp_path: Path) -> None:
    assert run_fresh(tmp_path, _HARNESS) == ["repo"]
    assert armed(tmp_path)
    assert compositions(tmp_path) == 1


def test_run_tests_through_the_root_facade_composes_once_then_finds_nothing(
    tmp_path: Path,
) -> None:
    source = _ROOT_RUN_TESTS.format(out=str(tmp_path / "out"))
    assert run_fresh(tmp_path, source) == ["NoTestsMatchedError", "No tests matched the selection."]
    assert armed(tmp_path)
    assert compositions(tmp_path) == 1


@pytest.mark.parametrize(
    "spelling",
    ["from otto.context import OttoContext", "from otto import OttoContext"],
    ids=["otto.context", "otto"],
)
def test_a_hand_built_context_composes_on_its_first_read_and_only_then(
    spelling: str, tmp_path: Path
) -> None:
    assert run_fresh(tmp_path, _HAND_BUILT.format(spelling=spelling)) == [
        ["repo"],
        ["repo"],
        ["repo"],
    ]
    assert armed(tmp_path)
    assert compositions(tmp_path) == 1


def test_a_hand_built_context_caches_a_refused_composition(tmp_path: Path) -> None:
    assert run_fresh(tmp_path, _REFUSED, extra=_UNSCOPED_PROVIDER) == [
        "ProjectScopeError",
        2,
        True,
    ]
    assert armed(tmp_path)
    assert compositions(tmp_path) == 1, "the refusal is re-raised, never recomposed"


def test_a_context_handed_a_result_never_composes(tmp_path: Path) -> None:
    assert run_fresh(tmp_path, _GIVEN + _ARM) == [[], []]
    assert armed(tmp_path)
    assert compositions(tmp_path) == 1, "only the control's bootstrap() composes"


def test_the_repo_aware_logging_install_composes_once(tmp_path: Path) -> None:
    assert run_fresh(tmp_path, _LOGGING.format(init=INIT)) == [True]
    assert armed(tmp_path)
    assert compositions(tmp_path) == 1


def test_discovery_reads_the_repos_and_composes_nothing(tmp_path: Path) -> None:
    source = _DISCOVERY.format(init=INIT) + _ARM
    assert run_fresh(tmp_path, source) == [["repo"], False, False]
    assert armed(tmp_path)
    assert compositions(tmp_path) == 1, "only the control's bootstrap() composes"


def test_a_read_below_the_root_with_no_root_loaded_refuses_and_names_the_routes(
    tmp_path: Path,
) -> None:
    assert run_fresh(tmp_path, _NO_ROOT_READ + _ARM) == [
        "RuntimeError",
        (
            "Nothing composed otto's projects, and no composition root is loaded to compose "
            "them: call otto.bootstrap.bootstrap(), or open a run with "
            "otto.open_context(...)."
        ),
    ]
    assert armed(tmp_path)
    assert compositions(tmp_path) == 1, "only the control's bootstrap() composes"

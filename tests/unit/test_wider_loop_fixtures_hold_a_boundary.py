"""Every async fixture on a wider pytest-asyncio loop holds that loop's cleanup boundary.

A fixture with ``loop_scope`` class, module, package or session keeps its
hosts on that loop across tests. The root conftest's orphan guard does not
exempt a live wider runner loop, so unless the fixture holds a boundary for
its scope (:func:`tests._fixtures.scope_boundary.held_for_scope`), the first
test to use it errors at teardown with ``LeakedRegistrationError``.

Those fixtures live in the lab and docker trees (``tests/integration``,
``tests/e2e``), which no hostless gate runs, so the miss shows up first in a
full ``make coverage``: the docker and link-impair fixtures did exactly that.
The scan below catches it hostlessly; the inner session pins that the helper
really exempts the loop and closes what the fixture left behind.

What the scan cannot see, none of it present today:

- A sync fixture of wider scope whose host a test first connects on a wider
  loop (``pytestmark = pytest.mark.asyncio(loop_scope="module")``). The host
  registers on the module loop with no boundary, but the fixture declares no
  ``loop_scope`` for the scan to read.
- A ``loop_scope`` that is not a literal (a name, an expression). Only a
  string constant is read.
- A widened ``asyncio_default_fixture_loop_scope``. It is ``"function"``, so
  an async fixture that names no ``loop_scope`` runs on its test's own loop.
  Widened, every such fixture would be wider and the scan would see none of
  them; :func:`test_the_default_fixture_loop_scope_is_function` makes that
  widening fail loudly instead.

And what it can flag needlessly: a wider-loop fixture that holds no hosts, or
one in a SUT repo under ``tests/repo*`` that only ``otto test`` runs, where
the suite plugin already holds the boundary (and ``tests._fixtures`` may not
be importable). Neither exists today; if one appears, narrow the scan rather
than add a boundary the fixture does not need.
"""

import ast
from pathlib import Path

import pytest

from tests._fixtures.paths import PROJECT_ROOT
from tests._fixtures.root_conftest_pytester import INNER_ARGS, root_conftest_session

pytest_plugins = ["pytester"]

_WIDER_LOOPS = {"class", "module", "package", "session"}
_HOLDS_A_BOUNDARY = {"held_for_scope", "acquire_boundary"}


def _wider_loop_scope(decorator: ast.expr) -> "str | None":
    """The decorator's ``loop_scope`` when it is a fixture call naming a wider loop."""
    if not isinstance(decorator, ast.Call):
        return None
    func = decorator.func
    name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
    if name != "fixture":
        return None
    for keyword in decorator.keywords:
        if (
            keyword.arg == "loop_scope"
            and isinstance(keyword.value, ast.Constant)
            and keyword.value.value in _WIDER_LOOPS
        ):
            return keyword.value.value
    return None


def _holds_a_boundary(node: ast.AsyncFunctionDef) -> bool:
    for call in ast.walk(node):
        if isinstance(call, ast.Call):
            func = call.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            if name in _HOLDS_A_BOUNDARY:
                return True
    return False


def _wider_loop_fixtures() -> dict[str, bool]:
    """Each wider-loop async fixture under ``tests/`` (``path::name``), and whether it holds."""
    found: dict[str, bool] = {}
    for path in sorted((PROJECT_ROOT / "tests").rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        if "loop_scope=" not in source:
            continue
        for node in ast.walk(ast.parse(source, filename=str(path))):
            if isinstance(node, ast.AsyncFunctionDef) and any(
                _wider_loop_scope(d) for d in node.decorator_list
            ):
                found[f"{path.relative_to(PROJECT_ROOT)}::{node.name}"] = _holds_a_boundary(node)
    return found


def test_every_wider_loop_fixture_holds_its_loops_boundary() -> None:
    fixtures = _wider_loop_fixtures()
    # Never vacuous: the docker and link-impair modules each declare one.
    assert "tests/integration/test_docker_build.py::parent" in fixtures, sorted(fixtures)
    unheld = sorted(name for name, held in fixtures.items() if not held)
    assert not unheld, (
        "these fixtures keep hosts on a wider pytest-asyncio loop without holding its "
        "cleanup boundary, so the first test to use them errors at teardown with "
        f"LeakedRegistrationError; wrap the body in `held_for_scope`: {unheld}"
    )


def test_the_default_fixture_loop_scope_is_function(pytestconfig: pytest.Config) -> None:
    """The scan reads only an explicit ``loop_scope``, so a wider default would blind it."""
    default = pytestconfig.getini("asyncio_default_fixture_loop_scope")
    assert default == "function", (
        f"asyncio_default_fixture_loop_scope is {default!r}: every async fixture that "
        "names no loop_scope now runs on a wider loop, and the boundary scan above "
        "cannot see one of them. Teach the scan the new default before widening it."
    )


PROBE_HELPER = """\
# A module fixture shares a host across tests, holding the loop with the helper,
# and leaves the host for the helper's release to close.
import asyncio
from pathlib import Path

import pytest
import pytest_asyncio

from tests._fixtures.registry import register_duck
from tests._fixtures.scope_boundary import held_for_scope

pytestmark = pytest.mark.asyncio(loop_scope="module")


class _Double:
    id = "shared-host"

    async def close(self):
        Path(__file__).with_name("closed.txt").write_text(self.id)


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def shared_host():
    async with held_for_scope("the probe module's loop"):
        register_duck(_Double(), asyncio.get_running_loop())
        yield


async def test_a(shared_host):
    pass


async def test_b(shared_host):
    pass
"""


def test_held_for_scope_exempts_the_loop_and_closes_what_the_fixture_left(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    inner = root_conftest_session(pytester, monkeypatch)
    (inner.path / "test_probe_helper.py").write_text(PROBE_HELPER)
    result = inner.runpytest_subprocess(*INNER_ARGS, "-p", "no:randomly", "-rE", timeout=180)
    combined = str(result.stdout) + str(result.stderr)

    outcomes = {k: v for k, v in result.parseoutcomes().items() if k != "warnings"}
    assert outcomes == {"passed": 2}, combined
    assert result.ret == 0, combined
    assert (Path(inner.path) / "closed.txt").read_text() == "shared-host", combined

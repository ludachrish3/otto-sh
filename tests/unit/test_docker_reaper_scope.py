"""The docker-stack reaper runs for tests that NEED docker, never for a directory.

``tests/integration/conftest.py``'s ``reap_orphan_docker_stacks`` SSHes to the
docker host and ``docker rm -f``s every ``-e2e-`` / ``-noexist-`` stack. It was
``autouse=True`` at session scope, so running ANY test under
``tests/integration/`` — 27 of 31 files never mention docker — reaped the
shared bed, including a concurrent developer's live stacks (ledger
2026-08-16: a session ran ``cov/test_overrides_report.py``, a pure-git
module, and the reap executed twice against the lab). The fixture's own
docstring said "all tests in this directory drive docker"; the subtree had
outgrown it.

Two pins, because narrowing alone drifts again the moment the tree grows:

1. The fixture is NOT autouse. It is requested by name, from the modules
   that build a ``docker_capable=True`` host — the four docker modules, all
   of which already declare their need through
   ``xdist_group("docker_e2e")``.
2. The two declarations agree in both directions: every module that asks
   for the reaper is in the docker group, and every module in the docker
   group asks for the reaper. A docker test without the reap inherits the
   address-pool exhaustion the reap exists to prevent; a non-docker test
   with the reap re-creates the hazard this file closes.

The fixture also asserts the premise at runtime from ``session.items`` (see
its docstring) — an offender errors that session before anything is
reaped. This file is the half that runs in the default lane, where the
integration tree is never executed, so the drift is caught on push.

STATED BOUND: the third pin below keys on the literal ``docker_capable=True``
in a test module's source. A fifth docker module that built its host through
a shared helper (the ``tests/e2e/chaos/_docker.py`` shape) would carry no such
literal and pass all three; the runtime assertion still catches it the first
time it runs, because that module would not declare the group either.
"""

import ast
import re
from pathlib import Path

from otto.docker.compose import use_case_project
from tests._fixtures.paths import TESTS_ROOT

_INTEGRATION = TESTS_ROOT / "integration"
_CONFTEST = _INTEGRATION / "conftest.py"
_FIXTURE = "reap_orphan_docker_stacks"
_GROUP = "docker_e2e"

# The modules that mint a disposable compose-project suffix, and the shape
# they mint it with: `return "<literal>" + uuid.uuid4().hex[:8]`.
_E2E_SUFFIX_SOURCES = (
    TESTS_ROOT / "e2e" / "docker" / "test_docker_e2e_cli.py",
    TESTS_ROOT / "e2e" / "run" / "test_run_exec_e2e.py",
)
_FRESH_SUFFIX = re.compile(r"""return\s+["']([^"']+)["']\s*\+\s*uuid\.uuid4\(\)""")


def reaper_is_autouse(conftest_source: str) -> bool:
    """Whether the reaper fixture's decorator carries ``autouse=True`` (AST, not regex)."""
    tree = ast.parse(conftest_source)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == _FIXTURE:
            for decorator in node.decorator_list:
                if isinstance(decorator, ast.Call):
                    for keyword in decorator.keywords:
                        if keyword.arg == "autouse":
                            return bool(getattr(keyword.value, "value", False))
            return False
    raise AssertionError(f"{_CONFTEST} no longer defines `{_FIXTURE}`")


def modules_declaring(source: str) -> tuple[bool, bool]:
    """(in the docker xdist group, requests the reaper) for one module's source."""
    in_group = (
        re.search(rf"xdist_group\(\s*(?:name\s*=\s*)?[\"']{_GROUP}[\"']\s*\)", source) is not None
    )
    wants_reap = re.search(rf"usefixtures\(\s*[\"']{_FIXTURE}[\"']\s*\)", source) is not None
    return in_group, wants_reap


def _integration_modules() -> list[Path]:
    return sorted(p for p in _INTEGRATION.rglob("test_*.py"))


def test_the_reaper_is_requested_not_ambient() -> None:
    assert not reaper_is_autouse(_CONFTEST.read_text()), (
        f"`{_FIXTURE}` is autouse again — every test under tests/integration/ would reap "
        f"the shared docker host, including modules that never touch docker (the 2026-08-16 "
        f"ledger). It must be requested by the docker modules and nothing else"
    )


def test_every_docker_module_requests_the_reaper_and_no_other_module_does() -> None:
    disagreements = []
    docker_modules = []
    for path in _integration_modules():
        in_group, wants_reap = modules_declaring(path.read_text())
        if in_group:
            docker_modules.append(path.name)
        if in_group != wants_reap:
            disagreements.append(
                f"{path.relative_to(TESTS_ROOT)}: xdist_group({_GROUP!r})={in_group}, "
                f"usefixtures({_FIXTURE!r})={wants_reap}"
            )
    assert docker_modules, "no module declares the docker group — the premise of this pin is gone"
    assert not disagreements, (
        "a module's docker need and its reaper request disagree — a docker test without the "
        "reap inherits address-pool exhaustion, a non-docker test with it reaps the shared bed:\n  "
        + "\n  ".join(disagreements)
    )


def test_docker_capable_hosts_are_built_only_in_docker_modules() -> None:
    """The need is declared where it is exercised: `docker_capable=True` lives in the group."""
    stray = [
        str(path.relative_to(TESTS_ROOT))
        for path in _integration_modules()
        if "docker_capable=True" in path.read_text() and not modules_declaring(path.read_text())[0]
    ]
    assert not stray, (
        f"module(s) build a docker_capable host without declaring xdist_group({_GROUP!r}): "
        f"{stray} — they share test3's compose staging dir and must join the group (and the "
        f"reap) rather than race it"
    )


def test_the_scanners_see_the_shapes_they_judge() -> None:
    """Positive controls: an autouse decorator, and both agreement failures, read correctly."""
    autouse = (
        "import pytest\n\n@pytest.fixture(scope='session', autouse=True)\n"
        f"def {_FIXTURE}():\n    pass\n"
    )
    plain = f"import pytest\n\n@pytest.fixture(scope='session')\ndef {_FIXTURE}():\n    pass\n"
    assert reaper_is_autouse(autouse)
    assert not reaper_is_autouse(plain)
    assert modules_declaring(f'pytestmark = pytest.mark.xdist_group("{_GROUP}")\n') == (True, False)
    assert modules_declaring(f'pytestmark = [pytest.mark.usefixtures("{_FIXTURE}")]\n') == (
        False,
        True,
    )
    assert modules_declaring("x = 1\n") == (False, False)


def orphan_project_fragments(conftest_source: str) -> tuple[str, ...]:
    """The reaper's infix list, read off the conftest SOURCE (AST, not import).

    Read rather than imported for the reason the rest of this module reads
    source: importing ``tests.integration.conftest`` from the default lane
    would execute a conftest for a tree this lane never collects.
    """
    tree = ast.parse(conftest_source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "_ORPHAN_PROJECT_FRAGMENTS" for t in node.targets
        ):
            return tuple(ast.literal_eval(node.value))
    raise AssertionError(f"{_CONFTEST} no longer defines `_ORPHAN_PROJECT_FRAGMENTS`")


def minted_suffixes(source: str) -> list[str]:
    """The literal prefixes an e2e module's ``fresh_suffix`` fixture mints."""
    return _FRESH_SUFFIX.findall(source)


def test_the_e2e_suffix_idiom_still_yields_a_reapable_project_name() -> None:
    """The reap and the naming scheme must keep agreeing (spec §9).

    The reaper filters on INFIXES (``-e2e-``), never on a prefix, which is
    the only reason it survived the use-case cutover: compose projects went
    from ``otto-<repo>-<suffix>`` to ``<lab>-<usecase>-<suffix>`` and the
    ``otto-`` prefix is gone for good. Nothing FAILS when a reap stops
    matching — orphans from crashed runs simply accumulate until the docker
    daemon runs out of address pools and every subsequent ``compose up``
    breaks, on a machine nobody is looking at. So it is asserted here, on the
    real name-builder rather than on a hand-written string.

    Both halves are checked because docker derives the second from the first:
    the reap greps ``docker ps --filter name=`` and ``docker network ls
    --filter name=``, which match CONTAINER and NETWORK names
    (``<project>-<service>-<n>``, ``<project>_default``), not the project.
    """
    fragments = orphan_project_fragments(_CONFTEST.read_text())
    assert fragments, "the reaper matches nothing at all"

    minted = [
        (path, suffix)
        for path in _E2E_SUFFIX_SOURCES
        for suffix in minted_suffixes(path.read_text())
    ]
    assert len(minted) == len(_E2E_SUFFIX_SOURCES), (
        f"expected one `fresh_suffix` literal per e2e module, found {minted} — "
        f"the regex no longer sees the shape it judges"
    )

    # A real project name, from the production builder — not a literal
    # assembled here, which would keep passing after the scheme changed.
    projects = [(path, use_case_project("unix", "integration", suffix)) for path, suffix in minted]
    unreapable = [
        f"{path.relative_to(TESTS_ROOT)}: {name!r}"
        for path, project in projects
        for name in (project, f"{project}-api-1", f"{project}_default")
        if not any(frag in name for frag in fragments)
    ]
    assert not unreapable, (
        f"a compose name minted by an e2e module carries none of the reaper's "
        f"infixes {fragments} — a crashed run would leak that stack forever:\n  "
        + "\n  ".join(unreapable)
    )

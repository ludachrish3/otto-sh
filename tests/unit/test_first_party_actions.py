"""Otto's six project instructions reach the tables through a slot the tables never import.

The instruction tables sit below ``otto.project``, and ``otto.project.actions``
holds otto's own ``ProjectActions``. So the tables keep a one-row table,
``FIRST_PARTY_ACTIONS``, that the actions module fills (``declare()``, at its
import and again on demand), and they derive the six from it (spec 2026-10-11
placement-and-phases §2.1, §3.2). These tests pin what that must keep:

* a view read before the slot is filled derives the six once it is filled;
* declaring is idempotent and atomic, and a second class is refused;
* with the slot empty, the six names are still refused to a standalone
  instruction, and no repo's actions class qualifies;
* ``run_instruction`` and ``bootstrap()`` declare before they read, and
  ``bootstrap()`` before any repo's init runs;
* the slot is otto's (repo ``None``) even when a repo's init import is what
  first imports the installer, and a raw write under a repo's marker is refused.
"""

import dataclasses
import inspect
import os
import subprocess
import sys

import pytest

from otto import bootstrap as bs
from otto.context import OttoContext
from otto.instruction_catalog import (
    FIRST_PARTY_ACTIONS,
    FIRST_PARTY_INSTRUCTIONS,
    INSTRUCTIONS,
    PROJECT_ACTIONS,
    PROJECT_INSTRUCTIONS,
    STANDALONE_INSTRUCTIONS,
    ProjectActionsEntry,
    ProjectInstructionError,
    _bodies_of,
    declare_first_party_actions,
)
from otto.instructions import instruction, run_instruction
from otto.labs.loader import Lab
from otto.project.actions import ProjectActions, declare, register_project_actions
from otto.registry import DuplicateRegistration, get_registering_project, registering_project
from tests._fixtures.sutrepo import make_sut_repo

SIX = sorted(FIRST_PARTY_INSTRUCTIONS)


def _empty_the_slot() -> None:
    """Leave the slot as a process that never imported otto.project.actions has it.

    The root conftest restores the table after the test and fills it again
    while ``otto.project.actions`` is loaded.
    """
    if "otto" in FIRST_PARTY_ACTIONS:
        FIRST_PARTY_ACTIONS.unregister("otto")


async def _handler() -> None: ...


_ACCEPTANCE = """
import sys
from otto.instruction_catalog import INSTRUCTIONS
early = sorted(INSTRUCTIONS.names())
assert 'otto.project.actions' not in sys.modules, 'reading the tables imported the project layer'
{initialize}
entry = INSTRUCTIONS.get('install')
print(early, sorted(INSTRUCTIONS.names()), entry.project.spec.declared_by, entry.module)
"""


@pytest.mark.parametrize(
    "initialize",
    [
        "import otto.project.actions",
        "from otto.bootstrap import bootstrap; bootstrap()",
    ],
    ids=["import-actions", "bootstrap"],
)
def test_a_view_read_early_derives_the_six_once_otto_declares_them(initialize, tmp_path):
    """The acceptance case: read early, initialize, read again, in a fresh process."""
    env = {
        **os.environ,
        "OTTOPATH": str(make_sut_repo(tmp_path / "repo", name="repo")),
        "OTTO_HOME": str(tmp_path / "home"),
    }
    out = subprocess.run(
        [sys.executable, "-c", _ACCEPTANCE.format(initialize=initialize)],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
        env=env,
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.split("\n")[0] == f"[] {SIX} None otto.project.actions"


def test_otto_s_class_is_declared_by_its_installer_outside_any_repo():
    entry = FIRST_PARTY_ACTIONS.peek("otto")
    assert entry.cls is ProjectActions
    assert sorted(body.name for body in entry.bodies) == SIX
    assert {body.project for body in entry.bodies} == {None}
    assert FIRST_PARTY_ACTIONS.origin("otto") == "otto.project.actions"
    assert FIRST_PARTY_ACTIONS.project("otto") is None


def test_redeclaring_inside_a_repo_s_import_keeps_the_slot_otto_s():
    _empty_the_slot()
    with registering_project("acme"):
        declare()
        assert get_registering_project() == "acme"
    assert FIRST_PARTY_ACTIONS.project("otto") is None
    assert {body.project for body in FIRST_PARTY_ACTIONS.peek("otto").bodies} == {None}


@dataclasses.dataclass(frozen=True)
class _Parameter:
    """One parameter of a signature: whether its type is ``str``, however spelled."""

    name: str
    kind: str
    """The kind's name, e.g. ``POSITIONAL_OR_KEYWORD``."""
    default: object
    annotated_str: bool


def test_the_public_project_marker_is_unchanged():
    """The slot's owner reset is private: registering_project keeps its base signature and contract.

    Compared parameter by parameter (name, kind, default, type), so a change
    in how an annotation is spelled is not a signature change.
    """
    signature = inspect.signature(registering_project)
    assert [
        _Parameter(p.name, p.kind.name, p.default, p.annotation in {str, "str"})
        for p in signature.parameters.values()
    ] == [
        _Parameter(
            "name", inspect.Parameter.POSITIONAL_OR_KEYWORD.name, inspect.Parameter.empty, True
        )
    ]
    assert registering_project.__doc__ == (
        "Attribute registrations inside this block to project *name* (bootstrap-only)."
    )
    with registering_project("acme"):
        assert get_registering_project() == "acme"
    assert get_registering_project() is None


_FIRST_IMPORT_IN_A_REPO = """
from otto.registry import get_registering_project, registering_project
with registering_project('acme'):
    import otto.project.actions
    inside = get_registering_project()
from otto.instruction_catalog import FIRST_PARTY_ACTIONS
print(FIRST_PARTY_ACTIONS.project('otto'), inside)
"""


def test_a_first_import_inside_a_repo_s_import_keeps_the_slot_otto_s(tmp_path):
    """A fresh process whose first import of the installer runs under a repo's marker."""
    out = subprocess.run(
        [sys.executable, "-c", _FIRST_IMPORT_IN_A_REPO],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
        env={**os.environ, "OTTO_HOME": str(tmp_path / "home")},
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.split() == ["None", "acme"]


def test_a_raw_write_under_a_repo_s_marker_is_refused():
    """The table's own check keeps the row otto's when a write goes around the writer."""
    _empty_the_slot()
    entry = ProjectActionsEntry(ProjectActions, _bodies_of(ProjectActions, None))
    with (
        registering_project("acme"),
        pytest.raises(ValueError, match="first-party actions are otto's, but project 'acme'"),
    ):
        FIRST_PARTY_ACTIONS.register("otto", entry)
    assert "otto" not in FIRST_PARTY_ACTIONS


def test_declaring_again_does_nothing():
    revision = FIRST_PARTY_ACTIONS.revision
    declare()
    declare_first_party_actions(ProjectActions)
    assert FIRST_PARTY_ACTIONS.revision == revision
    assert FIRST_PARTY_ACTIONS.peek("otto").cls is ProjectActions


def test_a_second_class_is_refused_and_changes_nothing():
    other = type("OtherActions", (ProjectActions,), {})
    revision = FIRST_PARTY_ACTIONS.revision
    with pytest.raises(DuplicateRegistration):
        declare_first_party_actions(other)
    assert FIRST_PARTY_ACTIONS.peek("otto").cls is ProjectActions
    assert FIRST_PARTY_ACTIONS.revision == revision


def test_a_refused_declaration_stores_nothing():
    """A class that does not mark the six is refused whole, by the table's own check."""
    _empty_the_slot()
    revision = FIRST_PARTY_ACTIONS.revision
    with pytest.raises(ProjectInstructionError, match="first-party instructions are"):
        declare_first_party_actions(type("Bare", (), {}))
    assert "otto" not in FIRST_PARTY_ACTIONS
    assert FIRST_PARTY_ACTIONS.revision == revision


def test_the_views_follow_the_slot():
    _empty_the_slot()
    assert "install" not in PROJECT_INSTRUCTIONS
    assert "install" not in INSTRUCTIONS
    revision = PROJECT_INSTRUCTIONS.revision
    declare()
    assert sorted(PROJECT_INSTRUCTIONS.names()) == SIX
    assert PROJECT_INSTRUCTIONS.revision != revision
    assert INSTRUCTIONS.get("install").project.spec.declared_by is None


@pytest.mark.parametrize("project", [None, "acme"], ids=["outside-a-repo", "from-a-repo"])
def test_an_empty_slot_still_refuses_the_six_to_a_standalone_instruction(project):
    _empty_the_slot()
    register = instruction(name="install")
    if project is None:
        with pytest.raises(ProjectInstructionError, match="first-party instructions"):
            register(_handler)
    else:
        with registering_project(project), pytest.raises(ValueError, match="project instruction"):
            register(_handler)
    assert "install" not in STANDALONE_INSTRUCTIONS


def test_an_empty_slot_refuses_every_repo_actions_class():
    _empty_the_slot()
    with (
        registering_project("acme"),
        pytest.raises(TypeError, match="is not a ProjectActions subclass"),
    ):
        register_project_actions(type("AcmeActions", (ProjectActions,), {}))
    assert PROJECT_ACTIONS.names() == []


@pytest.mark.asyncio
async def test_run_instruction_declares_before_its_lookup():
    """A library caller with an empty slot still sees otto's six (the lookup error lists them)."""
    _empty_the_slot()
    with pytest.raises(ValueError, match=r"Registered: .*install"):
        await run_instruction(OttoContext(lab=Lab(name="t")), "no-such-instruction")


_LIBRARY = """
import asyncio
import sys
from otto.labs.loader import Lab
from otto.context import OttoContext
from otto.instructions import run_instruction
before = 'otto.project.actions' in sys.modules
try:
    asyncio.run(run_instruction(OttoContext(lab=Lab(name='t')), 'no-such-instruction'))
except ValueError as exc:
    print(before, 'install' in str(exc))
"""


def test_a_library_caller_that_never_bootstrapped_sees_the_six(tmp_path):
    """A fresh process: nothing imported the installer before run_instruction did."""
    out = subprocess.run(
        [sys.executable, "-c", _LIBRARY],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
        env={**os.environ, "OTTO_HOME": str(tmp_path / "home")},
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.split() == ["False", "True"]


_ORDER_INIT_MODULE = "first_party_order_init"

_ORDER_INIT = """
from otto.project import ProjectActions, register_project_actions


@register_project_actions
class OrderActions(ProjectActions):
    pass
"""


def test_bootstrap_declares_before_any_repo_init(tmp_path, monkeypatch):
    """The repo's actions class qualifies only if otto's class was declared before its init ran.

    With the slot empty every repo actions class is refused, and bootstrap
    contains that refusal as the repo's init error; so a declare that moved
    after the repo loop fails here, not only one that went missing.
    """
    project = make_sut_repo(
        tmp_path / "repo",
        name="repo",
        extra=f'libs = ["lib"]\ninit = ["{_ORDER_INIT_MODULE}"]\n',
        files={f"lib/{_ORDER_INIT_MODULE}.py": _ORDER_INIT},
    )
    monkeypatch.setenv("OTTOPATH", str(project))
    monkeypatch.delitem(sys.modules, _ORDER_INIT_MODULE, raising=False)
    monkeypatch.setattr(sys, "path", list(sys.path))
    bs._reset()
    _empty_the_slot()
    try:
        result = bs.bootstrap()
    finally:
        bs._reset()
        sys.modules.pop(_ORDER_INIT_MODULE, None)
    assert FIRST_PARTY_ACTIONS.peek("otto").cls is ProjectActions
    assert result.errors == []
    assert PROJECT_ACTIONS.names() == ["repo"]
    assert PROJECT_ACTIONS.peek("repo").cls.__name__ == "OrderActions"

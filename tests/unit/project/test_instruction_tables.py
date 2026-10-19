"""Three writable sources, one constraint, two derived views.

``STANDALONE_INSTRUCTIONS`` (``@instruction()`` on a plain function),
``PROJECT_ACTIONS`` (one ``ProjectActions`` class per project, carrying its
bodies) and ``FIRST_PARTY_ACTIONS`` (otto's own class, which
``otto.project.actions`` declares) are the only tables anything writes.
``PROJECT_INSTRUCTIONS`` and ``INSTRUCTIONS`` are derived from them, with
otto's own bodies first, and one check over the sources refuses a
registration that would leave them inconsistent.
"""

import os
import subprocess
import sys
from typing import Annotated

import click
import pytest
import typer

from otto import options
from otto.instruction_catalog import (
    INSTRUCTIONS,
    PROJECT_ACTIONS,
    PROJECT_INSTRUCTIONS,
    STANDALONE_INSTRUCTIONS,
    ProjectActionsEntry,
    ProjectInstructionError,
)
from otto.instructions import instruction
from otto.project import InstallOptions, ProjectActions, register_project_actions
from otto.registry import DuplicateRegistration, registering_project
from tests._fixtures.project_scaffold import make_project


def _actions(name: str, method: str = "smoke", **shape):
    @instruction(name=method, **shape)
    async def body(self) -> None: ...

    return type(name, (ProjectActions,), {method.replace("-", "_"): body})


def _bare(name: str):
    """A ProjectActions subclass that declares no project instruction of its own."""
    return type(name, (ProjectActions,), {})


def test_otto_s_bodies_come_from_their_own_table_not_a_project_s():
    assert PROJECT_ACTIONS.names() == []  # no project registered anything
    assert "install" in PROJECT_INSTRUCTIONS  # derived from FIRST_PARTY_ACTIONS


def test_the_views_have_no_mutation_method():
    for view in (INSTRUCTIONS, PROJECT_INSTRUCTIONS):
        assert not hasattr(view, "register")
        assert not hasattr(view, "unregister")


def test_a_project_instruction_s_entry_has_no_project_and_the_first_declarer_s_module():
    with registering_project("a"):
        register_project_actions(_actions("A"))
    assert INSTRUCTIONS.project("smoke") is None
    assert INSTRUCTIONS.get("smoke").module == __name__


def test_re_registering_the_same_class_needs_overwrite():
    cls = _actions("A")
    with registering_project("a"):
        register_project_actions(cls)
        with pytest.raises(DuplicateRegistration):
            register_project_actions(cls)
        register_project_actions(cls, overwrite=True)
    assert PROJECT_ACTIONS.get("a").cls is cls


def test_replacing_an_actions_entry_drops_its_old_bodies():
    with registering_project("a"):
        register_project_actions(_actions("A", "smoke"))
        register_project_actions(_actions("A2", "other"), overwrite=True)
    assert "smoke" not in INSTRUCTIONS
    assert "other" in INSTRUCTIONS


def test_a_rejected_contribution_changes_neither_source_nor_view():
    with registering_project("a"):
        register_project_actions(_actions("A", walk="reverse"))
    before = (PROJECT_ACTIONS.revision, STANDALONE_INSTRUCTIONS.revision, INSTRUCTIONS.names())
    with (
        registering_project("b"),
        pytest.raises(ProjectInstructionError, match="fixes the walk shape"),
    ):
        register_project_actions(_actions("B", walk="forward"))
    assert (
        PROJECT_ACTIONS.revision,
        STANDALONE_INSTRUCTIONS.revision,
        INSTRUCTIONS.names(),
    ) == before


def test_a_project_declared_over_a_project_s_standalone_instruction_names_its_owner():
    async def smoke() -> None: ...

    with registering_project("s"):
        instruction(name="smoke")(smoke)
    with (
        registering_project("a"),
        pytest.raises(
            ProjectInstructionError,
            match=r"'smoke' \(declared by project 'a' on A\) and a standalone instruction "
            r"registered by project 's' \(",
        ),
    ):
        register_project_actions(_actions("A"))


def test_a_standalone_instruction_over_a_project_instruction_is_refused():
    """From a project's init import it is the ProjectActions advice;
    from anywhere else, the clash.
    """
    with registering_project("a"):
        register_project_actions(_actions("A"))

    async def smoke() -> None: ...

    with registering_project("s"), pytest.raises(ValueError, match="is a project instruction"):
        instruction(name="smoke")(smoke)
    with pytest.raises(ProjectInstructionError, match=r"registered by otto \("):
        instruction(name="smoke")(smoke)
    assert "smoke" not in STANDALONE_INSTRUCTIONS


def test_an_omitted_shape_keyword_inherits_the_first_declarer_s_value():
    with registering_project("a"):
        register_project_actions(_actions("A", walk="reverse"))
    with registering_project("b"):
        register_project_actions(_actions("B"))
    assert PROJECT_INSTRUCTIONS.get("smoke").spec.walk == "reverse"
    (body,) = PROJECT_ACTIONS.get("b").bodies
    assert dict(body.shape) == {}


def test_removing_the_first_declarer_lifts_its_shape():
    with registering_project("a"):
        register_project_actions(_actions("A", walk="reverse"))
    with registering_project("b"):
        register_project_actions(_actions("B"))
    with registering_project("a"):
        register_project_actions(_bare("A2"), overwrite=True)
    assert PROJECT_INSTRUCTIONS.get("smoke").spec.walk == "forward"
    with registering_project("c"):
        register_project_actions(_actions("C", walk="forward"))
    assert [b.project for b in PROJECT_INSTRUCTIONS.get("smoke").bodies] == ["b", "c"]


def test_replacing_the_first_declarer_s_shape_is_checked_against_the_others():
    a = _actions("A", walk="reverse")
    with registering_project("a"):
        register_project_actions(a)
    with registering_project("b"):
        register_project_actions(_actions("B", walk="reverse"))
    revision = PROJECT_ACTIONS.revision
    with (
        registering_project("a"),
        pytest.raises(ProjectInstructionError, match="fixes the walk shape"),
    ):
        register_project_actions(_actions("A2", walk="forward"), overwrite=True)
    assert PROJECT_ACTIONS.revision == revision
    assert PROJECT_ACTIONS.get("a").cls is a
    assert PROJECT_INSTRUCTIONS.get("smoke").spec.walk == "reverse"


def test_an_override_of_a_first_party_instruction_needs_its_options_base():
    with (
        registering_project("a"),
        pytest.raises(ProjectInstructionError, match=r"must inherit otto\.project\.InstallOptions"),
    ):
        register_project_actions(_actions("A", "install"))
    assert "a" not in PROJECT_ACTIONS


def test_a_first_party_override_with_the_options_base_is_accepted():
    @options
    class WidgetInstall(InstallOptions):
        pass

    class A(ProjectActions):
        @instruction(options=WidgetInstall)
        async def install(self, opts: WidgetInstall):
            return await super().install(opts)

    with registering_project("a"):
        register_project_actions(A)
    entry = PROJECT_INSTRUCTIONS.get("install")
    assert [b.project for b in entry.bodies] == [None, "a"]
    assert entry.first_party_options is InstallOptions


def test_two_bodies_for_one_owner_and_name_are_refused():
    @instruction(name="smoke")
    async def one(self) -> None: ...

    @instruction(name="smoke")
    async def two(self) -> None: ...

    twice = type("Twice", (ProjectActions,), {"one": one, "two": two})
    with registering_project("a"), pytest.raises(ProjectInstructionError, match="twice"):
        register_project_actions(twice)
    assert "a" not in PROJECT_ACTIONS

    # The table refuses the same thing in a hand-built record.
    single = _actions("Single")
    with registering_project("a"):
        register_project_actions(single)
    (body,) = PROJECT_ACTIONS.get("a").bodies
    with registering_project("a"), pytest.raises(ProjectInstructionError, match="twice"):
        PROJECT_ACTIONS.register("a", ProjectActionsEntry(single, (body, body)), overwrite=True)


def test_one_class_registered_by_two_projects_is_refused_naming_both():
    shared = _actions("Shared")
    with registering_project("a"):
        register_project_actions(shared)
    with (
        registering_project("b"),
        pytest.raises(
            ProjectInstructionError,
            match=r"^project 'b' registers class Shared, which project 'a' already registered -- "
            r"each project registers its own ProjectActions subclass$",
        ),
    ):
        register_project_actions(shared)
    assert "b" not in PROJECT_ACTIONS


def test_a_project_claiming_install_through_the_decorator_gets_today_s_message(monkeypatch):
    monkeypatch.delitem(sys.modules, "otto.project.actions", raising=False)

    async def install() -> None: ...

    with registering_project("a"), pytest.raises(ValueError, match="is a project instruction"):
        instruction()(install)
    assert "otto.project.actions" not in sys.modules
    assert "install" not in STANDALONE_INSTRUCTIONS


def test_check_project_instruction_options_names_both_projects_on_a_clash():
    from otto.params import OptionsCollisionError
    from otto.project.commands import check_project_instruction_options

    @options
    class AInstall(InstallOptions):
        lab_env: Annotated[str, typer.Option(help="A's lab environment.")] = "bench"

    @options
    class BInstall(InstallOptions):
        lab_env: Annotated[str, typer.Option(help="B's lab environment.")] = "floor"

    class A(ProjectActions):
        @instruction(options=AInstall)
        async def install(self, opts: AInstall):
            return await super().install(opts)

    class B(ProjectActions):
        @instruction(options=BInstall)
        async def install(self, opts: BInstall):
            return await super().install(opts)

    with registering_project("a"):
        register_project_actions(A)
    with registering_project("b"):
        register_project_actions(B)
    with pytest.raises(OptionsCollisionError) as excinfo:
        check_project_instruction_options()
    message = str(excinfo.value)
    assert "project 'a'" in message
    assert "project 'b'" in message


_ACTIONS_INIT = """\
from typing import Annotated

import typer

from otto import options
from otto.instructions import instruction
from otto.project import ProjectActions, register_project_actions


@options
class {cls}Opts:
    {field}: Annotated[str, typer.Option()] = "x"


@register_project_actions
class {cls}(ProjectActions):
    @instruction(name="tables-smoke", options={cls}Opts)
    async def smoke(self, opts: {cls}Opts) -> None: ...
"""


def test_post_loop_options_check_is_idempotent(tmp_path, monkeypatch):
    from otto import bootstrap as bs
    from otto.project.commands import check_project_instruction_options

    dirs = []
    for project, cls, field in (("tables-a", "TablesA", "alpha"), ("tables-b", "TablesB", "beta")):
        module = f"{cls.lower()}_init"
        dirs.append(
            make_project(
                tmp_path / project,
                name=project,
                extra=f'libs = ["lib"]\ninit = ["{module}"]\n',
                files={f"lib/{module}.py": _ACTIONS_INIT.format(cls=cls, field=field)},
            )
        )
    monkeypatch.setenv("OTTOPATH", os.pathsep.join(str(d) for d in dirs))
    bs._reset()
    try:
        result = bs.bootstrap()
        assert result.errors == []
        assert sorted(PROJECT_ACTIONS.names()) == ["tables-a", "tables-b"]
        check_project_instruction_options()
        check_project_instruction_options()
    finally:
        bs._reset()
    assert [b.project for b in PROJECT_INSTRUCTIONS.get("tables-smoke").bodies] == [
        "tables-a",
        "tables-b",
    ]


def _run_group():
    from otto.cli.run import run_app

    group = typer.main.get_command(run_app)
    return group, click.Context(group)


def test_the_run_group_serves_a_replaced_instruction():
    async def tables_smoke() -> None:
        """The first one."""

    async def tables_smoke_again() -> None:
        """The replacement."""

    instruction(name="tables-smoke")(tables_smoke)
    group, ctx = _run_group()
    first = group.get_command(ctx, "tables-smoke")
    assert first is not None
    assert group.get_command(ctx, "tables-smoke") is first  # cached while nothing changes
    instruction(name="tables-smoke", overwrite=True)(tables_smoke_again)
    second = group.get_command(ctx, "tables-smoke")
    assert second is not first
    assert "replacement" in (second.help or "")


@pytest.mark.asyncio
async def test_run_instruction_refuses_an_inactive_owner_through_the_view_s_project():
    from otto.context import OttoContext
    from otto.instructions import run_instruction
    from otto.labs.loader import Lab
    from otto.session import InstructionInactiveError

    ran: list[str] = []

    async def tables_blink() -> None:
        ran.append("blink")

    with registering_project("acme"):
        instruction(name="tables-blink")(tables_blink)
    assert INSTRUCTIONS.project("tables-blink") == "acme"
    ctx = OttoContext(lab=Lab(name="t"), exclude_projects=("acme",))
    ctx.scopes = {}
    with pytest.raises(InstructionInactiveError):
        await run_instruction(ctx, "tables-blink")
    assert ran == []


def test_actions_for_returns_the_registered_class():
    from otto.context import OttoContext
    from otto.labs.loader import Lab
    from otto.project import actions_for
    from tests._fixtures.fake_project import fake_project

    cls = _actions("A")
    with registering_project("a"):
        register_project_actions(cls)
    ctx = OttoContext(lab=Lab(name="t"))
    assert type(actions_for(fake_project("a"), ctx)) is cls
    assert type(actions_for(fake_project("other"), ctx)) is ProjectActions


def test_a_registry_check_imports_nothing_from_the_project_layer_in_a_fresh_process():
    """A standalone registration's check reads only the tables, never ``otto.project``.

    The six names are refused from the static ``FIRST_PARTY_INSTRUCTIONS``, so
    the check needs nothing ``otto.project.actions`` declares.
    """
    script = (
        "import sys\n"
        "from otto.instruction_catalog import STANDALONE_INSTRUCTIONS, instruction\n"
        "from otto.registry import registering_project\n"
        "assert 'otto.project.actions' not in sys.modules\n"
        "async def deploy(): ...\n"
        "with registering_project('acme'):\n"
        "    instruction()(deploy)\n"
        "assert 'otto.project.actions' not in sys.modules, 'the check imported the project layer'\n"
        "print(STANDALONE_INSTRUCTIONS.project('deploy'))\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, check=False, timeout=120
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "acme"


@pytest.mark.parametrize(
    ("script", "declared"),
    [
        (
            (
                "import otto.project.actions, otto.instruction_catalog as i; "
                "print(sorted(i.PROJECT_INSTRUCTIONS.names()))"
            ),
            True,
        ),
        (
            (
                "import sys, otto.instruction_catalog as i; "
                "print(sorted(i.INSTRUCTIONS.names())); "
                "assert 'otto.project.actions' not in sys.modules"
            ),
            False,
        ),
    ],
    ids=["actions-first", "instructions-first"],
)
def test_importing_the_tables_in_a_fresh_process(script, declared):
    """The six are there once ``otto.project.actions`` is imported, and not before."""
    out = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, check=False, timeout=120
    )
    assert out.returncode == 0, out.stderr
    assert ("'install'" in out.stdout) is declared

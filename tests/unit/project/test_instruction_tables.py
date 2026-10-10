"""Two writable sources, one constraint, two derived views.

``STANDALONE_INSTRUCTIONS`` (``@instruction()`` on a plain function) and
``PROJECT_ACTIONS`` (one ``ProjectActions`` class per repo, carrying its
bodies) are the only tables anything writes. ``PROJECT_INSTRUCTIONS`` and
``INSTRUCTIONS`` are derived from them, with otto's own bodies
(``FIRST_PARTY_BODIES``, a constant) first, and one check over both sources
refuses a registration that would leave them inconsistent.
"""

import subprocess
import sys
from typing import Annotated

import click
import pytest
import typer

from otto import options
from otto.instructions import (
    INSTRUCTIONS,
    PROJECT_ACTIONS,
    PROJECT_INSTRUCTIONS,
    STANDALONE_INSTRUCTIONS,
    ProjectActionsEntry,
    ProjectInstructionError,
    instruction,
)
from otto.project import InstallOptions, ProjectActions, register_project_actions
from otto.registry import DuplicateRegistration, registering_repo
from tests._fixtures.sutrepo import make_sut_repo


def _actions(name: str, method: str = "smoke", **shape):
    @instruction(name=method, **shape)
    async def body(self) -> None: ...

    return type(name, (ProjectActions,), {method.replace("-", "_"): body})


def _bare(name: str):
    """A ProjectActions subclass that declares no project instruction of its own."""
    return type(name, (ProjectActions,), {})


def test_otto_s_bodies_are_a_constant_not_a_registration():
    assert PROJECT_ACTIONS.names() == []  # nothing registered at import
    assert "install" in PROJECT_INSTRUCTIONS  # derived from FIRST_PARTY_BODIES


def test_the_views_have_no_mutation_method():
    for view in (INSTRUCTIONS, PROJECT_INSTRUCTIONS):
        assert not hasattr(view, "register")
        assert not hasattr(view, "unregister")


def test_a_project_instruction_s_entry_has_no_repo_and_the_first_declarer_s_module():
    with registering_repo("a"):
        register_project_actions(_actions("A"))
    assert INSTRUCTIONS.repo("smoke") is None
    assert INSTRUCTIONS.get("smoke").module == __name__


def test_re_registering_the_same_class_needs_overwrite():
    cls = _actions("A")
    with registering_repo("a"):
        register_project_actions(cls)
        with pytest.raises(DuplicateRegistration):
            register_project_actions(cls)
        register_project_actions(cls, overwrite=True)
    assert PROJECT_ACTIONS.get("a").cls is cls


def test_replacing_an_actions_entry_drops_its_old_bodies():
    with registering_repo("a"):
        register_project_actions(_actions("A", "smoke"))
        register_project_actions(_actions("A2", "other"), overwrite=True)
    assert "smoke" not in INSTRUCTIONS
    assert "other" in INSTRUCTIONS


def test_a_rejected_contribution_changes_neither_source_nor_view():
    with registering_repo("a"):
        register_project_actions(_actions("A", walk="reverse"))
    before = (PROJECT_ACTIONS.revision, STANDALONE_INSTRUCTIONS.revision, INSTRUCTIONS.names())
    with (
        registering_repo("b"),
        pytest.raises(ProjectInstructionError, match="fixes the walk shape"),
    ):
        register_project_actions(_actions("B", walk="forward"))
    assert (
        PROJECT_ACTIONS.revision,
        STANDALONE_INSTRUCTIONS.revision,
        INSTRUCTIONS.names(),
    ) == before


def test_a_project_declared_over_a_repo_s_standalone_instruction_names_its_owner():
    async def smoke() -> None: ...

    with registering_repo("s"):
        instruction(name="smoke")(smoke)
    with (
        registering_repo("a"),
        pytest.raises(
            ProjectInstructionError,
            match=r"'smoke' \(declared by repo 'a' on A\) and a standalone instruction "
            r"registered by repo 's' \(",
        ),
    ):
        register_project_actions(_actions("A"))


def test_a_standalone_instruction_over_a_project_instruction_is_refused():
    """From a repo's init import it is the ProjectActions advice; from anywhere else, the clash."""
    with registering_repo("a"):
        register_project_actions(_actions("A"))

    async def smoke() -> None: ...

    with registering_repo("s"), pytest.raises(ValueError, match="is a project instruction"):
        instruction(name="smoke")(smoke)
    with pytest.raises(ProjectInstructionError, match=r"registered by otto \("):
        instruction(name="smoke")(smoke)
    assert "smoke" not in STANDALONE_INSTRUCTIONS


def test_an_omitted_shape_keyword_inherits_the_first_declarer_s_value():
    with registering_repo("a"):
        register_project_actions(_actions("A", walk="reverse"))
    with registering_repo("b"):
        register_project_actions(_actions("B"))
    assert PROJECT_INSTRUCTIONS.get("smoke").spec.walk == "reverse"
    (body,) = PROJECT_ACTIONS.get("b").bodies
    assert dict(body.shape) == {}


def test_removing_the_first_declarer_lifts_its_shape():
    with registering_repo("a"):
        register_project_actions(_actions("A", walk="reverse"))
    with registering_repo("b"):
        register_project_actions(_actions("B"))
    with registering_repo("a"):
        register_project_actions(_bare("A2"), overwrite=True)
    assert PROJECT_INSTRUCTIONS.get("smoke").spec.walk == "forward"
    with registering_repo("c"):
        register_project_actions(_actions("C", walk="forward"))
    assert [b.repo for b in PROJECT_INSTRUCTIONS.get("smoke").bodies] == ["b", "c"]


def test_replacing_the_first_declarer_s_shape_is_checked_against_the_others():
    a = _actions("A", walk="reverse")
    with registering_repo("a"):
        register_project_actions(a)
    with registering_repo("b"):
        register_project_actions(_actions("B", walk="reverse"))
    revision = PROJECT_ACTIONS.revision
    with (
        registering_repo("a"),
        pytest.raises(ProjectInstructionError, match="fixes the walk shape"),
    ):
        register_project_actions(_actions("A2", walk="forward"), overwrite=True)
    assert PROJECT_ACTIONS.revision == revision
    assert PROJECT_ACTIONS.get("a").cls is a
    assert PROJECT_INSTRUCTIONS.get("smoke").spec.walk == "reverse"


def test_an_override_of_a_first_party_instruction_needs_its_options_base():
    with (
        registering_repo("a"),
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

    with registering_repo("a"):
        register_project_actions(A)
    entry = PROJECT_INSTRUCTIONS.get("install")
    assert [b.repo for b in entry.bodies] == [None, "a"]
    assert entry.first_party_options is InstallOptions


def test_two_bodies_for_one_owner_and_name_are_refused():
    @instruction(name="smoke")
    async def one(self) -> None: ...

    @instruction(name="smoke")
    async def two(self) -> None: ...

    twice = type("Twice", (ProjectActions,), {"one": one, "two": two})
    with registering_repo("a"), pytest.raises(ProjectInstructionError, match="twice"):
        register_project_actions(twice)
    assert "a" not in PROJECT_ACTIONS

    # The table refuses the same thing in a hand-built record.
    single = _actions("Single")
    with registering_repo("a"):
        register_project_actions(single)
    (body,) = PROJECT_ACTIONS.get("a").bodies
    with registering_repo("a"), pytest.raises(ProjectInstructionError, match="twice"):
        PROJECT_ACTIONS.register("a", ProjectActionsEntry(single, (body, body)), overwrite=True)


def test_one_class_registered_by_two_repos_is_refused_naming_both():
    shared = _actions("Shared")
    with registering_repo("a"):
        register_project_actions(shared)
    with (
        registering_repo("b"),
        pytest.raises(
            ProjectInstructionError,
            match=r"^repo 'b' registers class Shared, which repo 'a' already registered -- "
            r"each repo registers its own ProjectActions subclass$",
        ),
    ):
        register_project_actions(shared)
    assert "b" not in PROJECT_ACTIONS


def test_a_repo_claiming_install_through_the_decorator_gets_today_s_message(monkeypatch):
    monkeypatch.delitem(sys.modules, "otto.project.actions", raising=False)

    async def install() -> None: ...

    with registering_repo("a"), pytest.raises(ValueError, match="is a project instruction"):
        instruction()(install)
    assert "otto.project.actions" not in sys.modules
    assert "install" not in STANDALONE_INSTRUCTIONS


def test_check_project_instruction_options_names_both_repos_on_a_clash():
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

    with registering_repo("a"):
        register_project_actions(A)
    with registering_repo("b"):
        register_project_actions(B)
    with pytest.raises(OptionsCollisionError) as excinfo:
        check_project_instruction_options()
    message = str(excinfo.value)
    assert "repo 'a'" in message
    assert "repo 'b'" in message


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
    for repo, cls, field in (("tables-a", "TablesA", "alpha"), ("tables-b", "TablesB", "beta")):
        module = f"{cls.lower()}_init"
        dirs.append(
            make_sut_repo(
                tmp_path / repo,
                name=repo,
                extra=f'libs = ["lib"]\ninit = ["{module}"]\n',
                files={f"lib/{module}.py": _ACTIONS_INIT.format(cls=cls, field=field)},
            )
        )
    monkeypatch.setenv("OTTO_SUT_DIRS", ",".join(str(d) for d in dirs))
    bs._reset()
    try:
        result = bs.bootstrap()
        assert result.errors == []
        assert sorted(PROJECT_ACTIONS.names()) == ["tables-a", "tables-b"]
        check_project_instruction_options()
        check_project_instruction_options()
    finally:
        bs._reset()
    assert [b.repo for b in PROJECT_INSTRUCTIONS.get("tables-smoke").bodies] == [
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
async def test_run_instruction_refuses_an_inactive_owner_through_the_view_s_repo():
    from otto.config.lab import Lab
    from otto.context import OttoContext
    from otto.instructions import run_instruction
    from otto.session import InstructionInactiveError

    ran: list[str] = []

    async def tables_blink() -> None:
        ran.append("blink")

    with registering_repo("acme"):
        instruction(name="tables-blink")(tables_blink)
    assert INSTRUCTIONS.repo("tables-blink") == "acme"
    ctx = OttoContext(lab=Lab(name="t"), exclude_projects=("acme",))
    ctx.scopes = {}
    with pytest.raises(InstructionInactiveError):
        await run_instruction(ctx, "tables-blink")
    assert ran == []


def test_actions_for_returns_the_registered_class():
    from otto.config.lab import Lab
    from otto.context import OttoContext
    from otto.project import actions_for
    from tests._fixtures.fake_repo import fake_repo

    cls = _actions("A")
    with registering_repo("a"):
        register_project_actions(cls)
    ctx = OttoContext(lab=Lab(name="t"))
    assert type(actions_for(fake_repo("a"), ctx)) is cls
    assert type(actions_for(fake_repo("other"), ctx)) is ProjectActions


def test_a_registry_check_imports_otto_s_bodies_in_a_fresh_process():
    """A standalone registration's check reads otto's bodies before anything imported them.

    The import happens inside a registry check, so it must register nothing:
    a registration made there would be refused for being made during a check.
    """
    script = (
        "import sys\n"
        "from otto.instructions import STANDALONE_INSTRUCTIONS, instruction\n"
        "from otto.registry import registering_repo\n"
        "assert 'otto.project.actions' not in sys.modules\n"
        "async def deploy(): ...\n"
        "with registering_repo('acme'):\n"
        "    instruction()(deploy)\n"
        "assert 'otto.project.actions' in sys.modules, 'the check never read the bodies'\n"
        "print(STANDALONE_INSTRUCTIONS.repo('deploy'))\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, check=False, timeout=120
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "acme"


@pytest.mark.parametrize(
    "script",
    [
        (
            "import otto.project.actions, otto.instructions as i; "
            "print(sorted(i.PROJECT_INSTRUCTIONS.names()))"
        ),
        (
            "import sys, otto.instructions as i; "
            "assert 'otto.project.actions' not in sys.modules; "
            "print(sorted(i.INSTRUCTIONS.names()))"
        ),
    ],
    ids=["actions-first", "instructions-first"],
)
def test_importing_the_tables_in_a_fresh_process(script):
    out = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, check=False, timeout=120
    )
    assert out.returncode == 0, out.stderr
    assert "'install'" in out.stdout

"""The project-instruction table: one spec per name, one body per repo, loud rules.

Registration order is bootstrap's: otto's base class first (repo ``None``),
then each repo in dependency order. The FIRST declaration of a name fixes its
walk shape; a later declaration may restate a keyword identically but never
differently. An override of a first-party name must inherit that name's
first-party options class. The root conftest's ``_isolate_registries`` rolls
``PROJECT_INSTRUCTIONS`` back after each test because it is a ``Registry``.

THE SYNTHETIC NAME IS ``provision``, never one of otto's six. ``otto.project.actions``
registers its own bodies AT IMPORT, and those entries are the process's baseline
rather than a leak (the isolation fixture snapshots them and puts them back), so a
test that declared ``install`` here would be a second declaration of a name already
in the table in any worker that imported the package -- and would pass or fail on
collection order alone.
"""

from typing import Annotated

import pytest
import typer

from otto import options
from otto.instructions import (
    INSTRUCTIONS,
    PROJECT_INSTRUCTIONS,
    InstructionEntry,
    ProjectInstructionError,
    ProjectInstructionMark,
    register_project_instruction_body,
)


@options
class InstallOptions:
    ensure: Annotated[bool, typer.Option(help="e")] = False


@options
class WidgetInstall(InstallOptions):
    variant: Annotated[str, typer.Option(help="v")] = "field"


@options
class Foreign:
    ensure: Annotated[bool, typer.Option(help="e")] = False


class Base:
    async def provision(self, opts): ...


class Widget(Base):
    async def provision(self, opts): ...


def _mark(name="provision", options_cls=InstallOptions, **shape):
    return ProjectInstructionMark(name=name, options_cls=options_cls, shape=shape, help="h")


def _first_party():
    register_project_instruction_body(
        Base, "provision", _mark(walk="forward", continue_on_failure=False), repo=None
    )


class TestFirstDeclaration:
    def test_registers_spec_and_body(self) -> None:
        _first_party()
        entry = PROJECT_INSTRUCTIONS.get("provision")
        assert entry.spec.walk == "forward"
        assert entry.spec.declared_by is None
        assert entry.spec.module == Base.__module__
        assert entry.spec.help == "h"
        assert [b.owner_class for b in entry.bodies] == [Base]

    def test_unstated_keywords_take_defaults(self) -> None:
        register_project_instruction_body(Base, "deploy", _mark("deploy", None), repo="a")
        spec = PROJECT_INSTRUCTIONS.get("deploy").spec
        assert (spec.walk, spec.continue_on_failure, spec.require_dependencies) == (
            "forward",
            False,
            True,
        )
        assert spec.combine_results is None
        assert spec.render is None

    def test_body_for_resolves_through_the_mro(self) -> None:
        _first_party()
        entry = PROJECT_INSTRUCTIONS.get("provision")
        assert entry.body_for(Widget).owner_class is Base
        assert entry.body_for(object) is None


class TestSecondDeclaration:
    def test_override_appends_a_body_and_keeps_the_spec(self) -> None:
        _first_party()
        register_project_instruction_body(
            Widget, "provision", _mark(options_cls=WidgetInstall), repo="widget"
        )
        entry = PROJECT_INSTRUCTIONS.get("provision")
        assert [b.repo for b in entry.bodies] == [None, "widget"]
        assert entry.body_for(Widget).options_cls is WidgetInstall
        assert entry.first_party_options is InstallOptions

    def test_restating_a_keyword_identically_is_allowed(self) -> None:
        _first_party()
        register_project_instruction_body(
            Widget, "provision", _mark(options_cls=WidgetInstall, walk="forward"), repo="widget"
        )
        assert len(PROJECT_INSTRUCTIONS.get("provision").bodies) == 2

    def test_restating_a_keyword_differently_is_refused_naming_it(self) -> None:
        _first_party()
        with pytest.raises(ProjectInstructionError, match=r"walk.*'widget'.*first declaration"):
            register_project_instruction_body(
                Widget, "provision", _mark(options_cls=WidgetInstall, walk="reverse"), repo="widget"
            )

    def test_first_party_override_must_inherit_the_first_party_options(self) -> None:
        _first_party()
        with pytest.raises(ProjectInstructionError, match=r"'widget'.*'provision'.*InstallOptions"):
            register_project_instruction_body(
                Widget, "provision", _mark(options_cls=Foreign), repo="widget"
            )

    def test_first_party_override_without_options_is_refused(self) -> None:
        _first_party()
        with pytest.raises(ProjectInstructionError, match=r"InstallOptions"):
            register_project_instruction_body(
                Widget, "provision", _mark(options_cls=None), repo="widget"
            )

    def test_repo_added_name_has_no_inheritance_rule(self) -> None:
        register_project_instruction_body(Base, "deploy", _mark("deploy", None), repo="a")
        register_project_instruction_body(Widget, "deploy", _mark("deploy", Foreign), repo="b")
        assert len(PROJECT_INSTRUCTIONS.get("deploy").bodies) == 2


class TestStandaloneConflict:
    def test_a_name_already_taken_by_a_standalone_instruction_is_refused(self) -> None:
        INSTRUCTIONS.register(
            "deploy",
            InstructionEntry(
                name="deploy", sub_app=typer.Typer(), module="repo_a.init", registered_by="a"
            ),
            origin="repo_a.init",
        )
        with pytest.raises(ProjectInstructionError, match=r"'b'.*'deploy'.*standalone.*'a'"):
            register_project_instruction_body(Widget, "deploy", _mark("deploy", None), repo="b")

    def test_a_name_already_in_the_project_table_is_not_a_standalone_collision(self) -> None:
        _first_party()
        INSTRUCTIONS.register(
            "provision",
            InstructionEntry(
                name="provision",
                sub_app=typer.Typer(),
                module="dummy.module",
                registered_by="dummy",
            ),
            origin="dummy.module",
        )
        register_project_instruction_body(
            Widget, "provision", _mark(options_cls=WidgetInstall), repo="widget"
        )
        entry = PROJECT_INSTRUCTIONS.get("provision")
        assert [b.repo for b in entry.bodies] == [None, "widget"]

"""The project-instruction table: one spec per name, one body per repo, loud rules.

Declaration order is bootstrap's: otto's own bodies first (repo ``None``, the
constant ``FIRST_PARTY_BODIES``), then each repo's actions in registration
(dependency) order. The FIRST declaration of a name fixes its walk shape; a
later declaration may restate a keyword identically but never differently. An
override of a first-party name must inherit that name's first-party options
class. ``PROJECT_INSTRUCTIONS`` is derived from ``PROJECT_ACTIONS``, which the
root conftest's ``_isolate_registries`` rolls back after each test.

The repo-declared synthetic names are ``provision`` and ``deploy``, never one
of otto's six; the first-party rules use ``install`` itself, since otto's
bodies are a constant every test sees.
"""

from typing import Annotated

import pytest
import typer

from otto import options
from otto.instructions import (
    INSTRUCTIONS,
    PROJECT_INSTRUCTIONS,
    ProjectInstructionError,
    instruction,
)
from otto.project import InstallOptions, ProjectActions, register_project_actions
from otto.registry import registering_repo


@options
class ProvisionOptions:
    ensure: Annotated[bool, typer.Option(help="e")] = False


@options
class WidgetProvision(ProvisionOptions):
    variant: Annotated[str, typer.Option(help="v")] = "field"


@options
class Foreign:
    ensure: Annotated[bool, typer.Option(help="e")] = False


def _declare(repo: str, name: str = "provision", options_cls=ProvisionOptions, **shape):
    """Register, as *repo*, a ``ProjectActions`` subclass whose one method declares *name*."""
    if options_cls is None:

        @instruction(name, help="h", **shape)
        async def body(self) -> None: ...

    else:

        @instruction(name, options=options_cls, help="h", **shape)
        async def body(self, opts: options_cls) -> None: ...  # ty: ignore[invalid-type-form]

    cls = type(f"{repo.title()}Actions", (ProjectActions,), {name.replace("-", "_"): body})
    with registering_repo(repo):
        register_project_actions(cls)
    return cls


class TestFirstDeclaration:
    def test_registers_spec_and_body(self) -> None:
        cls = _declare("a", walk="forward", continue_on_failure=False)
        entry = PROJECT_INSTRUCTIONS.get("provision")
        assert entry.spec.walk == "forward"
        assert entry.spec.declared_by == "a"
        assert entry.spec.module == cls.__module__
        assert entry.spec.help == "h"
        assert [b.owner_class for b in entry.bodies] == [cls]

    def test_unstated_keywords_take_defaults(self) -> None:
        _declare("a", "deploy", None)
        spec = PROJECT_INSTRUCTIONS.get("deploy").spec
        assert (spec.walk, spec.continue_on_failure, spec.require_dependencies) == (
            "forward",
            False,
            True,
        )
        assert spec.dry_run_preview is False
        assert spec.combine_results is None
        assert spec.render is None

    def test_body_for_resolves_through_the_mro(self) -> None:
        cls = _declare("a")
        sub = type("Sub", (cls,), {})
        entry = PROJECT_INSTRUCTIONS.get("provision")
        assert entry.body_for(sub).owner_class is cls
        assert entry.body_for(object) is None


class TestSecondDeclaration:
    def test_override_appends_a_body_and_keeps_the_spec(self) -> None:
        _declare("a", walk="reverse")
        widget = _declare("widget", options_cls=WidgetProvision)
        entry = PROJECT_INSTRUCTIONS.get("provision")
        assert [b.repo for b in entry.bodies] == ["a", "widget"]
        assert entry.body_for(widget).options_cls is WidgetProvision
        assert entry.spec.walk == "reverse"

    def test_restating_a_keyword_identically_is_allowed(self) -> None:
        _declare("a", walk="forward")
        _declare("widget", options_cls=WidgetProvision, walk="forward")
        assert len(PROJECT_INSTRUCTIONS.get("provision").bodies) == 2

    def test_restating_a_keyword_differently_is_refused_naming_it(self) -> None:
        _declare("a", walk="forward")
        with pytest.raises(ProjectInstructionError, match=r"walk.*'widget'.*first declaration"):
            _declare("widget", options_cls=WidgetProvision, walk="reverse")

    def test_first_party_override_must_inherit_the_first_party_options(self) -> None:
        with pytest.raises(ProjectInstructionError, match=r"'widget'.*'install'.*InstallOptions"):
            _declare("widget", "install", Foreign)

    def test_first_party_override_without_options_is_refused(self) -> None:
        with pytest.raises(ProjectInstructionError, match=r"InstallOptions"):
            _declare("widget", "install", None)

    def test_first_party_override_with_the_first_party_options_is_accepted(self) -> None:
        @options
        class WidgetInstall(InstallOptions):
            variant: Annotated[str, typer.Option(help="v")] = "field"

        _declare("widget", "install", WidgetInstall)
        entry = PROJECT_INSTRUCTIONS.get("install")
        assert [b.repo for b in entry.bodies] == [None, "widget"]
        assert entry.first_party_options is InstallOptions

    def test_repo_added_name_has_no_inheritance_rule(self) -> None:
        _declare("a", "deploy", None)
        _declare("b", "deploy", Foreign)
        assert len(PROJECT_INSTRUCTIONS.get("deploy").bodies) == 2


class TestStandaloneConflict:
    def test_a_name_already_taken_by_a_standalone_instruction_is_refused(self) -> None:
        async def deploy() -> None: ...

        with registering_repo("a"):
            instruction()(deploy)
        with pytest.raises(
            ProjectInstructionError, match=r"'deploy'.*repo 'b'.*standalone.*repo 'a'"
        ):
            _declare("b", "deploy", None)
        assert INSTRUCTIONS.get("deploy").handler is deploy


def test_the_instructions_view_derives_a_data_entry():
    _declare("a")
    entry = INSTRUCTIONS.get("provision")
    assert entry.project == PROJECT_INSTRUCTIONS.get("provision")
    assert entry.handler is None
    assert INSTRUCTIONS.repo("provision") is None


def test_the_shipped_install_verbs_opt_in_to_the_dry_run_preview():
    previewed = {
        name: PROJECT_INSTRUCTIONS.get(name).spec.dry_run_preview
        for name in ("install", "uninstall", "install-tools", "status", "cleanup", "get-logs")
    }
    assert previewed == {
        "install": True,
        "uninstall": True,
        "install-tools": True,
        "status": False,
        "cleanup": False,
        "get-logs": False,
    }

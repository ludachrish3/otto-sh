"""Conformance cases for the instruction tables: two sources and the two views over them."""

import functools

import pytest

from otto.instructions import (
    INSTRUCTIONS,
    PROJECT_ACTIONS,
    PROJECT_INSTRUCTIONS,
    STANDALONE_INSTRUCTIONS,
    InstructionEntry,
    ProjectActionsEntry,
    ProjectInstructionError,
    _bodies_of,
    instruction,
)
from otto.project import ProjectActions, register_project_actions
from otto.registry import registering_repo, registration_boundary
from tests.unit.registry import conformance

COVERS = [
    "otto.instructions:STANDALONE_INSTRUCTIONS",
    "otto.instructions:PROJECT_ACTIONS",
    "otto.instructions:PROJECT_INSTRUCTIONS",
    "otto.instructions:INSTRUCTIONS",
]

_REPO = "case-repo"


def _handler(i: int):
    async def handler() -> None: ...

    handler.__name__ = f"case_handler_{i}"
    return handler


_HANDLERS = {i: _handler(i) for i in range(12)}


def _standalone(name: str, i: int = 0) -> InstructionEntry:
    handler = _HANDLERS[i]
    return InstructionEntry(name=name, module=handler.__module__, handler=handler)


def _actions_class(i: int, method: str | None = None) -> "type[ProjectActions]":
    body = {}
    if method is not None:

        @instruction(name=method)
        async def run(self) -> None: ...

        body = {method.replace("-", "_"): run}
    return type(f"CaseActions{i}", (ProjectActions,), body)


_CLASSES = {i: _actions_class(i) for i in range(12)}


def test_standalone_instructions_raw_case():
    conformance.assert_raw_registry(
        STANDALONE_INSTRUCTIONS,
        make=lambda i: ("__c-standalone", _standalone("__c-standalone", i)),
        invalid=("__c-mismatch", _standalone("not-the-key")),
    )


@registration_boundary
def _decorate(handler, *, overwrite):
    """Apply ``@instruction()`` as a decorating module does (transparent to attribution)."""
    return instruction(name="__c-wrap", overwrite=overwrite)(handler)


def test_standalone_instructions_wrapper_case():
    handler = _HANDLERS[1]
    conformance.assert_wrapper_matches_raw(
        STANDALONE_INSTRUCTIONS,
        via_wrapper=functools.partial(_decorate, handler),
        record_for=lambda: _standalone("__c-wrap", 1),
    )


def test_the_decorator_credits_the_decorating_module():
    decorate = instruction(name="__c-deco")
    conformance.from_module("case_decorating.init", decorate, _HANDLERS[2])
    assert STANDALONE_INSTRUCTIONS.origin("__c-deco") == "case_decorating.init"
    assert INSTRUCTIONS.origin("__c-deco") == "case_decorating.init"


def test_project_actions_raw_case():
    conformance.assert_raw_registry(
        PROJECT_ACTIONS,
        make=lambda i: (_REPO, ProjectActionsEntry(_CLASSES[i], ())),
        invalid=("case-other-repo", ProjectActionsEntry(_CLASSES[0], ())),
        require_repo=_REPO,
    )


def test_project_actions_wrapper_case():
    cls = _actions_class(20, "__c-body")
    with registering_repo(_REPO):
        conformance.assert_wrapper_matches_raw(
            PROJECT_ACTIONS,
            via_wrapper=functools.partial(register_project_actions, cls),
            record_for=lambda: ProjectActionsEntry(cls, _bodies_of(cls, _REPO)),
        )
        register_project_actions(cls)
        (body,) = PROJECT_ACTIONS.get(_REPO).bodies
        assert (body.name, body.owner_class, body.repo) == ("__c-body", cls, _REPO)


def test_project_actions_refuse_a_registration_outside_a_repo():
    from otto.registry import RegistrationRefused

    with pytest.raises(RegistrationRefused):
        register_project_actions(_CLASSES[3])
    assert PROJECT_ACTIONS.names() == []


def test_the_instructions_view_case():
    def contribute(name: str) -> None:
        STANDALONE_INSTRUCTIONS.register(name, _standalone(name, 4))

    def clash() -> None:
        with registering_repo(_REPO):
            STANDALONE_INSTRUCTIONS.register("install", _standalone("install", 5))

    conformance.assert_view(
        INSTRUCTIONS,
        sources=[STANDALONE_INSTRUCTIONS, PROJECT_ACTIONS],
        contribute=contribute,
        clash=clash,
    )


def test_the_project_instructions_view_case():
    def contribute(name: str) -> None:
        with registering_repo(_REPO):
            register_project_actions(_actions_class(30, name))

    def clash() -> None:
        # A second repo restating __v_one's walk differently: a derivation conflict.
        @instruction(name="__v_one", walk="reverse")
        async def run(self) -> None: ...

        with registering_repo("case-clash"):
            register_project_actions(type("CaseClash", (ProjectActions,), {"v_one": run}))

    conformance.assert_view(
        PROJECT_INSTRUCTIONS,
        sources=[PROJECT_ACTIONS],
        contribute=contribute,
        clash=clash,
        refusal=ProjectInstructionError,
    )

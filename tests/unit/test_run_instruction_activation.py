"""``otto.run_instruction`` refuses an inactive repo's instruction before running anything."""

import pytest

from otto.config.lab import Lab
from otto.context import OttoContext, try_get_context
from otto.instructions import INSTRUCTIONS, InstructionEntry, run_instruction
from otto.session import InstructionInactiveError
from tests._fixtures.bootstrap_seam import seed_scope_verdicts

pytestmark = pytest.mark.asyncio


def _ctx(*, exclude: "tuple[str, ...]" = ()) -> OttoContext:
    ctx = OttoContext(lab=Lab(name="t"), exclude_projects=exclude)
    seed_scope_verdicts(ctx, {})
    return ctx


@pytest.fixture
def ran() -> "list[str]":
    """Register a throwaway acme-owned instruction; its handler appends to the list.

    Registry isolation is the root conftest's ``_isolate_registries``.
    """
    calls: list[str] = []

    async def blink() -> str:
        calls.append("blink")
        return "blinked"

    INSTRUCTIONS.register(
        "blink",
        InstructionEntry(name="blink", handler=blink, module="m", registered_by="acme"),
        origin="m",
    )
    return calls


async def test_an_excluded_repos_instruction_is_refused_and_never_runs(ran):
    with pytest.raises(InstructionInactiveError) as exc:
        await run_instruction(_ctx(exclude=("acme",)), "blink")
    assert exc.value.reason == "excluded"
    assert ran == []


async def test_the_same_instruction_runs_when_its_repo_is_active(ran):
    assert await run_instruction(_ctx(), "blink") == "blinked"
    assert ran == ["blink"]


async def test_a_refused_call_leaves_the_active_context_as_it_was(ran):
    before = try_get_context()
    with pytest.raises(InstructionInactiveError):
        await run_instruction(_ctx(exclude=("acme",)), "blink")
    assert try_get_context() is before


async def test_the_refusal_comes_before_option_validation(ran):
    """An instance of a class the instruction does not accept would raise
    OptionsRegistrationError; the inactive refusal must win."""

    class Stray:
        pass

    with pytest.raises(InstructionInactiveError):
        await run_instruction(_ctx(exclude=("acme",)), "blink", [Stray()])
    assert ran == []

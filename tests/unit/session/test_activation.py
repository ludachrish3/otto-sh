"""``otto.session.check_instruction_active`` — a repo-owned instruction needs an active repo."""

import pytest

from otto.config.lab import Lab
from otto.config.scope import ProjectScope
from otto.context import OttoContext
from tests._fixtures.scoping import verdict


def _ctx(
    *,
    exclude: "tuple[str, ...]" = (),
    scopes: "dict[str, ProjectScope] | None" = None,
) -> OttoContext:
    ctx = OttoContext(lab=Lab(name="t"), exclude_projects=exclude)
    ctx.scopes = dict(scopes or {})
    return ctx


@pytest.fixture
def plain_ctx() -> OttoContext:
    return _ctx()


@pytest.fixture
def ctx_excluding_acme() -> OttoContext:
    return _ctx(exclude=("acme",))


@pytest.fixture
def ctx_acme_out_of_lab_scope() -> OttoContext:
    return _ctx(scopes={"acme": verdict("acme", excluded=True)})


@pytest.fixture
def ctx_acme_host_starved() -> OttoContext:
    return _ctx(scopes={"acme": verdict("acme", universe=())})


def test_first_party_is_never_refused(ctx_excluding_acme):
    from otto.session import check_instruction_active

    check_instruction_active("blink", None, ctx_excluding_acme)


def test_an_active_owner_passes(plain_ctx):
    from otto.session import check_instruction_active

    check_instruction_active("blink", "acme", plain_ctx)


def test_an_excluded_owner_is_refused_with_the_switch(ctx_excluding_acme):
    from otto.session import InstructionInactiveError, check_instruction_active

    with pytest.raises(InstructionInactiveError) as exc:
        check_instruction_active("blink", "Acme", ctx_excluding_acme)
    e = exc.value
    assert (e.reason, e.project, e.owner, e.instruction) == ("excluded", "acme", "Acme", "blink")
    assert "exclude_projects acme" in str(e)


def test_an_out_of_scope_owner_carries_the_patterns(ctx_acme_out_of_lab_scope):
    from otto.session import InstructionInactiveError, check_instruction_active

    with pytest.raises(InstructionInactiveError) as exc:
        check_instruction_active("blink", "acme", ctx_acme_out_of_lab_scope)
    e = exc.value
    assert e.reason == "out_of_lab_scope"
    assert e.loaded_labs == ["bench"]
    assert e.lab_patterns == ["acme-lab"]


def test_a_host_starved_owner_carries_the_host_patterns(ctx_acme_host_starved):
    from otto.session import InstructionInactiveError, check_instruction_active

    with pytest.raises(InstructionInactiveError) as exc:
        check_instruction_active("blink", "acme", ctx_acme_host_starved)
    e = exc.value
    assert e.reason == "host_starved"
    assert e.host_patterns == [".*"]
    assert e.loaded_labs == ["bench"]

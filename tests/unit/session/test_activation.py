"""``otto.session.check_instruction_active``: a project's instruction needs an active project.

Beneath it, ``owner_refusal`` is the one judgement of a project-owned entry at
dispatch, in either tier: with the lab built (a context) or before it (the
``-l`` / ``-I`` / ``-E`` selection alone).
"""

import pytest

from otto.config.dependencies import ResolvedDependency
from otto.config.project import Project
from otto.config.scope import ProjectScope, ScopeSelection
from otto.context import OttoContext
from otto.labs.loader import Lab
from tests._fixtures.bootstrap_seam import fake_bootstrap_result, seed_scope_verdicts
from tests._fixtures.fake_project import fake_project
from tests._fixtures.scoping import verdict


def _ctx(
    *,
    exclude: "tuple[str, ...]" = (),
    scopes: "dict[str, ProjectScope] | None" = None,
    projects: "list[Project] | None" = None,
) -> OttoContext:
    """A context over *projects* (``acme`` alone by default) whose verdicts are *scopes*."""
    ctx = OttoContext(
        lab=Lab(name="t"),
        exclude_projects=exclude,
        bootstrap=fake_bootstrap_result(projects or [fake_project("acme")]),
    )
    seed_scope_verdicts(ctx, scopes or {})
    return ctx


def _requiring(name: str, *, needs: str) -> Project:
    """A project with a satisfied required dependency on *needs*, as bootstrap records it."""
    dep = ResolvedDependency(
        name=needs,
        normalized=needs,
        constraint="",
        required=True,
        status="satisfied",
        provider_version=None,
    )
    return fake_project(name, dependencies=[dep])


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

    check_instruction_active(name="blink", owner=None, ctx=ctx_excluding_acme)


def test_an_active_owner_passes(plain_ctx):
    from otto.session import check_instruction_active

    check_instruction_active(name="blink", owner="acme", ctx=plain_ctx)


def test_an_excluded_owner_is_refused_with_the_switch(ctx_excluding_acme):
    from otto.session import InstructionInactiveError, check_instruction_active

    with pytest.raises(InstructionInactiveError) as exc:
        check_instruction_active(name="blink", owner="Acme", ctx=ctx_excluding_acme)
    e = exc.value
    assert (e.reason, e.project, e.owner, e.instruction) == ("excluded", "acme", "Acme", "blink")
    assert "exclude_projects acme" in str(e)


def test_an_out_of_scope_owner_carries_the_patterns(ctx_acme_out_of_lab_scope):
    from otto.session import InstructionInactiveError, check_instruction_active

    with pytest.raises(InstructionInactiveError) as exc:
        check_instruction_active(name="blink", owner="acme", ctx=ctx_acme_out_of_lab_scope)
    e = exc.value
    assert e.reason == "out_of_lab_scope"
    assert e.loaded_labs == ["bench"]
    assert e.lab_patterns == ["acme-lab"]


def test_a_host_starved_owner_carries_the_host_patterns(ctx_acme_host_starved):
    from otto.session import InstructionInactiveError, check_instruction_active

    with pytest.raises(InstructionInactiveError) as exc:
        check_instruction_active(name="blink", owner="acme", ctx=ctx_acme_host_starved)
    e = exc.value
    assert e.reason == "host_starved"
    assert e.host_patterns == [".*"]
    assert e.loaded_labs == ["bench"]


def test_host_starved_requirer_tier_difference():
    """Fail-first: a host-starved requirer exempts its requirement in tier P, not in tier L.

    ``app`` requires ``fw``, and ``-E fw`` switches ``fw`` off. Before the lab
    is built, ``app``'s ``host_patterns`` cannot be judged, so ``app`` takes
    part and ``fw`` runs. With the lab built ``app`` is host-starved, so it no
    longer takes part and ``fw`` is refused for its switch.
    """
    from otto.session.activation import owner_refusal

    app, fw = _requiring("app", needs="fw"), fake_project("fw")
    projects = [app, fw]

    pre_lab = ScopeSelection(labs=("bench",), include=(), exclude=("fw",))
    assert owner_refusal("fw", projects=projects, selection=pre_lab) is None

    ctx = _ctx(exclude=("fw",), scopes={"app": verdict("app", universe=())}, projects=projects)
    reason = owner_refusal("fw", projects=projects, ctx=ctx)
    assert reason is not None
    assert (reason.project, reason.reason) == ("fw", "excluded")


def test_owner_refusal_judges_participation_once(monkeypatch):
    """The owner's reason and the requirers come from ONE participation answer (Q34)."""
    from otto.config import scope
    from otto.session.activation import owner_refusal

    calls: "list[int]" = []
    real = scope.participating

    def counting(projects, **kw):
        calls.append(len(projects))
        return real(projects, **kw)

    monkeypatch.setattr(scope, "participating", counting)
    projects = [_requiring("app", needs="fw"), fake_project("fw")]
    selection = ScopeSelection(labs=(), include=(), exclude=("fw",))

    assert owner_refusal("fw", projects=projects, selection=selection) is None
    assert calls == [2]

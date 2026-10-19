"""The context carries the run's projects: one BootstrapResult, read lazily, cached (spec 2 §4)."""

import pytest

from otto import bootstrap as bs
from otto.config.scope import scopes_of
from otto.context import OttoContext
from otto.labs.loader import Lab
from tests._fixtures.bootstrap_seam import fake_bootstrap_result, seed_scope_verdicts
from tests._fixtures.fake_project import fake_project

LIBRARY_LAB = "<library>"
"""The library sentinel's lab name: the host-less context ``run_tests`` builds when none is
active. Spelled here, not imported, because only its value is public behaviour; the verdicts
special-case it, so a lab of any other name fails the sentinel test below."""


@pytest.fixture(autouse=True)
def _fresh():
    bs.invalidate()
    yield
    bs.invalidate()


def _counting(monkeypatch, *results):
    """Make bootstrap() return *results* in turn (an exception instance raises); count the calls."""
    calls = []

    def fake():
        calls.append(1)
        outcome = results[min(len(calls), len(results)) - 1]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr("otto.bootstrap.bootstrap", fake)
    return calls


def test_a_hand_built_context_reads_its_projects_once_lazily(monkeypatch):
    project = fake_project("alpha")
    calls = _counting(monkeypatch, fake_bootstrap_result([project]))
    ctx = OttoContext(lab=Lab(name="rig"))
    assert calls == []  # nothing read at construction
    assert [r.name for r in ctx.projects] == ["alpha"]
    assert [r.name for r in ctx.ordered_projects] == ["alpha"]
    assert calls == [1]


def test_a_given_result_is_used_and_bootstrap_is_never_called(monkeypatch):
    project = fake_project("alpha")
    calls = _counting(monkeypatch, RuntimeError("must not be called"))
    ctx = OttoContext(lab=Lab(name="rig"), bootstrap=fake_bootstrap_result([project]))
    assert [r.name for r in ctx.projects] == ["alpha"]
    assert calls == []


def test_known_empty_and_unavailable_are_distinct_and_a_refusal_is_cached(monkeypatch):
    empty = OttoContext(lab=Lab(name="rig"), bootstrap=fake_bootstrap_result([]))
    assert empty.projects == []
    assert scopes_of(empty) == {}

    refusal = FileNotFoundError("no such project root")
    calls = _counting(monkeypatch, refusal)
    unavailable = OttoContext(lab=Lab(name="rig"))
    for _ in range(2):
        with pytest.raises(FileNotFoundError) as raised:
            _ = unavailable.projects
        assert raised.value is refusal
    with pytest.raises(FileNotFoundError):
        scopes_of(unavailable)
    assert calls == [1]


def test_the_library_sentinel_still_bootstraps_for_its_projects(monkeypatch):
    project = fake_project("alpha")
    _counting(monkeypatch, fake_bootstrap_result([project]))
    ctx = OttoContext(lab=Lab(name=LIBRARY_LAB))
    assert [r.name for r in ctx.projects] == ["alpha"]
    assert scopes_of(ctx) == {}  # verdicts special-case the sentinel; projects do not


def test_projects_ordered_projects_and_the_verdicts_read_one_result_across_an_invalidate(
    monkeypatch,
):
    first, second = fake_project("first"), fake_project("second")
    _counting(monkeypatch, fake_bootstrap_result([first]), fake_bootstrap_result([second]))
    ctx = OttoContext(lab=Lab(name="rig"))
    assert [r.name for r in ctx.projects] == ["first"]
    bs.invalidate()
    assert [r.name for r in ctx.ordered_projects] == ["first"]
    assert list(scopes_of(ctx)) == [
        "first"
    ]  # every parsed project is keyed, so "second" would show


def test_mutating_the_returned_projects_changes_nothing():  # Review Focus 5
    alpha, beta = fake_project("alpha"), fake_project("beta")
    ctx = OttoContext(lab=Lab(name="rig"), bootstrap=fake_bootstrap_result([alpha]))
    ctx.projects.append(beta)
    ctx.ordered_projects.clear()
    # Resolved only now, after the mutations: verdicts over a live list would key "beta" too.
    assert list(scopes_of(ctx)) == ["alpha"]
    assert [r.name for r in ctx.projects] == ["alpha"]
    assert [r.name for r in ctx.ordered_projects] == ["alpha"]


def test_scopes_is_no_longer_a_member_and_the_private_alias_is_gone():
    assert not hasattr(OttoContext, "scopes")
    assert not hasattr(OttoContext, "_admissible_ids")


def test_seeded_verdicts_are_what_scopes_of_returns():
    ctx = OttoContext(lab=Lab(name="rig"), bootstrap=fake_bootstrap_result([]))
    seed_scope_verdicts(ctx, {"alpha": object()})
    assert list(scopes_of(ctx)) == ["alpha"]


def _first_then_later(monkeypatch):
    """bootstrap() answers a project named "given" once, then "later" on every further call."""
    return _counting(
        monkeypatch,
        fake_bootstrap_result([fake_project("given")]),
        fake_bootstrap_result([fake_project("later")]),
    )


@pytest.mark.parametrize("root_callback_ran", [True, False])
def test_the_cli_context_holds_the_result_ensure_lab_context_read(monkeypatch, root_callback_ran):
    from types import SimpleNamespace

    import typer
    from typer.core import TyperGroup

    from otto.cli.invoke import ensure_lab_context
    from otto.invocation import RunPolicy, install_policy, reset_binding
    from tests._fixtures.rootoptions import make_root_options

    calls = _first_then_later(monkeypatch)
    monkeypatch.setattr("otto.session.lab.build_lab", lambda projects, labs: Lab(name="rig"))
    monkeypatch.setattr(
        "otto.reservations.factory.build_reservation_gate",
        lambda *args, **kwargs: SimpleNamespace(identity=None),
    )
    binding = install_policy(RunPolicy()) if root_callback_ran else None
    try:
        with typer.Context(TyperGroup(name="otto")) as ctx:
            ctx.meta["_otto_root_options"] = make_root_options(labs=["rig"])
            built = ensure_lab_context(ctx)
            # The one read the lab was built from, not a second one by the context.
            assert [r.name for r in built.projects] == ["given"]
            assert [r.name for r in built.ordered_projects] == ["given"]
    finally:
        if binding is not None:
            reset_binding(binding)
    assert calls == [1]


@pytest.mark.asyncio
async def test_open_context_hands_its_context_the_result_it_read(monkeypatch):
    from otto.context import open_context

    _first_then_later(monkeypatch)
    # open_context reads bootstrap() first; a context reading its own would see "later".
    async with open_context(lab=Lab(name="rig")) as ctx:
        assert [r.name for r in ctx.projects] == ["given"]
        assert [r.name for r in ctx.ordered_projects] == ["given"]

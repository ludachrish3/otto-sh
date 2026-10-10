"""The context carries the run's repos: one BootstrapResult, read lazily and cached (spec 2 §4)."""

import pytest

from otto import bootstrap as bs
from otto.config.lab import Lab
from otto.config.scope import scopes_of
from otto.context import LIBRARY_LAB_NAME, OttoContext
from tests._fixtures.bootstrap_seam import fake_bootstrap_result, seed_scope_verdicts
from tests._fixtures.fake_repo import fake_repo


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


def test_a_hand_built_context_reads_its_repos_once_lazily(monkeypatch):
    repo = fake_repo("alpha")
    calls = _counting(monkeypatch, fake_bootstrap_result([repo]))
    ctx = OttoContext(lab=Lab(name="rig"))
    assert calls == []  # nothing read at construction
    assert [r.name for r in ctx.repos] == ["alpha"]
    assert [r.name for r in ctx.ordered_repos] == ["alpha"]
    assert calls == [1]


def test_a_given_result_is_used_and_bootstrap_is_never_called(monkeypatch):
    repo = fake_repo("alpha")
    calls = _counting(monkeypatch, RuntimeError("must not be called"))
    ctx = OttoContext(lab=Lab(name="rig"), bootstrap=fake_bootstrap_result([repo]))
    assert [r.name for r in ctx.repos] == ["alpha"]
    assert calls == []


def test_known_empty_and_unavailable_are_distinct_and_a_refusal_is_cached(monkeypatch):
    empty = OttoContext(lab=Lab(name="rig"), bootstrap=fake_bootstrap_result([]))
    assert empty.repos == []
    assert scopes_of(empty) == {}

    refusal = FileNotFoundError("no such SUT dir")
    calls = _counting(monkeypatch, refusal)
    unavailable = OttoContext(lab=Lab(name="rig"))
    for _ in range(2):
        with pytest.raises(FileNotFoundError) as raised:
            _ = unavailable.repos
        assert raised.value is refusal
    with pytest.raises(FileNotFoundError):
        scopes_of(unavailable)
    assert calls == [1]


def test_the_library_sentinel_still_bootstraps_for_its_repos(monkeypatch):
    repo = fake_repo("alpha")
    _counting(monkeypatch, fake_bootstrap_result([repo]))
    ctx = OttoContext(lab=Lab(name=LIBRARY_LAB_NAME))
    assert [r.name for r in ctx.repos] == ["alpha"]
    assert scopes_of(ctx) == {}  # verdicts special-case the sentinel; repos do not


def test_repos_ordered_repos_and_the_verdicts_read_one_result_across_an_invalidate(
    monkeypatch,
):
    first, second = fake_repo("first"), fake_repo("second")
    _counting(monkeypatch, fake_bootstrap_result([first]), fake_bootstrap_result([second]))
    ctx = OttoContext(lab=Lab(name="rig"))
    assert [r.name for r in ctx.repos] == ["first"]
    bs.invalidate()
    assert [r.name for r in ctx.ordered_repos] == ["first"]
    assert list(scopes_of(ctx)) == ["first"]  # every parsed repo is keyed, so "second" would show


def test_mutating_the_returned_repos_changes_nothing():  # Review Focus 5
    alpha, beta = fake_repo("alpha"), fake_repo("beta")
    ctx = OttoContext(lab=Lab(name="rig"), bootstrap=fake_bootstrap_result([alpha]))
    ctx.repos.append(beta)
    ctx.ordered_repos.clear()
    # Resolved only now, after the mutations: verdicts over a live list would key "beta" too.
    assert list(scopes_of(ctx)) == ["alpha"]
    assert [r.name for r in ctx.repos] == ["alpha"]
    assert [r.name for r in ctx.ordered_repos] == ["alpha"]


def test_scopes_is_no_longer_a_member_and_the_private_alias_is_gone():
    assert not hasattr(OttoContext, "scopes")
    assert not hasattr(OttoContext, "_admissible_ids")


def test_seeded_verdicts_are_what_scopes_of_returns():
    ctx = OttoContext(lab=Lab(name="rig"), bootstrap=fake_bootstrap_result([]))
    seed_scope_verdicts(ctx, {"alpha": object()})
    assert list(scopes_of(ctx)) == ["alpha"]


def _first_then_later(monkeypatch):
    """bootstrap() answers a repo named "given" once, then "later" on every further call."""
    return _counting(
        monkeypatch,
        fake_bootstrap_result([fake_repo("given")]),
        fake_bootstrap_result([fake_repo("later")]),
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
    monkeypatch.setattr("otto.session.lab.build_lab", lambda repos, labs: Lab(name="rig"))
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
            assert [r.name for r in built.repos] == ["given"]
            assert [r.name for r in built.ordered_repos] == ["given"]
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
        assert [r.name for r in ctx.repos] == ["given"]
        assert [r.name for r in ctx.ordered_repos] == ["given"]

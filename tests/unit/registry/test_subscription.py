"""``Subscription``: an ordered collection of values, with occurrence tokens."""

import pytest

from otto import registry as reg
from otto.registry import RegistrationRefused, Subscription


def _from_module(module: str, fn, *args, **kwargs):
    """Call *fn* from a frame whose module is *module* (attribution is by frame)."""
    code = compile("result = fn(*args, **kwargs)", f"<{module}>", "exec")
    scope = {"__name__": module, "fn": fn, "args": args, "kwargs": kwargs}
    exec(code, scope)  # noqa: S102 — a synthetic caller module, the attribution under test
    return scope["result"]


def _sub() -> "Subscription[object]":
    return Subscription("hook", register_hint="register_hook()")


def test_order_is_preserved():
    sub = _sub()
    for value in (len, abs, repr):
        sub.subscribe(value)
    assert [s.value for s in sub.items()] == [len, abs, repr]


def test_two_subscriptions_of_one_callable_stay_two():
    sub = _sub()
    sub.subscribe(len)
    sub.subscribe(len)
    assert len(sub) == 2


def test_cancel_twice_removes_exactly_one_occurrence():
    sub = _sub()
    first = sub.subscribe(len)
    sub.subscribe(len)
    first.cancel()
    first.cancel()
    assert [s.value for s in sub.items()] == [len]


def test_origin_and_repo_are_recorded_per_occurrence():
    sub = _sub()
    _from_module("pkg_a.init", sub.subscribe, len)
    with reg.registering_repo("b"):
        _from_module("pkg_b.init", sub.subscribe, len)
    assert [(s.origin, s.repo) for s in sub.items()] == [("pkg_a.init", None), ("pkg_b.init", "b")]


def test_the_refusal_applies():
    sub = _sub()
    with reg.loading_test_files(), pytest.raises(RegistrationRefused, match="init module"):
        _from_module("test_hooks", sub.subscribe, len)
    assert sub.items() == []
    with reg.loading_test_files():
        _from_module("otto.host.product", sub.subscribe, len)
    assert len(sub) == 1


def test_snapshot_and_restore_reinstall_occurrences_exactly():
    sub = _sub()
    _from_module("pkg_a.init", sub.subscribe, len)
    with reg.registering_repo("b"):
        _from_module("pkg_b.init", sub.subscribe, abs)
    before = sub.items()
    state = sub._snapshot()
    sub.subscribe(repr)
    sub.items()  # a read changes nothing
    revision = sub.revision
    sub._restore(state)
    assert sub.items() == before
    assert sub.revision == revision + 1


def test_a_token_issued_after_the_snapshot_cancels_nothing_after_restore():
    """Mutation: key tokens by value instead of by occurrence id."""
    sub = _sub()
    sub.subscribe(len)
    state = sub._snapshot()
    late = sub.subscribe(len)
    sub._restore(state)
    late.cancel()
    assert [s.value for s in sub.items()] == [len]


def test_a_token_issued_before_the_snapshot_still_cancels_after_restore():
    sub = _sub()
    early = sub.subscribe(len)
    state = sub._snapshot()
    sub.subscribe(abs)
    sub._restore(state)
    early.cancel()
    assert sub.items() == []


def test_drop_non_otto_keeps_ottos_occurrences():
    sub = _sub()
    _from_module("otto.host.product", sub.subscribe, len)
    _from_module("third.party", sub.subscribe, abs)
    assert sub._drop_non_otto() == {"third.party"}
    assert [s.value for s in sub.items()] == [len]

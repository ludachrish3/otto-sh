"""``RegistryView``: a read-only table derived from source registries."""

from dataclasses import dataclass

import pytest

from otto.registry import Derived, DuplicateRegistration, Registry, RegistryView


@dataclass(frozen=True)
class Rec:
    value: object


def _sources() -> "tuple[Registry[Rec], Registry[Rec]]":
    left = Registry("left", entry=Rec, register_hint="register_left()")
    right = Registry("right", entry=Rec, register_hint="register_right()")
    return left, right


def _view(left, right, calls=None) -> "RegistryView[Rec]":
    def derive():
        if calls is not None:
            calls.append(1)
        for source in (left, right):
            for name, entry in source.raw_items():
                yield Derived(name, entry, source.origin(name), source.repo(name))

    return RegistryView(
        "derived thing", register_hint="register_left()", sources=[left, right], derive=derive
    )


def test_derivation_equals_derive_rows():
    left, right = _sources()
    left.register("a", Rec(1))
    right.register("b", Rec(2))
    view = _view(left, right)
    assert view.items() == [("a", Rec(1)), ("b", Rec(2))]
    assert view.names() == ["a", "b"]
    assert view.get("b") == Rec(2)
    assert view.find("zzz") is None
    assert "a" in view and len(view) == 2  # noqa: PT018


def test_a_view_has_no_mutation_method():
    left, right = _sources()
    view = _view(left, right)
    for method in ("register", "register_many", "unregister"):
        assert not hasattr(view, method)


def test_revision_is_an_int_that_moves_with_any_source():
    left, right = _sources()
    view = _view(left, right)
    before = view.revision
    assert isinstance(before, int)
    assert view.revision == before  # reading alone never moves it
    left.register("a", Rec(1))
    after_left = view.revision
    assert after_left > before
    right.register("b", Rec(2))
    assert view.revision > after_left


def test_derived_rows_are_cached_per_token():
    left, right = _sources()
    left.register("a", Rec(1))
    calls = []
    view = _view(left, right, calls)
    view.names()
    view.get("a")
    view.items()
    assert len(calls) == 1
    right.register("b", Rec(2))
    assert view.names() == ["a", "b"]
    assert len(calls) == 2


def test_an_unknown_name_raises_the_rich_error():
    left, right = _sources()
    left.register("alpha", Rec(1))
    view = _view(left, right)
    with pytest.raises(ValueError, match="Unknown derived thing 'alhpa'") as err:
        view.get("alhpa")
    assert "Did you mean 'alpha'?" in str(err.value)
    assert "register_left()" in str(err.value)


def test_origin_and_repo_come_from_the_derived_rows():
    left, right = _sources()
    left.register("a", Rec(1))
    view = _view(left, right)
    assert view.origin("a") == __name__
    assert view.repo("a") is None


def test_a_name_derived_twice_raises():
    left, right = _sources()
    left.register("a", Rec(1))
    right.register("a", Rec(2))
    view = _view(left, right)
    with pytest.raises(DuplicateRegistration, match="derived twice"):
        view.names()

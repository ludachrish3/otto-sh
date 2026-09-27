"""Registry references: listing is free, lookup imports one entry (spec 2026-09-26 §4.2)."""

import sys

import pytest

from otto.registry import Ref, Registry


def _reg(**kw):
    return Registry("demo thing", register_hint="register_demo()", **kw)


def test_ref_rejects_a_target_without_an_attribute():
    with pytest.raises(ValueError, match="package.module:attribute"):  # noqa: RUF043
        Ref("json")


def test_ref_resolves_on_its_own_without_a_registry():
    assert Ref("json:dumps").resolve() is __import__("json").dumps


def test_names_and_membership_never_import_the_target(monkeypatch):
    monkeypatch.delitem(sys.modules, "colorsys", raising=False)
    r = _reg()
    r.register("c", Ref("colorsys:rgb_to_hsv"), origin="otto.test")
    assert r.names() == ["c"] and "c" in r and len(r) == 1  # noqa: PT018
    assert r.origin("c") == "otto.test"
    assert "colorsys" not in sys.modules


def test_get_imports_resolves_caches_and_validates_once(monkeypatch):
    monkeypatch.delitem(sys.modules, "colorsys", raising=False)
    seen = []
    r = _reg(validate=lambda name, obj: seen.append((name, obj)))
    r.register("c", Ref("colorsys:rgb_to_hsv"), origin="otto.test")

    assert "colorsys" not in sys.modules
    resolved = r.get("c")
    assert resolved is sys.modules["colorsys"].rgb_to_hsv
    assert r.get("c") is resolved
    assert seen == [("c", resolved)]


def test_items_resolves_every_reference():
    r = _reg()
    r.register("d", Ref("json:dumps"), origin="otto.test")
    r.register("l", Ref("json:loads"), origin="otto.test")
    import json

    assert r.items() == [("d", json.dumps), ("l", json.loads)]


def test_collision_with_a_reference_is_detected_without_resolving_it(monkeypatch):
    monkeypatch.delitem(sys.modules, "colorsys", raising=False)
    r = _reg()
    r.register("c", Ref("colorsys:rgb_to_hsv"), origin="otto.test")
    with pytest.raises(ValueError, match="already registered by 'otto.test'"):  # noqa: RUF043
        r.register("c", len, origin="plugin")
    assert "colorsys" not in sys.modules
    r.register("c", len, origin="plugin", overwrite=True)
    assert r.get("c") is len


def test_a_failing_reference_raises_on_get_but_names_still_list_it():
    r = _reg()
    r.register("x", Ref("otto_no_such_module_anywhere:thing"), origin="otto.test")
    assert r.names() == ["x"]
    with pytest.raises(ModuleNotFoundError):
        r.get("x")
    assert "x" in r


def test_a_real_object_is_validated_at_register():
    def refuse(name, obj):
        raise ValueError(f"bad {name}")

    reg = _reg(validate=refuse)
    with pytest.raises(ValueError, match="bad y"):
        reg.register("y", len, origin="otto.test")
    assert "y" not in reg


def test_unregistering_a_reference_does_not_import_it(monkeypatch):
    monkeypatch.delitem(sys.modules, "colorsys", raising=False)
    r = _reg()
    r.register("c", Ref("colorsys:rgb_to_hsv"), origin="otto.test")
    r.unregister("c")
    assert "c" not in r and "colorsys" not in sys.modules  # noqa: PT018


def test_a_failing_validator_leaves_the_reference_unresolved_and_retryable(monkeypatch):
    monkeypatch.delitem(sys.modules, "colorsys", raising=False)
    calls = []

    def flaky(name, obj):
        calls.append(obj)
        if len(calls) == 1:
            raise ValueError("not ready yet")

    r = _reg(validate=flaky)
    ref = Ref("colorsys:rgb_to_hsv")
    r.register("c", ref, origin="otto.test")

    with pytest.raises(ValueError, match="not ready yet"):
        r.get("c")

    raw = {name: entry for name, entry, _origin in r._raw_items()}
    assert raw["c"] is ref
    assert r.origin("c") == "otto.test"

    resolved = r.get("c")
    assert resolved is sys.modules["colorsys"].rgb_to_hsv
    assert calls == [resolved, resolved]

    assert r.get("c") is resolved
    assert len(calls) == 2, "a cached entry must not re-validate"


def test_snapshot_and_restore_round_trip_never_calls_validate(monkeypatch):
    monkeypatch.delitem(sys.modules, "colorsys", raising=False)
    seen = []
    r = _reg(validate=lambda name, obj: seen.append((name, obj)))
    r.register("real", 42, origin="otto.test")  # a real object: validates now
    ref = Ref("colorsys:rgb_to_hsv")
    r.register("ref", ref, origin="otto.test")  # a Ref: validate deferred

    assert seen == [("real", 42)]
    snapshot = list(r._raw_items())  # raw: "real" -> 42, "ref" -> the Ref itself

    resolved = r.get("ref")  # resolves and validates once
    assert seen == [("real", 42), ("ref", resolved)]

    for name, entry, origin in snapshot:
        r._restore_raw(name, entry, origin)

    assert seen == [("real", 42), ("ref", resolved)], "restore must not re-run validate"
    raw = {name: entry for name, entry, _origin in r._raw_items()}
    assert raw["real"] == 42
    assert raw["ref"] is ref, "restore must put the unresolved Ref back, not the cached resolution"

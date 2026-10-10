"""Registry references: listing is free, lookup imports one entry (spec 2026-09-26 §4.2)."""

import sys
from dataclasses import dataclass

import pytest

from otto.registry import Ref, Registry


@dataclass(frozen=True)
class Entry:
    """A record whose one field may name its value by reference."""

    value: "object | Ref"


def _reg(**kw) -> "Registry[Entry]":
    kw.setdefault("check_resolved", lambda name, entry: None)
    return Registry("demo thing", entry=Entry, register_hint="register_demo()", **kw)


def test_ref_rejects_a_target_without_an_attribute():
    with pytest.raises(ValueError, match="package.module:attribute"):  # noqa: RUF043
        Ref("json")


def test_ref_resolves_on_its_own_without_a_registry():
    assert Ref("json:dumps").resolve() is __import__("json").dumps


def test_names_and_membership_never_import_the_target(monkeypatch):
    monkeypatch.delitem(sys.modules, "colorsys", raising=False)
    r = _reg()
    r.register("c", Entry(Ref("colorsys:rgb_to_hsv")))
    assert r.names() == ["c"] and "c" in r and len(r) == 1  # noqa: PT018
    assert r.origin("c") == __name__
    assert "colorsys" not in sys.modules


def test_get_imports_resolves_caches_and_checks_once(monkeypatch):
    monkeypatch.delitem(sys.modules, "colorsys", raising=False)
    seen = []
    r = _reg(check_resolved=lambda name, entry: seen.append((name, entry.value)))
    r.register("c", Entry(Ref("colorsys:rgb_to_hsv")))

    assert "colorsys" not in sys.modules
    resolved = r.get("c")
    assert resolved.value is sys.modules["colorsys"].rgb_to_hsv
    assert r.get("c") is resolved
    assert seen == [("c", resolved.value)]


def test_items_resolves_every_reference():
    r = _reg()
    r.register("d", Entry(Ref("json:dumps")))
    r.register("l", Entry(Ref("json:loads")))
    import json

    assert r.items() == [("d", Entry(json.dumps)), ("l", Entry(json.loads))]


def test_collision_with_a_reference_is_detected_without_resolving_it(monkeypatch):
    monkeypatch.delitem(sys.modules, "colorsys", raising=False)
    r = _reg()
    r.register("c", Entry(Ref("colorsys:rgb_to_hsv")))
    with pytest.raises(ValueError, match="already registered by"):
        r.register("c", Entry(len))
    assert "colorsys" not in sys.modules
    r.register("c", Entry(len), overwrite=True)
    assert r.get("c").value is len


def test_a_failing_reference_raises_on_get_but_names_still_list_it():
    r = _reg()
    r.register("x", Entry(Ref("otto_no_such_module_anywhere:thing")))
    assert r.names() == ["x"]
    with pytest.raises(ModuleNotFoundError):
        r.get("x")
    assert "x" in r


def test_a_real_object_is_validated_at_register():
    def refuse(name, entry, proposed):
        raise ValueError(f"bad {name}")

    reg = _reg(validate=refuse)
    with pytest.raises(ValueError, match="bad y"):
        reg.register("y", Entry(len))
    assert "y" not in reg


def test_unregistering_a_reference_does_not_import_it(monkeypatch):
    monkeypatch.delitem(sys.modules, "colorsys", raising=False)
    r = _reg()
    r.register("c", Entry(Ref("colorsys:rgb_to_hsv")))
    r.unregister("c")
    assert "c" not in r and "colorsys" not in sys.modules  # noqa: PT018


def test_a_failing_check_leaves_the_reference_unresolved_and_retryable(monkeypatch):
    monkeypatch.delitem(sys.modules, "colorsys", raising=False)
    calls = []

    def flaky(name, entry):
        calls.append(entry.value)
        if len(calls) == 1:
            raise ValueError("not ready yet")

    r = _reg(check_resolved=flaky)
    ref = Ref("colorsys:rgb_to_hsv")
    r.register("c", Entry(ref))

    with pytest.raises(ValueError, match="not ready yet"):
        r.get("c")

    assert r.peek("c").value is ref
    assert r.origin("c") == __name__

    resolved = r.get("c")
    assert resolved.value is sys.modules["colorsys"].rgb_to_hsv
    assert calls == [resolved.value, resolved.value]

    assert r.get("c") is resolved
    assert len(calls) == 2, "a cached entry must not re-check"


def test_snapshot_and_restore_round_trip_never_runs_a_check(monkeypatch):
    monkeypatch.delitem(sys.modules, "colorsys", raising=False)
    validated = []
    checked = []
    r = _reg(
        validate=lambda name, entry, proposed: validated.append(name),
        check_resolved=lambda name, entry: checked.append(name),
    )
    r.register("real", Entry(42))
    ref = Ref("colorsys:rgb_to_hsv")
    r.register("ref", Entry(ref))

    assert validated == ["real", "ref"]
    snapshot = r._snapshot()  # raw: "real" -> 42, "ref" -> the Ref itself

    r.get("ref")  # resolves and checks once
    assert checked == ["ref"]

    r._restore(snapshot)

    assert validated == ["real", "ref"], "restore must not re-run validate"
    assert checked == ["ref"], "restore must not re-run check_resolved"
    assert r.peek("real") == Entry(42)
    assert r.peek("ref").value is ref, "restore must put the unresolved Ref back"

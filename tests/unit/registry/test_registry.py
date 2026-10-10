"""Unit tests for the generic component Registry: round trips, duplicates, lookups."""

from dataclasses import dataclass

import pytest

from otto.registry import DuplicateRegistration, Registry


@dataclass(frozen=True)
class Backend:
    label: str


def _make() -> Registry[Backend]:
    return Registry("term backend", entry=Backend, register_hint="otto.register_term_backend()")


def _from_module(module: str, fn, *args, **kwargs):
    """Call *fn* from a frame whose module is *module* (attribution is by frame)."""
    code = compile("result = fn(*args, **kwargs)", f"<{module}>", "exec")
    scope = {"__name__": module, "fn": fn, "args": args, "kwargs": kwargs}
    exec(code, scope)  # noqa: S102 — a synthetic caller module, the attribution under test
    return scope["result"]


class TestRegisterAndGet:
    def test_round_trip_and_order(self):
        r = _make()
        r.register("ssh", Backend("SSH"))
        r.register("telnet", Backend("TELNET"))
        assert r.get("ssh") == Backend("SSH")
        assert r.names() == ["ssh", "telnet"]  # registration order, not sorted
        assert "ssh" in r
        assert len(r) == 2
        assert r.items() == [("ssh", Backend("SSH")), ("telnet", Backend("TELNET"))]

    def test_duplicate_raises_naming_both_origins(self):
        r = _make()
        _from_module("repo_a.init", r.register, "ssh", Backend("A"))
        with pytest.raises(
            DuplicateRegistration, match=r"already registered by 'repo_a.init'"
        ) as ei:
            _from_module("repo_b.init", r.register, "ssh", Backend("B"))
        assert "repo_b.init" in str(ei.value)

    def test_default_collision_message_mentions_overwrite(self):
        r = _make()
        r.register("ssh", Backend("A"))
        with pytest.raises(ValueError, match=r"Pass overwrite=True to replace it deliberately\."):
            r.register("ssh", Backend("B"))

    def test_overwrite_replaces(self):
        r = _make()
        r.register("json", Backend("OLD"))
        r.register("json", Backend("NEW"), overwrite=True)
        assert r.get("json") == Backend("NEW")

    def test_origin_is_the_calling_module(self):
        r = _make()
        r.register("ssh", Backend("A"))
        assert r.origin("ssh") == __name__

    def test_unregister(self):
        r = _make()
        r.register("ssh", Backend("A"))
        r.unregister("ssh")
        assert "ssh" not in r
        with pytest.raises(ValueError, match="Unknown term backend"):
            r.unregister("ssh")


class TestErrors:
    def test_unknown_lists_names_hint_and_suggestion(self):
        r = _make()
        r.register("telnet", Backend("T"))
        with pytest.raises(ValueError, match="Unknown term backend") as ei:
            r.get("tellnet")
        msg = str(ei.value)
        assert "Unknown term backend 'tellnet'" in msg
        assert "Did you mean 'telnet'?" in msg
        assert "telnet" in msg
        assert "otto.register_term_backend()" in msg

    def test_unknown_without_close_match_has_no_suggestion(self):
        r = _make()
        r.register("telnet", Backend("T"))
        assert "Did you mean" not in _get_error(r, "zzz")

    def test_empty_registry_says_none(self):
        assert "<none>" in _get_error(_make(), "x")


def _get_error(r: Registry[Backend], name: str) -> str:
    with pytest.raises(ValueError, match="Unknown") as ei:
        r.get(name)
    return str(ei.value)

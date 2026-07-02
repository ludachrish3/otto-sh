"""Unit tests for the generic component Registry (spec 2026-07-01)."""

import pytest

from otto.registry import Registry, caller_module


def _make() -> Registry[str]:
    return Registry("term backend", register_hint="otto.register_term_backend()")


class TestRegisterAndGet:
    def test_round_trip_and_order(self):
        r = _make()
        r.register("ssh", "SSH")
        r.register("telnet", "TELNET")
        assert r.get("ssh") == "SSH"
        assert r.names() == ["ssh", "telnet"]  # registration order, not sorted
        assert "ssh" in r
        assert len(r) == 2
        assert r.items() == [("ssh", "SSH"), ("telnet", "TELNET")]

    def test_duplicate_raises_naming_both_origins(self):
        r = _make()
        r.register("ssh", "A", origin="repo_a.init")
        with pytest.raises(ValueError, match=r"already registered by 'repo_a.init'") as ei:
            r.register("ssh", "B", origin="repo_b.init")
        assert "repo_b.init" in str(ei.value)

    def test_default_collision_hint_mentions_overwrite(self):
        r = _make()
        r.register("ssh", "A")
        with pytest.raises(ValueError, match=r"Pass overwrite=True to replace it deliberately\."):
            r.register("ssh", "B")

    def test_custom_collision_hint_replaces_overwrite_sentence(self):
        r = Registry(
            "CLI command",
            register_hint="otto.register_cli_command()",
            collision_hint="CLI command names cannot be overwritten; pick a unique name.",
        )
        r.register("run", "A")
        with pytest.raises(ValueError, match="already registered") as ei:
            r.register("run", "B")
        msg = str(ei.value)
        assert "CLI command names cannot be overwritten; pick a unique name." in msg
        assert "overwrite=True" not in msg  # the dead-end hint is gone

    def test_overwrite_replaces(self):
        r = _make()
        r.register("json", "OLD")
        r.register("json", "NEW", overwrite=True)
        assert r.get("json") == "NEW"

    def test_origin_defaults_to_caller_module(self):
        r = _make()
        r.register("ssh", "A")
        assert r.origin("ssh") == __name__

    def test_unregister(self):
        r = _make()
        r.register("ssh", "A")
        r.unregister("ssh")
        assert "ssh" not in r
        with pytest.raises(ValueError, match="Unknown term backend"):
            r.unregister("ssh")


class TestErrors:
    def test_unknown_lists_names_hint_and_suggestion(self):
        r = _make()
        r.register("telnet", "T")
        with pytest.raises(ValueError, match="Unknown term backend") as ei:
            r.get("tellnet")
        msg = str(ei.value)
        assert "Unknown term backend 'tellnet'" in msg
        assert "Did you mean 'telnet'?" in msg
        assert "telnet" in msg
        assert "otto.register_term_backend()" in msg

    def test_unknown_without_close_match_has_no_suggestion(self):
        r = _make()
        r.register("telnet", "T")
        assert "Did you mean" not in _get_error(r, "zzz")

    def test_empty_registry_says_none(self):
        assert "<none>" in _get_error(_make(), "x")


def _get_error(r: Registry[str], name: str) -> str:
    with pytest.raises(ValueError, match="Unknown") as ei:
        r.get(name)
    return str(ei.value)


def test_caller_module_depth():
    def inner() -> str:
        return caller_module()

    assert inner() == __name__

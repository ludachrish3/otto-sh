"""Unit tests for the generic component Registry: round trips, duplicates, lookups."""

import json
import logging
from dataclasses import dataclass

import pytest

from otto.registry import DuplicateRegistration, Ref, RegistrationRefused, Registry
from tests._fixtures.registrant import from_module


@dataclass(frozen=True)
class Backend:
    label: str


def _make() -> Registry[Backend]:
    return Registry("term backend", entry=Backend, register_hint="otto.register_term_backend()")


class TestRegisterAndGet:
    def test_round_trip_and_order(self):
        r = _make()
        r.register(name="ssh", entry=Backend("SSH"))
        r.register(name="telnet", entry=Backend("TELNET"))
        assert r.get("ssh") == Backend("SSH")
        assert r.names() == ["ssh", "telnet"]  # registration order, not sorted
        assert "ssh" in r
        assert len(r) == 2
        assert r.items() == [("ssh", Backend("SSH")), ("telnet", Backend("TELNET"))]

    def test_duplicate_raises_naming_both_origins(self):
        r = _make()
        from_module("project_a.init", r.register, name="ssh", entry=Backend("A"))
        with pytest.raises(
            DuplicateRegistration, match=r"already registered by 'project_a.init'"
        ) as ei:
            from_module("project_b.init", r.register, name="ssh", entry=Backend("B"))
        assert "project_b.init" in str(ei.value)

    def test_default_collision_message_mentions_overwrite(self):
        r = _make()
        r.register(name="ssh", entry=Backend("A"))
        with pytest.raises(ValueError, match=r"Pass overwrite=True to replace it deliberately\."):
            r.register(name="ssh", entry=Backend("B"))

    def test_overwrite_replaces(self):
        r = _make()
        r.register(name="json", entry=Backend("OLD"))
        r.register(name="json", entry=Backend("NEW"), overwrite=True)
        assert r.get("json") == Backend("NEW")

    def test_origin_is_the_calling_module(self):
        r = _make()
        r.register(name="ssh", entry=Backend("A"))
        assert r.origin(name="ssh") == __name__

    def test_unregister(self):
        r = _make()
        r.register(name="ssh", entry=Backend("A"))
        r.unregister(name="ssh")
        assert "ssh" not in r
        with pytest.raises(ValueError, match="Unknown term backend"):
            r.unregister(name="ssh")


@dataclass(frozen=True)
class Named:
    """A record whose one field may name its value by reference."""

    value: "object | Ref"


def _make_named(**kw) -> "Registry[Named]":
    kw.setdefault("check_resolved", lambda name, entry: None)
    return Registry("lab repository backend", entry=Named, register_hint="register_x()", **kw)


_BUILTIN = "otto.labs.builtin_double"
_TEAM = "acme.init"


def _yielded_warnings(caplog) -> "list[str]":
    return [
        rec.getMessage()
        for rec in caplog.records
        if rec.name == "otto.registry" and rec.levelno == logging.WARNING
    ]


class TestYielding:
    """A built-in registered with ``yielding=True`` gives its name to a team's registration."""

    def test_team_registration_keeps_a_yielding_builtin_name(self, caplog):
        r = _make_named()
        from_module(_BUILTIN, r.register, name="jsonnet", entry=Named("otto"), yielding=True)
        with caplog.at_level(logging.WARNING, logger="otto.registry"):
            from_module(_TEAM, r.register, name="jsonnet", entry=Named("team"))
        assert r.get("jsonnet") == Named("team")
        assert r.origin(name="jsonnet") == _TEAM
        assert _yielded_warnings(caplog) == [
            (
                f"lab repository backend 'jsonnet': {_TEAM} replaces otto's built-in "
                f"({_BUILTIN}); otto's 'jsonnet' is unavailable in this process"
            )
        ]

    def test_yielding_survives_ref_publication(self, caplog):
        r = _make_named()
        from_module(
            _BUILTIN,
            r.register,
            name="jsonnet",
            entry=Named(Ref(target="json:dumps")),
            yielding=True,
        )
        assert r.get("jsonnet") == Named(json.dumps)  # publishes the resolved record
        with caplog.at_level(logging.WARNING, logger="otto.registry"):
            from_module(_TEAM, r.register, name="jsonnet", entry=Named("team"))
        assert r.get("jsonnet") == Named("team")
        assert len(_yielded_warnings(caplog)) == 1

    def test_yielding_survives_snapshot_and_restore(self, caplog):
        r = _make_named()
        from_module(_BUILTIN, r.register, name="jsonnet", entry=Named("otto"), yielding=True)
        state = r._snapshot()
        from_module(_TEAM, r.register, name="jsonnet", entry=Named("team"))
        r._restore(state)
        assert r.get("jsonnet") == Named("otto")
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="otto.registry"):
            from_module("other.init", r.register, name="jsonnet", entry=Named("other"))
        assert r.get("jsonnet") == Named("other")
        assert len(_yielded_warnings(caplog)) == 1

    def test_a_replaced_yielding_name_no_longer_yields(self):
        r = _make_named()
        from_module(_BUILTIN, r.register, name="jsonnet", entry=Named("otto"), yielding=True)
        from_module(_TEAM, r.register, name="jsonnet", entry=Named("team"))
        with pytest.raises(DuplicateRegistration, match=r"already registered by 'acme.init'"):
            from_module("other.init", r.register, name="jsonnet", entry=Named("other"))
        assert r.get("jsonnet") == Named("team")

    def test_overwrite_of_a_yielding_name_replaces_it_silently(self, caplog):
        r = _make_named()
        from_module(_BUILTIN, r.register, name="jsonnet", entry=Named("otto"), yielding=True)
        with caplog.at_level(logging.WARNING, logger="otto.registry"):
            from_module(_TEAM, r.register, name="jsonnet", entry=Named("team"), overwrite=True)
        assert r.get("jsonnet") == Named("team")
        assert _yielded_warnings(caplog) == []

    def test_a_refused_registration_keeps_the_builtin_and_logs_nothing(self, caplog):
        def refuse_team(name, entry, proposed):
            if entry == Named("team"):
                raise ValueError("refused")

        r = _make_named(validate=refuse_team)
        from_module(_BUILTIN, r.register, name="jsonnet", entry=Named("otto"), yielding=True)
        with (
            caplog.at_level(logging.WARNING, logger="otto.registry"),
            pytest.raises(ValueError, match="refused"),
        ):
            from_module(_TEAM, r.register, name="jsonnet", entry=Named("team"))
        assert r.get("jsonnet") == Named("otto")
        assert r.origin(name="jsonnet") == _BUILTIN
        assert _yielded_warnings(caplog) == []

    def test_register_many_replaces_each_yielding_name_once(self, caplog):
        r = _make_named()
        from_module(_BUILTIN, r.register, name="jsonnet", entry=Named("otto"), yielding=True)
        with caplog.at_level(logging.WARNING, logger="otto.registry"):
            from_module(
                _TEAM,
                r.register_many,
                [("jsonnet", Named("team")), ("yaml", Named("team-yaml"))],
            )
        assert r.get("jsonnet") == Named("team")
        assert len(_yielded_warnings(caplog)) == 1

    def test_register_many_with_yielding_marks_every_entry(self, caplog):
        r = _make_named()
        from_module(
            _BUILTIN,
            r.register_many,
            [("jsonnet", Named("otto")), ("yaml", Named("otto-yaml"))],
            yielding=True,
        )
        with caplog.at_level(logging.WARNING, logger="otto.registry"):
            from_module(_TEAM, r.register, name="jsonnet", entry=Named("team"))
            from_module(_TEAM, r.register, name="yaml", entry=Named("team-yaml"))
        assert r.get("jsonnet") == Named("team")
        assert r.get("yaml") == Named("team-yaml")
        assert len(_yielded_warnings(caplog)) == 2

    @pytest.mark.parametrize("registrant", ["acme.init", "ottoish.init", "__main__"])
    def test_yielding_is_refused_outside_otto(self, registrant):
        r = _make_named()
        with pytest.raises(RegistrationRefused, match=r"only otto's own modules may yield"):
            from_module(registrant, r.register, name="jsonnet", entry=Named("x"), yielding=True)
        with pytest.raises(RegistrationRefused, match=r"only otto's own modules may yield"):
            from_module(registrant, r.register_many, [("yaml", Named("y"))], yielding=True)
        assert r.names() == []

    def test_an_otto_module_re_registering_its_own_yielding_name_raises(self, caplog):
        r = _make_named()
        from_module(_BUILTIN, r.register, name="jsonnet", entry=Named("otto"), yielding=True)
        with (
            caplog.at_level(logging.WARNING, logger="otto.registry"),
            pytest.raises(
                DuplicateRegistration, match=r"already registered by 'otto\.labs\.builtin_double'"
            ),
        ):
            from_module(_BUILTIN, r.register, name="jsonnet", entry=Named("again"), yielding=True)
        assert r.get("jsonnet") == Named("otto")
        assert _yielded_warnings(caplog) == []


class TestErrors:
    def test_unknown_lists_names_hint_and_suggestion(self):
        r = _make()
        r.register(name="telnet", entry=Backend("T"))
        with pytest.raises(ValueError, match="Unknown term backend") as ei:
            r.get("tellnet")
        msg = str(ei.value)
        assert "Unknown term backend 'tellnet'" in msg
        assert "Did you mean 'telnet'?" in msg
        assert "telnet" in msg
        assert "otto.register_term_backend()" in msg

    def test_unknown_without_close_match_has_no_suggestion(self):
        r = _make()
        r.register(name="telnet", entry=Backend("T"))
        assert "Did you mean" not in _get_error(r, "zzz")

    def test_empty_registry_says_none(self):
        assert "<none>" in _get_error(_make(), "x")


def _get_error(r: Registry[Backend], name: str) -> str:
    with pytest.raises(ValueError, match="Unknown") as ei:
        r.get(name)
    return str(ei.value)

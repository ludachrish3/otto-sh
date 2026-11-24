"""CapabilityResolver — menu validation + active-selection resolution."""

import copy
import dataclasses
import inspect
import json
import pickle
import types

import pytest

from otto.host.capability import (
    TERM_RESOLVER,
    TRANSFER_RESOLVER,
    CapabilityResolver,
    default_selection,
    is_default_selection,
    menu_capabilities,
    resolve_selections,
    select_option_defaults,
    select_preferences,
    settle_selections,
    user_pin,
)
from otto.host.remote_host import RemoteHost
from otto.host.unix_host import UnixHost


def test_validate_choice_returns_in_menu():
    r = CapabilityResolver("transfer")
    assert r.validate_choice(["scp", "nc"], "nc") == "nc"


def test_validate_choice_rejects_out_of_menu_with_field_and_menu():
    r = CapabilityResolver("transfer")
    with pytest.raises(ValueError, match="transfer 'sftp' is not in"):
        r.validate_choice(["scp", "nc"], "sftp")


def test_resolve_active_no_pin_no_preference_is_menu_first():
    r = CapabilityResolver("term")
    assert r.resolve_active(["ssh", "telnet"]) == "ssh"


def test_resolve_active_pin_in_menu_wins():
    r = CapabilityResolver("term")
    assert r.resolve_active(["ssh", "telnet"], pin="telnet") == "telnet"


def test_resolve_active_pin_out_of_menu_raises():
    r = CapabilityResolver("term")
    with pytest.raises(ValueError, match="term 'rsh' is not in"):
        r.resolve_active(["ssh", "telnet"], pin="rsh")


def test_resolve_active_first_preference_in_menu():
    r = CapabilityResolver("transfer")
    assert r.resolve_active(["scp", "nc"], preference=["sftp", "nc"]) == "nc"


def test_resolve_active_preference_all_out_of_menu_falls_to_first():
    r = CapabilityResolver("transfer")
    assert r.resolve_active(["scp", "nc"], preference=["sftp", "ftp"]) == "scp"


def test_resolve_active_preference_beats_pin():
    r = CapabilityResolver("transfer")
    # Product preference now wins over the lab pin, as long as it is in the menu.
    assert r.resolve_active(["scp", "nc"], pin="scp", preference=["nc"]) == "nc"


def test_resolve_active_pin_still_validated_when_preference_overrides():
    # A malformed lab pin (outside its own menu) is still fail-loud even when a
    # preference would override it.
    r = CapabilityResolver("transfer")
    with pytest.raises(ValueError, match="transfer 'rsh' is not in"):
        r.resolve_active(["scp", "nc"], pin="rsh", preference=["nc"])


def test_resolve_active_falls_back_to_validated_pin():
    # No in-menu preference -> the validated pin is used.
    r = CapabilityResolver("term")
    assert r.resolve_active(["ssh", "telnet"], pin="telnet", preference=["rsh"]) == "telnet"


def test_module_singletons_carry_field_names():
    assert TERM_RESOLVER.field == "term"
    assert TRANSFER_RESOLVER.field == "transfer"


class TestSelectPreferences:
    def test_empty_table_is_empty(self):
        assert select_preferences({}, "test1") == {}

    def test_dotstar_matches_all(self):
        table = {".*": {"transfer": ["sftp"]}}
        assert select_preferences(table, "anything_here") == {"transfer": ["sftp"]}

    def test_specific_overrides_base_per_capability(self):
        table = {
            ".*": {"term": ["ssh"], "transfer": ["sftp", "scp"]},
            "zephyr.*": {"transfer": ["console"]},
        }
        # zephyr host: term from the base, transfer overridden by the specific
        assert select_preferences(table, "zephyr1-board") == {
            "term": ["ssh"],
            "transfer": ["console"],
        }
        # non-zephyr host: only the base applies
        assert select_preferences(table, "test1") == {"term": ["ssh"], "transfer": ["sftp", "scp"]}

    def test_definition_order_breaks_ties(self):
        table = {
            ".*": {"transfer": ["a"]},
            "test.*": {"transfer": ["b"]},
            "test1.*": {"transfer": ["c"]},  # last matching selector wins
        }
        assert select_preferences(table, "test1")["transfer"] == ["c"]

    def test_fullmatch_not_search(self):
        # "test" only matches the whole id; it does NOT match "test1"
        assert select_preferences({"test": {"transfer": ["x"]}}, "test1") == {}


class TestSelectOptionDefaults:
    def test_empty_table_is_empty(self):
        assert select_option_defaults({}, "test1") == {}

    def test_dict_entries_returned_lists_ignored(self):
        table = {".*": {"transfer": ["nc"], "ssh_options": {"port": 22}}}
        assert select_option_defaults(table, "anything") == {"ssh_options": {"port": 22}}

    def test_per_key_merge_across_matching_selectors(self):
        table = {
            ".*": {"ssh_options": {"connect_timeout": 5.0}},
            "router.*": {"ssh_options": {"port": 2222}},
        }
        assert select_option_defaults(table, "router1") == {
            "ssh_options": {"connect_timeout": 5.0, "port": 2222}
        }
        assert select_option_defaults(table, "test1") == {"ssh_options": {"connect_timeout": 5.0}}


def test_select_preferences_ignores_option_tables():
    table = {".*": {"transfer": ["nc"], "ssh_options": {"port": 22}}}
    assert select_preferences(table, "anything") == {"transfer": ["nc"]}


def test_a_default_selection_reads_as_the_plain_string():
    m = default_selection(value="scp")
    assert m == "scp"
    assert hash(m) == hash("scp")
    assert repr(m) == "'scp'"
    assert f"{m}" == "scp"
    assert json.dumps({"t": m}) == '{"t": "scp"}'
    assert {"scp": 1}[m] == 1
    assert is_default_selection(m)
    assert not is_default_selection("scp")
    assert is_default_selection(copy.deepcopy(m))
    assert is_default_selection(pickle.loads(pickle.dumps(m)))  # noqa: S301 — round trip of a value this test just built


def test_user_pin_reads_a_marked_value_as_no_pin():
    assert user_pin(default_selection(value="scp")) is None
    assert user_pin("scp") == "scp"
    assert user_pin(None) is None


def test_the_marked_class_default_keeps_its_recorded_value():
    (declared,) = [f for f in dataclasses.fields(RemoteHost) if f.name == "transfer"]
    assert declared.default == "scp"
    assert is_default_selection(declared.default)
    assert str(inspect.signature(UnixHost).parameters["transfer"]) == "transfer: str = 'scp'"


UNIX = menu_capabilities("unix")


def _select(menus, pins=None, preferences=None, caps=UNIX):
    full = {c.pin_field: c.default_menu for c in caps} | menus
    return resolve_selections(
        caps, label="h", menus=full, pins=pins or {}, preferences=preferences or {}
    )


def test_the_table_names_each_familys_capabilities():
    assert [(c.pin_field, c.menu_field, c.default_menu, c.witnessed) for c in UNIX] == [
        ("term", "valid_terms", ["ssh", "telnet"], True),
        ("transfer", "valid_transfers", ["scp", "sftp", "ftp", "nc"], True),
        ("impairer", "valid_impairers", ["netem"], False),
    ]
    assert [(c.pin_field, c.default_menu) for c in menu_capabilities("embedded")] == [
        ("term", ["telnet"]),
        ("transfer", ["console"]),
    ]
    assert menu_capabilities(None) == []
    assert menu_capabilities("nope") == []
    for cap in [*UNIX, *menu_capabilities("embedded")]:
        assert cap.default_pin == cap.default_menu[0]
        assert is_default_selection(cap.default_pin)
        assert cap.fresh_menu() == cap.default_menu
        assert cap.fresh_menu() is not cap.default_menu


def test_head_of_list_is_marked_and_a_pin_or_preference_is_plain():
    assert is_default_selection(_select({"transfer": ["nc", "sftp"]})["transfer"])
    pinned = _select({"transfer": ["nc", "sftp"]}, pins={"transfer": "sftp"})["transfer"]
    assert pinned == "sftp"
    assert not is_default_selection(pinned)
    chosen = _select({"transfer": ["nc", "sftp"]}, preferences={"transfer": ["sftp"]})["transfer"]
    assert chosen == "sftp"
    assert not is_default_selection(chosen)


def test_a_marked_pin_is_no_pin():
    got = _select({"transfer": ["nc", "sftp"]}, pins={"transfer": default_selection(value="scp")})
    assert got["transfer"] == "nc"


def test_an_empty_list_is_refused_by_name():
    with pytest.raises(ValueError, match="valid_transfers must be a non-empty list"):
        _select({"transfer": []})


def test_the_console_rule_is_applied_from_the_resolved_term():
    console = {"term": ["console"]}
    assert _select(console | {"transfer": ["nc", "shell"]})["transfer"] == "shell"
    marked_nc = {"transfer": default_selection(value="nc")}
    assert _select(console | {"transfer": ["nc", "shell"]}, pins=marked_nc)["transfer"] == "shell"
    with pytest.raises(ValueError, match="transfer 'nc' cannot run on a console term"):
        _select(console | {"transfer": ["nc", "shell"]}, pins={"transfer": "nc"})
    with pytest.raises(ValueError, match="leaves a console term no transfer"):
        _select(console | {"transfer": ["nc"]})
    embedded = menu_capabilities("embedded")
    assert _select({"term": ["console"]}, caps=embedded)["transfer"] == "console"


def test_settle_selections_sets_each_pin_and_leaves_the_lists():
    host = types.SimpleNamespace(
        name="h",
        term=default_selection(value="ssh"),
        transfer=default_selection(value="scp"),
        impairer="netem",
        valid_terms=["telnet"],
        valid_transfers=["nc", "sftp"],
        valid_impairers=["netem"],
    )
    settle_selections(host, family="unix")
    assert (host.term, host.transfer, host.impairer) == ("telnet", "nc", "netem")
    assert is_default_selection(host.transfer)
    assert not is_default_selection(host.impairer)
    assert host.valid_transfers == ["nc", "sftp"]

"""CapabilityResolver — menu validation + active-selection resolution."""

import pytest

from otto.host.capability import (
    TERM_RESOLVER,
    TRANSFER_RESOLVER,
    CapabilityResolver,
    select_option_defaults,
    select_preferences,
)


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
        assert select_preferences(table, "zephyr1_board") == {
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

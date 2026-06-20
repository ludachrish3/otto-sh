"""CapabilityResolver — menu validation + active-selection resolution."""
import pytest

from otto.host.capability import (
    TERM_RESOLVER,
    TRANSFER_RESOLVER,
    CapabilityResolver,
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


def test_resolve_active_pin_beats_preference():
    r = CapabilityResolver("transfer")
    assert r.resolve_active(["scp", "nc"], pin="scp", preference=["nc"]) == "scp"


def test_module_singletons_carry_field_names():
    assert TERM_RESOLVER.field == "term"
    assert TRANSFER_RESOLVER.field == "transfer"

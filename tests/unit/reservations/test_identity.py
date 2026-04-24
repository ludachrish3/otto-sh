"""Unit tests for reservation identity resolution."""

from otto.reservations import resolve_username


def test_as_user_overrides(monkeypatch):
    monkeypatch.setattr("getpass.getuser", lambda: "local-user")
    identity = resolve_username("alice")
    assert identity.username == "alice"
    assert identity.source == "--as-user"


def test_none_falls_through_to_getpass(monkeypatch):
    monkeypatch.setattr("getpass.getuser", lambda: "local-user")
    identity = resolve_username(None)
    assert identity.username == "local-user"
    assert identity.source == "$USER"


def test_empty_string_treated_as_not_supplied(monkeypatch):
    """An empty --as-user value should fall through, not impersonate "" ."""
    monkeypatch.setattr("getpass.getuser", lambda: "local-user")
    identity = resolve_username("")
    assert identity.username == "local-user"
    assert identity.source == "$USER"


def test_resolved_identity_is_frozen():
    identity = resolve_username("alice")
    import dataclasses
    assert dataclasses.is_dataclass(identity)
    import pytest
    with pytest.raises(dataclasses.FrozenInstanceError):
        identity.username = "hacked"  # type: ignore[misc]

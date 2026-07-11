"""Tests for cached --as-user usernames + the best-effort collector."""

import types

import otto.config.completion_cache as cc


def test_usernames_round_trip(tmp_path, monkeypatch):
    cache_file = tmp_path / "cache.json"
    monkeypatch.setattr(cc, "_cache_path", lambda: cache_file)
    monkeypatch.setattr(cc, "compute_fingerprint", lambda repos: "fp")
    repos = [object()]  # truthy; not inspected (fingerprint patched)

    cc.write_cache(repos, [], [], [], usernames=["alice", "bob"])
    result = cc.read_cache(repos)

    assert result is not None
    assert result["usernames"] == ["alice", "bob"]


def test_collect_usernames_from_capable_backend(tmp_path):
    from otto.reservations import register_reservation_backend
    from otto.reservations.registry import RESERVATION_BACKENDS

    class UCBackend:
        def __init__(self, **kwargs):
            pass

        def get_reserved_resources(self, username):
            return set()

        def who_reserved(self, resource):
            return []

        def backend_name(self):
            return "uc"

        def list_usernames(self):
            return ["bob", "alice"]

    register_reservation_backend("uc-test", UCBackend)
    try:
        repo = types.SimpleNamespace(reservation_settings={"backend": "uc-test"}, sut_dir=tmp_path)
        assert cc.collect_reservation_usernames([repo]) == ["alice", "bob"]
    finally:
        RESERVATION_BACKENDS.unregister("uc-test")


def test_collect_usernames_empty_when_capability_absent(tmp_path):
    repo = types.SimpleNamespace(reservation_settings={"backend": "none"}, sut_dir=tmp_path)
    assert cc.collect_reservation_usernames([repo]) == []


def test_collect_usernames_empty_when_no_reservation_settings(tmp_path):
    repo = types.SimpleNamespace(reservation_settings={}, sut_dir=tmp_path)
    assert cc.collect_reservation_usernames([repo]) == []


def test_collect_usernames_swallows_build_errors(tmp_path):
    # An unknown backend name makes build_backend raise ValueError; the collector
    # must swallow it and return [] (best-effort, never block the slow path).
    repo = types.SimpleNamespace(
        reservation_settings={"backend": "no-such-backend"}, sut_dir=tmp_path
    )
    assert cc.collect_reservation_usernames([repo]) == []

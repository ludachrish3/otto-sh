"""Tests for cached --holder usernames + the best-effort collector."""

import otto.config.completion_cache as cc
from tests._fixtures.fake_repo import fake_repo


def test_usernames_round_trip(tmp_path, monkeypatch):
    cache_file = tmp_path / "cache.json"
    monkeypatch.setattr(cc, "_cache_path", lambda: cache_file)
    # The section digests enumerate real repo attributes (sut_dir, init,
    # libs, tests, lab_sources), and `_fingerprint_is_ephemeral` reads
    # `inventory_settings` — absent, that raises, the digest is treated as
    # ephemeral, and the write silently stands down. A real Repo has them all.
    repos = [fake_repo(sut_dir=tmp_path / "sut")]

    cc.write_cache(repos, [], [], usernames=["alice", "bob"])
    result = cc.read_cache(repos)

    assert result is not None
    assert result["usernames"] == ["alice", "bob"]


def test_collect_usernames_from_capable_backend(tmp_path):
    from otto.reservations import register_reservation_backend
    from otto.reservations.registry import RESERVATION_BACKENDS

    class UCBackend:
        def __init__(self, **kwargs):
            pass

        def fetch_reservations(self, username, start=None, end=None):
            return []

        def backend_name(self):
            return "uc"

        def list_usernames(self):
            return ["bob", "alice"]

    register_reservation_backend("uc-test", UCBackend)
    try:
        repo = fake_repo(sut_dir=tmp_path, settings={"reservations": {"backend": "uc-test"}})
        assert cc.collect_reservation_usernames([repo]) == ["alice", "bob"]
    finally:
        RESERVATION_BACKENDS.unregister("uc-test")


def test_collect_usernames_empty_when_capability_absent(tmp_path):
    repo = fake_repo(sut_dir=tmp_path, settings={"reservations": {"backend": "none"}})
    assert cc.collect_reservation_usernames([repo]) == []


def test_collect_usernames_empty_when_no_reservation_settings(tmp_path):
    repo = fake_repo(sut_dir=tmp_path)
    assert cc.collect_reservation_usernames([repo]) == []


def test_collect_usernames_swallows_build_errors(tmp_path):
    # An unknown backend name makes build_backend raise ValueError; the collector
    # must swallow it and return [] (best-effort, never block the slow path).
    repo = fake_repo(sut_dir=tmp_path, settings={"reservations": {"backend": "no-such-backend"}})
    assert cc.collect_reservation_usernames([repo]) == []

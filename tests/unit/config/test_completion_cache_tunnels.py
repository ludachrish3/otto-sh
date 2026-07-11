import time
from types import SimpleNamespace

import otto.config.completion_cache as cc


def _repos(tmp_path):
    # one repo whose fingerprint sources exist under tmp_path/.otto
    (tmp_path / ".otto").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".otto" / "settings.toml").write_text("")
    return [SimpleNamespace(sut_dir=tmp_path, init=[], libs=[], tests=[], labs=[])]


def test_record_and_read_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(cc, "_cache_path", lambda: tmp_path / ".otto" / "completion_cache.json")
    repos = _repos(tmp_path)
    cc.record_tunnel_ids(repos, ["tun-abc123def456-161", "tun-def456abc123-53"])
    assert cc.read_tunnel_ids(repos) == ["tun-abc123def456-161", "tun-def456abc123-53"]


def test_read_expired_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(cc, "_cache_path", lambda: tmp_path / ".otto" / "completion_cache.json")
    repos = _repos(tmp_path)
    cc.record_tunnel_ids(repos, ["tun-abc123def456-161"])
    # Capture the frozen "future" timestamp before patching: cc.time is the
    # same module object as this file's `import time` (modules are process-
    # wide singletons), so a lambda that calls time.time() at *call* time
    # would recurse into its own patched self. Freezing the value up front
    # avoids that self-reference while still jumping the clock past the TTL.
    frozen = time.time() + cc.DYNAMIC_TUNNELS_TTL_SECONDS + 1
    monkeypatch.setattr(cc.time, "time", lambda: frozen)
    assert cc.read_tunnel_ids(repos) is None

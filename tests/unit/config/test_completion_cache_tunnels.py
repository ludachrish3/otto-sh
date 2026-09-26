import os
import time
from collections import Counter
from types import SimpleNamespace

import otto.config.completion_cache as cc
from otto.config.repo import Repo
from tests._fixtures.labdata import write_lab_json
from tests._fixtures.sutrepo import make_sut_repo, touch_settings


def _repos(tmp_path):
    # one repo whose fingerprint sources exist under tmp_path/.otto
    touch_settings(tmp_path)
    return [
        SimpleNamespace(
            sut_dir=tmp_path,
            init=[],
            libs=[],
            tests=[],
            labs=[],
            lab_sources=[],
            inventory_settings={},
            creds_settings={},
        )
    ]


def _repo_with_corpus_and_lab(tmp_path):
    """A real ``Repo`` with nested test files AND a json ``[[lab.sources]]``.

    Built (not a ``SimpleNamespace``) so ``lab_sources`` is compiled the same
    way a real repo's is, and ``tests`` resolves through settings — the shape
    both the corpus-walk-avoidance test and the lab-vs-test invalidation test
    need in one repo.
    """
    root = tmp_path / "sut"
    make_sut_repo(
        root,
        tests=["tests"],
        extra='[[lab.sources]]\nbackend = "json"\npaths = ["lab"]\n',
        files={
            "tests/test_top.py": "def test_x():\n    pass\n",
            "tests/sub/test_nested.py": "def test_y():\n    pass\n",
        },
    )
    write_lab_json(root / "lab" / "lab.json", [], declare_labs=True)
    return Repo(sut_dir=root)


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


def test_record_and_read_never_hash_a_test_source(tmp_path, monkeypatch):
    """Tunnel ids depend on the lab and inventory, never on the test corpus.

    Keying by ``compute_fingerprint`` (the old behavior) would hash every
    test source under ``repo.tests`` on every ``otto tunnel list``/``remove``
    — exactly the corpus-proportional cost ordinary commands must not pay
    (spec 2026-09-25-dispatch-startup-cost-design.md §4.2). Counting
    ``hash_file`` calls by path and asserting none falls under the repo's
    ``tests`` dir proves the new tunnel-scope digest never walks it, whether
    or not a rebuild ever ran.
    """
    monkeypatch.setattr(cc, "_cache_path", lambda: tmp_path / ".otto" / "completion_cache.json")
    repo = _repo_with_corpus_and_lab(tmp_path)

    counts: Counter = Counter()
    real_hash_file = cc.hash_file

    def counting(h, path):
        counts[path] += 1
        real_hash_file(h, path)

    monkeypatch.setattr(cc, "hash_file", counting)

    cc.record_tunnel_ids([repo], ["tun-abc123def456-161"])
    assert cc.read_tunnel_ids([repo]) == ["tun-abc123def456-161"]

    tests_dir = repo.sut_dir / "tests"
    hashed_test_paths = [p for p in counts if tests_dir in p.parents]
    assert not hashed_test_paths, f"tunnel-id keying hashed test sources: {hashed_test_paths}"


def test_editing_a_nested_test_file_does_not_invalidate_cached_tunnel_ids(tmp_path, monkeypatch):
    """A test edit must not move the tunnel-scope digest — only the lab and inventory do.

    Migrated from the old ``compute_fingerprint``-keyed premise: tunnel ids
    never depended on tests conceptually, but keying by the SAME digest as
    the corpus-aware namespaces meant a test edit invalidated them anyway.
    The new tunnel-scope digest (settings + lab files + inventory) must not
    move on this edit, while :func:`test_editing_a_lab_file_invalidates_cached_tunnel_ids`
    shows a lab edit still does.
    """
    monkeypatch.setattr(cc, "_cache_path", lambda: tmp_path / ".otto" / "completion_cache.json")
    repo = _repo_with_corpus_and_lab(tmp_path)

    cc.record_tunnel_ids([repo], ["tun-abc123def456-161"])
    nested = repo.sut_dir / "tests" / "sub" / "test_nested.py"
    nested.write_text("def test_y():\n    pass\n\ndef test_z():\n    pass\n")

    assert cc.read_tunnel_ids([repo]) == ["tun-abc123def456-161"]


def test_editing_a_lab_file_invalidates_cached_tunnel_ids(tmp_path, monkeypatch):
    """The counterpart to the nested-test-edit test: lab files DO scope tunnel ids."""
    monkeypatch.setattr(cc, "_cache_path", lambda: tmp_path / ".otto" / "completion_cache.json")
    repo = _repo_with_corpus_and_lab(tmp_path)

    cc.record_tunnel_ids([repo], ["tun-abc123def456-161"])
    lab_json = repo.sut_dir / "lab" / "lab.json"
    write_lab_json(lab_json, [], declare_labs=True)
    # write_lab_json re-serializes the same (empty) document, so the byte
    # length is identical; force the mtime forward so hash_file's stat triple
    # actually moves regardless of filesystem timestamp granularity.
    st = lab_json.stat()
    os.utime(lab_json, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))

    assert cc.read_tunnel_ids([repo]) is None

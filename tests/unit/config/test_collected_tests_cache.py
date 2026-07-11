"""Collected (pytest-accurate) test-name cache behind ``otto test --tests``.

Covers the pure, subprocess-free layer: name shaping, the reserved-namespace
read/write round-trip, freshness/failure/cooldown handling, dump framing, and
the guarantee that warming the collected set never clobbers the main cache
entries.
"""

import json
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from otto.config import completion_cache as cc


def _fake_repo(tmp_path: Path):
    """A repo stand-in with just the attributes ``compute_fingerprint`` reads."""
    repo = MagicMock()
    repo.sut_dir = tmp_path / "sut"
    repo.sut_dir.mkdir(exist_ok=True)
    (repo.sut_dir / ".otto").mkdir(exist_ok=True)
    (repo.sut_dir / ".otto" / "settings.toml").write_text("")
    repo.init = []
    repo.libs = []
    repo.tests = []
    repo.labs = []
    return repo


# --- name shaping ----------------------------------------------------------


def test_names_from_items_collapses_parametrizations():
    items = [
        SimpleNamespace(name="test_x[case-a]", cls_name=None),
        SimpleNamespace(name="test_x[case-b]", cls_name=None),
        SimpleNamespace(name="test_method[1]", cls_name="TestThing"),
    ]
    # Parametrizations collapse to the base; class methods emit bare + scoped.
    assert cc._test_names_from_items(items) == [
        "TestThing::test_method",
        "test_method",
        "test_x",
    ]


# --- read / write round-trip ----------------------------------------------


def test_collected_round_trip(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OTTO_XDIR", str(tmp_path))
    repos = [_fake_repo(tmp_path)]

    assert cc.read_collected_tests(repos) is None  # cold
    cc._record_collected_tests(repos, ["test_a", "TestX::test_a"])
    assert cc.read_collected_tests(repos) == ["test_a", "TestX::test_a"]


def test_collected_empty_list_is_a_valid_fresh_result(tmp_path: Path, monkeypatch):
    """An empty collection is a hit (``[]``), distinct from a miss (``None``)."""
    monkeypatch.setenv("OTTO_XDIR", str(tmp_path))
    repos = [_fake_repo(tmp_path)]
    cc._record_collected_tests(repos, [])
    assert cc.read_collected_tests(repos) == []


def test_failed_attempt_reads_as_miss(tmp_path: Path, monkeypatch):
    """A recorded failure (``names=None``) is a miss for the reader."""
    monkeypatch.setenv("OTTO_XDIR", str(tmp_path))
    repos = [_fake_repo(tmp_path)]
    cc._record_collected_tests(repos, None)
    assert cc.read_collected_tests(repos) is None


def test_collected_ttl_expiry(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OTTO_XDIR", str(tmp_path))
    repos = [_fake_repo(tmp_path)]
    cc._record_collected_tests(repos, ["test_a"])

    cache = json.loads(cc._cache_path().read_text())
    fp = cc.compute_fingerprint(repos)
    cache[cc.COLLECTED_TESTS_KEY][fp]["generated_at"] = int(time.time()) - cc.CACHE_TTL_SECONDS - 1
    cc._cache_path().write_text(json.dumps(cache))

    assert cc.read_collected_tests(repos) is None


def test_collected_schema_mismatch(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OTTO_XDIR", str(tmp_path))
    repos = [_fake_repo(tmp_path)]
    cc._record_collected_tests(repos, ["test_a"])

    cache = json.loads(cc._cache_path().read_text())
    fp = cc.compute_fingerprint(repos)
    cache[cc.COLLECTED_TESTS_KEY][fp]["schema_version"] = cc.COLLECTED_SCHEMA_VERSION + 1
    cc._cache_path().write_text(json.dumps(cache))

    assert cc.read_collected_tests(repos) is None


def test_collected_disabled_without_xdir(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("OTTO_XDIR", raising=False)
    repos = [_fake_repo(tmp_path)]
    assert cc.read_collected_tests(repos) is None
    cc._record_collected_tests(repos, ["test_a"])  # no-op, must not raise


def test_warming_does_not_clobber_main_cache(tmp_path: Path, monkeypatch):
    """Recording collected names must leave the main fingerprint entry intact."""
    monkeypatch.setenv("OTTO_XDIR", str(tmp_path))
    repos = [_fake_repo(tmp_path)]

    cc.write_cache(
        repos,
        instructions=[{"name": "install", "options": []}],
        suites=[{"name": "TestX", "options": []}],
        hosts=["h1"],
        tests=["test_static"],
    )
    cc._record_collected_tests(repos, ["test_static", "test_dynamic"])

    main = cc.read_cache(repos)
    assert main is not None
    assert main["instructions"] == [{"name": "install", "options": []}]
    assert main["tests"] == ["test_static"]  # static floor untouched
    assert cc.read_collected_tests(repos) == ["test_static", "test_dynamic"]


# --- dump framing ----------------------------------------------------------


def test_parse_dumped_names_extracts_payload():
    lines = ["noise", cc._DUMP_BEGIN, "test_a", "", "test_b", cc._DUMP_END, "trailing"]
    assert cc._parse_dumped_names("\n".join(lines)) == ["test_a", "test_b"]


def test_parse_dumped_names_rejects_missing_or_reversed_markers():
    assert cc._parse_dumped_names("no markers here") is None
    assert cc._parse_dumped_names(f"{cc._DUMP_END}\nx\n{cc._DUMP_BEGIN}") is None


# --- warm orchestration (subprocess mocked) -------------------------------


def test_maybe_warm_records_and_returns(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OTTO_XDIR", str(tmp_path))
    repos = [_fake_repo(tmp_path)]
    monkeypatch.setattr(cc, "_run_collect_subprocess", lambda: ["test_dynamic"])

    assert cc.maybe_warm_collected_tests(repos) == ["test_dynamic"]
    assert cc.read_collected_tests(repos) == ["test_dynamic"]
    # Lock file is cleaned up.
    assert not (cc._cache_path().parent / cc.COLLECT_LOCK_FILENAME).exists()


def test_maybe_warm_respects_cooldown_after_failure(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OTTO_XDIR", str(tmp_path))
    repos = [_fake_repo(tmp_path)]
    cc._record_collected_tests(repos, None)  # a recent failed attempt

    def _must_not_run():
        raise AssertionError("subprocess must not run during cooldown")

    monkeypatch.setattr(cc, "_run_collect_subprocess", _must_not_run)
    assert cc.maybe_warm_collected_tests(repos) is None


def test_maybe_warm_stamps_cooldown_on_failure(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OTTO_XDIR", str(tmp_path))
    repos = [_fake_repo(tmp_path)]
    monkeypatch.setattr(cc, "_run_collect_subprocess", lambda: None)  # collection failed

    assert cc.maybe_warm_collected_tests(repos) is None
    # A failed attempt is recorded so the next TAB honors the cooldown.
    entry = cc._collected_cache_entry(repos)
    assert entry is not None
    assert entry["names"] is None


def test_record_from_items_round_trip(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OTTO_XDIR", str(tmp_path))
    repos = [_fake_repo(tmp_path)]
    items = [
        SimpleNamespace(name="test_a[x]", cls_name=None),
        SimpleNamespace(name="test_m", cls_name="TestC"),
    ]
    cc.record_collected_tests_from_items(repos, items)
    assert cc.read_collected_tests(repos) == ["TestC::test_m", "test_a", "test_m"]

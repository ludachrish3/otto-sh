"""The warm completion readers never prepare a lab source or glob its files.

The cache writer runs after init: from each source's prepared facts it
computes the lab key paths (the files, then the directories they were found
in) and stores them, with the entry's TTL class, beside the entry. The warm
readers (``read_sections``, ``read_tunnel_ids``, ``inspect_section``)
validate against those stored values: they stat what was stored and never
call ``prepared_lab_sources``, a config model or ``expand_lab_paths``. A new
file matching a glob still invalidates, because it moves its directory's
mtime, and that directory is one of the stored key paths.
"""

import json
import os
import time
from pathlib import Path

import pytest

import otto.config.completion_cache as cc
from otto.config.cache_sections import write_section
from otto.config.repo import Repo
from otto.host.os_profile import ProfileContext
from tests._fixtures.labdata import write_lab_json
from tests._fixtures.sutrepo import make_sut_repo

_PAYLOAD = {"instructions": [], "hosts": ["dut"]}


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    """A private OTTO_HOME, so the cache file is this test's own."""
    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))


def _glob_repo(tmp_path: Path) -> Repo:
    """A real Repo whose one json source reads ``labs/*.json``."""
    root = tmp_path / "sut"
    make_sut_repo(root, extra='[[lab.sources]]\nbackend = "json"\npaths = ["labs/*.json"]\n')
    write_lab_json(root / "labs" / "a.json", [], declare_labs=True)
    return Repo(sut_dir=root)


def _poison(monkeypatch) -> None:
    """Make every route to preparing a source or globbing its files raise."""

    def boom(*args, **kwargs):
        raise AssertionError("a warm reader prepared a lab source or globbed its files")

    from otto.labs import json_repository, sources

    monkeypatch.setattr(json_repository, "expand_lab_paths", boom)
    monkeypatch.setattr(json_repository.JsonLabSourceConfig, "model_validate", classmethod(boom))
    monkeypatch.setattr(sources, "prepared_lab_sources", boom)


def _new_match(repo: Repo) -> None:
    """Add a file the glob matches, and make sure its directory's mtime moves."""
    labs = repo.sut_dir / "labs"
    before = labs.stat().st_mtime_ns
    write_lab_json(labs / "b.json", [], declare_labs=False)
    os.utime(labs, ns=(before + 2_000_000_000, before + 2_000_000_000))


def _cache_data() -> dict:
    path = cc._cache_path()
    assert path is not None
    return json.loads(path.read_text())


def _rewrite(data: dict) -> None:
    path = cc._cache_path()
    assert path is not None
    path.write_text(json.dumps(data))


def test_the_warm_reader_never_prepares_or_globs(tmp_path, monkeypatch):
    repo = _glob_repo(tmp_path)
    write_section([repo], "names", _PAYLOAD)
    _poison(monkeypatch)
    payloads = cc.read_sections([repo], ["names"])
    assert payloads == {"names": _PAYLOAD}


def test_the_writer_stores_the_lab_key_paths_and_the_ttl_class(tmp_path):
    repo = _glob_repo(tmp_path)
    write_section([repo], "names", _PAYLOAD)
    entry = _cache_data()["sections"]["names"]
    labs = repo.sut_dir / "labs"
    assert entry["lab_key_paths"] == [str(labs / "a.json"), str(labs)]
    assert entry["ttl_seconds"] == cc.CACHE_TTL_SECONDS


def test_a_new_glob_match_invalidates(tmp_path):
    repo = _glob_repo(tmp_path)
    write_section([repo], "names", _PAYLOAD)
    assert cc.read_sections([repo], ["names"]) is not None
    _new_match(repo)
    assert cc.read_sections([repo], ["names"]) is None


def test_a_not_file_backed_source_stores_its_ttl_class(tmp_path, monkeypatch):
    from otto.labs.registry import register_lab_repository
    from otto.models.base import OttoModel

    class NoFilesConfig(OttoModel):
        """A custom source's config: no prepared facts, so nothing to fingerprint."""

    def no_files(c):
        raise AssertionError("the cache never builds a source to judge it")

    register_lab_repository("no-files", config=NoFilesConfig, factory=no_files)
    root = tmp_path / "sut"
    make_sut_repo(root, extra='[[lab.sources]]\nbackend = "no-files"\n')
    repo = Repo(sut_dir=root)
    write_section([repo], "names", _PAYLOAD)
    entry = _cache_data()["sections"]["names"]
    assert entry["ttl_seconds"] == cc.UNFINGERPRINTED_CACHE_TTL_SECONDS
    assert entry["lab_key_paths"] == []

    _poison(monkeypatch)
    monkeypatch.setattr(NoFilesConfig, "model_validate", classmethod(lambda *a, **k: 1 / 0))
    assert cc.read_sections([repo], ["names"]) == {"names": _PAYLOAD}
    # Older than the short TTL, younger than the long one: the stored class decides.
    stale = time.time() + cc.UNFINGERPRINTED_CACHE_TTL_SECONDS + 1
    monkeypatch.setattr(cc.time, "time", lambda: stale)
    assert cc.read_sections([repo], ["names"]) is None


def test_cache_info_describes_each_source_by_what_it_reads(tmp_path):
    from otto.config.cache_sections import describe_lab_sources
    from otto.labs.registry import register_lab_repository
    from otto.models.base import OttoModel

    class NoFilesConfig(OttoModel):
        """A custom source's config: no prepared facts."""

    register_lab_repository("no-files", config=NoFilesConfig, factory=lambda c: None)
    root = tmp_path / "sut"
    make_sut_repo(
        root,
        name="r",
        extra='[[lab.sources]]\nbackend = "json"\npaths = ["labs/*.json"]\n'
        '[[lab.sources]]\nbackend = "no-files"\n'
        '[[lab.sources]]\nbackend = "later"\n',
    )
    write_lab_json(root / "labs" / "a.json", [], declare_labs=True)
    assert describe_lab_sources(Repo(sut_dir=root), profiles=ProfileContext.empty()) == [
        ("r/json#1", str(root / "labs" / "a.json")),
        ("r/no-files#2", "not file-backed (no-files)"),
        ("r/later#3", "unprepared — backend 'later' is not registered before init"),
    ]


def test_cache_info_names_a_source_that_does_not_prepare(tmp_path):
    from otto.config.cache_sections import describe_lab_sources

    root = tmp_path / "sut"
    make_sut_repo(root, name="r", extra='[[lab.sources]]\nbackend = "json"\n')
    ((label, text),) = describe_lab_sources(Repo(sut_dir=root), profiles=ProfileContext.empty())
    assert label == "r/json#1"
    assert text.startswith("cannot prepare — ")
    assert "parse" in text


def test_the_tunnel_id_reader_never_prepares_or_globs(tmp_path, monkeypatch):
    repo = _glob_repo(tmp_path)
    cc.record_tunnel_ids([repo], ["tun-abc123def456-22"])
    _poison(monkeypatch)
    assert cc.read_tunnel_ids([repo]) == ["tun-abc123def456-22"]


def test_cache_info_never_prepares_or_globs(tmp_path, monkeypatch):
    repo = _glob_repo(tmp_path)
    write_section([repo], "names", _PAYLOAD)
    _poison(monkeypatch)
    assert cc.inspect_section([repo], "names").state == "fresh"


def test_a_new_glob_match_invalidates_tunnel_ids(tmp_path):
    repo = _glob_repo(tmp_path)
    cc.record_tunnel_ids([repo], ["tun-abc123def456-22"])
    assert cc.read_tunnel_ids([repo]) == ["tun-abc123def456-22"]
    _new_match(repo)
    assert cc.read_tunnel_ids([repo]) is None


def test_registering_an_unknown_source_s_backend_moves_the_tunnel_key(tmp_path):
    """A source on an unregistered backend keys as ``unknown:<label>``; registering it misses."""
    from otto.labs.registry import register_lab_repository
    from otto.models.base import OttoModel

    root = tmp_path / "sut"
    make_sut_repo(root, name="r", extra='[[lab.sources]]\nbackend = "later"\n')
    repo = Repo(sut_dir=root)
    cc.record_tunnel_ids([repo], ["tun-abc123def456-22"])
    assert cc.read_tunnel_ids([repo]) == ["tun-abc123def456-22"]

    class LaterConfig(OttoModel):
        """A backend registered after the ids were recorded."""

    register_lab_repository("later", config=LaterConfig, factory=lambda c: None)
    assert cc.read_tunnel_ids([repo]) is None


@pytest.mark.parametrize("dropped", ["lab_key_paths", "ttl_seconds"])
def test_an_entry_without_stored_lab_keys_is_a_miss(tmp_path, dropped):
    """A cache written by the previous otto stores no lab keys: a miss, never a traceback."""
    repo = _glob_repo(tmp_path)
    write_section([repo], "names", _PAYLOAD)
    data = _cache_data()
    del data["sections"]["names"][dropped]
    _rewrite(data)
    assert cc.read_sections([repo], ["names"]) is None
    assert cc.inspect_section([repo], "names").state != "fresh"


@pytest.mark.parametrize(
    ("field", "value"), [("lab_key_paths", "labs"), ("lab_key_paths", [1]), ("ttl_seconds", "1")]
)
def test_an_entry_with_ill_typed_lab_keys_is_a_miss(tmp_path, field, value):
    repo = _glob_repo(tmp_path)
    write_section([repo], "names", _PAYLOAD)
    data = _cache_data()
    data["sections"]["names"][field] = value
    _rewrite(data)
    assert cc.read_sections([repo], ["names"]) is None


def test_a_cache_at_the_previous_schema_is_a_miss(tmp_path):
    repo = _glob_repo(tmp_path)
    write_section([repo], "names", _PAYLOAD)
    data = _cache_data()
    data["schema"] = cc.SCHEMA_VERSION - 1
    _rewrite(data)
    assert cc.read_sections([repo], ["names"]) is None


@pytest.mark.parametrize("dropped", ["lab_key_paths", "lab_digest"])
def test_a_tunnel_entry_without_stored_lab_keys_is_a_miss(tmp_path, dropped):
    repo = _glob_repo(tmp_path)
    cc.record_tunnel_ids([repo], ["tun-abc123def456-22"])
    data = _cache_data()
    (entry,) = data[cc.DYNAMIC_TUNNELS_KEY].values()
    del entry[dropped]
    _rewrite(data)
    assert cc.read_tunnel_ids([repo]) is None


def test_a_tunnel_entry_at_the_previous_schema_is_a_miss(tmp_path):
    repo = _glob_repo(tmp_path)
    cc.record_tunnel_ids([repo], ["tun-abc123def456-22"])
    data = _cache_data()
    (entry,) = data[cc.DYNAMIC_TUNNELS_KEY].values()
    entry["schema_version"] = cc.DYNAMIC_TUNNELS_SCHEMA_VERSION - 1
    _rewrite(data)
    assert cc.read_tunnel_ids([repo]) is None

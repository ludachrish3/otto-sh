"""Completion's outlet: what the best-effort enumeration dropped, and why (otto.labs.drops).

A TAB never warns — a warning printed into a completing shell corrupts the
candidate list — so every skip along the host-enumeration chain was silent
until 2026-09-03, when four distinct "my host does not complete" causes were
reproduced and none of them could be told apart from the shell. These pins
cover each recording site, the ``names`` payload the records travel in, the
section inspection ``otto cache info`` reads, and the inventory line it
prints.

``OTTO_HOME`` is relocated inside every test for the reason
``test_completion_cache_inventory.py`` gives: ``build_inventory`` falls back
to ``~/.otto/settings.toml``.
"""

import json
import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import otto.config.completion_cache as cc
from otto.labs.drops import HostDrop, collecting_drops, record_drop
from tests._fixtures.labdata import json_lab_sources, write_lab_json
from tests._fixtures.sutrepo import touch_settings

_CREDS = [{"login": "u", "password": "p"}]
_RECORD = {"dut-1": {"ip": "10.0.0.1"}}


def _repo(tmp_path: Path, inventory_settings: dict, *, hosts: list[dict], name: str = "sut"):
    """A Repo stand-in: one json lab source over ``<name>/lab``, the given ``[inventory]``."""
    sut = tmp_path / name
    lab = sut / "lab"
    lab.mkdir(parents=True, exist_ok=True)
    touch_settings(sut)
    write_lab_json(lab / "lab.json", hosts, declare_labs=True)
    return SimpleNamespace(
        name=name,
        sut_dir=sut,
        init=[],
        libs=[],
        tests=[],
        lab_sources=json_lab_sources(sut, [lab]),
        inventory_settings=dict(inventory_settings),
    )


def _json_inventory(tmp_path: Path, records: dict, *, name: str = "sut") -> dict:
    (tmp_path / name).mkdir(parents=True, exist_ok=True)
    (tmp_path / name / "inventory.json").write_text(json.dumps(records))
    return {"backend": "json", "path": "inventory.json", "supplies": ["ip"]}


def _ref(key: str, element: str) -> dict:
    return {"inventory": key, "element": element, "creds": _CREDS}


# --- the sink ---------------------------------------------------------------


def test_recording_without_a_sink_is_a_no_op_and_a_sink_collects_only_its_own_thread():
    record_drop("nowhere", "nobody is listening")  # must not raise, must not leak

    from_other_thread: list[HostDrop] = []

    def _elsewhere() -> None:
        with collecting_drops() as theirs:
            record_drop("theirs", "recorded on the worker")
        from_other_thread.extend(theirs)

    with collecting_drops() as mine:
        record_drop("mine", "recorded here")
        worker = threading.Thread(target=_elsewhere)
        worker.start()
        worker.join()

    assert mine == [HostDrop(where="mine", reason="recorded here")]
    assert from_other_thread == [HostDrop(where="theirs", reason="recorded on the worker")]


# --- the recording sites ------------------------------------------------------


def test_an_entry_whose_reference_cannot_resolve_is_dropped_by_place(tmp_path, monkeypatch):
    """The per-entry skip in the json backend names the file, element and index.

    The reproduced case: a lab entry references a key the inventory does not
    have. ``otto --list-hosts`` fails loud on it; completion skipped it
    silently, and nothing said which entry.
    """
    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
    table = _json_inventory(tmp_path, _RECORD)
    repo = _repo(tmp_path, table, hosts=[_ref("dut-1", "dut"), _ref("ghost", "phantom")])

    ids = cc.collect_host_ids([repo])
    drops = cc.collect_host_drops([repo])

    assert "dut" in ids
    assert "phantom" not in ids
    assert len(drops) == 1, drops
    (drop,) = drops
    assert drop["repo"] == "sut"
    assert drop["where"] == f"{repo.sut_dir / 'lab' / 'lab.json'}: element 'phantom' hosts[0]"
    assert "ghost" in drop["reason"]


def test_a_broken_inventory_declaration_is_the_inventory_line_not_a_drop(tmp_path, monkeypatch):
    """Nothing is enumerated, and — the digest being ephemeral — nothing is written
    that could carry a drop; the case is ``describe_inventory``'s (the ``inventory``
    line), with the blocker the standing line is worded on."""
    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
    broken = {"backend": "json"}  # no path
    first = _repo(tmp_path, broken, hosts=[_ref("dut-1", "dut")], name="first")
    second = _repo(tmp_path, {}, hosts=[_ref("dut-1", "dut")], name="second")

    assert cc.collect_host_drops([first, second]) == []
    described = cc.describe_inventory([first, second])
    assert described.blocker == "is broken"
    assert "requires a 'path'" in described.text


def test_a_malformed_lab_file_is_dropped_by_name(tmp_path, monkeypatch):
    """The best-effort document load names the file it could not parse.

    The broken file is a LISTED path: a directory entry expands to its
    ``lab.json`` only, so a stray file beside it is never read at all — the
    "supplement not in ``paths``" case is a file that does not appear in the
    ``lab files`` line, not a drop.
    """
    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
    repo = _repo(tmp_path, {}, hosts=[{"ip": "10.0.0.9", "element": "plain", "creds": _CREDS}])
    broken = repo.sut_dir / "broken.json"
    broken.write_text("{not json")
    repo.lab_sources = json_lab_sources(repo.sut_dir, [repo.sut_dir / "lab", broken])

    ids = cc.collect_host_ids([repo])
    drops = cc.collect_host_drops([repo])

    assert "plain" in ids  # the good file still counts
    assert [d["where"] for d in drops] == [str(broken)], drops
    assert drops[0]["reason"]


def test_a_stalled_host_source_is_dropped_with_the_deadline(tmp_path, monkeypatch):
    """The deadline wrapper's give-up is a drop too, not just a warning."""
    monkeypatch.setenv(cc.HOST_SUMMARY_DEADLINE_ENV_VAR, "0.05")
    repo = SimpleNamespace(sut_dir=tmp_path / "slow")

    def _stalls(abandoned: threading.Event) -> cc.RepoEnumeration:
        abandoned.wait(5)  # returns as soon as the wrapper gives up on us
        return cc.RepoEnumeration()

    result = cc._bounded(_stalls, repo)

    assert result.summaries == []
    assert len(result.drops) == 1
    assert result.drops[0].where == str(repo.sut_dir)
    assert "did not answer within" in result.drops[0].reason
    assert cc.HOST_SUMMARY_DEADLINE_ENV_VAR in result.drops[0].reason


def test_an_enumeration_that_raises_is_dropped_for_the_repo(tmp_path, monkeypatch):
    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
    repo = _repo(tmp_path, {}, hosts=[])

    def _explodes(_repos):
        raise RuntimeError("backend on fire")

    monkeypatch.setattr("otto.labs.build_lab_sources", _explodes)

    drops = cc.collect_host_drops([repo])

    assert [d["where"] for d in drops] == [str(repo.sut_dir)]
    assert "backend on fire" in drops[0]["reason"]


# --- the payload ------------------------------------------------------------------


def test_host_drops_ride_the_names_payload(tmp_path, monkeypatch):
    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
    repo = _repo(tmp_path, {}, hosts=[])
    recorded = [{"repo": "sut", "where": "somewhere", "reason": "some reason"}]

    cc.write_cache([repo], instructions=[], suites=[], hosts=["dut"], host_drops=recorded)

    assert cc.read_cache([repo])["host_drops"] == recorded


def test_a_names_payload_missing_host_drops_still_reads_as_an_empty_list(tmp_path, monkeypatch):
    """Older payload shape without the key: empty, not a miss and not a crash."""
    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
    repo = _repo(tmp_path, {}, hosts=[])
    cc.write_cache([repo], instructions=[], suites=[], hosts=["dut"])
    path = cc._cache_path()
    assert path is not None
    data = json.loads(path.read_text())
    del data["sections"]["names"]["payload"]["host_drops"]
    path.write_text(json.dumps(data))

    assert cc.read_cache([repo])["host_drops"] == []


# --- inspect_section: what `otto cache info` reads ---------------------------------


def test_inspect_reports_fresh_with_the_payload(tmp_path, monkeypatch):
    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
    repo = _repo(tmp_path, {}, hosts=[])
    cc.write_cache([repo], instructions=[], suites=[], hosts=["dut"])

    status = cc.inspect_section([repo], "names")

    assert status.state == "fresh"
    assert status.payload is not None
    assert status.payload["hosts"] == ["dut"]
    assert status.generated_at is not None
    assert time.time() - status.generated_at < 60


def test_inspect_reports_stale_after_a_key_file_moves_and_keeps_the_payload(tmp_path, monkeypatch):
    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
    repo = _repo(tmp_path, {}, hosts=[])
    cc.write_cache([repo], instructions=[], suites=[], hosts=["dut"])
    lab_file = repo.sut_dir / "lab" / "lab.json"
    later = lab_file.stat().st_mtime + 10
    os.utime(lab_file, (later, later))

    status = cc.inspect_section([repo], "names")

    assert status.state == "stale"
    assert status.payload is not None
    assert status.payload["hosts"] == ["dut"]


def test_inspect_reports_expired_tainted_outdated_unreadable_and_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
    repo = _repo(tmp_path, {}, hosts=[])
    assert cc.inspect_section([repo], "names").state == "missing"

    cc.write_cache([repo], instructions=[], suites=[], hosts=["dut"], tainted=True)
    assert cc.inspect_section([repo], "names").state == "tainted"

    cc.write_cache([repo], instructions=[], suites=[], hosts=["dut"])
    path = cc._cache_path()
    assert path is not None
    data = json.loads(path.read_text())
    data["sections"]["names"]["generated_at"] = time.time() - 2 * cc.CACHE_TTL_SECONDS
    path.write_text(json.dumps(data))
    assert cc.inspect_section([repo], "names").state == "expired"

    data["sections"]["names"]["generated_at"] = time.time()
    data["schema"] = cc.SCHEMA_VERSION - 1
    path.write_text(json.dumps(data))
    assert cc.inspect_section([repo], "names").state == "outdated"

    path.write_text("{corrupt")
    assert cc.inspect_section([repo], "names").state == "unreadable"


# --- describe_inventory --------------------------------------------------------------


def test_describe_inventory_names_each_case(tmp_path, monkeypatch):
    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
    none = _repo(tmp_path, {}, hosts=[], name="none")
    assert cc.describe_inventory([none]) == cc.InventoryDescription(text="none declared")

    broken = _repo(tmp_path, {"backend": "json"}, hosts=[], name="broken")
    described = cc.describe_inventory([broken])
    assert described.text.startswith("BROKEN")
    assert "requires a 'path'" in described.text
    assert "no host completes" in described.text
    assert described.blocker == "is broken"

    table = _json_inventory(tmp_path, _RECORD, name="declared")
    declared = _repo(tmp_path, table, hosts=[], name="declared")
    described = cc.describe_inventory([declared])
    assert "inventory.json" in described.text
    assert "freshness" not in described.text
    assert described.blocker is None


def test_describe_inventory_says_when_completion_can_never_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
    repo = _repo(tmp_path, {}, hosts=[])
    unfingerprinted = SimpleNamespace(label="probe:live", fingerprint=lambda: None)
    monkeypatch.setattr("otto.inventory.build_inventory", lambda _repos: unfingerprinted)

    described = cc.describe_inventory([repo])

    assert described.text.startswith("probe:live")
    assert "never caches" in described.text
    assert described.blocker == "cannot report freshness"

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

import pytest

import otto.config.completion_cache as cc
from otto.labs.drops import HostDrop, collecting_drops, record_drop
from tests._fixtures.fake_project import fake_project
from tests._fixtures.labdata import json_lab_sources, write_lab_json
from tests._fixtures.project_scaffold import touch_settings

_CREDS = [{"login": "u", "password": "p"}]
_RECORD = {"dut-1": {"ip": "10.0.0.1"}}


def _project(tmp_path: Path, inventory_settings: dict, *, hosts: list[dict], name: str = "sut"):
    """A Project stand-in: one json lab source over ``<name>/lab``, the given ``[inventory]``."""
    sut = tmp_path / name
    lab = sut / "lab"
    lab.mkdir(parents=True, exist_ok=True)
    touch_settings(sut)
    write_lab_json(lab / "lab.json", hosts, declare_labs=True)
    return fake_project(
        name,
        root=sut,
        lab_sources=json_lab_sources(sut, [lab]),
        settings={"inventory": dict(inventory_settings)},
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
    project = _project(tmp_path, table, hosts=[_ref("dut-1", "dut"), _ref("ghost", "phantom")])

    ids = cc.collect_host_ids([project])
    drops = cc.collect_host_drops([project])

    assert "dut" in ids
    assert "phantom" not in ids
    assert len(drops) == 1, drops
    (drop,) = drops
    assert drop["project"] == "sut"
    assert drop["where"] == f"{project.root / 'lab' / 'lab.json'}: element 'phantom' hosts[0]"
    assert "ghost" in drop["reason"]


def test_a_broken_inventory_declaration_is_the_inventory_line_not_a_drop(tmp_path, monkeypatch):
    """Nothing is enumerated, and — the digest being ephemeral — nothing is written
    that could carry a drop; the case is ``describe_inventory``'s (the ``inventory``
    line), with the blocker the standing line is worded on."""
    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
    broken = {"backend": "json"}  # no path
    first = _project(tmp_path, broken, hosts=[_ref("dut-1", "dut")], name="first")
    second = _project(tmp_path, {}, hosts=[_ref("dut-1", "dut")], name="second")

    assert cc.collect_host_drops([first, second]) == []
    described = cc.describe_inventory([first, second])
    assert described.blocker == "is broken"
    assert "parse failed: path: Field required" in described.text


def test_a_malformed_lab_file_is_dropped_by_name(tmp_path, monkeypatch):
    """The best-effort document load names the file it could not parse.

    The broken file is a LISTED path: a directory entry expands to its
    ``lab.json`` only, so a stray file beside it is never read at all — the
    "supplement not in ``paths``" case is a file that does not appear in the
    ``lab files`` line, not a drop.
    """
    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
    project = _project(
        tmp_path, {}, hosts=[{"ip": "10.0.0.9", "element": "plain", "creds": _CREDS}]
    )
    broken = project.root / "broken.json"
    broken.write_text("{not json")
    project.lab_sources = json_lab_sources(project.root, [project.root / "lab", broken])

    ids = cc.collect_host_ids([project])
    drops = cc.collect_host_drops([project])

    assert "plain" in ids  # the good file still counts
    assert [d["where"] for d in drops] == [str(broken)], drops
    assert drops[0]["reason"]


def test_a_stalled_host_source_is_dropped_with_the_deadline(tmp_path, monkeypatch):
    """The deadline wrapper's give-up is a drop too, not just a warning."""
    monkeypatch.setenv(cc.HOST_SUMMARY_DEADLINE_ENV_VAR, "0.05")
    project = fake_project(root=tmp_path / "slow")

    def _stalls(abandoned: threading.Event) -> cc.ProjectEnumeration:
        abandoned.wait(5)  # returns as soon as the wrapper gives up on us
        return cc.ProjectEnumeration()

    result = cc._bounded(_stalls, project)

    assert result.summaries == []
    assert len(result.drops) == 1
    assert result.drops[0].where == str(project.root)
    assert "did not answer within" in result.drops[0].reason
    assert cc.HOST_SUMMARY_DEADLINE_ENV_VAR in result.drops[0].reason


def test_an_enumeration_that_raises_is_dropped_for_the_project(tmp_path, monkeypatch):
    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
    project = _project(tmp_path, {}, hosts=[])

    def _explodes(_projects, *, profiles):
        raise RuntimeError("backend on fire")

    monkeypatch.setattr("otto.labs.sources.build_lab_sources", _explodes)

    drops = cc.collect_host_drops([project])

    assert [d["where"] for d in drops] == [str(project.root)]
    assert "backend on fire" in drops[0]["reason"]


# --- the payload ------------------------------------------------------------------


def test_host_drops_ride_the_names_payload(tmp_path, monkeypatch):
    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
    project = _project(tmp_path, {}, hosts=[])
    recorded = [{"project": "sut", "where": "somewhere", "reason": "some reason"}]

    cc.write_cache([project], instructions=[], hosts=["dut"], host_drops=recorded)

    assert cc.read_cache([project])["host_drops"] == recorded


_DEADLINE = {"project": "sut", "where": "sut", "reason": "did not answer", "timed_out": True}
_FAILED = {"project": "sut", "where": "sut", "reason": "host source failed", "timed_out": False}


def _write_through_write_cache(project, drops):
    cc.write_cache([project], instructions=[], hosts=["local"], host_drops=drops, shim={})


def _write_through_write_section(project, drops):
    from otto.config.cache_sections import write_section

    write_section([project], "names", {"hosts": ["local"], "host_drops": drops})


_WRITERS = [_write_through_write_cache, _write_through_write_section]

#: The last schema whose writer stored a deadline answer for the full TTL (#744).
_PRE_FIX_SCHEMA = 29


def _stored_sections() -> dict:
    path = cc._cache_path()
    assert path is not None
    return json.loads(path.read_text())


@pytest.mark.parametrize("write", _WRITERS, ids=lambda w: w.__name__)
def test_a_payload_from_a_host_enumeration_past_its_deadline_is_served_on_the_short_ttl(
    tmp_path, monkeypatch, write
):
    """Both writers reach the one decision: a deadline drop caps the TTL (#744).

    Stored with the normal TTL, the deadline's ``hosts: ["local"]`` left
    ``otto host <TAB>`` offering only ``local`` for a day. It is still an
    answer worth serving, as TAB after TAB would otherwise each pay the
    deadline: it is served, and its stored TTL is the five-minute tier, so the
    next TAB after that asks the source again.
    """
    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
    project = _project(tmp_path, {}, hosts=[])
    assert cc._cache_ttl_seconds([project]) == cc.CACHE_TTL_SECONDS, "control: a file lab"

    write(project, [_DEADLINE])

    assert cc.read_sections([project], ["names"]) is not None, "a deadline answer is an answer"
    status = cc.inspect_section([project], "names")
    assert status.state == "fresh"
    assert status.payload is not None
    assert status.payload["host_drops"] == [_DEADLINE]
    for name, entry in _stored_sections()["sections"].items():
        assert entry["tainted"] is False, name
        assert entry["ttl_seconds"] <= cc.UNFINGERPRINTED_CACHE_TTL_SECONDS, name


@pytest.mark.parametrize("write", _WRITERS, ids=lambda w: w.__name__)
def test_a_payload_with_only_ordinary_drops_keeps_the_normal_ttl(tmp_path, monkeypatch, write):
    """A failed or skipped source is an answer, cached with its reason for the full TTL."""
    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
    project = _project(tmp_path, {}, hosts=[])

    write(project, [_FAILED])

    assert cc.inspect_section([project], "names").state == "fresh"
    for name, entry in _stored_sections()["sections"].items():
        assert entry["ttl_seconds"] == cc.CACHE_TTL_SECONDS, name


def test_a_deadline_answer_expires_after_the_short_ttl_and_the_next_read_misses(
    tmp_path, monkeypatch
):
    """Past the short TTL the entry is a miss, so the next TAB enumerates again (#744).

    The clock is injected, not slept on: the read sees ``now`` one second past
    the stored TTL.
    """
    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
    project = _project(tmp_path, {}, hosts=[])
    _write_through_write_cache(project, [_DEADLINE])
    assert cc.read_cache([project]) is not None, "control: served inside the short TTL"

    written = _stored_sections()["sections"]["names"]["generated_at"]
    later = written + cc.UNFINGERPRINTED_CACHE_TTL_SECONDS + 1
    monkeypatch.setattr(cc.time, "time", lambda: later)

    assert cc.read_cache([project]) is None, "a deadline answer outlived the short TTL"
    assert cc.inspect_section([project], "names").state == "expired"


@pytest.mark.parametrize(("drop", "served"), [(_DEADLINE, False), (_FAILED, True)])
def test_the_shim_enforces_the_ttl_the_writer_decided(tmp_path, monkeypatch, drop, served):
    """The shim's TTL is the one ``write_sections`` stored, never one of its own (#744).

    Past the short tier, the shim hands a deadline answer over (expired) and
    keeps serving an ordinary one, whose entry holds the normal TTL.
    """
    from otto import _shim_complete as sc

    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
    project = _project(tmp_path, {}, hosts=[])
    _write_through_write_cache(project, [drop])
    data = _stored_sections()
    written = data["sections"]["shim"]["generated_at"]

    assert sc.servable_shim(data, now=written + 1) == {}, "control: served when fresh"
    later = written + cc.UNFINGERPRINTED_CACHE_TTL_SECONDS + 1
    if served:
        assert sc.servable_shim(data, now=later) == {}
    else:
        with pytest.raises(sc.Handover, match="expired"):
            sc.servable_shim(data, now=later)


def test_the_shim_hands_over_when_the_names_entry_it_serves_has_expired(tmp_path, monkeypatch):
    """An expired ``names`` entry is never shim-served, whatever the shim entry says (#744).

    The names entry holds the short deadline TTL and is past it; the shim
    entry beside it is fresh on the normal day TTL. The shim serves ``names``
    data, so it judges that entry by its own ``generated_at`` and
    ``ttl_seconds`` too, as ``read_sections`` does.
    """
    from otto import _shim_complete as sc

    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
    project = _project(tmp_path, {}, hosts=[])
    _write_through_write_cache(project, [_DEADLINE])
    data = _stored_sections()
    names = data["sections"]["names"]
    assert names["ttl_seconds"] == cc.UNFINGERPRINTED_CACHE_TTL_SECONDS
    later = names["generated_at"] + cc.UNFINGERPRINTED_CACHE_TTL_SECONDS + 1
    shim = data["sections"]["shim"]
    shim["ttl_seconds"] = cc.CACHE_TTL_SECONDS
    shim["generated_at"] = later - 1

    with pytest.raises(sc.Handover, match="expired") as raised:
        sc.servable_shim(data, now=later)
    assert raised.value.stale


def test_one_enumeration_answers_both_host_collectors_even_past_the_deadline(tmp_path, monkeypatch):
    """``collect_host_ids`` and ``collect_host_ids_by_lab`` share ONE enumeration (#744).

    The enumeration misses its deadline on its first call only; a later call
    would answer at once. A collector that missed the memo would therefore
    enumerate again and find ``dut-1``, writing ``hosts`` and ``hosts_by_lab``
    that disagree. Exactly one enumeration runs, and both agree on its outcome.
    """
    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
    monkeypatch.setenv(cc.HOST_SUMMARY_DEADLINE_ENV_VAR, "0.05")
    monkeypatch.setattr(cc, "_SUMMARY_MEMO", {})
    project = _project(
        tmp_path,
        {},
        hosts=[{"ip": "10.0.0.9", "element": "dut-1", "creds": _CREDS, "labs": ["bench"]}],
    )
    real = cc._enumerate_host_summaries
    never = threading.Event()
    calls: list[int] = []

    def _slow_first(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            never.wait(5)  # past the deadline; abandoned, released at teardown
        return real(*args, **kwargs)

    monkeypatch.setattr(cc, "_enumerate_host_summaries", _slow_first)
    try:
        hosts = cc.collect_host_ids([project])
        by_lab = cc.collect_host_ids_by_lab([project])
    finally:
        never.set()

    bucketed = {host for ids in by_lab.values() for host in ids}
    assert set(hosts) - {"local"} == bucketed, "two enumerations, two answers"
    assert "dut-1" not in bucketed, "the one enumeration missed its deadline"
    assert len(calls) == 1, "a second collector enumerated again"


def test_a_pre_fix_cache_holding_a_deadline_answer_is_not_served(tmp_path, monkeypatch):
    """A cache the pre-fix schema (29) wrote from a deadline answer: dropped (#744).

    Schema 29, the last one before the fix, stored ``hosts: ["local"]`` from a
    host source that missed its deadline with the full TTL (a day). The bump
    makes every such entry a miss rather than leaving it served until it
    expires. 29 is that pre-fix schema, pinned on purpose: the assertion is
    about it, not about whatever the schema is today.
    """
    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
    project = _project(tmp_path, {}, hosts=[])
    pre_fix = {k: v for k, v in _DEADLINE.items() if k != "timed_out"}
    cc.write_cache([project], instructions=[], hosts=["local"], host_drops=[pre_fix])
    assert cc.read_cache([project]) is not None, "control: a v30 entry like this is served"

    path = cc._cache_path()
    assert path is not None
    data = json.loads(path.read_text())
    data["schema"] = _PRE_FIX_SCHEMA
    path.write_text(json.dumps(data))

    assert cc.SCHEMA_VERSION > _PRE_FIX_SCHEMA
    assert cc.read_cache([project]) is None


def test_a_names_payload_missing_host_drops_still_reads_as_an_empty_list(tmp_path, monkeypatch):
    """Older payload shape without the key: empty, not a miss and not a crash."""
    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
    project = _project(tmp_path, {}, hosts=[])
    cc.write_cache([project], instructions=[], hosts=["dut"])
    path = cc._cache_path()
    assert path is not None
    data = json.loads(path.read_text())
    del data["sections"]["names"]["payload"]["host_drops"]
    path.write_text(json.dumps(data))

    assert cc.read_cache([project])["host_drops"] == []


# --- inspect_section: what `otto cache info` reads ---------------------------------


def test_inspect_reports_fresh_with_the_payload(tmp_path, monkeypatch):
    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
    project = _project(tmp_path, {}, hosts=[])
    cc.write_cache([project], instructions=[], hosts=["dut"])

    status = cc.inspect_section([project], "names")

    assert status.state == "fresh"
    assert status.payload is not None
    assert status.payload["hosts"] == ["dut"]
    assert status.generated_at is not None
    assert time.time() - status.generated_at < 60


def test_inspect_reports_stale_after_a_key_file_moves_and_keeps_the_payload(tmp_path, monkeypatch):
    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
    project = _project(tmp_path, {}, hosts=[])
    cc.write_cache([project], instructions=[], hosts=["dut"])
    lab_file = project.root / "lab" / "lab.json"
    later = lab_file.stat().st_mtime + 10
    os.utime(lab_file, (later, later))

    status = cc.inspect_section([project], "names")

    assert status.state == "stale"
    assert status.payload is not None
    assert status.payload["hosts"] == ["dut"]


def test_inspect_reports_expired_tainted_outdated_unreadable_and_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
    project = _project(tmp_path, {}, hosts=[])
    assert cc.inspect_section([project], "names").state == "missing"

    cc.write_cache([project], instructions=[], hosts=["dut"], tainted=True)
    assert cc.inspect_section([project], "names").state == "tainted"

    cc.write_cache([project], instructions=[], hosts=["dut"])
    path = cc._cache_path()
    assert path is not None
    data = json.loads(path.read_text())
    data["sections"]["names"]["generated_at"] = time.time() - 2 * cc.CACHE_TTL_SECONDS
    path.write_text(json.dumps(data))
    assert cc.inspect_section([project], "names").state == "expired"

    data["sections"]["names"]["generated_at"] = time.time()
    data["schema"] = cc.SCHEMA_VERSION - 1
    path.write_text(json.dumps(data))
    assert cc.inspect_section([project], "names").state == "outdated"

    path.write_text("{corrupt")
    assert cc.inspect_section([project], "names").state == "unreadable"


# --- describe_inventory --------------------------------------------------------------


def test_describe_inventory_names_each_case(tmp_path, monkeypatch):
    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
    none = _project(tmp_path, {}, hosts=[], name="none")
    assert cc.describe_inventory([none]) == cc.InventoryDescription(text="none declared")

    broken = _project(tmp_path, {"backend": "json"}, hosts=[], name="broken")
    described = cc.describe_inventory([broken])
    assert described.text.startswith("BROKEN")
    assert "parse failed: path: Field required" in described.text
    assert "no host completes" in described.text
    assert described.blocker == "is broken"

    table = _json_inventory(tmp_path, _RECORD, name="declared")
    declared = _project(tmp_path, table, hosts=[], name="declared")
    described = cc.describe_inventory([declared])
    assert "inventory.json" in described.text
    assert "freshness" not in described.text
    assert described.blocker is None


def test_describe_inventory_says_when_completion_can_never_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
    project = _project(tmp_path, {}, hosts=[])
    unfingerprinted = SimpleNamespace(label="probe:live", fingerprint=lambda: None)
    monkeypatch.setattr("otto.inventory.config.build_inventory", lambda _projects: unfingerprinted)

    described = cc.describe_inventory([project])

    assert described.text.startswith("probe:live")
    assert "never caches" in described.text
    assert described.blocker == "cannot report freshness"

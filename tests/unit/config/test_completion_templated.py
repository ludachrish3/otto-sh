"""Templated labs and hosts complete like hand-written ones (lab templating, spec section 6.8).

Completion reads a ``jsonnet`` source the way dispatch does: one bounded
enumeration per project (:func:`otto.config.completion_cache._project_enumeration`)
renders each entry once, through one view, and yields the project's host
summaries, its lab names and its declared links. Its deadline cancels the
project's renders (:meth:`otto.templating.memo.RenderMemo.cancel`), so an
abandoned worker starts no further render or discovery and publishes nothing.
A warm TAB is the shim's stat pass over the stored key paths, whatever the
fleet's size.

The deadline tests never measure time: each blocks the worker at a named step
on an event the test owns, and the deadline passes once the worker is there
(:func:`tests._fixtures.completion_deadline.expire_when`); the test then
counts what the worker did after it. A native evaluation still running when
TAB returns is held the same way, inside its own import callback.
"""

import json
import os
import subprocess
import sys
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

import otto.config.completion_cache as cc
from otto import _shim_complete
from otto.config.cache_sections import read_section, write_section
from otto.config.project import Project
from otto.inventory.registry import register_inventory_backend
from otto.models.base import OttoModel
from otto.session import build_lab
from otto.templating.memo import RenderMemo, cancellation
from tests._fixtures.completion_deadline import expire_when
from tests._fixtures.named_inventory import FakeDiscoveryInventory
from tests._fixtures.paths import CHECKOUT_ROOT
from tests.unit.templating import conftest as _templating
from tests.unit.templating._doctor_project import SITES, entry_path, records, templated
from tests.unit.templating._lab_project import CREDS, inventory, jsonnet_source, project, table
from tests.unit.templating._lab_project import write as write_file
from tests.unit.templating._render_helpers import count_evaluations

# The templating conftest's fixtures: the binding proof, and a private OTTO_HOME.
jsonnet_binding_under_test = _templating.jsonnet_binding_under_test
otto_home = _templating.otto_home

WAIT = 60.0
"""How long a test waits on a worker it released; never reached when the code is right."""

TRUNK = "{ name: 'trunk', endpoints: [{ host: 'east-dut0' }, { host: 'west-dut0' }] }"
"""A link between the two site DUTs :data:`SITES` renders for the sites ``east`` and ``west``."""


def _linked(*links: str) -> str:
    """:data:`SITES` (``unix`` DUTs) with *links* added to the rendered document."""
    return SITES % {"os_type": "'unix'"} + "  + { links: [" + ", ".join(links) + "] }\n"


@pytest.fixture(autouse=True)
def _fresh_enumeration(monkeypatch: pytest.MonkeyPatch) -> None:
    """A fresh per-process enumeration memo, and a deadline no sunny-day test reaches."""
    monkeypatch.setattr(cc, "_SUMMARY_MEMO", {})
    monkeypatch.setenv(cc.HOST_SUMMARY_DEADLINE_ENV_VAR, str(WAIT))


def _rebuild(projects: "list[Project]") -> dict[str, Any]:
    """Collect the lab half of the ``names`` payload as one rebuild does, and write it."""
    cc._SUMMARY_MEMO.clear()
    payload = {
        "hosts": cc.collect_host_ids(projects),
        "hosts_by_lab": cc.collect_host_ids_by_lab(projects),
        "labs": cc.collect_lab_names(projects),
        "links": cc.collect_links(projects),
        "host_drops": cc.collect_host_drops(projects),
    }
    write_section(projects, "names", payload)
    return payload


def _done_event(monkeypatch: pytest.MonkeyPatch, name: str) -> threading.Event:
    """Return an event set once the enumeration worker *name* returns, however late."""
    done = threading.Event()
    real = getattr(cc, name)

    def recording(*args: Any, **kwargs: Any) -> cc.ProjectEnumeration:
        try:
            return real(*args, **kwargs)
        finally:
            done.set()

    monkeypatch.setattr(cc, name, recording)
    return done


@pytest.fixture
def names_done(monkeypatch: pytest.MonkeyPatch) -> threading.Event:
    """Set once a names-only enumeration worker returns, however late."""
    return _done_event(monkeypatch, "_enumerate_lab_names")


class _Gate:
    """Blocks the first call through it on :attr:`release`, after setting :attr:`entered`."""

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls: list[Any] = []

    def wrap(self, real: Callable[..., Any], *, record: "Callable[..., Any] | None" = None):
        def gated(*args: Any, **kwargs: Any) -> Any:
            self.calls.append(record(*args) if record is not None else args)
            if len(self.calls) == 1:
                self.entered.set()
                assert self.release.wait(WAIT), "the test never released the gate"
            return real(*args, **kwargs)

        return gated


@pytest.fixture
def gate() -> Iterator[_Gate]:
    """A :class:`_Gate`, released at teardown whatever the test did."""
    held = _Gate()
    yield held
    held.release.set()


def _gate_evaluator(monkeypatch: pytest.MonkeyPatch, gate: _Gate) -> None:
    """Block the evaluator's first entry on *gate*, recording each entry it is entered for."""
    from otto.templating import render

    monkeypatch.setattr(
        render, "_evaluate", gate.wrap(render._evaluate, record=lambda _b, entry, *_: entry)
    )


def _timed_out(drops: "list[dict[str, Any]]", project: Project) -> bool:
    """Whether *drops* is exactly *project*'s deadline drop."""
    return [(d["where"], d["timed_out"]) for d in drops] == [(str(project.root), True)]


# --------------------------------------------------------------------------- #
# Cold: hosts, labs and links of a templated source complete.
# --------------------------------------------------------------------------- #


def test_templated_host_lab_and_link_complete_on_a_cold_cache(tmp_path, otto_home, monkeypatch):
    sut = templated(tmp_path / "sut", sites=("east", "west"), entry=_linked(TRUNK))
    evaluations = count_evaluations(monkeypatch)
    payload = _rebuild([sut])
    assert payload["labs"] == ["site-east", "site-west"]
    assert {"east-dut0", "west-dut0"} <= set(payload["hosts"])
    assert payload["hosts_by_lab"] == {"site-east": ["east-dut0"], "site-west": ["west-dut0"]}
    assert payload["links"] == [{"id": "trunk", "hosts": ["east-dut0", "west-dut0"], "scope": None}]
    assert payload["host_drops"] == []
    assert cc.collect_link_ids([sut]) == ["trunk"]
    assert evaluations() == 1, "one enumeration, one view, one render of the entry"
    # What completion offers is what dispatch loads.
    assert set(build_lab([sut], labs=["site-east"]).hosts) == {"east-dut0", "local"}


def test_lab_names_come_from_the_bounded_enumeration(monkeypatch, tmp_path, otto_home):
    """``--lab`` names are the names-only enumeration's, never a recorded ``list_labs()``."""
    from otto.labs import CompositeLabRepository
    from otto.labs.jsonnet_source import _JsonnetLabRepository

    def refuse(self: Any) -> list[str]:
        raise AssertionError("completion read list_labs()")

    monkeypatch.setattr(CompositeLabRepository, "list_labs", refuse)
    monkeypatch.setattr(_JsonnetLabRepository, "list_labs", refuse)
    sut = templated(tmp_path / "sut", sites=("east",))
    assert cc.collect_lab_names([sut]) == ["site-east"]
    cc._SUMMARY_MEMO.clear()
    monkeypatch.setattr(
        cc,
        "_enumerate_lab_names",
        lambda *a, **k: cc.ProjectEnumeration(lab_names=["from-the-enumeration"]),
    )
    assert cc.collect_lab_names([sut]) == ["from-the-enumeration"]


class _Legacy:
    """A legacy lab source: ``list_labs`` and ``load_lab``, no summaries; a load waits on a gate."""

    def __init__(self, gate: "_Gate") -> None:
        self.gate = gate
        self.loads: list[str] = []

    def list_labs(self) -> list[str]:
        return ["bench", "rig"]

    def load_lab(self, name: str, **_: Any) -> Any:
        self.loads.append(name)
        self.gate.entered.set()
        assert self.gate.release.wait(WAIT), "the test never released the load"
        raise LookupError(name)


def _legacy_project(root: Path, gate: "_Gate", *, backend: str) -> "tuple[Project, _Legacy]":
    from otto.labs import register_lab_repository

    legacy = _Legacy(gate)
    register_lab_repository(backend, config=_NoKeys, factory=lambda c: legacy)
    return project(root, table("[[lab.sources]]", backend=backend)), legacy


def test_lab_names_never_load_a_legacy_backends_lab(tmp_path, otto_home, gate):
    """``--lab`` asks each backend for its names alone: a legacy ``load_lab`` is never called."""
    gate.release.set()  # a load, were one made, would not block
    sut, legacy = _legacy_project(tmp_path / "sut", gate, backend="legacy-unloaded")
    assert cc.collect_lab_names([sut]) == ["bench", "rig"]
    assert legacy.loads == []
    cc.collect_host_ids([sut])  # control: host completion is the legacy exception
    assert sorted(set(legacy.loads)) == ["bench", "rig"]


def test_lab_names_come_back_when_the_host_summaries_time_out(
    tmp_path, otto_home, monkeypatch, gate
):
    """The names enumeration has its own deadline: a slow legacy load never costs ``--lab``.

    The deadline passes for a worker only once the legacy load has blocked,
    so the names worker, which loads nothing, always finishes first.
    """

    def wait(thread: threading.Thread, _deadline: float) -> None:
        while thread.is_alive() and not gate.entered.is_set():
            thread.join(0.01)

    monkeypatch.setattr(cc, "_await_worker", wait)
    sut, legacy = _legacy_project(tmp_path / "sut", gate, backend="legacy-slow")
    payload = _rebuild([sut])  # the host enumeration runs first, and times out in a load
    assert legacy.loads, "control: the host summaries loaded a lab"
    assert _timed_out(payload["host_drops"], sut)
    assert payload["labs"] == ["bench", "rig"]
    assert cc.collect_lab_names([sut]) == ["bench", "rig"]


class _StalledStatus:
    """A legacy lab source whose status read (``list_labs``) waits on a gate, every call."""

    def __init__(self, gate: "_Gate") -> None:
        self.gate = gate
        self.reads = 0

    def list_labs(self) -> list[str]:
        self.reads += 1
        self.gate.entered.set()
        assert self.gate.release.wait(WAIT), "the test never released the status read"
        return ["bench"]

    def load_lab(self, name: str, **_: Any) -> Any:
        raise LookupError(name)


def _counting_deadline(monkeypatch: pytest.MonkeyPatch, gate: "_Gate") -> "list[threading.Thread]":
    """Fire each worker's deadline once a status read is held on *gate*; return the workers waited.

    A worker no read holds is waited to its end, so a healthy enumeration
    never reaches its deadline. One entry per deadline wait, so a test
    counts the deadlines a TAB can cost, never their wall-clock length.
    """
    waited: list[threading.Thread] = []

    def wait(thread: threading.Thread, _deadline: float) -> None:
        waited.append(thread)
        while thread.is_alive() and not (gate.entered.is_set() and not gate.release.is_set()):
            thread.join(0.01)

    monkeypatch.setattr(cc, "_await_worker", wait)
    return waited


def test_a_stalled_status_read_costs_a_cold_host_tab_one_deadline(
    tmp_path, otto_home, monkeypatch, gate
):
    """The names and the summaries read the same status: one stall is one deadline, not two.

    The names enumeration is given up on at its deadline, and the full
    enumeration is that same give-up: no second worker starts, no second
    deadline is waited, and the project's one drop is the deadline.
    """
    from otto.labs import register_lab_repository

    waited = _counting_deadline(monkeypatch, gate)
    stalled = _StalledStatus(gate)
    register_lab_repository("legacy-stalled", config=_NoKeys, factory=lambda c: stalled)
    sut = project(tmp_path / "sut", table("[[lab.sources]]", backend="legacy-stalled"))
    payload = _rebuild([sut])  # every collector of one cold host TAB
    assert len(waited) == 1, f"a stalled status read cost {len(waited)} deadline waits"
    assert stalled.reads == 1, "a second worker repeated the stalled status read"
    assert payload["hosts_by_lab"] == {}, "no source summary was read"
    assert payload["labs"] == []
    assert _timed_out(payload["host_drops"], sut)


def test_a_healthy_tab_still_reads_its_names_and_summaries_on_their_own_deadlines(
    tmp_path, otto_home, monkeypatch, gate
):
    """Control: with nothing stalled, the names and the full enumeration each run and finish."""
    gate.release.set()
    waited = _counting_deadline(monkeypatch, gate)
    sut = templated(tmp_path / "sut", sites=("east", "west"), entry=_linked(TRUNK))
    payload = _rebuild([sut])
    assert len(waited) == 2, "the names and the full enumeration, each bounded once"
    assert not any(t.is_alive() for t in waited)
    assert payload["labs"] == ["site-east", "site-west"]
    assert {"east-dut0", "west-dut0"} <= set(payload["hosts"])
    assert payload["host_drops"] == []


def test_a_recorded_name_missing_from_the_render_never_completes(tmp_path, otto_home, monkeypatch):
    """A healthy scoped source's recorded last-known names are diagnostics, never completions."""
    from otto.labs import last_good
    from otto.labs.sources import build_lab_sources

    sut = templated(tmp_path / "sut", sites=("east", "gone"), scope={"prefixes": ["site-"]})
    assert _rebuild([sut])["labs"] == ["site-east", "site-gone"]
    # Keep that record stale from here on, then drop the site from the inventory.
    monkeypatch.setattr(last_good, "record_last_good", lambda **_: None)
    inventory(sut.root, "physical", records("east"))
    [source] = build_lab_sources(projects=[sut]).sources
    assert source.repository.list_labs() == ["site-east", "site-gone"], "control: it is recorded"
    payload = _rebuild([sut])
    assert payload["labs"] == ["site-east"]
    assert set(payload["hosts_by_lab"]) == {"site-east"}
    resolution = cc.resolve_process_inventory([sut])
    summaries = cc.project_host_summaries(sut, resolution, profiles=cc._profiles([sut]))
    assert all("site-gone" not in s.labs for s in summaries), summaries


def test_healthy_source_survives_beside_a_failed_unscoped_source(tmp_path, otto_home):
    from tests._fixtures.labdata import write_lab_json

    root = tmp_path / "sut"
    lab = write_lab_json(
        root / "lab_data" / "bench" / "lab.json",
        [{"element": "console", "ip": "10.9.0.1", "labs": ["bench"], "creds": CREDS}],
        links=[{"name": "loop", "endpoints": [{"host": "console"}, {"host": "console"}]}],
    )
    write_file(entry_path(root), "function(otto) error 'the template is broken'\n")
    settings = (
        table("[[lab.sources]]", backend="json", paths=[str(lab.parent)])
        + inventory(root, "physical", records("east"))
        + jsonnet_source(paths=[str(entry_path(root))], inventories=["physical"])
    )
    sut = project(root, settings)
    payload = _rebuild([sut])
    assert payload["labs"] == ["bench"]
    assert "console" in payload["hosts"]
    assert [link["id"] for link in payload["links"]] == ["loop"]
    reasons = [d["reason"] for d in payload["host_drops"]]
    assert any("the template is broken" in r for r in reasons), reasons


SECRET = "TOPSECRET"
"""A password in the creds file a template tries to read."""


def test_a_failed_inventory_resolution_still_protects_the_creds_file(tmp_path, otto_home):
    """Completion's fallback view renders under the real catalog: a protected file never leaks.

    A broken ``[inventory]`` fails the resolution, so the enumeration reads
    through an empty fallback view. The catalog still comes from the
    projects' tables: the source that ``importstr``s the creds file is
    refused, while a source that reads nothing still renders, so the
    fallback renders under a real catalog, not under none.
    """
    from otto.inventory.config import deferred_inventory_view
    from otto.labs.sources import build_lab_sources

    root = tmp_path / "sut"
    lib = root / "lab_data"
    creds = write_file(lib / "creds.json", [{"login": "admin", "password": SECRET}])
    leaked = "std.strReplace(std.strReplace(importstr 'creds.json', '\"', ''), ' ', '')"
    write_file(
        lib / "layouts" / "leak.jsonnet",
        f"function(otto) {{ labs: {{ [{leaked}]: {{}} }}, elements: [], links: [{{ name: {leaked}, "
        "endpoints: [{ host: 'a' }, { host: 'b' }] }] }\n",
    )
    write_file(
        lib / "layouts" / "plain.jsonnet", "function(otto) { labs: { plain: {} }, elements: [] }\n"
    )
    settings = (
        table("[creds]", backend="json", path=str(creds))
        + table("[inventory]", backend="nosuchbackend")
        + jsonnet_source(
            paths=[str(lib / "layouts" / "leak.jsonnet")],
            import_paths=[str(lib)],
            scope={"prefixes": ["["]},  # a scoped source records its lab names on disk
        )
        + jsonnet_source(paths=[str(lib / "layouts" / "plain.jsonnet")], import_paths=[str(lib)])
    )
    sut = project(root, settings)
    assert cc.resolve_process_inventory([sut]).error is not None, "control: resolution fails"
    report = build_lab_sources(projects=[sut]).lab_status_report(
        view=deferred_inventory_view(projects=[sut])
    )
    reasons = [outage.diagnostic.reason for outage in report.unavailable_sources]
    assert any("protected file" in r for r in reasons), reasons  # control: dispatch refuses it
    payload = _rebuild([sut])
    assert payload["labs"] == ["plain"], "the fallback renders under a real catalog"
    assert SECRET not in json.dumps(cc.collect_lab_names([sut]))
    assert SECRET not in json.dumps(cc.collect_links([sut]))
    assert SECRET not in json.dumps(payload)
    written = [p for p in otto_home.rglob("*") if p.is_file()]
    assert not [p for p in written if SECRET.encode() in p.read_bytes()], written


def test_an_invalid_user_file_renders_nothing_in_the_fallback(tmp_path, otto_home):
    """A user file that cannot be read gives the fallback no catalog: no template renders.

    The user file names the creds file, so a catalog built without it would
    leave that file unprotected. Nothing renders, the secret is nowhere, and
    a hand-written lab is still offered.
    """
    from otto.config.user_settings import clear_user_settings_memo
    from otto.inventory.config import deferred_inventory_view

    root = tmp_path / "sut"
    lib = root / "lab_data"
    creds = write_file(tmp_path / "secrets" / "creds.json", [{"login": "a", "password": SECRET}])
    leaked = "std.strReplace(std.strReplace(importstr 'creds.json', '\"', ''), ' ', '')"
    write_file(
        lib / "layouts" / "leak.jsonnet", f"function(otto) {{ labs: {{ [{leaked}]: {{}} }} }}\n"
    )
    hand = write_file(lib / "hand" / "lab.json", {"labs": {"hand": {}}})
    settings = jsonnet_source(
        paths=[str(lib / "layouts" / "leak.jsonnet")],
        import_paths=[str(creds.parent)],
        scope={"prefixes": ["["]},  # a scoped source records its lab names on disk
    ) + table("[[lab.sources]]", backend="json", paths=[str(hand.parent)])
    sut = project(root, settings)
    (otto_home / "settings.toml").write_text(  # scaffold-exempt: the invalid USER file under test
        f'[creds]\nbackend = "json"\npath = "{creds}"\n\n[lab]\nname = "a project table"\n'
    )
    clear_user_settings_memo()
    assert cc.resolve_process_inventory([sut]).error is not None, "control: resolution fails"
    refusal = deferred_inventory_view(projects=[sut]).protected().refusal
    assert refusal is not None
    assert "cannot be read" in refusal
    payload = _rebuild([sut])
    assert payload["labs"] == ["hand"], "a hand-written lab is still offered"
    assert SECRET not in json.dumps(cc.collect_lab_names([sut]))
    assert SECRET not in json.dumps(payload)
    written = [p for p in otto_home.rglob("*") if p.is_file() and p.name != "settings.toml"]
    assert not [p for p in written if SECRET.encode() in p.read_bytes()], written


def test_tab_on_a_snapshot_served_inventory_matches_dispatch(tmp_path, otto_home):
    """An inventory that is down but snapshot-served completes as ``--lab`` loads it."""
    from datetime import datetime, timedelta

    from otto.inventory.protocol import FILLABLE_INVENTORY_FIELDS

    class _NoKeys(OttoModel, frozen=True):
        pass

    held = {k: {f: v for f, v in r.items() if f != "creds"} for k, r in records("east").items()}
    fake = FakeDiscoveryInventory(
        held, supplies=sorted(FILLABLE_INVENTORY_FIELDS - {"creds"}), max_stale="7d"
    )
    register_inventory_backend("fake:disc", config=_NoKeys, factory=lambda c: fake)
    root = tmp_path / "sut"
    write_file(
        entry_path(root), SITES % {"os_type": "'unix', creds: [{login: 'u', password: 'p'}]"}
    )
    settings = table(
        "[[inventories]]", name="physical", backend="fake:disc", cache_ttl="24h"
    ) + jsonnet_source(paths=[str(entry_path(root))], inventories=["physical"])
    sut = project(root, settings)
    assert sorted(build_lab([sut], labs=["site-east"]).hosts) == ["east-dut0", "local"]
    [meta_file] = sorted((otto_home / "inventory-cache").glob("*.meta.json"))
    meta = json.loads(meta_file.read_text())
    meta["fetched_at"] = (
        datetime.fromisoformat(meta["fetched_at"]) - timedelta(hours=31)
    ).isoformat()
    meta_file.write_text(json.dumps(meta))
    fake.fail = "unreachable"
    dispatched = sorted(set(build_lab([sut], labs=["site-east"]).hosts) - {"local"})
    payload = _rebuild([sut])
    assert payload["labs"] == ["site-east"]
    assert payload["hosts_by_lab"] == {"site-east": dispatched} == {"site-east": ["east-dut0"]}
    assert payload["host_drops"] == []


# --------------------------------------------------------------------------- #
# Links: one relevance decision, link_attaches, for dispatch and completion.
# --------------------------------------------------------------------------- #

MGMT = "{ name: 'mgmt', endpoints: [{ host: 'east-dut0' }, { host: 'console' }] }"
"""A scoped source's link to a console server that lives in the outside lab ``bench``."""


def _scoped_with_a_shared_console(root: Path) -> Project:
    """A scoped ``site-`` jsonnet source with :data:`TRUNK` and :data:`MGMT`, beside ``bench``."""
    write_file(
        root / "lab_data" / "bench" / "lab.json",
        {
            "labs": {"bench": {}},
            "elements": [
                {
                    "name": "console",
                    "labs": ["bench"],
                    "hosts": [{"ip": "10.9.0.1", "creds": CREDS}],
                }
            ],
        },
    )
    write_file(entry_path(root), _linked(TRUNK, MGMT))
    settings = (
        table("[[lab.sources]]", backend="json", paths=[str(root / "lab_data" / "bench")])
        + inventory(root, "physical", records("east", "west"))
        + jsonnet_source(
            paths=[str(entry_path(root))], inventories=["physical"], scope={"prefixes": ["site-"]}
        )
    )
    return project(root, settings)


def _offered(projects: "list[Project]", payload: dict[str, Any], labs: "list[str]") -> list[str]:
    """The links offered under *labs*, live and from the cache; assert the two agree."""
    if labs:
        loaded = {"local"}.union(*(payload["hosts_by_lab"].get(lab, []) for lab in labs))
        live = cc.collect_link_ids(projects, loaded_ids=loaded, selection=frozenset(labs))
    else:
        live = cc.collect_link_ids(projects)
    source = {"key": "links", "lab_scoped": True, "sort": True, "always": ["local"]}
    cached = _shim_complete._payload_values(source, payload, labs)
    assert cached == live, (labs, cached, live)
    return live


def test_scoped_link_never_joins_an_outside_lab_in_completion(tmp_path, otto_home):
    sut = _scoped_with_a_shared_console(tmp_path / "sut")
    payload = _rebuild([sut])
    scope = {"names": [], "prefixes": ["site-"]}
    assert payload["links"] == [
        {"id": "mgmt", "hosts": ["east-dut0", "console"], "scope": scope},
        {"id": "trunk", "hosts": ["east-dut0", "west-dut0"], "scope": scope},
    ]
    assert _offered([sut], payload, ["bench"]) == [], "the console is loaded; bench is outside"
    assert _offered([sut], payload, ["site-east"]) == ["mgmt", "trunk"]


def test_link_completion_with_no_lab_and_with_a_multi_lab_selection(tmp_path, otto_home):
    sut = _scoped_with_a_shared_console(tmp_path / "sut")
    payload = _rebuild([sut])
    assert _offered([sut], payload, []) == ["mgmt", "trunk"]
    assert _offered([sut], payload, ["bench", "site-west"]) == ["mgmt", "trunk"]
    assert _offered([sut], payload, ["bench"]) == []


def test_cold_link_completion_builds_no_host(monkeypatch, tmp_path, otto_home):
    from otto.labs import json_repository

    def refuse(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("link completion built a host")

    monkeypatch.setattr(json_repository, "_add_host", refuse)
    root = tmp_path / "sut"
    extra = {
        "links": [{"name": "extra", "endpoints": [{"host": "east-dut0"}, {"host": "west-dut0"}]}]
    }
    write_file(root / "lab_data" / "links" / "lab.json", extra)
    write_file(entry_path(root), _linked(TRUNK))
    settings = (
        inventory(root, "physical", records("east", "west", "north"))
        + jsonnet_source(paths=[str(entry_path(root))], inventories=["physical"])
        + table("[[lab.sources]]", backend="json", paths=[str(root / "lab_data" / "links")])
    )
    sut = project(root, settings)
    assert cc.collect_link_ids([sut]) == ["extra", "trunk"]
    assert cc.collect_lab_names([sut]) == ["site-east", "site-north", "site-west"]


# --------------------------------------------------------------------------- #
# Warm: the shim stats stored key paths, and nothing else.
# --------------------------------------------------------------------------- #

_RECORDED_TAB = """
import json, os, sys
sys.argv = ["otto"]
import otto
from otto import _shim, _shim_complete
ops = {{"opens": [], "listdirs": [], "sockets": [], "stats": []}}
def hook(event, args):
    if event == "open" and args and isinstance(args[0], str):
        ops["opens"].append(args[0])
    elif event in ("os.listdir", "os.scandir"):
        ops["listdirs"].append(str(args[0]) if args else ".")
    elif event.startswith("socket."):
        ops["sockets"].append(event)
real_stat = os.stat
class CountingStat:  # a callable object, not a function: a class attribute never binds it
    def __call__(self, path, *args, **kwargs):
        ops["stats"].append(str(path))
        return real_stat(path, *args, **kwargs)
os.stat = CountingStat()
sys.addaudithook(hook)
try:
    _shim.main()
except SystemExit:
    pass
finally:
    os.stat = real_stat
    ops["templating"] = sorted(m for m in sys.modules if m.startswith("otto.templating"))
    ops["cli"] = "otto.cli.main" in sys.modules
    with open({out!r}, "w") as fh:
        json.dump(ops, fh)
"""
"""A bash TAB through the real shim entry, recording every open, listing, socket and stat."""


def _tab_env(sut: Project, home: Path) -> dict[str, str]:
    return {
        **os.environ,
        "OTTOPATH": str(sut.root),
        "OTTO_HOME": str(home),
        cc.HOST_SUMMARY_DEADLINE_ENV_VAR: str(WAIT),
        "_OTTO_COMPLETE": "complete_bash",
        "COMP_WORDS": "otto host ",
        "COMP_CWORD": "2",
    }


def _recorded_tab(tmp_path: Path, env: dict[str, str], name: str) -> "tuple[str, dict[str, Any]]":
    """Run one TAB through the shim in a child; return its answer and what it touched."""
    out = tmp_path / f"{name}.ops.json"
    child = tmp_path / f"{name}.py"
    child.write_text(_RECORDED_TAB.format(out=str(out)))
    done = subprocess.run(
        [sys.executable, str(child)], env=env, capture_output=True, text=True, check=False
    )
    assert done.returncode == 0, done.stderr
    return done.stdout, json.loads(out.read_text())


def _warm_tab(tmp_path: Path, env: dict[str, str], name: str) -> "tuple[str, dict[str, Any]]":
    """A warm TAB whose ``names`` marker is backdated, so the shim runs its stat pass."""
    import time

    cache = Path(_shim_complete.locate_cache(env))
    marker = cache.parent / _shim_complete.MARKER_FILENAMES["names"]
    if marker.exists():
        old = time.time() - 10 * _shim_complete.WINDOW_SECONDS
        os.utime(marker, (old, old))
    answer, ops = _recorded_tab(tmp_path, env, name)
    ops["cache_file"] = str(cache)
    ops["marker"] = str(marker)
    # The child's own report of what it touched, written after the TAB answered.
    ops["opens"] = [p for p in ops["opens"] if p != str(tmp_path / f"{name}.ops.json")]
    return answer, ops


def test_warm_tab_is_stat_only(tmp_path, otto_home):
    sut = templated(tmp_path / "sut", sites=("east", "west"), entry=_linked(TRUNK))
    write_file(sut.root / "lab_data" / "lib" / "unused.libsonnet", "{}\n")
    env = _tab_env(sut, otto_home)
    cold, cold_ops = _recorded_tab(tmp_path, env, "cold")
    assert cold_ops["cli"], "control: the cold TAB took the full path and rebuilt the cache"
    assert "east-dut0" in cold.split()
    warm, ops = _warm_tab(tmp_path, env, "warm")
    assert not ops["cli"], "the warm TAB handed over"
    assert warm == cold
    assert ops["templating"] == [], "a warm TAB imports no templating code, so it evaluates nothing"
    assert ops["listdirs"] == []
    assert ops["sockets"] == []
    # The shim's own cache read and its marker touch; no inventory, template or library read.
    assert [p for p in ops["opens"] if p not in (ops["cache_file"], ops["marker"])] == []
    assert str(entry_path(sut.root)) in ops["stats"], "the stat pass ran over the stored keys"


def _patched_library(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Serve ``otto.libsonnet``'s place from a copy under *tmp_path*, which an upgrade may edit."""
    from otto.templating import imports

    library = write_file(
        tmp_path / "site-packages" / "otto" / "templating" / imports.LIBRARY_NAME,
        imports.reserved_library()[imports.LIBRARY_NAME].decode(),
    )
    monkeypatch.setattr(imports, "library_file", lambda: library)
    return library


def _upgrade(library: Path) -> None:
    """Change the library as an otto upgrade would."""
    library.write_text(library.read_text() + "\n// a newer otto\n")


def test_a_warm_tab_hands_over_when_the_library_changes_under_no_import_root(
    tmp_path, otto_home, monkeypatch
):
    """``import_paths = []`` still keys the source on ``otto.libsonnet``, which it may import."""
    from otto.config.cache_sections import lab_key_paths
    from otto.config.completion_tree import stat_triple

    library = _patched_library(tmp_path, monkeypatch)
    sut = templated(tmp_path / "sut", import_paths=[])
    keys = lab_key_paths([sut])
    assert library in keys
    triples = [stat_triple(p) for p in keys]
    _shim_complete._stat_pass(triples)  # control: unchanged, the shim serves
    _upgrade(library)
    with pytest.raises(_shim_complete.Handover, match="changed"):
        _shim_complete._stat_pass(triples)


def test_a_warm_tab_hands_over_when_a_jsonnet_inventorys_library_changes(
    tmp_path, otto_home, monkeypatch
):
    """A ``.jsonnet`` inventory's ``stat_paths()`` hold the library too, so the shim sees it."""
    from otto.config.completion_tree import inventory_block

    library = _patched_library(tmp_path, monkeypatch)
    root = tmp_path / "sut"
    fleet = write_file(root / "lab_data" / "virtual.jsonnet", "{ k: { ip: '10.0.0.1' } }\n")
    sut = project(root, table("[[inventories]]", name="virtual", backend="json", path=str(fleet)))
    block = inventory_block([sut])
    assert block["kind"] == "stat", block
    assert str(library) in [triple[0] for triple in block["files"]]
    _shim_complete._inventory_pass(block)  # control: unchanged, the shim serves
    _upgrade(library)
    with pytest.raises(_shim_complete.Handover, match="changed"):
        _shim_complete._inventory_pass(block)


_FLEET = """
function(otto) {
  labs: { fleet: {} },
  elements: [
    {
      name: r.key,
      labs: ['fleet'],
      hosts: [{ inventory: { name: 'physical', key: r.key }, os_type: 'unix' }],
    }
    for r in otto.inventories.physical
  ],
}
"""
"""One lab holding a host per ``physical`` record."""


def _fleet(root: Path, hosts: int) -> Project:
    """A project whose one jsonnet source renders *hosts* hosts from its ``physical`` inventory."""
    held = {
        f"dut-{i:04d}": {"ip": f"10.{i // 250}.{i % 250}.1", "creds": CREDS} for i in range(hosts)
    }
    write_file(entry_path(root), _FLEET)
    write_file(root / "lab_data" / "lib" / "unused.libsonnet", "{}\n")
    settings = inventory(root, "physical", held) + jsonnet_source(
        paths=[str(entry_path(root))], inventories=["physical"]
    )
    return project(root, settings)


def _warm_stat_count(tmp_path: Path, sut: Project, home: Path) -> int:
    env = _tab_env(sut, home)
    cold, _ = _recorded_tab(tmp_path, env, "cold")
    assert "dut-0000" in cold.split()
    _, ops = _warm_tab(tmp_path, env, "warm")
    assert not ops["cli"], "the warm TAB handed over"
    return len(ops["stats"])


def test_warm_tab_file_ops_do_not_grow_with_fleet_size(tmp_path, otto_home):
    """Same entry and import roots, 10 vs 1000 generated hosts: equal stat counts.

    The count grows with the files under the import roots, by design: each is a key.
    """
    small = _warm_stat_count(tmp_path / "a", _fleet(tmp_path / "a" / "sut", 10), otto_home / "a")
    large = _warm_stat_count(tmp_path / "b", _fleet(tmp_path / "b" / "sut", 1000), otto_home / "b")
    assert small == large


# --------------------------------------------------------------------------- #
# Freshness: the stored key paths name every input of the render.
# --------------------------------------------------------------------------- #

_WITH_LIBRARY = """
local helpers = import 'helpers.libsonnet';
local o = import 'otto.libsonnet';
function(otto)
  o.layout({
    instances: o.groupBy(otto.inventories.physical, ['site']),
    lab(i): helpers.prefix + i.id.site,
    elementName(i, role, record, n): i.id.site + '-' + role + n,
    roles: {
      dut: {
        match: o.inGroup('dut'), count: 1, host: { label: 'dut', fn(r): { os_type: 'unix' } },
      },
    },
  })
"""
""":data:`SITES` with its lab-name prefix read from ``lab_data/lib/helpers.libsonnet``."""


def _touch(path: Path, text: str) -> None:
    """Rewrite *path* with *text* and move its mtime (and its directory's) visibly forward."""
    created = not path.exists()
    before = path.parent.stat().st_mtime_ns
    path.write_text(text)
    stamp = before + 2_000_000_000
    os.utime(path, ns=(stamp, stamp))
    if created:
        os.utime(path.parent, ns=(stamp, stamp))


@pytest.mark.parametrize("edited", ["entry", "library"])
def test_editing_an_entry_or_library_re_evaluates_once(edited, tmp_path, otto_home, monkeypatch):
    sut = templated(tmp_path / "sut", sites=("east",), entry=_WITH_LIBRARY)
    library = sut.root / "lab_data" / "lib" / "helpers.libsonnet"
    write_file(library, "{ prefix: 'site-' }\n")
    evaluations = count_evaluations(monkeypatch)
    assert _rebuild([sut])["labs"] == ["site-east"]
    assert evaluations() == 1
    assert read_section([sut], "names") is not None
    assert evaluations() == 1, "a fresh read evaluates nothing"
    if edited == "entry":
        _touch(entry_path(sut.root), _WITH_LIBRARY.replace("helpers.prefix + ", "'rig-' + "))
    else:
        _touch(library, "{ prefix: 'rig-' }\n")
    assert read_section([sut], "names") is None, f"an edited {edited} left the cache fresh"
    assert _rebuild([sut])["labs"] == ["rig-east"]
    assert evaluations() == 2, "the rebuild evaluated the entry once"


def test_creating_a_file_under_an_import_root_invalidates(tmp_path, otto_home):
    sut = templated(tmp_path / "sut", sites=("east",))
    lib = sut.root / "lab_data" / "lib"
    write_file(lib / "existing.libsonnet", "{}\n")
    _rebuild([sut])
    assert read_section([sut], "names") is not None
    _touch(lib / "new.libsonnet", "{}\n")
    assert read_section([sut], "names") is None


# --------------------------------------------------------------------------- #
# The deadline: one cancel, no later render, discovery or publication.
# --------------------------------------------------------------------------- #


def test_a_render_still_evaluating_times_out_once_and_stays_timed_out(
    tmp_path, otto_home, monkeypatch, gate
):
    """A real evaluation that outlives the deadline: TAB returns while the evaluator still runs.

    The evaluator is held inside itself: the entry ``importstr``s a file
    whose open (the import callback's ``_open_candidate`` seam) waits on the
    test's gate, so the evaluation provably has not finished when TAB
    returns, whatever the binding's speed. The deadline passes once the
    callback is entered. The late result never replaces the timeout.
    """
    from otto.templating import imports, render

    slow = write_file(tmp_path / "sut" / "lab_data" / "lib" / "slow.txt", "late")
    entry = "function(otto) { labs: { ['heavy-' + importstr 'slow.txt']: {} } }\n"
    sut = templated(tmp_path / "sut", entry=entry)
    finished = threading.Event()
    calls: list[Path] = []
    real_evaluate, real_open = render._evaluate, imports._open_candidate

    def timed(binding: Any, entry: Path, *args: Any) -> str:
        calls.append(entry)
        try:
            return real_evaluate(binding, entry, *args)
        finally:
            finished.set()

    def held_open(path: Path) -> int:
        if path == slow.resolve():
            return gate.wrap(real_open)(path)
        return real_open(path)

    monkeypatch.setattr(render, "_evaluate", timed)
    monkeypatch.setattr(imports, "_open_candidate", held_open)
    memos: list[RenderMemo] = []
    real_get = RenderMemo.get_or_render

    def remembering(self: RenderMemo, key: Any, run: Any) -> Any:
        memos.append(self)
        return real_get(self, key, run)

    monkeypatch.setattr(RenderMemo, "get_or_render", remembering)
    expire_when(monkeypatch, gate.entered)
    drops = cc.collect_host_drops([sut])
    assert gate.entered.is_set()
    assert not finished.is_set(), "TAB waited for the evaluator"
    assert _timed_out(drops, sut), drops
    # A later section reads the same enumeration: it never evaluates.
    assert cc.collect_lab_names([sut]) == []
    assert cc.collect_links([sut]) == []
    gate.release.set()
    assert finished.wait(WAIT)
    assert len(calls) == 1
    [memo, *_] = memos
    assert memo.cancelled(sut.root)(), "the deadline cancelled the project's renders"
    assert _timed_out(cc.collect_host_drops([sut]), sut), "the late result replaced the timeout"


def test_first_entry_times_out_and_second_never_starts(
    tmp_path, otto_home, monkeypatch, gate, names_done
):
    root = tmp_path / "sut"
    layouts = entry_path(root).parent
    write_file(layouts / "a.jsonnet", SITES % {"os_type": "'unix'"})
    write_file(layouts / "b.jsonnet", SITES.replace("'site-'", "'rig-'") % {"os_type": "'unix'"})
    settings = inventory(root, "physical", records("east")) + jsonnet_source(
        paths=[str(layouts / "*.jsonnet")], inventories=["physical"]
    )
    sut = project(root, settings)
    _gate_evaluator(monkeypatch, gate)
    expire_when(monkeypatch, gate.entered)
    assert _timed_out(cc.collect_host_drops([sut]), sut)
    gate.release.set()
    assert names_done.wait(WAIT), "the abandoned names worker never returned"
    assert [p.name for p in gate.calls] == ["a.jsonnet"], "the second entry was evaluated"


def test_late_completion_publishes_no_last_good_names(
    tmp_path, otto_home, monkeypatch, gate, names_done
):
    """A status read accepted after the deadline records nothing: its publication is refused."""
    from otto.config.home import lab_names_cache_dir
    from otto.labs import jsonnet_source as source_module

    sut = templated(tmp_path / "sut", sites=("east",), scope={"prefixes": ["site-"]})
    monkeypatch.setattr(source_module, "lab_statuses_of", gate.wrap(source_module.lab_statuses_of))
    expire_when(monkeypatch, gate.entered)
    assert _timed_out(cc.collect_host_drops([sut]), sut)
    gate.release.set()
    assert names_done.wait(WAIT), "the abandoned names worker never returned"
    assert not list(lab_names_cache_dir().glob("*.json")), "a late status read was published"


def test_blocked_last_good_write_never_delays_tab(
    tmp_path, otto_home, monkeypatch, gate, names_done
):
    """An accepted write finishes in the background; ``cancel`` never waits for it.

    The record is written by the names-only enumeration, which reads the
    source's statuses first.
    """
    from otto.config.home import lab_names_cache_dir

    sut = templated(tmp_path / "sut", sites=("east",), scope={"prefixes": ["site-"]})
    real_fsync = os.fsync

    def fsync(fd: int) -> None:
        if threading.current_thread() is threading.main_thread():
            real_fsync(fd)
        else:
            gate.wrap(real_fsync)(fd)

    monkeypatch.setattr(os, "fsync", fsync)
    real_wait = cc._await_worker
    expire_when(monkeypatch, gate.entered)
    assert cc.collect_lab_names([sut]) == [], "the deadline passed while the record was written"
    assert not names_done.is_set(), "TAB waited for the write"
    gate.release.set()
    assert names_done.wait(WAIT)
    [record] = lab_names_cache_dir().glob("*.json")
    assert json.loads(record.read_text())["names"] == ["site-east"]
    monkeypatch.setattr(cc, "_await_worker", real_wait)
    writes = len(gate.calls)
    assert _rebuild([sut])["labs"] == ["site-east"], "the next TAB completes"
    assert len(gate.calls) == writes, "the next TAB read the record and wrote nothing"


class _NoKeys(OttoModel, frozen=True):
    pass


class _Blocking(FakeDiscoveryInventory):
    """A discovery inventory whose first discovery waits on *gate* (``None``: never blocks)."""

    def __init__(self, records: dict[str, dict[str, Any]], *, gate: "_Gate | None") -> None:
        super().__init__(records, supplies=["ip"])
        self.gate = gate

    def inventory_discovery(self) -> Any:
        if self.gate is None:
            return super().inventory_discovery()
        return self.gate.wrap(super().inventory_discovery)()


def test_deadline_during_the_first_inventory_never_starts_the_second(
    tmp_path, otto_home, monkeypatch, gate, names_done
):
    """The names worker renders first, so it is the one the deadline catches in a discovery."""
    first = _Blocking({"dut-east": {"ip": "10.0.1.1"}}, gate=gate)
    second = _Blocking({"dut-west": {"ip": "10.0.2.1"}}, gate=None)
    register_inventory_backend(
        "fake:first", config=_NoKeys, factory=lambda c: first, snapshot_cache=False
    )
    register_inventory_backend(
        "fake:second", config=_NoKeys, factory=lambda c: second, snapshot_cache=False
    )
    root = tmp_path / "sut"
    write_file(entry_path(root), "function(otto) { labs: { bench: {} } }\n")
    settings = (
        table("[[inventories]]", name="first", backend="fake:first")
        + table("[[inventories]]", name="second", backend="fake:second")
        + jsonnet_source(paths=[str(entry_path(root))], inventories=["first", "second"])
    )
    sut = project(root, settings)
    evaluations = count_evaluations(monkeypatch)
    expire_when(monkeypatch, gate.entered)
    assert _timed_out(cc.collect_host_drops([sut]), sut)
    gate.release.set()
    assert names_done.wait(WAIT)
    assert (first.fetches, second.fetches) == (1, 0)
    assert evaluations() == 0


_ONLY_INVENTORY_TAB = """
import json, sys, threading
sys.argv = ["otto"]
from otto.inventory.registry import register_inventory_backend
from otto.models.base import OttoModel
from tests._fixtures.completion_deadline import expire_when
from tests._fixtures.named_inventory import FakeDiscoveryInventory
import otto.config.completion_cache as cc
from otto.templating import render

entered, release = threading.Event(), threading.Event()
workers = ("_enumerate_lab_names", "_enumerate_host_summaries")
started = {{name: threading.Event() for name in workers}}
done = {{name: threading.Event() for name in workers}}
class _NoKeys(OttoModel, frozen=True):
    pass
class _Slow(FakeDiscoveryInventory):
    def inventory_discovery(self):
        entered.set()
        release.wait(60)
        return super().inventory_discovery()
fake = _Slow({{"dut-east": {{"ip": "10.0.1.1"}}}}, supplies=["ip"])
register_inventory_backend(
    "fake:slow", config=_NoKeys, factory=lambda c: fake, snapshot_cache=False
)
evaluations = []
real_evaluate = render._evaluate
def counting(*args):
    evaluations.append(1)
    return real_evaluate(*args)
render._evaluate = counting
def then_say(name):
    real = getattr(cc, name)
    def enumerate_then_say(*args, **kwargs):
        started[name].set()
        try:
            return real(*args, **kwargs)
        finally:
            done[name].set()
    return enumerate_then_say
for name in workers:  # the names worker blocks; its give-up is the host enumeration's too
    setattr(cc, name, then_say(name))
# The deadline passes once the discovery is blocked (tests/_fixtures/completion_deadline.py).
cc._await_worker = lambda thread, deadline: entered.wait(60)
from otto.cli.main import entry
try:
    entry()
finally:
    answered_while_blocked = entered.is_set() and not release.is_set()
    release.set()
    finished = done["_enumerate_lab_names"].wait(60)
    with open({out!r}, "w") as fh:
        json.dump({{"evaluations": len(evaluations), "fetches": fake.fetches,
                   "answered_while_blocked": answered_while_blocked, "finished": finished,
                   "summaries_started": started["_enumerate_host_summaries"].is_set()}}, fh)
"""
"""A real TAB through the CLI entry, its one inventory's discovery blocked at the deadline."""


def test_deadline_during_the_only_inventory_never_evaluates(tmp_path, otto_home):
    root = tmp_path / "sut"
    write_file(entry_path(root), "function(otto) { labs: { bench: {} } }\n")
    settings = table("[[inventories]]", name="slow", backend="fake:slow") + jsonnet_source(
        paths=[str(entry_path(root))], inventories=["slow"]
    )
    sut = project(root, settings)
    out = tmp_path / "tab.json"
    env = _tab_env(sut, otto_home)
    done = subprocess.run(
        [sys.executable, "-c", _ONLY_INVENTORY_TAB.format(out=str(out))],
        env=env,
        cwd=CHECKOUT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert "local" in done.stdout.split(), "the TAB answered"
    seen = json.loads(out.read_text())
    assert seen == {
        "evaluations": 0,
        "fetches": 1,
        "answered_while_blocked": True,
        "finished": True,
        "summaries_started": False,
    }, (seen, done.stderr)


_INHERITED_CANCEL = """
import sys
from pathlib import Path
sys.path.insert(0, {checkout!r})
from otto.config.project import Project
import otto.config.completion_cache as cc
from otto.templating.memo import cancellation
sut = Project(root=Path({root!r}))
with cancellation(lambda: True):
    print(" ".join(cc.collect_host_ids([sut])))
"""


def test_worker_ignores_an_already_cancelled_parent_context(tmp_path, otto_home):
    """The worker's own project token governs it, never its caller's (a fresh context)."""
    sut = templated(tmp_path / "sut", sites=("east",))
    with cancellation(lambda: True):
        assert "east-dut0" in cc.collect_host_ids([sut])


@pytest.mark.skipif(sys.version_info < (3, 14), reason="thread_inherit_context is 3.14's")
def test_worker_ignores_an_inherited_cancelled_context(tmp_path, otto_home):
    """The same, on an interpreter whose threads inherit their creator's context."""
    sut = templated(tmp_path / "sut", sites=("east",))
    script = _INHERITED_CANCEL.format(checkout=str(CHECKOUT_ROOT), root=str(sut.root))
    done = subprocess.run(
        [sys.executable, "-X", "thread_inherit_context=1", "-c", script],
        env={**os.environ, cc.HOST_SUMMARY_DEADLINE_ENV_VAR: str(WAIT)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert "east-dut0" in done.stdout.split()


# --------------------------------------------------------------------------- #
# A deadline is told from a fault by its chain, never by its words.
# --------------------------------------------------------------------------- #


def _wrapped_during(inner: BaseException) -> BaseException:
    """A LabRepositoryError raised while *inner* was being handled: ``__context__`` only."""
    from otto.labs import LabRepositoryError

    outer = LabRepositoryError("the read failed")
    outer.__context__ = inner
    return outer


def test_is_timeout_follows_both_chains():
    from otto.labs import LabRepositoryError
    from otto.labs.drops import is_refusal, is_timeout
    from otto.templating.errors import RenderCancelled, RenderError

    cancelled = RenderCancelled("cancelled")
    caused = LabRepositoryError("source x: cancelled")
    caused.__cause__ = cancelled
    assert is_timeout(cancelled)
    assert is_timeout(caused)
    assert is_timeout(_wrapped_during(RenderCancelled("cancelled")))
    assert not is_timeout(LabRepositoryError("a malformed entry"))
    assert not is_timeout(RenderError("boom", origin=Path("e"), line=None))
    assert not is_refusal(caused), "one chain walk, two questions"


def test_a_skip_records_a_fault_and_lets_a_timeout_through():
    from otto.inventory.errors import BareReferenceRefused
    from otto.labs import LabRepositoryError
    from otto.labs.drops import HostDrop, collecting_drops, skip_failure
    from otto.templating.errors import RenderCancelled

    cancelled = LabRepositoryError("source x: cancelled")
    cancelled.__cause__ = RenderCancelled("cancelled")
    with collecting_drops() as drops:
        skip_failure(where="lab 'a'", error=LabRepositoryError("bad"), reason="could not load: bad")
        skip_failure(where="lab 'b'", error=LabRepositoryError("worse"))
        skip_failure(where="lab 'c'", error=BareReferenceRefused("refused"))
        with pytest.raises(LabRepositoryError) as caught:
            skip_failure(where="lab 'd'", error=cancelled)
    assert caught.value is cancelled
    assert drops == [
        HostDrop(where="lab 'a'", reason="could not load: bad"),
        HostDrop(where="lab 'b'", reason="worse"),
    ]


def test_a_failed_link_listing_is_reported_as_links_never_as_hosts(
    tmp_path, otto_home, monkeypatch, caplog
):
    """The enumeration names what failed: a link fault costs the links, a cancel is the deadline."""
    import logging

    from otto.templating.errors import RenderCancelled

    sut = templated(tmp_path / "sut", sites=("east",))

    def failing(error: Exception) -> None:
        def raise_it(*_args: Any, **_kwargs: Any) -> None:
            raise error

        monkeypatch.setattr(cc, "_link_inputs", raise_it)
        cc._SUMMARY_MEMO.clear()

    failing(RuntimeError("boom"))
    with caplog.at_level(logging.WARNING, logger="otto.config.completion_cache"):
        drops = cc.collect_host_drops([sut])
    assert [(d["where"], d["reason"], d["timed_out"]) for d in drops] == [
        (str(sut.root), "could not list links: boom", False)
    ]
    assert "could not enumerate links" in caplog.text
    assert "east-dut0" in cc.collect_host_ids([sut]), "the hosts survive a link fault"
    caplog.clear()
    failing(RenderCancelled("cancelled"))
    with caplog.at_level(logging.WARNING, logger="otto.config.completion_cache"):
        assert _timed_out(cc.collect_host_drops([sut]), sut)
    assert not caplog.records, "a cancel is the deadline, never a warned fault"


def test_the_status_report_keeps_a_cancelled_scoped_read_a_timeout(tmp_path, otto_home):
    from otto.labs.drops import is_timeout
    from tests.unit.templating._lab_project import operation

    sut = templated(tmp_path / "sut", sites=("east",), scope={"prefixes": ["site-"]})
    op = operation([sut])
    with cancellation(lambda: True):
        report = op.repository.lab_status_report(view=op.view)
    [outage] = report.unavailable_sources
    assert outage.timed_out
    refusal = report.refusal(name="site-east")
    assert refusal is not None
    assert is_timeout(refusal), "a cancelled outage's refusal is raised from a cancellation"
    broken = templated(
        tmp_path / "broken", sites=("east",), scope={"prefixes": ["site-"]}, entry="error 'x'"
    )
    op = operation([broken])
    [outage] = op.repository.lab_status_report(view=op.view).unavailable_sources
    assert not outage.timed_out, "a fault is not a deadline"
    refusal = op.repository.lab_status_report(view=op.view).refusal(name="site-east")
    assert refusal is not None
    assert not is_timeout(refusal)

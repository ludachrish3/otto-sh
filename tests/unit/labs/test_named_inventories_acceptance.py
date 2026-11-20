"""Named inventories end to end, through every public entry point (lab templating, #1176).

Each test writes real settings to disk with
:func:`~tests._fixtures.project_scaffold.make_project`, registers what a
third-party ``init`` module would register, and drives the entry points a
user reaches: :func:`otto.session.build_lab` (what ``--lab`` builds),
``otto --list-labs`` and ``otto inventory`` through the root CLI app, the
completion enumeration, the doctor's :func:`~otto.init.doctor.check_project`
and the conformance helpers. A red here is a defect in the task that produced
the behaviour, never a reason to soften the assertion.

Deferred to phase 3, where their inputs first exist: spec §8's "a first run
with no cache" for ``unavailable`` from a templated source, and §6.8's labs
"shown as unavailable when their names are known from cache".
"""

import json
import os
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest import mock

import pytest
from typer.testing import CliRunner

import otto.config.completion_cache as cc
from otto.config.project import Project
from otto.host.os_profile import ProfileContext
from otto.models.base import OttoModel
from tests._fixtures.named_inventory import (
    FakeDiscoveryInventory,
    FakeInventory,
    register_lookup_counting,
)
from tests._fixtures.project_scaffold import make_project
from tests.unit.labs.test_unchanged_loading import _lab

_CREDS = [{"login": "admin", "password": "pw"}]

# --------------------------------------------------------------------------- #
# Settings on disk.
# --------------------------------------------------------------------------- #


def _toml(value: Any) -> str:
    """*value* as an inline TOML value: tables inline, strings as basic strings."""
    if isinstance(value, dict):
        return "{ " + ", ".join(f"{json.dumps(k)} = {_toml(v)}" for k, v in value.items()) + " }"
    if isinstance(value, list):
        return "[" + ", ".join(_toml(v) for v in value) + "]"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    return json.dumps(value)


def _table(header: str, **keys: Any) -> str:
    """A TOML table (``[x]`` or ``[[x]]`` *header*) holding *keys* as inline values."""
    return header + "\n" + "".join(f"{k} = {_toml(v)}\n" for k, v in keys.items()) + "\n"


def _records(path: Path, records: dict[str, dict[str, Any]]) -> str:
    """Write *records* as a json inventory file at *path*; return the path as a string."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(records))
    return str(path)


def _lab_file(path: Path, labs: dict[str, list[dict[str, Any]]]) -> str:
    """Write a v2 ``lab.json`` at *path*: each lab's element dicts; return its directory.

    Each element belongs to the labs that list it; an element listed by two
    labs is written once.
    """
    elements: dict[str, dict[str, Any]] = {}
    for lab, entries in labs.items():
        for entry in entries:
            held = elements.setdefault(entry["name"], {**entry, "labs": []})
            held["labs"].append(lab)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"labs": {n: {} for n in labs}, "elements": list(elements.values())})
    )
    return str(path.parent)


def _project(root: Path, settings: str, *, name: str = "sut") -> Project:
    """A real project at *root* whose ``.otto/settings.toml`` holds *settings*."""
    return Project(root=make_project(root, name=name, extra=settings))


def _el(name: str, host: dict[str, Any]) -> dict[str, Any]:
    """An element dict holding the one host *host*, with logins."""
    return {"name": name, "hosts": [{"creds": _CREDS, **host}]}


@pytest.fixture
def home(tmp_path, monkeypatch) -> Iterator[Path]:
    """A private ``OTTO_HOME`` for the test; ``write_user`` puts a ``settings.toml`` in it."""
    from otto.config.user_settings import clear_user_settings_memo

    path = tmp_path / "home"
    path.mkdir()
    monkeypatch.setenv("OTTO_HOME", str(path))
    clear_user_settings_memo()
    yield path
    clear_user_settings_memo()


def _write_user(home: Path, text: str) -> None:
    from otto.config.user_settings import clear_user_settings_memo

    (home / "settings.toml").write_text(text)  # scaffold-exempt: the otto home's user file
    clear_user_settings_memo()


# --------------------------------------------------------------------------- #
# Entry points.
# --------------------------------------------------------------------------- #


def _cli(projects: list[Project], args: list[str], *, monkeypatch) -> Any:
    """``otto <args>`` through the root app, *projects* the active ones."""
    from otto import bootstrap
    from otto.cli.main import app

    monkeypatch.setenv("COLUMNS", "300")
    monkeypatch.setenv("OTTOPATH", os.pathsep.join(str(p.root) for p in projects))
    bootstrap._reset()
    with (
        mock.patch("otto.declared.get_projects", return_value=projects),
        mock.patch("rich._console", None),
    ):
        return CliRunner().invoke(app, args)


def _host_ids(projects: list[Project]) -> list[str]:
    """The host ids ``otto host <TAB>`` completes over *projects*."""
    return cc.collect_host_ids(projects)


def _completion_drops(project: Project, projects: list[Project]) -> list[Any]:
    """The drops completion records for *project*'s hosts over *projects*."""
    resolution = cc.resolve_process_inventory(projects)
    return cc.project_host_drops(project, resolution, profiles=ProfileContext.empty())


def _completion_ips(project: Project, projects: list[Project]) -> dict[str, "str | None"]:
    """Each host completion summarizes for *project*, by id, to its address."""
    resolution = cc.resolve_process_inventory(projects)
    profiles = ProfileContext.from_projects(projects=projects)
    return {s.id: s.ip for s in cc.project_host_summaries(project, resolution, profiles=profiles)}


# --------------------------------------------------------------------------- #
# Third-party backends, registered as an init module would.
# --------------------------------------------------------------------------- #


class _TeamDbConfig(OttoModel, frozen=True):
    """``teamdb``'s options: the records it serves, key to address."""

    records: dict[str, str]


def _teamdb(configured: Any) -> FakeInventory:
    """A pre-phase-1 inventory backend: lookup and list only, no discovery."""
    records = configured.config.records
    label = "teamdb:" + ",".join(f"{k}={v}" for k, v in sorted(records.items()))
    return FakeInventory({k: {"ip": ip} for k, ip in records.items()}, label=label)


def _register_example(name: str) -> None:
    """Register the unchanged example lab-source backend (``SupportsHostSummaries``) as *name*."""
    from otto.examples.lab_repository import ExampleLabSourceConfig, example_lab_source
    from otto.labs import register_lab_repository

    register_lab_repository(name, config=ExampleLabSourceConfig, factory=example_lab_source)


class _LoadOnly:
    """A legacy lab source with ``list_labs`` and ``load_lab`` only: no summaries."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def list_labs(self) -> list[str]:
        return self._inner.list_labs()

    def load_lab(self, name, *, preferences=None, inventory=None):
        return self._inner.load_lab(name, preferences=preferences, inventory=inventory)


def _register_load_only(name: str) -> None:
    """Register a legacy backend *name* without summaries, over the example's options."""
    from otto.examples.lab_repository import ExampleLabSourceConfig, example_lab_source
    from otto.labs import register_lab_repository

    register_lab_repository(
        name,
        config=ExampleLabSourceConfig,
        factory=lambda configured: _LoadOnly(example_lab_source(configured)),
    )


# --------------------------------------------------------------------------- #
# The acceptance cases.
# --------------------------------------------------------------------------- #

_TEAMDB_DEFAULT = {"sw-1": "10.7.0.1", "sw-2": "10.7.0.2"}
_TEAMDB_NAMED = {"sw-1": "10.8.0.1", "sw-2": "10.8.0.2"}


def test_unchanged_third_party_lab_and_inventory_backends(tmp_path, home, monkeypatch):
    """A third-party lab source and inventory backend, both written before phase 1.

    ``teamdb`` is bound twice: as ``[inventory]`` (``default``) and as the
    ``[[inventories]]`` entry ``teamdb``, at other addresses. The example
    source's bare ``sw-1`` resolves through ``default``; a json lab's
    qualified ``teamdb/sw-1`` through the named entry.
    """
    from otto.examples.lab_repository import ExampleLabRepository
    from otto.inventory import build_inventory_view
    from otto.inventory.registry import register_inventory_backend
    from otto.labs.sources import build_lab_sources
    from otto.session import build_lab
    from otto.testing import assert_inventory_conforms, assert_lab_repository_conforms

    register_inventory_backend(
        "teamdb", config=_TeamDbConfig, factory=_teamdb, snapshot_cache=False
    )
    _register_example("acceptance-example")
    bench = [
        _el("switch", {"inventory": "sw-1", "os_type": "unix"}),
        _el("probe", {"ip": "10.9.0.1", "os_type": "unix"}),
    ]
    core = _lab_file(
        tmp_path / "core" / "lab.json",
        {"core": [_el("core-sw", {"inventory": {"name": "teamdb", "key": "sw-1"}})]},
    )
    settings = (
        _table("[inventory]", backend="teamdb", records=_TEAMDB_DEFAULT)
        + _table("[[inventories]]", name="teamdb", backend="teamdb", records=_TEAMDB_NAMED)
        + _table("[[lab.sources]]", backend="acceptance-example", labs={"bench": bench})
        + _table("[[lab.sources]]", backend="json", paths=[core])
    )
    project = _project(tmp_path / "sut", settings)
    projects = [project]

    listed = _cli(projects, ["--list-labs"], monkeypatch=monkeypatch)
    assert listed.exit_code == 0, listed.output
    assert "• bench" in listed.output
    assert "• core" in listed.output
    assert "unavailable" not in listed.output

    lab = build_lab(projects, labs=["bench", "core"])
    assert lab.hosts["switch"].ip == "10.7.0.1"
    switch_ref = lab.hosts["switch"].inventory_ref
    assert (switch_ref.name, switch_ref.key, switch_ref.qualified) == ("default", "sw-1", False)
    assert lab.hosts["core-sw"].ip == "10.8.0.1"
    core_ref = lab.hosts["core-sw"].inventory_ref
    assert (core_ref.name, core_ref.key, core_ref.qualified) == ("teamdb", "sw-1", True)
    assert lab.hosts["probe"].ip == "10.9.0.1"

    ids = _host_ids(projects)
    assert {"switch", "probe", "core-sw"} <= set(ids)
    assert _completion_ips(project, projects) == {
        "switch": "10.7.0.1",
        "probe": "10.9.0.1",
        "core-sw": "10.8.0.1",
    }

    view = build_inventory_view(projects=projects)
    assert sorted(view.names()) == ["default", "teamdb"]
    # The source the registered factory built from the project's settings,
    # not a hand-made twin: conformance must judge what otto actually runs.
    composite = build_lab_sources(projects=projects)
    [example] = [s.repository for s in composite.sources if "/acceptance-example#" in s.label]
    assert isinstance(example, ExampleLabRepository)
    assert_lab_repository_conforms(
        example,
        expected_labs=["bench"],
        expect_host_summaries=True,
        inventory=view.inventories["default"],
        expected_ips={"switch": "10.7.0.1", "probe": "10.9.0.1"},
    )
    assert_inventory_conforms(view.inventories["default"], expected_keys=["sw-1", "sw-2"])
    assert_inventory_conforms(view.inventories["teamdb"], expected_keys=["sw-1", "sw-2"])


_EDGE = {"edge-17": {"ip": "10.4.0.17", "creds": _CREDS}}


@pytest.mark.parametrize("route", ["summaries", "fallback"])
def test_a_registered_legacy_backend_under_d1(tmp_path, home, monkeypatch, route):
    """D1 through a legacy lab source: the user's ``default`` is never consulted.

    The user file binds ``default`` to a counting inventory holding
    ``edge-17``. The project declares ``[[inventories]] physical`` without
    binding ``default``, so its bare ``edge-17`` is refused: the load fails
    with the D1 diagnostic, and completion (the source's own summaries, or
    the fallback that loads each lab) records it as exactly one drop while
    the inline sibling still completes. ``default`` serves no lookup.
    """
    from otto.inventory import BareReferenceRefused, bare_refusal_message
    from otto.session import build_lab

    counting = register_lookup_counting(monkeypatch, records=_EDGE)
    _write_user(home, _table("[inventory]", backend="fake:counting"))
    backend = f"acceptance-legacy-{route}"
    (_register_example if route == "summaries" else _register_load_only)(backend)
    physical = _records(tmp_path / "physical.json", {"edge-17": {"ip": "10.5.0.17"}})
    edge = [
        _el("edge", {"inventory": "edge-17", "os_type": "unix"}),
        _el("probe", {"ip": "10.9.0.1", "os_type": "unix"}),
    ]
    settings = _table("[[inventories]]", name="physical", backend="json", path=physical) + _table(
        "[[lab.sources]]", backend=backend, labs={"edge": edge}
    )
    project = _project(tmp_path / "sut", settings)
    projects = [project]
    d1 = bare_refusal_message(
        origin=str(project.root / ".otto" / "settings.toml"),
        key="edge-17",
        declared=["default", "physical"],
    )

    with pytest.raises(BareReferenceRefused) as refused:
        build_lab(projects, labs=["edge"])
    assert str(refused.value) == d1
    assert counting.lookups == 0

    drops = _completion_drops(project, projects)
    assert [d.reason for d in drops] == [d1]
    ids = _host_ids(projects)
    assert "edge" not in ids
    # The source's own summaries skip the refused host; the fallback loads
    # each lab, so the refusal omits the lab whole, its inline host with it.
    assert ("probe" in ids) is (route == "summaries")
    assert counting.lookups == 0


def test_two_inventories_one_lab(tmp_path, home, monkeypatch):
    """One lab, its DUT from ``physical`` and its traffic generator from ``virtual``.

    Both inventories hold key ``k`` at their own address, so every answer
    names the inventory it came from.
    """
    from otto.init.doctor import InventoryLabel, check_project
    from otto.inventory import deferred_inventory_view
    from otto.session import build_lab

    physical = _records(tmp_path / "physical.json", {"k": {"ip": "10.1.0.1"}})
    virtual = _records(tmp_path / "virtual.json", {"k": {"ip": "10.2.0.1"}})
    lab_dir = _lab_file(
        tmp_path / "sut" / "lab" / "lab.json",
        {
            "bench": [
                _el("dut", {"inventory": {"name": "physical", "key": "k"}, "os_type": "unix"}),
                _el("tgen", {"inventory": {"name": "virtual", "key": "k"}, "os_type": "unix"}),
            ]
        },
    )
    settings = (
        _table("[[inventories]]", name="physical", backend="json", path=physical)
        + _table("[[inventories]]", name="virtual", backend="json", path=virtual)
        + _table("[[lab.sources]]", backend="json", paths=[lab_dir])
    )
    project = _project(tmp_path / "sut", settings)
    projects = [project]

    lab = build_lab(projects, labs=["bench"])
    assert (lab.hosts["dut"].ip, lab.hosts["tgen"].ip) == ("10.1.0.1", "10.2.0.1")
    assert lab.hosts["dut"].inventory_ref.name == "physical"
    assert lab.hosts["tgen"].inventory_ref.name == "virtual"

    for name, path, ip, other in (
        ("physical", physical, "10.1.0.1", "10.2.0.1"),
        ("virtual", virtual, "10.2.0.1", "10.1.0.1"),
    ):
        looked = _cli(
            projects, ["inventory", "lookup", "k", "--inventory", name], monkeypatch=monkeypatch
        )
        assert looked.exit_code == 0, looked.output
        assert f"backend:  json:{path}" in looked.output
        assert ip in looked.output
        assert other not in looked.output

    report = check_project(project.root, catalog=deferred_inventory_view(projects=projects))
    assert report.ok, [v.problems for v in report.verdicts]
    assert report.inventories == [
        InventoryLabel(name="physical", label=f"json:{physical}", creds_label=None),
        InventoryLabel(name="virtual", label=f"json:{virtual}", creds_label=None),
    ]
    assert not [w for w in report.warnings if "referenced by no lab file" in w]

    assert {"dut", "tgen"} <= set(_host_ids(projects))
    assert _completion_ips(project, projects) == {"dut": "10.1.0.1", "tgen": "10.2.0.1"}
    assert _completion_drops(project, projects) == []


def _reordered_project(root: Path, data: Path, *, order: str) -> Project:
    """A project over the shared files in *data*, its declarations in *order*.

    ``forward`` declares ``physical`` then ``virtual`` and source ``west``
    then ``east``; ``reverse`` the opposite. The sources hold disjoint labs
    and hosts, so no source restates another's element.
    """
    inventories = [
        _table("[[inventories]]", name=name, backend="json", path=str(data / f"{name}.json"))
        for name in ("physical", "virtual")
    ]
    sources = [
        _table("[[lab.sources]]", backend="json", paths=[str(data / side)])
        for side in ("west", "east")
    ]
    if order == "reverse":
        inventories.reverse()
        sources.reverse()
    default = _table("[inventory]", backend="json", path=str(data / "default.json"))
    return _project(root, default + "".join(inventories) + "".join(sources))


def test_reordering_declarations_changes_nothing(tmp_path, home):
    """Permuting ``[[inventories]]`` and non-overlapping sources builds the same labs."""
    from otto.session import build_lab

    data = tmp_path / "data"
    _records(data / "default.json", {"k": {"ip": "10.0.0.1"}})
    _records(data / "physical.json", {"k": {"ip": "10.1.0.1"}})
    _records(data / "virtual.json", {"k": {"ip": "10.2.0.1"}})
    _lab_file(
        data / "west" / "lab.json",
        {
            "west": [
                _el("w-bare", {"inventory": "k"}),
                _el("w-phys", {"inventory": {"name": "physical", "key": "k"}}),
            ]
        },
    )
    _lab_file(
        data / "east" / "lab.json",
        {
            "east": [
                _el("e-virt", {"inventory": {"name": "virtual", "key": "k"}}),
                _el("e-inline", {"ip": "10.3.0.1"}),
            ]
        },
    )

    built = {}
    for order in ("forward", "reverse"):
        project = _reordered_project(tmp_path / order, data, order=order)
        built[order] = {name: _lab(build_lab([project], labs=[name])) for name in ("west", "east")}
        built[order]["both"] = _lab(build_lab([project], labs=["west", "east"]))
    assert built["forward"] == built["reverse"]
    ips = {h: v["ip"] for h, v in built["forward"]["both"]["hosts"].items() if "ip" in v}
    assert ips == {
        "w-bare": "10.0.0.1",
        "w-phys": "10.1.0.1",
        "e-virt": "10.2.0.1",
        "e-inline": "10.3.0.1",
    }


class _NoOptions(OttoModel, frozen=True):
    """The remote backend takes no options."""


def test_a_data_error_never_loads_an_old_address(tmp_path, home, monkeypatch):
    """An expired snapshot never stands in for a data error: the build fails and names it.

    The snapshot cache's clock is injected, so the snapshot is exactly two
    hours old when the second build asks.
    """
    import otto.inventory.cache as cache_module
    from otto.inventory import InventoryDataError
    from otto.inventory.registry import register_inventory_backend
    from otto.labs import LabRepositoryError
    from otto.session import build_lab

    remote = FakeDiscoveryInventory(
        {"dut-1": {"ip": "10.6.0.1"}}, supplies=["ip"], label="fake:remote"
    )
    register_inventory_backend(
        "acceptance-remote", config=_NoOptions, factory=lambda configured: remote
    )
    lab_dir = _lab_file(
        tmp_path / "sut" / "lab" / "lab.json",
        {"bench": [_el("dut", {"inventory": {"name": "remote", "key": "dut-1"}})]},
    )
    settings = _table(
        "[[inventories]]", name="remote", backend="acceptance-remote", cache_ttl="1h"
    ) + _table("[[lab.sources]]", backend="json", paths=[lab_dir])
    projects = [_project(tmp_path / "sut", settings)]

    fetched = datetime(2026, 8, 27, 9, 14, tzinfo=timezone.utc)
    now = [fetched]
    monkeypatch.setattr(cache_module, "_utc_now", lambda: now[0])
    assert build_lab(projects, labs=["bench"]).hosts["dut"].ip == "10.6.0.1"
    now[0] = fetched + timedelta(hours=2)
    remote.fail = "data"

    with pytest.raises(LabRepositoryError) as refused:
        build_lab(projects, labs=["bench"])
    refusal = refused.value.__cause__
    assert isinstance(refusal, InventoryDataError)
    assert refusal.__cause__ is remote.raised
    assert str(refusal) in str(refused.value)
    assert str(refusal).startswith(
        "inventory 'fake:remote' returned unusable data "
        "(fake:remote: field 'ip' missing from the output); the cached snapshot (from "
    )
    assert "(from 2026-08-27 09:14 UTC, 2h old) is not served for a data error" in str(refusal)
    assert str(refusal).endswith("then run `otto inventory refresh --inventory remote`")
    assert "10.6.0.1" not in str(refused.value)

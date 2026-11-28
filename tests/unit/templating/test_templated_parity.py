"""The doctor, ``--list-labs`` and dispatch see a templated lab alike (spec section 6.4).

The ``otto init`` doctor renders nothing itself: for a project that is one of
the active catalog's projects it lists the labs with ``--list-labs``' own
function (:func:`~otto.labs.sources.lab_listing`) and loads each with
``--lab``'s (:func:`~otto.session.build_lab`), both through the catalog, so
one doctor pass is one operation over one view. Each test drives the doctor
and the dispatch route with one input and compares what they say.
"""

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from otto.cli.main import _lab_panels
from otto.config.project import Project
from otto.labs import LabRepositoryError
from otto.labs.sources import lab_listing
from otto.session import LabBuildError, build_lab

from ._doctor_project import (
    SITES,
    catalog,
    check,
    entry_path,
    lab_problems,
    records,
    sites_entry,
    templated,
)
from ._lab_project import CREDS, inventory, jsonnet_source, project, table, write
from ._render_helpers import count_evaluations


def _dispatch_error(projects: "list[Project]", name: str) -> str:
    """The text ``otto --lab <name>`` refuses with, built the way dispatch builds it."""
    with pytest.raises((LabBuildError, LabRepositoryError)) as caught:
        build_lab(projects, labs=[name])
    return str(caught.value)


def _panel_text(projects: "list[Project]") -> list[str]:
    """Each project's ``--list-labs`` panel, as plain text."""
    return [panel.renderable.plain for panel in _lab_panels(projects)]


def _recording_build_lab(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Record each call into :func:`~otto.session.lab.build_lab` and the lab it returned."""
    from otto.session import lab as session_lab

    calls: list[dict[str, Any]] = []
    real = session_lab.build_lab

    def recording(projects: Any, *, labs: Any, view: Any = None, **kw: Any) -> Any:
        call: dict[str, Any] = {"projects": projects, "labs": labs, "view": view}
        calls.append(call)
        call["lab"] = real(projects, labs=labs, view=view, **kw)
        return call["lab"]

    monkeypatch.setattr(session_lab, "build_lab", recording)
    return calls


# --------------------------------------------------------------------------- #
# The rows come from dispatch's own calls.
# --------------------------------------------------------------------------- #


def test_templating_rows_come_from_the_dispatch_calls(tmp_path, otto_home, monkeypatch):
    from otto.labs import sources
    from otto.session import lab as session_lab

    other = project(tmp_path / "other", "", name="other")
    sut = templated(tmp_path / "sut", sites=("east",), entry=sites_entry(os_type="'nosuch'"))
    view = catalog(other, sut)
    listings: list[dict[str, Any]] = []
    real_listing = sources.lab_listing

    def spy_listing(*, projects: Any, view: Any) -> Any:
        listings.append({"projects": projects, "view": view})
        return real_listing(projects=projects, view=view)

    loads: list[dict[str, Any]] = []
    real_build = session_lab.build_lab

    def spy_build(projects: Any, *, labs: Any, view: Any = None, **kw: Any) -> Any:
        loads.append({"projects": projects, "labs": labs, "view": view})
        return real_build(projects, labs=labs, view=view, **kw)

    monkeypatch.setattr(sources, "lab_listing", spy_listing)
    monkeypatch.setattr(session_lab, "build_lab", spy_build)
    report = check(sut.root, view)
    assert [(c["projects"], c["view"]) for c in listings] == [([other, sut], view)]
    assert [(c["projects"], c["labs"], c["view"]) for c in loads] == [
        ([other, sut], ["site-east"], view)
    ]
    monkeypatch.setattr(session_lab, "build_lab", real_build)
    assert lab_problems(report) == [_dispatch_error([other, sut], "site-east")]


def test_doctor_load_equals_dispatch_with_selection_wide_profiles(tmp_path, otto_home):
    """A host whose ``os_type`` only another project's data profile defines loads in the pass."""
    profiled = project(tmp_path / "a", '[os_profiles.alpha]\nbase = "unix"\n', name="proj-a")
    sut = templated(tmp_path / "b", name="proj-b", entry=sites_entry(os_type="'alpha'"))
    report = check(sut.root, catalog(profiled, sut))
    assert lab_problems(report) == []
    assert build_lab([profiled, sut], labs=["site-east"]).hosts["east-dut0"].os_type == "alpha"
    # With the project's own profiles alone the same lab is refused: the
    # doctor's clean row is the selection's answer, not the project's.
    alone = _dispatch_error([sut], "site-east")
    assert "Unknown os_type profile 'alpha'" in alone
    assert lab_problems(check(sut.root, catalog(sut))) == [alone]


def test_doctor_load_applies_host_preferences(tmp_path, otto_home, monkeypatch):
    prefs = '[host_preferences."east-.*"]\nterm = ["telnet"]\n'
    sut = templated(tmp_path / "sut", extra=prefs)
    calls = _recording_build_lab(monkeypatch)
    report = check(sut.root, catalog(sut))
    assert lab_problems(report) == []
    [call] = calls
    assert call["lab"].hosts["east-dut0"].term == "telnet"
    monkeypatch.undo()
    assert build_lab([sut], labs=["site-east"]).hosts["east-dut0"].term == "telnet"
    plain = templated(tmp_path / "plain")
    assert build_lab([plain], labs=["site-east"]).hosts["east-dut0"].term != "telnet"


def _broken_unscoped(root: Path, *, name: str) -> Project:
    """A sibling whose unscoped jsonnet source, reading no inventory, fails every status call."""
    write(entry_path(root), "function(otto) error 'sibling is broken'\n")
    return project(root, jsonnet_source(paths=[str(entry_path(root))]), name=name)


def test_healthy_templated_root_beside_a_failing_unscoped_sibling(tmp_path, otto_home):
    sut = templated(tmp_path / "sut", sites=("east", "west"))
    sibling = _broken_unscoped(tmp_path / "sib", name="sib")
    view = catalog(sut, sibling)
    listing = lab_listing(projects=[sut, sibling], view=view)
    assert listing.report is None
    own = listing.outcome(root=sut.root)
    assert not isinstance(own, Exception)
    assert own.labs_declared_in(project_root=sut.root) == ["site-east", "site-west"]
    assert isinstance(listing.outcome(root=sibling.root), LabRepositoryError)
    panels = _panel_text([sut, sibling])
    assert panels[0] == "• site-east\n• site-west"
    assert panels[1].startswith("⚠ host source unavailable: ")
    assert "sibling is broken" in panels[1]
    # Listing recovered; loading reads every source, so each load fails as
    # `otto --lab` does, a load failure and never a listing one. Both loads
    # fail with the one sibling error, which is one row.
    refusal = _dispatch_error([sut, sibling], "site-east")
    assert _dispatch_error([sut, sibling], "site-west") == refusal
    assert "sibling is broken" in refusal
    assert lab_problems(check(sut.root, view)) == [refusal]


def test_source_construction_failure_is_one_row(tmp_path, otto_home):
    """The root's own source does not construct: one row, the panel's error, no exception.

    The doctor's own preparation check (unchanged) refuses the source before
    any templating row is asked for, so the error is reported once, in the
    words the panel shows after its ``host source unavailable:`` lead.
    """
    sut = templated(tmp_path / "sut", import_paths=[""])
    report = check(sut.root, catalog(sut))
    [panel] = _panel_text([sut])
    [row] = lab_problems(report)
    assert panel == f"⚠ host source unavailable: {row}"
    assert "an entry is empty" in row


def test_list_labs_output_unchanged(tmp_path):
    """The listing moved into :func:`lab_listing`; every golden panel is byte for byte today's."""
    from tests.unit.labs import test_unchanged_loading as pins

    assert pins.listing(tmp_path).encode() == pins.LISTING_GOLDEN.read_bytes()


# --------------------------------------------------------------------------- #
# One pass, one view: one evaluation, a pinned result.
# --------------------------------------------------------------------------- #


def test_one_doctor_pass_evaluates_the_entry_once(tmp_path, otto_home, monkeypatch):
    sut = templated(tmp_path / "sut", sites=("east", "north", "west"))
    calls = _recording_build_lab(monkeypatch)
    evaluations = count_evaluations(monkeypatch)
    report = check(sut.root, catalog(sut))
    assert lab_problems(report) == []
    assert sorted(c["labs"][0] for c in calls) == ["site-east", "site-north", "site-west"]
    assert evaluations() == 1


def test_one_doctor_pass_reads_and_validates_the_source_once(tmp_path, otto_home, monkeypatch):
    """The listing and every lab's load share one validated read: the doctor is not N reads."""
    from otto.labs import jsonnet_source

    reads: list[Any] = []
    real = jsonnet_source._validated_read

    def counting(**kwargs: Any) -> Any:
        reads.append(kwargs["inputs"].env.label)
        return real(**kwargs)

    monkeypatch.setattr(jsonnet_source, "_validated_read", counting)
    sut = templated(tmp_path / "sut", sites=("east", "north", "west"))
    calls = _recording_build_lab(monkeypatch)
    assert lab_problems(check(sut.root, catalog(sut))) == []
    assert len(calls) == 3, "control: each lab was loaded"
    assert reads == ["sut/jsonnet#1"]
    check(sut.root, catalog(sut))
    assert len(reads) == 2, "the next operation reads again: a new view, a new memo"


def test_mid_pass_mutation_keeps_the_pinned_result(tmp_path, otto_home, monkeypatch):
    from otto.labs import sources

    sut = templated(tmp_path / "sut", sites=("east",))
    real = sources.lab_listing

    def then_edit(*, projects: Any, view: Any) -> Any:
        listing = real(projects=projects, view=view)
        entry_path(sut.root).write_text("function(otto) error 'edited mid-pass'\n")
        write(sut.root / "lab_data" / "physical.json", records("east", "west"))
        return listing

    monkeypatch.setattr(sources, "lab_listing", then_edit)
    calls = _recording_build_lab(monkeypatch)
    report = check(sut.root, catalog(sut))
    assert lab_problems(report) == []
    assert [sorted(c["lab"].hosts) for c in calls] == [["east-dut0", "local"]]
    monkeypatch.undo()
    fresh = check(sut.root, catalog(sut))
    [row] = lab_problems(fresh)
    assert "edited mid-pass" in row


def _broken_named(root: Path) -> Project:
    """A templated project whose ``virtual`` inventory table names no path."""
    broken = table("[[inventories]]", name="virtual", backend="json")
    return templated(root, extra=broken)


def test_broken_declaration_with_a_deferred_view_refuses_as_dispatch(tmp_path, otto_home):
    sut = _broken_named(tmp_path / "sut")
    with pytest.raises(LabBuildError) as eager:
        build_lab([sut], labs=["site-east"])
    with pytest.raises(LabBuildError) as deferred:
        build_lab([sut], labs=["site-east"], view=catalog(sut))
    assert eager.value.kind == deferred.value.kind == "inventory"
    assert str(eager.value) == str(deferred.value)
    # The doctor's own check reports the declaration; dispatch's refusal of
    # the load, with that same cause, is not repeated.
    assert lab_problems(check(sut.root, catalog(sut))) == [
        f"inventory: virtual: {eager.value.detail}"
    ]


def test_malformed_user_file_with_no_project_inventories_refuses_on_both_routes(
    tmp_path, otto_home
):
    (otto_home / "settings.toml").write_text(  # scaffold-exempt: malformed TOML under test
        "not toml [[["
    )
    lab = {
        "labs": {"bench": {}},
        "elements": [
            {"name": "e", "labs": ["bench"], "hosts": [{"ip": "10.0.0.1", "creds": CREDS}]}
        ],
    }
    write(tmp_path / "p" / "lab" / "lab.json", lab)
    sut = project(tmp_path / "p", table("[[lab.sources]]", backend="json", paths=["lab"]))
    view = catalog(sut)
    assert view.for_project(root=sut.root).names() == []
    with pytest.raises(LabBuildError) as eager:
        build_lab([sut], labs=["bench"])
    with pytest.raises(LabBuildError) as deferred:
        build_lab([sut], labs=["bench"], view=view)
    assert eager.value.kind == deferred.value.kind == "inventory"
    assert str(eager.value) == str(deferred.value)
    assert str(otto_home / "settings.toml") in str(eager.value)


def test_broken_default_and_broken_named_refuse_default_first_on_both_routes(tmp_path, otto_home):
    broken_default = '[inventory]\nbackend = "json"\n'
    sut = templated(
        tmp_path / "sut", extra=broken_default + table("[[inventories]]", name="z", backend="json")
    )
    with pytest.raises(LabBuildError) as eager:
        build_lab([sut], labs=["site-east"])
    with pytest.raises(LabBuildError) as deferred:
        build_lab([sut], labs=["site-east"], view=catalog(sut))
    assert str(eager.value) == str(deferred.value)
    settings = sut.root / ".otto" / "settings.toml"
    assert f"configured in {settings}): parse failed: path: Field required" in str(eager.value)
    assert "'z'" not in str(eager.value)


def test_require_eager_keeps_the_catalogs_instances(tmp_path, otto_home, monkeypatch):
    from otto.inventory import config

    sut = templated(tmp_path / "sut", extra=inventory(tmp_path / "sut", "spare", records("x")))
    view = catalog(sut)
    reads: list[Any] = []
    real_read = config._read_user_settings

    def counting(path: Any) -> Any:
        reads.append(path)
        return real_read(path)

    monkeypatch.setattr(config, "_read_user_settings", counting)
    config.require_eager(view)
    held = {name: view.inventories[name] for name in ("physical", "spare")}
    lab = build_lab([sut], labs=["site-east"], view=view)
    assert len(reads) == 1
    assert {name: view.inventories[name] for name in held} == held
    assert all(view.inventories[name] is held[name] for name in held)
    assert lab.hosts["east-dut0"].ip == "10.0.1.1"


def test_build_lab_with_a_view_that_is_not_deferred_uses_it_as_given(tmp_path, otto_home):
    from otto.inventory.config import build_inventory_view

    sut = templated(tmp_path / "sut")
    view = build_inventory_view(projects=[sut])
    assert build_lab([sut], labs=["site-east"], view=view).hosts["east-dut0"].ip == "10.0.1.1"


# --------------------------------------------------------------------------- #
# Parity: one input, the doctor and dispatch.
# --------------------------------------------------------------------------- #


def _one_source_parity(sut: Project, name: str, *projects: Project) -> None:
    """The doctor's one lab row for *sut* is dispatch's refusal of lab *name*."""
    selection = [*projects, sut]
    rows = lab_problems(check(sut.root, catalog(*selection)))
    panel = _panel_text(selection)[-1]
    assert rows, panel
    assert rows == [panel.removeprefix("⚠ ")]
    assert _dispatch_error(selection, name).endswith(
        rows[0].removeprefix("host source unavailable: ")
    )


def test_parity_generated_bare_reference_with_bound_default(tmp_path, otto_home):
    bare = """
    function(otto) {
      labs: { 'site-east': {} },
      elements: [{ name: 'east-dut0', labs: ['site-east'],
                   hosts: [{ inventory: 'dut-east', os_type: 'unix' }] }],
    }
    """
    default = table("[inventory]", backend="json", path=str(tmp_path / "sut" / "default.json"))
    write(tmp_path / "sut" / "default.json", records("east"))
    sut = templated(tmp_path / "sut", entry=bare, extra=default)
    _one_source_parity(sut, "site-east")
    assert "bare reference 'dut-east'" in lab_problems(check(sut.root, catalog(sut)))[0]


def test_parity_undeclared_inventory_name(tmp_path, otto_home):
    undeclared = """
    function(otto) {
      labs: { 'site-east': {} },
      elements: [{ name: 'east-dut0', labs: ['site-east'],
                   hosts: [{ inventory: { name: 'virtual', key: 'dut-east' }, os_type: 'unix' }] }],
    }
    """
    sut = templated(tmp_path / "sut", entry=undeclared)
    _one_source_parity(sut, "site-east")
    assert (
        "inventory 'virtual' is not one the source declares"
        in (lab_problems(check(sut.root, catalog(sut)))[0])
    )


def _one_host_entry(lab: str, element: str, **host: Any) -> str:
    """An entry declaring *lab* with one element *element* of one inline host *host*."""
    fields = {"os_type": "unix", "ip": "10.0.0.1", "creds": CREDS, **host}
    document = {
        "labs": {lab: {}},
        "elements": [{"name": element, "labs": [lab], "hosts": [fields]}],
    }
    return f"function(otto) {json.dumps(document)}\n"


def test_parity_canonical_collision_across_files(tmp_path, otto_home):
    root = tmp_path / "sut"
    layouts = entry_path(root).parent
    write(layouts / "a.jsonnet", _one_host_entry("bench", "dut", board="01"))
    write(layouts / "b.jsonnet", _one_host_entry("rig", "dut-01", ip="10.0.0.2"))
    sut = project(root, jsonnet_source(paths=[str(layouts / "*.jsonnet")]))
    _one_source_parity(sut, "bench")
    assert "composes host id 'dut-01'" in lab_problems(check(sut.root, catalog(sut)))[0]
    assert "one source, one host per id" in lab_problems(check(sut.root, catalog(sut)))[0]


def test_parity_out_of_scope_output(tmp_path, otto_home):
    sut = templated(tmp_path / "sut", scope={"prefixes": ["router-"]})
    rows = lab_problems(check(sut.root, catalog(sut)))
    [panel] = _panel_text([sut])
    assert rows == [line.removeprefix("⚠ ") for line in panel.splitlines()]
    assert "outside the source's scope" in rows[0]
    assert _dispatch_error([sut], "router-x").endswith(rows[0].split(" unavailable: ", 1)[1])


def test_parity_profile_defined_type_and_board_slot(tmp_path, otto_home, monkeypatch):
    profile = '[os_profiles.rack]\nbase = "unix"\nboard = "lc"\nslot = 2\n'
    sut = templated(tmp_path / "sut", entry=sites_entry(os_type="'rack'"), extra=profile)
    calls = _recording_build_lab(monkeypatch)
    assert lab_problems(check(sut.root, catalog(sut))) == []
    [call] = calls
    monkeypatch.undo()
    dispatched = build_lab([sut], labs=["site-east"])
    assert sorted(call["lab"].hosts) == sorted(dispatched.hosts) == ["east-dut0-lc-2", "local"]
    assert call["lab"].hosts["east-dut0-lc-2"].os_type == "rack"


# --------------------------------------------------------------------------- #
# --list-labs and dispatch agree on a templated lab.
# --------------------------------------------------------------------------- #


def _unavailable(root: Path) -> Project:
    """A project whose one lab needs two DUTs at a site that has one."""
    return templated(root, entry=SITES.replace("count: 1", "count: 2") % {"os_type": "'unix'"})


def test_list_labs_shows_a_templated_unavailable_lab_with_its_reason(tmp_path, otto_home):
    sut = _unavailable(tmp_path / "sut")
    reason = "dut: expected 2, found 1 (physical/dut-east)"
    assert _panel_text([sut]) == [f"• site-east — unavailable: {reason}"]
    # Declared unavailable: listed with its reason, and no doctor problem.
    report = check(sut.root, catalog(sut))
    assert report.verdict(name="lab").state == "ok"
    assert lab_problems(report) == []


def test_a_scoped_source_outage_is_one_problem_beside_its_recorded_labs(tmp_path, otto_home):
    """A failed scoped source whose recorded labs list unavailable: the source's one row."""
    sut = templated(tmp_path / "sut", sites=("east", "west"), scope={"prefixes": ["site-"]})
    assert lab_problems(check(sut.root, catalog(sut))) == []  # records site-east, site-west
    entry_path(sut.root).write_text("function(otto) error 'broke'\n")
    view = catalog(sut)
    report = lab_listing(projects=[sut], view=view).outcome(root=sut.root)
    assert not isinstance(report, Exception)
    assert [s.name for s in report.labs if s.unavailable is not None] == ["site-east", "site-west"]
    [row] = lab_problems(check(sut.root, catalog(sut)))
    assert row.startswith("source sut/jsonnet#1 unavailable: ")
    assert "broke" in row


def test_lab_dispatch_refuses_an_unavailable_templated_lab_naming_it(tmp_path, otto_home):
    sut = _unavailable(tmp_path / "sut")
    with pytest.raises(LabBuildError) as caught:
        build_lab([sut], labs=["site-east"])
    assert caught.value.kind == "unavailable_lab"
    assert "Lab 'site-east' is unavailable: dut: expected 2, found 1" in str(caught.value)
    assert str(entry_path(sut.root)) in str(caught.value)


def test_list_labs_and_dispatch_agree_on_a_snapshot_served_inventory(tmp_path, otto_home):
    from otto.inventory.protocol import FILLABLE_INVENTORY_FIELDS
    from otto.inventory.registry import register_inventory_backend
    from otto.models.base import OttoModel
    from tests._fixtures.named_inventory import FakeDiscoveryInventory

    class _NoKeys(OttoModel, frozen=True):
        pass

    held = {k: {f: v for f, v in r.items() if f != "creds"} for k, r in records("east").items()}
    fake = FakeDiscoveryInventory(
        held, supplies=sorted(FILLABLE_INVENTORY_FIELDS - {"creds"}), max_stale="7d"
    )
    register_inventory_backend("fake:disc", config=_NoKeys, factory=lambda c: fake)
    root = tmp_path / "sut"
    write(entry_path(root), sites_entry(os_type="'unix', creds: [{login: 'u', password: 'p'}]"))
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
    assert _panel_text([sut]) == ["• site-east"]
    assert sorted(build_lab([sut], labs=["site-east"]).hosts) == ["east-dut0", "local"]
    assert lab_problems(check(sut.root, catalog(sut))) == []


# --------------------------------------------------------------------------- #
# One templated read, and every dispatch error a row.
# --------------------------------------------------------------------------- #


def test_doctor_and_next_steps_read_one_templated_outcome(tmp_path, otto_home, monkeypatch):
    """Both callers go through :func:`templated_outcome` with one input and see one answer."""
    from otto.init import doctor
    from otto.init.next_steps import try_it_commands
    from otto.init.templates import EXAMPLE_LAB_NAME

    entry = sites_entry().replace("'site-' + i.id.site", f"'{EXAMPLE_LAB_NAME}'")
    sut = templated(tmp_path / "sut", entry=entry)
    view = catalog(sut)
    seen: list[Any] = []
    real = doctor.templated_outcome

    def recording(view: Any, *, root: Any, sources: Any) -> Any:
        seen.append(real(view, root=root, sources=sources))
        return seen[-1]

    monkeypatch.setattr(doctor, "templated_outcome", recording)
    assert lab_problems(check(sut.root, view)) == []
    commands = try_it_commands(sut.root, "kmodcov", "egcov", cwd=sut.root, view=view)
    assert commands == [f"otto --lab {EXAMPLE_LAB_NAME} --list-hosts"]
    [from_doctor, from_next_steps] = seen
    assert from_doctor.membership == from_next_steps.membership
    assert from_doctor.membership.project is sut
    assert from_doctor.statuses == from_next_steps.statuses
    assert [s.name for s in from_doctor.statuses] == [EXAMPLE_LAB_NAME]


def _register_lab_backend(name: str, repository: Any) -> None:
    """Register lab-source backend *name*, every build returning *repository*."""
    from otto.labs import register_lab_repository
    from otto.models.base import OttoModel

    class NoKeys(OttoModel):
        pass

    register_lab_repository(name, config=NoKeys, factory=lambda c: repository)


def test_a_raising_third_party_source_is_a_row_never_a_traceback(tmp_path, otto_home):
    class Raising:
        def list_labs(self) -> list[str]:
            raise RuntimeError("backend exploded")

        def load_lab(self, name: str, **_: Any) -> Any:
            raise AssertionError("never loaded")

    _register_lab_backend("raising", Raising())
    sut = templated(tmp_path / "sut")
    sibling = project(tmp_path / "sib", table("[[lab.sources]]", backend="raising"), name="sib")
    with pytest.raises(RuntimeError, match="backend exploded"):
        lab_listing(projects=[sut, sibling], view=catalog(sut, sibling))
    report = check(sut.root, catalog(sut, sibling))
    assert lab_problems(report) == ["host source unavailable: RuntimeError: backend exploded"]


def test_an_inventory_error_from_the_load_is_a_row_never_a_traceback(tmp_path, otto_home):
    from otto.inventory import InventoryError

    class DownAtLoad:
        def list_labs(self) -> list[str]:
            return ["site-east"]

        def load_lab(self, name: str, **_: Any) -> Any:
            raise InventoryError("inventory backend down")

    _register_lab_backend("down-at-load", DownAtLoad())
    sut = templated(tmp_path / "sut")
    sibling = project(
        tmp_path / "sib", table("[[lab.sources]]", backend="down-at-load"), name="sib"
    )
    with pytest.raises(InventoryError, match="inventory backend down"):
        build_lab([sut, sibling], labs=["site-east"])
    report = check(sut.root, catalog(sut, sibling))
    assert lab_problems(report) == ["inventory backend down"]


@pytest.mark.parametrize("scoped", [False, True], ids=["unscoped", "scoped"])
def test_a_cancelled_jsonnet_read_is_never_a_doctor_row(scoped, tmp_path, otto_home):
    """The built-in source's cancelled read, however it is wrapped, is the operation's, never a row.

    Unscoped, the read raises a ``LabRepositoryError`` from the cancellation;
    scoped, the report keeps a timed-out outage. Neither is a row.
    """
    from otto.labs.drops import is_timeout

    source = {"scope": {"prefixes": ["site-"]}} if scoped else {}
    sut = templated(tmp_path / "sut", **source)
    view = catalog(sut)
    view.render_memo.cancel(scope=sut.root, reason="the completion deadline passed")
    with pytest.raises(Exception, match="the completion deadline passed") as caught:
        check(sut.root, view)
    assert is_timeout(caught.value), repr(caught.value)


def test_a_row_is_dropped_only_when_its_cause_is_a_reported_error():
    """A distinct problem that merely contains a reported error's text is still shown (N1)."""
    from otto.init.doctor import _Row

    reported = {"connection refused"}
    assert _Row(cause="connection refused", text="t").caused_by(reported)
    entry = "/p/lab_data/layouts/sites.jsonnet"
    assert _Row(
        cause=f"{entry}: InventoryUnreachableError: connection refused", text="t"
    ).caused_by(reported)
    distinct = _Row(cause="source routers unavailable: connection refused (10.0.0.9:443)", text="t")
    assert not distinct.caused_by(reported)
    assert not _Row(cause="connection refused twice", text="t").caused_by(reported)
    assert not _Row(cause="anything", text="t").caused_by({""})


@pytest.mark.parametrize("at", ["list", "load"])
def test_a_cancelled_render_is_never_a_doctor_row(at, tmp_path, otto_home):
    """The doctor's catch-alls let a cancellation through: it is the operation's, not a row."""
    from otto.templating.errors import RenderCancelled

    class Cancelling:
        def list_labs(self) -> list[str]:
            if at == "list":
                raise RenderCancelled("the operation was cancelled")
            return ["site-east"]

        def load_lab(self, name: str, **_: Any) -> Any:
            raise RenderCancelled("the operation was cancelled")

    _register_lab_backend(f"cancelling-{at}", Cancelling())
    sut = templated(tmp_path / "sut")
    sibling = project(
        tmp_path / "sib", table("[[lab.sources]]", backend=f"cancelling-{at}"), name="sib"
    )
    with pytest.raises(RenderCancelled, match="the operation was cancelled"):
        check(sut.root, catalog(sut, sibling))

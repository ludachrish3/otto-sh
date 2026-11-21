"""The built-in ``jsonnet`` lab source: rendered entries read as hand-written lab files.

Most tests write a real project (``_lab_project``) and drive it the way an
operation does: one host source and one inventory view. The render memo's
and the render path's own rules are driven directly through
:func:`~otto.labs.documents.render_outcome` and
:func:`~otto.labs.documents.read_lab_document`, over a view built by hand.
"""

import dataclasses
import json
import logging
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from otto.host.os_profile import ProfileContext
from otto.inventory.config import ProtectedPaths
from otto.labs import LabRepositoryError
from otto.labs.documents import read_lab_document, render_outcome
from otto.labs.jsonnet_source import (
    JsonnetLabSourceConfig,
    JsonnetSourceInputs,
    LabScopeSpec,
    _JsonnetLabRepository,
)
from otto.labs.registry import LAB_REPOSITORIES, register_lab_repository
from otto.labs.sources import JSONNET_FILE_RULE, LabSourceEnv, prepared_lab_sources
from otto.templating.errors import RenderCancelled
from otto.templating.freshness import rendering_dependencies
from otto.templating.imports import library_file
from otto.templating.memo import cancellation
from tests._fixtures.named_inventory import FakeDiscoveryInventory
from tests._fixtures.registrant import from_module
from tests.e2e._otto_subprocess import CHECKOUT_ROOT, coverage_subprocess_env

from ._lab_project import (
    CREDS,
    inventory,
    json_source,
    jsonnet_source,
    operation,
    project,
    write,
)
from ._render_helpers import catalogued_view, count_evaluations

WAIT = 30.0
"""How long a test waits for a worker thread; never reached when the code is right."""

ROUTER = """
local o = import 'otto.libsonnet';
function(otto)
  o.layout({
    instances: o.groupBy(otto.inventories.physical, ['site', 'rack']),
    lab(i): 'router-lab.' + i.id.site + '-' + i.id.rack,
    elementName(i, role, record, n): i.id.site + '-' + i.id.rack + '-' + role + n,
    roles: {
      dut: {
        match: o.inGroup('dut'), count: 1, host: { label: 'dut', fn(r): { os_type: 'unix' } },
      },
    },
  })
"""
"""One router lab per site and rack, each with one DUT from ``physical``."""

PHYSICAL = {
    "edge-17": {"ip": "10.0.0.17", "site": "east", "rack": "r7", "groups": ["dut"], "creds": CREDS},
    "edge-18": {"ip": "10.0.0.18", "site": "west", "rack": "r8", "groups": ["dut"], "creds": CREDS},
}


def _layouts(root: Path) -> Path:
    return root / "lab_data" / "layouts"


def router_project(
    root: Path,
    *,
    entries: "dict[str, str] | None" = None,
    records: "dict[str, dict[str, Any]] | None" = None,
    name: str = "sut",
    extra: str = "",
    inventory_keys: "dict[str, Any] | None" = None,
    **source: Any,
):
    """A project rendering *entries* (``ROUTER`` by default) over its ``physical`` inventory.

    *inventory_keys* go into the ``physical`` table, *extra* is more settings,
    and *source* is more of the jsonnet source's options.
    """
    for file, text in (entries or {"router.jsonnet": ROUTER}).items():
        write(_layouts(root) / file, text)
    source.setdefault("inventories", ["physical"])
    settings = (
        inventory(
            root, "physical", PHYSICAL if records is None else records, **(inventory_keys or {})
        )
        + extra
        + jsonnet_source(paths=[str(_layouts(root) / "*.jsonnet")], **source)
    )
    return project(root, settings, name=name)


def jsonnet_repository(op: Any) -> _JsonnetLabRepository:
    """The one jsonnet source of *op*'s host source."""
    [found] = [s.repository for s in op.repository.sources if "/jsonnet#" in s.label]
    assert isinstance(found, _JsonnetLabRepository)
    return found


# --------------------------------------------------------------------------- #
# Hand-built inputs, for the render path's own rules.
# --------------------------------------------------------------------------- #


def _env(root: Path, *, project_name: "str | None" = "sut") -> LabSourceEnv:
    return LabSourceEnv(
        project_root=root,
        label="sut/jsonnet#1",
        origin=str(root / ".otto" / "settings.toml"),
        profiles=ProfileContext.empty(),
        project_name=project_name,
    )


def _source_inputs(root: Path, **options: Any) -> JsonnetSourceInputs:
    options.setdefault("paths", [str(root / "*.jsonnet")])
    env = _env(root)
    config = JsonnetLabSourceConfig.model_validate(options, context={"env": env})
    return JsonnetSourceInputs(config=config, env=env)


def _entry(root: Path, text: str, name: str = "lab.jsonnet") -> Path:
    return write(root / name, text)


SIMPLE = "function(otto) { labs: { ['lab-' + otto.project]: {} } }"
"""An entry that declares one lab, named from its project."""


# --------------------------------------------------------------------------- #
# One evaluation per entry per operation.
# --------------------------------------------------------------------------- #


def test_one_evaluation_per_entry_per_operation(monkeypatch, tmp_path, otto_home):
    sut = router_project(tmp_path / "sut")
    op = operation([sut])
    n = count_evaluations(monkeypatch)
    report = op.repository.lab_status_report(view=op.view)
    assert [s.name for s in report.labs] == ["router-lab.east-r7", "router-lab.west-r8"]
    lab = op.repository.load_lab_reported(
        "router-lab.east-r7", preferences=None, view=op.view, report=report
    )
    assert sorted(lab.hosts) == ["east-r7-dut0"]
    summaries = op.repository.list_host_summaries_in_view(view=op.view)
    assert {s.id for s in summaries} == {"east-r7-dut0", "west-r8-dut0"}
    links = jsonnet_repository(op).declared_link_entries(view=op.view.for_project(root=sut.root))
    assert links == []
    assert n() == 1


def test_an_edit_mid_operation_is_not_seen_until_the_next(monkeypatch, tmp_path, otto_home):
    entry = _entry(tmp_path, "function(otto) { labs: { before: {} } }")
    inputs = _source_inputs(tmp_path)
    view = catalogued_view(inventories={})
    first = read_lab_document(entry, render=inputs.render_inputs(view))
    entry.write_text("function(otto) { labs: { after: {} } }")
    again = read_lab_document(entry, render=inputs.render_inputs(view))
    assert list(first.sections["labs"]) == list(again.sections["labs"]) == ["before"]
    later = read_lab_document(entry, render=inputs.render_inputs(catalogued_view(inventories={})))
    assert list(later.sections["labs"]) == ["after"]


def test_two_projects_sharing_an_entry_render_separately(monkeypatch, tmp_path, otto_home):
    shared = _entry(tmp_path / "shared", SIMPLE)
    projects = [
        project(tmp_path / name, jsonnet_source(paths=[str(shared)]), name=name)
        for name in ("alpha", "beta")
    ]
    op = operation(projects)
    n = count_evaluations(monkeypatch)
    report = op.repository.lab_status_report(view=op.view)
    assert [s.name for s in report.labs] == ["lab-alpha", "lab-beta"]
    assert n() == 2


def test_opaque_inventory_across_two_operations_renders_twice(monkeypatch, tmp_path):
    opaque = FakeDiscoveryInventory({"edge-17": {"ip": "10.0.0.17"}})
    assert opaque.fingerprint() is None
    entry = _entry(tmp_path, "function(otto) { labs: { [r.key]: {} for r in otto.inventories.x } }")
    inputs = _source_inputs(tmp_path, inventories=["x"])
    n = count_evaluations(monkeypatch)
    for _ in range(2):
        view = catalogued_view(inventories={"x": opaque})
        for _ in range(2):  # two readers of one operation
            assert list(
                read_lab_document(entry, render=inputs.render_inputs(view)).sections["labs"]
            )
    assert n() == 2
    assert opaque.fetches == 2


def test_a_failed_input_discovery_is_settled_once_naming_the_entry(monkeypatch, tmp_path):
    """An InventoryError building the ``otto`` argument is the key's error: no reader retries it."""
    down = FakeDiscoveryInventory({"edge-17": {"ip": "10.0.0.17"}}, fail="unreachable")
    entry = _entry(tmp_path, "function(otto) { labs: { [r.key]: {} for r in otto.inventories.x } }")
    view = catalogued_view(inventories={"x": down})
    render = _source_inputs(tmp_path, inventories=["x"]).render_inputs(view)
    n = count_evaluations(monkeypatch)
    outcomes = [render_outcome(entry, render) for _ in range(3)]  # status, summaries, links
    assert down.fetches == 1, "a later reader retried the failed discovery"
    assert n() == 0
    assert len({id(o) for o in outcomes}) == 1, "one settled outcome"
    [outcome, *_] = outcomes
    assert outcome.result is None
    assert not outcome.timed_out
    assert down.raised is not None
    error = str(outcome.error)
    assert error.startswith(f"{entry}: {type(down.raised).__name__}: "), error
    with pytest.raises(LabRepositoryError, match=type(down.raised).__name__):
        read_lab_document(entry, render=render)
    assert down.fetches == 1


def test_a_hand_built_view_renders_nothing(monkeypatch, tmp_path):
    """A view no builder made has no catalog (NO_CATALOG): refused before anything runs."""
    from otto.inventory import InventoryView

    opaque = FakeDiscoveryInventory({"edge-17": {"ip": "10.0.0.17"}})
    entry = _entry(tmp_path, SIMPLE)
    n = count_evaluations(monkeypatch)
    for view in (
        InventoryView(inventories={"x": opaque}),
        InventoryView.of_default(inventory=opaque),
    ):
        render = _source_inputs(tmp_path, inventories=["x"]).render_inputs(view)
        outcome = render_outcome(entry, render)
        assert outcome.result is None
        assert "no protected-file catalog" in str(outcome.error)
        with pytest.raises(LabRepositoryError, match="no protected-file catalog"):
            read_lab_document(entry, render=render)
    assert n() == 0
    assert opaque.fetches == 0, "the inputs were built under no catalog"


def test_changed_protected_set_is_a_different_render(monkeypatch, tmp_path):
    entry = _entry(tmp_path, SIMPLE)
    inputs = _source_inputs(tmp_path)
    view = catalogued_view(inventories={})
    guarded = dataclasses.replace(
        view, protected=lambda: ProtectedPaths(files=frozenset({tmp_path / "creds.json"}))
    )
    assert guarded.render_memo is view.render_memo  # one operation
    n = count_evaluations(monkeypatch)
    for v in (view, guarded, view, guarded):
        assert render_outcome(entry, inputs.render_inputs(v)).result is not None
    assert n() == 2


def test_policy_and_key_share_one_resolved_catalog(monkeypatch, tmp_path):
    from otto.templating import memo, render

    entry = _entry(tmp_path, SIMPLE)
    calls: list[ProtectedPaths] = []

    def provider() -> ProtectedPaths:
        calls.append(ProtectedPaths(files=frozenset({tmp_path / f"settings-{len(calls)}.toml"})))
        return calls[-1]

    view = dataclasses.replace(catalogued_view(inventories={}), protected=provider)
    keys: list[Any] = []
    policies: list[Any] = []
    real_get, real_render = memo.RenderMemo.get_or_render, render.render

    def spy_get(self: Any, key: Any, run: Any) -> Any:
        keys.append(key)
        return real_get(self, key, run)

    def spy_render(request: Any) -> Any:
        policies.append(request.policy)
        return real_render(request)

    monkeypatch.setattr(memo.RenderMemo, "get_or_render", spy_get)
    monkeypatch.setattr(render, "render", spy_render)
    read_lab_document(entry, render=_source_inputs(tmp_path).render_inputs(view))
    assert len(calls) == 1
    [key], [policy] = keys, policies
    assert key.protected is policy.protected is calls[0]


def test_render_entry_and_load_share_one_render_outcome(monkeypatch, tmp_path, otto_home):
    """Every render key is built by ``render_key``, and every import policy in ``render_outcome``.

    ``otto lab render``'s :func:`~otto.labs.inspect.render_entry` builds its
    key and policy through the same functions. The load reads the source's
    read the report kept, so it renders nothing more.
    """
    from otto.labs import documents
    from otto.labs.inspect import render_entry
    from otto.templating import imports, memo

    depth = {"render_key": 0, "render_outcome": 0}
    outcomes: list[Path] = []
    built: list[tuple[str, bool]] = []

    def inside(name: str) -> Callable[..., Any]:
        real = getattr(documents, name)

        def spy(entry: Path, *args: Any, **kwargs: Any) -> Any:
            if name == "render_outcome":
                outcomes.append(entry)
            depth[name] += 1
            try:
                return real(entry, *args, **kwargs)
            finally:
                depth[name] -= 1

        return spy

    def recording(name: str, cls: Any, within: str) -> Callable[..., Any]:
        def build(**kwargs: Any) -> Any:
            built.append((name, depth[within] > 0))
            return cls(**kwargs)

        return build

    monkeypatch.setattr(documents, "render_outcome", inside("render_outcome"))
    monkeypatch.setattr(documents, "render_key", inside("render_key"))
    monkeypatch.setattr(memo, "RenderKey", recording("key", memo.RenderKey, "render_key"))
    monkeypatch.setattr(
        imports, "ImportPolicy", recording("policy", imports.ImportPolicy, "render_outcome")
    )
    sut = router_project(tmp_path / "sut")
    op = operation([sut])
    report = op.repository.lab_status_report(view=op.view)
    op.repository.load_lab_reported(
        "router-lab.east-r7", preferences=None, view=op.view, report=report
    )
    entry = _layouts(sut.root) / "router.jsonnet"
    render_entry(entry=entry, source=None, projects=[sut], view=op.view)
    assert [p.name for p in outcomes] == ["router.jsonnet"] * 2
    assert built, "control: keys and policies were built"
    assert all(inside for _, inside in built), built


def test_single_inventory_crossing_the_deadline_never_evaluates(monkeypatch, tmp_path):
    class Blocking(FakeDiscoveryInventory):
        """An inventory whose discovery blocks until the test releases it."""

        def __init__(self) -> None:
            super().__init__({"edge-17": {"ip": "10.0.0.17"}})
            self.started = threading.Event()
            self.release = threading.Event()

        def inventory_discovery(self) -> Any:
            self.started.set()
            assert self.release.wait(WAIT)
            return super().inventory_discovery()

    only = Blocking()
    view = catalogued_view(inventories={"only": only})
    entry = _entry(tmp_path, SIMPLE)
    render = _source_inputs(tmp_path, inventories=["only"]).render_inputs(view)
    n = count_evaluations(monkeypatch)
    outcomes: list[Any] = []

    def worker() -> None:
        with cancellation(view.render_memo.cancelled(tmp_path)):
            outcomes.append(render_outcome(entry, render))

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    assert only.started.wait(WAIT)
    view.render_memo.cancel(scope=tmp_path, reason="the completion deadline passed")
    only.release.set()
    thread.join(WAIT)
    assert not thread.is_alive()
    assert only.fetches == 1  # the discovery returned, after the deadline
    assert n() == 0
    [outcome] = outcomes
    assert outcome.timed_out
    assert render_outcome(entry, render).timed_out  # the outcome stays settled


def test_a_cancelled_render_is_reported_as_the_timeout(monkeypatch, tmp_path, otto_home):
    sut = router_project(tmp_path / "sut")
    op = operation([sut])
    repository = jsonnet_repository(op)
    view = op.view.for_project(root=sut.root)
    n = count_evaluations(monkeypatch)
    view.render_memo.cancel(
        scope=repository.inputs.env.project_root, reason="the completion deadline passed"
    )
    entry = _layouts(sut.root) / "router.jsonnet"
    with pytest.raises(LabRepositoryError, match="the completion deadline passed") as status:
        repository.list_lab_status(view=view)
    assert isinstance(status.value.__cause__, RenderCancelled)
    assert f"Lab file '{entry}' was not rendered" in str(status.value)
    # the best-effort read the links use reports the same timeout, never a skipped file
    with pytest.raises(LabRepositoryError, match="the completion deadline passed") as links:
        repository.declared_link_entries(view=view)
    assert isinstance(links.value.__cause__, RenderCancelled)
    assert n() == 0


# --------------------------------------------------------------------------- #
# The render path's refusals.
# --------------------------------------------------------------------------- #


def test_a_jsonnet_entry_without_its_source_is_refused(tmp_path):
    entry = _entry(tmp_path, SIMPLE)
    with pytest.raises(LabRepositoryError, match="a Jsonnet entry is read through its jsonnet"):
        read_lab_document(entry)


def test_the_factory_and_of_prepared_build_the_inputs_through_one_helper(monkeypatch, tmp_path):
    """``JsonnetSourceInputs.of`` is the one construction: the factory and the inspector use it."""
    from otto.labs.sources import build_lab_sources

    built: list[str] = []
    real = JsonnetSourceInputs.of.__func__  # type: ignore[attr-defined]

    def recording(cls: Any, **kwargs: Any) -> Any:
        built.append(kwargs["env"].label)
        return real(cls, **kwargs)

    monkeypatch.setattr(JsonnetSourceInputs, "of", classmethod(recording))
    sut = router_project(tmp_path / "sut")
    [source] = [s for s in build_lab_sources(projects=[sut]).sources if "/jsonnet#" in s.label]
    [state] = [s for s in prepared_lab_sources(sut) if s.prepared is not None]
    inputs = JsonnetSourceInputs.of_prepared(state.prepared)
    assert inputs == source.repository.inputs  # type: ignore[attr-defined]
    assert built == [source.label, source.label]


def test_an_entry_outside_the_source_paths_is_never_rendered(monkeypatch, tmp_path):
    _entry(tmp_path / "layouts", SIMPLE)  # the source's own entry; the stray is not one
    inputs = _source_inputs(tmp_path / "layouts").render_inputs(catalogued_view(inventories={}))
    stray = _entry(tmp_path / "elsewhere", SIMPLE)
    n = count_evaluations(monkeypatch)
    with pytest.raises(LabRepositoryError, match="not an entry file of this lab source's paths"):
        read_lab_document(stray, render=inputs)
    assert n() == 0


def test_a_protected_entry_is_never_rendered(monkeypatch, tmp_path):
    entry = _entry(tmp_path, SIMPLE)
    view = dataclasses.replace(
        catalogued_view(inventories={}),
        protected=lambda: ProtectedPaths(files=frozenset({entry.resolve()})),
    )
    n = count_evaluations(monkeypatch)
    outcome = render_outcome(entry, _source_inputs(tmp_path).render_inputs(view))
    assert outcome.result is None
    assert "a protected file is never rendered" in str(outcome.error)
    assert n() == 0


def test_a_source_without_a_project_name_never_invents_one(tmp_path):
    env = _env(tmp_path, project_name=None)
    config = JsonnetLabSourceConfig.model_validate({"paths": ["x"]}, context={"env": env})
    with pytest.raises(LabRepositoryError, match="names none"):
        JsonnetSourceInputs(config=config, env=env).render_inputs(catalogued_view(inventories={}))


# --------------------------------------------------------------------------- #
# Generated-source validation.
# --------------------------------------------------------------------------- #


def _one_host_entry(host: str, *, lab: str = "bench", element: str = "dut") -> str:
    """An entry declaring *lab* with one element holding the host *host* (Jsonnet)."""
    return (
        f"function(otto) {{ labs: {{ {lab}: {{}} }}, "
        f"elements: [{{ name: '{element}', labs: ['{lab}'], hosts: [{host}] }}] }}"
    )


def test_bare_reference_in_rendered_output_is_refused(tmp_path, otto_home):
    sut = router_project(
        tmp_path / "sut", entries={"bench.jsonnet": _one_host_entry("{ inventory: 'edge-17' }")}
    )
    op = operation([sut])
    with pytest.raises(LabRepositoryError) as caught:
        op.repository.lab_status_report(view=op.view)
    message = str(caught.value)
    entry = _layouts(sut.root) / "bench.jsonnet"
    assert f"Lab file '{entry}'" in message
    assert "element 'dut' (labs 'bench') hosts[0] field 'inventory'" in message
    assert "bare reference 'edge-17'" in message


def test_reference_to_an_undeclared_inventory_is_refused(tmp_path, otto_home):
    root = tmp_path / "sut"
    host = "{ inventory: { name: 'virtual', key: 'ixia-9' } }"
    sut = router_project(
        root,
        entries={"bench.jsonnet": _one_host_entry(host)},
        extra=inventory(root, "virtual", {"ixia-9": {"ip": "10.0.0.9"}}),
    )
    op = operation([sut])
    assert "virtual" in op.view.names()  # configured, but not the source's
    with pytest.raises(LabRepositoryError, match="inventory 'virtual' is not one the source"):
        op.repository.lab_status_report(view=op.view)


def test_dut_dot_01_and_dut_dash_01_are_one_duplicate_id(tmp_path, otto_home):
    by_key = """
    local o = import 'otto.libsonnet';
    function(otto) {
      labs: { bench: {} },
      elements: [o.element(r.key, r, { os_type: 'unix' }, ['bench'])
                 for r in otto.inventories.physical],
    }
    """
    records = {
        "dut.01": {"ip": "10.0.0.1", "creds": CREDS},
        "dut-01": {"ip": "10.0.0.2", "creds": CREDS},
    }
    sut = router_project(tmp_path / "sut", entries={"bench.jsonnet": by_key}, records=records)
    op = operation([sut])
    with pytest.raises(LabRepositoryError, match="both slug to 'dut-01'"):
        op.repository.lab_status_report(view=op.view)


def test_duplicate_id_from_record_board_across_two_entry_files(tmp_path, otto_home):
    records = {
        "dut-a": {"ip": "10.0.0.1", "board": "01", "creds": CREDS},
        "dut-b": {"ip": "10.0.0.2", "creds": CREDS},
    }

    def ref(key: str) -> str:
        return f"{{ os_type: 'unix', inventory: {{ name: 'physical', key: '{key}' }} }}"

    sut = router_project(
        tmp_path / "sut",
        records=records,
        entries={
            "a.jsonnet": _one_host_entry(ref("dut-a"), lab="bench", element="dut"),
            "b.jsonnet": _one_host_entry(ref("dut-b"), lab="rig", element="dut-01"),
        },
    )
    op = operation([sut])
    with pytest.raises(LabRepositoryError) as caught:
        op.repository.lab_status_report(view=op.view)
    a, b = (_layouts(sut.root) / f for f in ("a.jsonnet", "b.jsonnet"))
    assert str(caught.value) == (
        f"Lab file '{b}': element 'dut-01' (labs 'rig') hosts[0] composes host id 'dut-01', "
        f"which element 'dut' (labs 'bench') hosts[0] in '{a}' already composes; one source, "
        "one host per id"
    )


def test_duplicate_lab_across_two_entry_files_is_refused(tmp_path, otto_home):
    bench = "function(otto) { labs: { bench: {} } }"
    sut = router_project(tmp_path / "sut", entries={"a.jsonnet": bench, "b.jsonnet": bench})
    op = operation([sut])
    a, b = (_layouts(sut.root) / f for f in ("a.jsonnet", "b.jsonnet"))
    with pytest.raises(LabRepositoryError, match=f"lab 'bench' declared in both {a} and {b}"):
        op.repository.lab_status_report(view=op.view)


def test_validation_error_names_entry_lab_element_index_and_field(tmp_path, otto_home):
    creds = json.dumps(CREDS)
    host = f"{{ ip: '10.0.0.1', os_type: 'unix', docker_priority: 'high', creds: {creds} }}"
    sut = router_project(tmp_path / "sut", entries={"bench.jsonnet": _one_host_entry(host)})
    op = operation([sut])
    report = op.repository.lab_status_report(view=op.view)
    with pytest.raises(LabRepositoryError) as caught:
        op.repository.load_lab_reported("bench", preferences=None, view=op.view, report=report)
    message = str(caught.value)
    entry = _layouts(sut.root) / "bench.jsonnet"
    assert message.startswith(f"Lab file '{entry}': element 'dut' hosts[0] in lab 'bench': ")
    assert "docker_priority" in message


def test_status_is_strict_with_one_good_and_one_broken_entry(tmp_path, otto_home):
    sut = router_project(
        tmp_path / "sut",
        entries={"good.jsonnet": ROUTER, "zbroken.jsonnet": "function(otto) error 'boom'"},
    )
    op = operation([sut])
    broken = _layouts(sut.root) / "zbroken.jsonnet"
    with pytest.raises(LabRepositoryError, match=f"Lab file '{broken}' could not be rendered"):
        jsonnet_repository(op).list_lab_status(view=op.view.for_project(root=sut.root))
    with pytest.raises(LabRepositoryError, match="boom"):
        op.repository.lab_status_report(view=op.view)


def test_selected_machine_never_depends_on_inventory_order(tmp_path, otto_home):
    pair = """
    local o = import 'otto.libsonnet';
    function(otto)
      o.layout({
        instances: o.groupBy(otto.inventories.virtual + otto.inventories.physical, ['site']),
        lab(i): 'edge-lab',
        elementName(i, role, record, n): role + n,
        roles: { dut: { match: o.inGroup('dut'), count: 2,
                        host: { label: 'dut', fn(r): { os_type: 'unix' } } } },
      })
    """
    edge = {"site": "east", "groups": ["dut"]}
    physical = {"edge-17": {"ip": "10.1.0.17", **edge, "creds": CREDS}}
    virtual = {"edge-17": {"ip": "10.2.0.17", **edge, "creds": [{"login": "v", "password": "w"}]}}
    seen: list[Any] = []
    for order in (["physical", "virtual"], ["virtual", "physical"]):
        root = tmp_path / "-".join(order)
        tables = {
            "physical": inventory(root, "physical", physical),
            "virtual": inventory(root, "virtual", virtual),
        }
        write(_layouts(root) / "edge.jsonnet", pair)
        settings = "".join(tables[n] for n in order) + jsonnet_source(
            paths=[str(_layouts(root) / "edge.jsonnet")], inventories=order
        )
        from otto.session import build_lab

        lab = build_lab([project(root, settings)], labs=["edge-lab"])
        seen.append(
            {
                host_id: (
                    host.ip,
                    host.inventory_ref.name,
                    host.inventory_ref.key,
                    [c.login for c in host.creds],
                )
                for host_id, host in lab.hosts.items()
                if host_id != "local"
            }
        )
    assert (
        seen[0]
        == seen[1]
        == {
            "dut0": ("10.1.0.17", "physical", "edge-17", ["admin"]),
            "dut1": ("10.2.0.17", "virtual", "edge-17", ["v"]),
        }
    )


def test_provenance_survives_into_lab_document(tmp_path, otto_home):
    sut = router_project(tmp_path / "sut")
    op = operation([sut])
    view = op.view.for_project(root=sut.root)
    [doc] = jsonnet_repository(op)._load_documents(best_effort=False, view=view)
    assert doc.path == _layouts(sut.root) / "router.jsonnet"
    assert doc.provenance == {
        "east-r7-dut0": {"instance": {"site": "east", "rack": "r7"}, "role": "dut", "host": "dut"},
        "west-r8-dut0": {"instance": {"site": "west", "rack": "r8"}, "role": "dut", "host": "dut"},
    }


def test_a_hand_written_lab_file_has_no_provenance(tmp_path):
    lab = write(tmp_path / "lab.json", {"_otto": {"provenance": {"x": {}}}, "labs": {"a": {}}})
    assert read_lab_document(lab).provenance is None


# --------------------------------------------------------------------------- #
# Configuration, files and key paths.
# --------------------------------------------------------------------------- #


def test_default_import_root_is_anchored_at_the_project(tmp_path, otto_home):
    config = _source_inputs(tmp_path).config
    assert config.import_paths == (tmp_path / "lab_data" / "lib",)
    library = "{ dut(r): { os_type: 'unix' } }"
    write(tmp_path / "sut" / "lab_data" / "lib" / "hosts.libsonnet", library)
    uses_library = ROUTER.replace("fn(r): { os_type: 'unix' }", "fn: hosts.dut").replace(
        "function(otto)", "local hosts = import 'hosts.libsonnet';\nfunction(otto)"
    )
    sut = router_project(tmp_path / "sut", entries={"router.jsonnet": uses_library})
    op = operation([sut])
    assert [s.name for s in op.repository.lab_status_report(view=op.view).labs] == [
        "router-lab.east-r7",
        "router-lab.west-r8",
    ]


def test_config_refuses_an_empty_entry_and_an_empty_scope(tmp_path):
    env = _env(tmp_path)
    for options, needle in (
        ({"paths": [""]}, "name a directory, a .jsonnet file or a glob"),
        ({"paths": ["x"], "import_paths": [""]}, "an entry is empty; name a directory"),
        ({"paths": []}, "at least 1 item"),
        ({"paths": ["x"], "scope": {}}, "a scope names at least one lab name or prefix"),
    ):
        with pytest.raises(ValueError, match=needle):
            JsonnetLabSourceConfig.model_validate(options, context={"env": env})


def test_scope_and_overrides_are_parsed(tmp_path):
    config = _source_inputs(
        tmp_path, scope={"names": ["bench"], "prefixes": ["router-lab."]}, overrides=["edge-17"]
    ).config
    assert config.scope == LabScopeSpec(names=("bench",), prefixes=("router-lab.",))
    scope = config.scope.to_scope()
    assert scope.contains(name="bench")
    assert scope.contains(name="router-lab.east")
    assert not scope.contains(name="bench2")
    assert config.overrides == ("edge-17",)


def test_lab_files_and_entries_agree(tmp_path, otto_home):
    sut = router_project(tmp_path / "sut", entries={"b.jsonnet": SIMPLE, "a.jsonnet": SIMPLE})
    write(_layouts(sut.root) / "notes.json", {})  # not an entry: the wrong suffix
    [state] = prepared_lab_sources(sut)
    assert state.file_rule() is JSONNET_FILE_RULE
    repository = jsonnet_repository(operation([sut]))
    assert state.lab_files() == repository.inputs.entries()
    assert [p.name for p in state.lab_files()] == ["a.jsonnet", "b.jsonnet"]


def test_jsonnet_key_paths_cover_the_import_roots_without_a_view(monkeypatch, tmp_path, otto_home):
    root = tmp_path / "sut"
    lib = root / "lab_data" / "lib"
    write(lib / "hosts.libsonnet", "{}")
    write(lib / "nested" / "deeper" / "roles.libsonnet", "{}")
    sut = router_project(root)
    n = count_evaluations(monkeypatch)
    [state] = prepared_lab_sources(sut)
    [entry] = state.lab_files()
    visited: set[Path] = set()
    paths = state.key_paths(visited=visited)
    assert paths[0] == entry
    library = library_file()
    assert library is not None, "control: a source checkout's library is a plain file"
    assert paths == rendering_dependencies(entries=[entry], roots=(lib,))
    assert library in paths
    assert {lib / "nested", lib / "nested" / "deeper" / "roles.libsonnet"} <= set(paths)
    assert _layouts(root) in visited
    assert n() == 0


def test_key_paths_walk_the_import_roots_once_for_every_entry(monkeypatch, tmp_path, otto_home):
    """Many entries over one set of roots: the roots are walked once, every entry keyed."""
    import os

    from otto.templating import freshness

    root = tmp_path / "sut"
    lib = root / "lab_data" / "lib"
    write(lib / "hosts.libsonnet", "{}")
    layouts = _layouts(root)
    entries = [write(layouts / f"{name}.jsonnet", ROUTER) for name in ("a", "b", "c")]
    sut = project(root, jsonnet_source(paths=[str(layouts / "*.jsonnet")]))
    walked: list[Any] = []
    real_walk = os.walk

    def counting_walk(top: Any, *args: Any, **kwargs: Any) -> Any:
        walked.append(top)
        return real_walk(top, *args, **kwargs)

    monkeypatch.setattr(freshness.os, "walk", counting_walk)
    [state] = prepared_lab_sources(sut)
    paths = state.key_paths()
    assert paths[:3] == entries
    assert lib / "hosts.libsonnet" in paths
    assert library_file() in paths
    assert walked == [lib], "the import root was walked once per entry"


# --------------------------------------------------------------------------- #
# Hermeticity.
# --------------------------------------------------------------------------- #


def test_secret_in_creds_json_never_appears_anywhere(capfd, caplog, tmp_path, otto_home):
    """A secret in ``creds.json`` and in a record's creds reaches no output, error or file."""
    from otto.session import build_lab

    secret = "SEKRIT"
    root = tmp_path / "sut"
    creds = write(
        root / "lab_data" / "creds.json", {"edge-17": [{"login": "a", "password": secret}]}
    )
    records = {
        "edge-17": {"ip": "10.0.0.17", "site": "east", "rack": "r7", "groups": ["dut"]},
        "edge-18": {
            "ip": "10.0.0.18",
            "site": "west",
            "rack": "r8",
            "groups": ["dut"],
            "creds": [{"login": "b", "password": secret + "-2"}],
        },
    }
    # The whole input lands in each lab's metadata, so the render shows what a template sees.
    dumped = ROUTER.replace(
        "lab(i):", "labsEntry(i): { metadata: { input: otto } },\n    lab(i):"
    ).replace("fn(r): { os_type: 'unix' }", "fn(r): { os_type: 'unix', docker_priority: 'x' }")
    sut = router_project(
        root,
        records=records,
        entries={"router.jsonnet": dumped},
        inventory_keys={"creds": {"backend": "json", "path": str(creds)}},
    )
    caplog.set_level(logging.DEBUG)
    op = operation([sut])
    texts: list[str] = []
    view = op.view.for_project(root=sut.root)
    render = jsonnet_repository(op).inputs.render_inputs(view)
    [entry] = render.entries
    outcome = render_outcome(entry, render)
    assert outcome.result is not None
    texts.append(outcome.result.text)
    report = op.repository.lab_status_report(view=op.view)
    texts.append(repr(report))
    texts.append(repr(op.repository.list_host_summaries_in_view(view=op.view)))
    with pytest.raises(LabRepositoryError) as caught:  # docker_priority 'x' fails every host
        build_lab([sut], labs=["router-lab.east-r7"])
    texts.append(str(caught.value))
    out, err = capfd.readouterr()
    texts.extend([out, err, caplog.text])
    for text in texts:
        assert secret not in text
    leaked = [
        p
        for p in tmp_path.rglob("*")
        if p.is_file() and p not in {creds, root / "lab_data" / "physical.json"}
        if secret.encode() in p.read_bytes()
    ]
    assert leaked == []


# --------------------------------------------------------------------------- #
# Registration and import weight.
# --------------------------------------------------------------------------- #


def _run_python(code: str, *args: str) -> Any:
    """Run *code* with *args* in a fresh interpreter and return the JSON it prints last."""
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "-c", code, *args],
        env=coverage_subprocess_env(),
        capture_output=True,
        text=True,
        cwd=str(CHECKOUT_ROOT),
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_jsonnet_is_registered_by_reference():
    assert _run_python(
        "import json, sys\n"
        "from otto.labs.registry import LAB_REPOSITORIES\n"
        "from otto.registry import Ref\n"
        "e = LAB_REPOSITORIES.peek(name='jsonnet')\n"
        "print(json.dumps([isinstance(e.config, Ref), isinstance(e.factory, Ref),\n"
        "                  'otto.labs.jsonnet_source' in sys.modules]))\n"
    ) == [True, True, False]


def test_a_team_jsonnet_lab_source_keeps_its_name_with_a_warning(caplog):
    # LAB_REPOSITORIES is imported at module scope, so the table exists when the
    # root isolation fixture snapshots it and the built-in comes back after.
    from otto.models.base import OttoModel

    class TeamConfig(OttoModel, frozen=True):
        paths: list[str]

    with caplog.at_level(logging.WARNING, logger="otto.registry"):
        from_module(
            "acme.init",
            register_lab_repository,
            "jsonnet",
            config=TeamConfig,
            factory=lambda c: None,
        )
    assert LAB_REPOSITORIES.origin(name="jsonnet") == "acme.init"
    assert [r.getMessage() for r in caplog.records if r.name == "otto.registry"] == [
        (
            "lab repository backend 'jsonnet': acme.init replaces otto's built-in "
            "(otto.labs.registry); otto's 'jsonnet' is unavailable in this process"
        )
    ]


_LISTING_PROBE = """
import json, sys
from pathlib import Path
from otto.config.project import Project
from otto.labs.sources import build_lab_sources, compile_lab_sources, prepared_lab_sources

project = Project(root=Path(sys.argv[1]))
assert [s.backend for s in project.lab_sources] == ["json", "jsonnet"], project.lab_sources
assert compile_lab_sources(None, project_name="sut", root=project.root) == []
states = prepared_lab_sources(project)
keys = [path for state in states for path in state.key_paths()]
composite = build_lab_sources(projects=[project])
labs = composite.list_labs()
bindings = ("_gojsonnet", "_jsonnet", "otto.templating.binding")
loaded = [name for name in bindings if name in sys.modules]
print(json.dumps({"labs": labs, "keys": len(keys), "loaded": loaded}))
"""


def test_no_binding_is_imported_by_settings_preparation_or_lab_listing(tmp_path):
    root = tmp_path / "sut"
    hand = write(root / "lab_data" / "lab.json", {"labs": {"bench": {}}})
    write(_layouts(root) / "router.jsonnet", ROUTER)
    settings = json_source(hand) + jsonnet_source(paths=[str(_layouts(root) / "*.jsonnet")])
    project(root, settings)
    found = _run_python(_LISTING_PROBE, str(root))
    assert found["labs"] == ["bench"]
    assert found["keys"] >= 2
    assert found["loaded"] == []

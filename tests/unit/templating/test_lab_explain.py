"""``otto lab explain``: which layer supplied each of a host's values, read from the build.

Every case builds a real project's lab the way a session does
(:func:`~otto.session.build_lab_and_view`) inside
:func:`~otto.host.provenance.recording`, then asks
:func:`~otto.labs.inspect.explain_host` about one host, so the winners are
the merges' own notes. What the user sees is driven through the root app,
``otto -l NAME lab explain HOST_ID``, over a project ``OTTOPATH`` names.
"""

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from otto import bootstrap
from otto.cli.main import app
from otto.config.project import Project
from otto.host import provenance
from otto.host.provenance import EntryKey, FieldLayer, recording
from otto.inventory import InventoryView, SnapshotCache, merge_creds
from otto.labs.inspect import HostExplanation, explain_host
from otto.session.lab import build_lab_and_view
from tests._fixtures.named_inventory import FakeDiscoveryInventory, FakeInventory

from ._lab_project import CREDS, inventory, json_source, jsonnet_source, project, table, write

runner = CliRunner()

SENTINEL = "SENTINEL-6b1f04e2"
"""A store password no field name, label or path could contain by accident."""

LAB_SENTINEL = "SENTINEL-lab-93ad7c55"
"""A password written in a lab file, which explain must not show either."""

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

SU_ROOT = [{"login": "admin"}, {"login": "root", "proxy": "su", "via": "admin"}]
"""A record's creds: ``root`` is reached through ``su`` from ``admin``, with no password here."""

PHYSICAL = {
    "edge-17": {"ip": "10.0.0.17", "site": "east", "rack": "r7", "groups": ["dut"], "creds": CREDS},
    "edge-18": {"ip": "10.0.0.18", "site": "west", "rack": "r8", "groups": ["dut"], "creds": CREDS},
}


def _router(root: Path) -> Project:
    """A project rendering ``ROUTER`` over its ``physical`` inventory."""
    entry = write(root / "lab_data" / "router.jsonnet", ROUTER)
    settings = inventory(root, "physical", PHYSICAL) + jsonnet_source(
        paths=[str(entry)], inventories=["physical"]
    )
    return project(root, settings)


def _lab_file(
    root: Path,
    hosts: "list[dict[str, Any]]",
    *,
    name: str = "lab.json",
    peer: "list[dict[str, Any]] | None" = None,
) -> Path:
    """Write a plain lab file declaring lab ``bench``: element ``dut`` holds *hosts*.

    *peer*, when given, is the hosts of a second element, ``peer``.
    """
    elements = [{"name": "dut", "labs": ["bench"], "hosts": hosts}]
    if peer is not None:
        elements.append({"name": "peer", "labs": ["bench"], "hosts": peer})
    return write(root / "lab_data" / name, {"labs": {"bench": {}}, "elements": elements})


def _with_store(
    root: Path,
    *,
    records: "dict[str, dict[str, Any]]",
    store: "dict[str, list[dict[str, Any]]]",
    hosts: "list[dict[str, Any]]",
    extra: str = "",
) -> Project:
    """A project whose ``default`` inventory holds *records* over a ``[creds]`` *store*."""
    records_file = write(root / "lab_data" / "inventory.json", records)
    store_file = write(root / "lab_data" / "creds.json", store)
    settings = (
        table("[inventory]", backend="json", path=str(records_file))
        + table("[creds]", backend="json", path=str(store_file))
        + json_source(_lab_file(root, hosts))
        + extra
    )
    return project(root, settings)


def _explain(projects: "list[Project]", lab: str, host_id: str, **build: Any) -> HostExplanation:
    """Build *lab* as a session does, recorded, and explain *host_id* from that build."""
    with recording() as trace:
        built = build_lab_and_view(projects, labs=[lab], **build)
    return explain_host(host_id=host_id, lab=built.lab, view=built.view, trace=trace)


def _layers(explanation: HostExplanation) -> "dict[tuple[str, str | None], str]":
    """Each winner as ``(field, detail) -> layer label``, as the table shows it."""
    return {(w.field, w.detail): explanation.layer_label(w) for w in explanation.winners}


@pytest.fixture
def cli(monkeypatch: pytest.MonkeyPatch, otto_home: Path):
    """Run the root app over the projects ``OTTOPATH`` names; return the runner call."""
    monkeypatch.setenv("COLUMNS", "600")  # no fold may split a path or a value
    monkeypatch.delenv("OTTO_LAB", raising=False)

    def invoke(roots: "list[Path]", *args: str):
        monkeypatch.setenv("OTTOPATH", ":".join(str(r) for r in roots))
        bootstrap._reset()  # the root conftest snapshot-restores bootstrap's caches
        return runner.invoke(app, list(args))

    return invoke


# --------------------------------------------------------------------------- #
# Where the entry came from.
# --------------------------------------------------------------------------- #


def test_explain_names_source_entry_instance_and_role(tmp_path, otto_home, cli):
    sut = _router(tmp_path / "sut")
    explained = _explain([sut], "router-lab.east-r7", "east-r7-dut0")
    assert (explained.source_label, explained.element, explained.index) == (
        "sut/jsonnet#1",
        "east-r7-dut0",
        0,
    )
    assert explained.entry == sut.root / "lab_data" / "router.jsonnet"
    assert explained.rendered
    assert explained.provenance == {
        "instance": {"rack": "r7", "site": "east"},
        "role": "dut",
        "host": "dut",
    }
    result = cli([sut.root], "-l", "router-lab.east-r7", "lab", "explain", "east-r7-dut0")
    assert result.exit_code == 0, result.output
    lines = result.stdout.splitlines()
    assert "source: sut/jsonnet#1" in lines
    assert (
        f"entry: {sut.root / 'lab_data' / 'router.jsonnet'} (element 'east-r7-dut0', hosts[0])"
        in lines
    )
    assert 'instance: {"rack": "r7", "site": "east"}' in lines
    assert "role: dut" in lines
    assert "host label: dut" in lines


def test_supplied_fields_stay_record_not_template(tmp_path, otto_home):
    """A field the record supplies reaches the host dict, and is still the record's."""
    explained = _explain([_router(tmp_path / "sut")], "router-lab.east-r7", "east-r7-dut0")
    layers = _layers(explained)
    for name in ("ip", "site", "rack"):
        assert layers[(name, None)] == "inventory record"
    assert layers[("os_type", None)] == "template"
    assert layers[("creds.password", "admin")] == "inventory record"


def test_plain_json_host_reads_lab_file(tmp_path, otto_home):
    root = tmp_path / "sut"
    sut = project(root, json_source(_lab_file(root, [{"ip": "10.0.0.9", "creds": CREDS}])))
    explained = _explain([sut], "bench", "dut")
    assert not explained.rendered
    assert explained.provenance is None
    assert explained.source_label == "sut/json#1"
    assert _layers(explained) == {
        ("ip", None): "lab file",
        ("creds.password", "admin"): "lab file",
    }


def test_multiple_hosts_are_attributed_separately(tmp_path, otto_home):
    root = tmp_path / "sut"
    records = {"edge-17": {"ip": "10.0.0.17", "creds": CREDS}}
    records_file = write(root / "lab_data" / "inventory.json", records)
    lab_file = _lab_file(
        root,
        [{"ip": "10.0.0.9", "creds": CREDS}, {"ip": "10.0.0.10", "board": "b", "creds": CREDS}],
        peer=[{"inventory": "edge-17"}],
    )
    sut = project(
        root, table("[inventory]", backend="json", path=str(records_file)) + json_source(lab_file)
    )
    with recording() as trace:
        built = build_lab_and_view([sut], labs=["bench"])
    first, second, joined = (
        explain_host(host_id=host_id, lab=built.lab, view=built.view, trace=trace)
        for host_id in ("dut", "dut-b", "peer")
    )
    assert [(e.element, e.index) for e in (first, second, joined)] == [
        ("dut", 0),
        ("dut", 1),
        ("peer", 0),
    ]
    assert _layers(first)[("ip", None)] == "lab file"
    assert ("board", None) not in _layers(first)
    assert _layers(second)[("board", None)] == "lab file"
    assert _layers(joined)[("ip", None)] == "inventory record"
    assert {(w.entry.element, w.entry.index) for w in second.winners} == {("dut", 1)}


def test_overridden_source_contributes_no_winner(tmp_path, otto_home):
    """The later source's element replaces the earlier's, and only its entry explains the host."""
    root = tmp_path / "sut"
    first = _lab_file(root, [{"ip": "10.0.0.1", "creds": CREDS, "ssh_options": {"port": 2222}}])
    second = _lab_file(root, [{"ip": "10.0.0.1", "creds": CREDS}], name="override.json")
    sut = project(root, json_source(first) + json_source(second))
    with recording() as trace:
        built = build_lab_and_view([sut], labs=["bench"])
    explained = explain_host(host_id="dut", lab=built.lab, view=built.view, trace=trace)
    assert explained.source_label == "sut/json#2"
    assert explained.entry == second
    assert [e.key.source for e in trace.entries] == ["sut/json#2"]
    assert ("ssh_options.port", None) not in _layers(explained)


# --------------------------------------------------------------------------- #
# Credentials: winners per identity and field, kept across both merges.
# --------------------------------------------------------------------------- #


def test_explain_mixed_credential_field_winners(tmp_path, otto_home):
    """The store's password and the record's ``proxy`` for one login, passed through whole."""
    sut = _with_store(
        tmp_path / "sut",
        records={"edge-17": {"ip": "10.0.0.17", "creds": SU_ROOT}},
        store={"edge-17": [{"login": "root", "password": SENTINEL}]},
        hosts=[{"inventory": "edge-17"}],
    )
    layers = _layers(_explain([sut], "bench", "dut"))
    assert layers[("creds.password", "root")] == "creds store"
    assert layers[("creds.proxy", "root")] == "inventory record"
    assert ("creds.password", "admin") not in layers  # no layer states one


def test_three_layer_credential_provenance(tmp_path, otto_home):
    """Store password, record proxy, and a scoped lab-file identity: each keeps its layer."""
    sut = _with_store(
        tmp_path / "sut",
        records={"edge-17": {"ip": "10.0.0.17", "creds": SU_ROOT}},
        store={"edge-17": [{"login": "root", "password": SENTINEL}]},
        hosts=[
            {
                "inventory": "edge-17",
                "creds": [{"login": "ops", "protocols": ["ssh"], "proxy": "su", "via": "admin"}],
            }
        ],
    )
    layers = _layers(_explain([sut], "bench", "dut"))
    assert layers[("creds.password", "root")] == "creds store"
    assert layers[("creds.proxy", "root")] == "inventory record"
    assert layers[("creds.via", "root")] == "inventory record"
    assert layers[("creds.proxy", "ops [ssh]")] == "lab file"
    assert layers[("creds.via", "ops [ssh]")] == "lab file"
    assert sorted(d for f, d in layers if f.startswith("creds.")) == [
        "ops [ssh]",
        "ops [ssh]",
        "root",
        "root",
        "root",
    ]


def test_same_login_two_protocols_attributed_separately(tmp_path, otto_home):
    sut = _with_store(
        tmp_path / "sut",
        records={"edge-17": {"ip": "10.0.0.17"}},
        store={"edge-17": [{"login": "root", "protocols": ["ssh"], "password": SENTINEL}]},
        hosts=[
            {
                "inventory": "edge-17",
                "creds": [{"login": "root", "protocols": ["telnet"], "password": LAB_SENTINEL}],
            }
        ],
    )
    layers = _layers(_explain([sut], "bench", "dut"))
    assert layers[("creds.password", "root [ssh]")] == "creds store"
    assert layers[("creds.password", "root [telnet]")] == "lab file"


# --------------------------------------------------------------------------- #
# The profile, the entry and [host_preferences].
# --------------------------------------------------------------------------- #


def test_profile_default_vs_lab_data_vs_preferences(tmp_path, otto_home):
    root = tmp_path / "sut"
    host = {
        "ip": "10.0.0.9",
        "os_type": "alpha",
        "creds": CREDS,
        "ssh_options": {"connect_timeout": 5.0},
    }
    settings = (
        "[os_profiles.alpha]\n"
        'base = "unix"\n'
        "shell_history = true\n"
        "ssh_options = { port = 2222, connect_timeout = 9.0 }\n\n"
        '[host_preferences."du.*".ssh_options]\n'
        "keepalive_interval = 30\n\n" + json_source(_lab_file(root, [host]))
    )
    layers = _layers(_explain([project(root, settings)], "bench", "dut"))
    assert layers[("shell_history", "alpha")] == "os profile"
    assert layers[("ssh_options.port", "alpha")] == "os profile"
    assert layers[("ssh_options.connect_timeout", None)] == "lab file"
    assert layers[("ssh_options.keepalive_interval", "du.*")] == "host preferences"
    assert layers[("os_type", None)] == "lab file"


def test_capability_preferences_are_attributed(tmp_path, otto_home):
    root = tmp_path / "sut"
    settings = (
        '[host_preferences.".*"]\nterm = ["ssh"]\n\n'
        '[host_preferences."dut"]\nterm = ["telnet", "ssh"]\n\n'
        + json_source(_lab_file(root, [{"ip": "10.0.0.9", "creds": CREDS}]))
    )
    with recording() as trace:
        built = build_lab_and_view([project(root, settings)], labs=["bench"])
    explained = explain_host(host_id="dut", lab=built.lab, view=built.view, trace=trace)
    assert built.lab.hosts["dut"].term == "telnet"
    # The later selector's list replaces the earlier's, so it is the one named.
    assert _layers(explained)[("term", "dut")] == "host preferences"
    assert ("term", ".*") not in _layers(explained)


@pytest.mark.parametrize("options", [False, True], ids=["no-option-table", "option-table"])
def test_a_capability_winner_does_not_depend_on_an_unrelated_option_table(
    tmp_path, otto_home, options
):
    """The profile pins ``telnet``, a preference picks ``ssh``: the preference wins, either way."""
    root = tmp_path / "sut"
    settings = (
        '[os_profiles.alpha]\nbase = "unix"\nterm = "telnet"\n\n'
        '[host_preferences.".*"]\nterm = ["ssh"]\n\n'
        + ('[host_preferences.".*".ssh_options]\nkeepalive_interval = 30\n\n' if options else "")
        + json_source(_lab_file(root, [{"ip": "10.0.0.9", "os_type": "alpha", "creds": CREDS}]))
    )
    with recording() as trace:
        built = build_lab_and_view([project(root, settings)], labs=["bench"])
    explained = explain_host(host_id="dut", lab=built.lab, view=built.view, trace=trace)
    layers = _layers(explained)
    assert built.lab.hosts["dut"].term == "ssh"
    assert layers[("term", ".*")] == "host preferences"
    assert ("term", "alpha") not in layers
    assert (("ssh_options.keepalive_interval", ".*") in layers) is options


def test_an_out_of_menu_preference_leaves_the_pin_its_layer(tmp_path, otto_home):
    """A preference the host's menu lacks is skipped by the resolver: the pin decides."""
    root = tmp_path / "sut"
    host = {"ip": "10.0.0.9", "valid_terms": ["ssh"], "term": "ssh", "creds": CREDS}
    settings = '[host_preferences.".*"]\nterm = ["telnet"]\n\n' + json_source(
        _lab_file(root, [host])
    )
    with recording() as trace:
        built = build_lab_and_view([project(root, settings)], labs=["bench"])
    explained = explain_host(host_id="dut", lab=built.lab, view=built.view, trace=trace)
    layers = _layers(explained)
    assert built.lab.hosts["dut"].term == "ssh"
    assert layers[("term", None)] == "lab file"
    assert ("term", ".*") not in layers


# --------------------------------------------------------------------------- #
# Inventory ages.
# --------------------------------------------------------------------------- #


def test_inventory_ages_are_shown(tmp_path, otto_home):
    """A fresh fetch, a stale snapshot, an unread cache and a file-backed inventory."""
    now = datetime.now(timezone.utc)
    records = {"edge-17": {"ip": "10.0.0.17", "creds": CREDS}}
    cache_dir = tmp_path / "cache"

    def cache(inner: Any, *, slug: str, fetched_at: datetime) -> SnapshotCache:
        return SnapshotCache(
            inner,
            ttl=timedelta(hours=24),
            cache_dir=cache_dir,
            slug_material=slug,
            clock=lambda: fetched_at,
        )

    # A first process fetched "stale" 50 hours ago; this one cannot reach it.
    cache(
        FakeDiscoveryInventory(records), slug="stale", fetched_at=now - timedelta(hours=50)
    ).lookup("edge-17")
    down = FakeDiscoveryInventory(records, fail="unreachable", max_stale="7d")
    view = InventoryView(
        inventories={
            "default": cache(
                FakeDiscoveryInventory(records), slug="fresh", fetched_at=now - timedelta(hours=3)
            ),
            "stale": cache(down, slug="stale", fetched_at=now),
            "spare": cache(FakeDiscoveryInventory(records), slug="spare", fetched_at=now),
            "file": FakeInventory(records),
        }
    )
    root = tmp_path / "sut"
    lab_file = _lab_file(
        root, [{"inventory": "edge-17"}], peer=[{"inventory": {"name": "stale", "key": "edge-17"}}]
    )
    explained = _explain([project(root, json_source(lab_file))], "bench", "dut", view=view)
    ages = {i.name: (i.age, i.served_stale) for i in explained.inventories}
    assert ages == {
        "default": ("3h", False),
        "file": ("live", False),
        "spare": ("not read", False),
        "stale": ("2d 2h", True),
    }


# --------------------------------------------------------------------------- #
# No secret, ever.
# --------------------------------------------------------------------------- #


def test_explain_output_holds_no_secret(tmp_path, otto_home, cli):
    sut = _with_store(
        tmp_path / "sut",
        records={"edge-17": {"ip": "10.0.0.17", "creds": SU_ROOT}},
        store={"edge-17": [{"login": "root", "password": SENTINEL}]},
        hosts=[
            {
                "inventory": "edge-17",
                "creds": [{"login": "admin", "protocols": ["ssh"], "password": LAB_SENTINEL}],
            }
        ],
    )
    explained = _explain([sut], "bench", "dut")
    assert SENTINEL not in repr(explained)
    assert LAB_SENTINEL not in repr(explained)
    result = cli([sut.root], "-l", "bench", "lab", "explain", "dut")
    assert result.exit_code == 0, result.output
    assert "creds store" in result.stdout
    assert "admin [ssh]" in result.stdout
    assert SENTINEL not in result.output
    assert LAB_SENTINEL not in result.output


# --------------------------------------------------------------------------- #
# Recording: a no-op outside explain, one shared trace inside it.
# --------------------------------------------------------------------------- #


def test_recording_is_a_noop_outside_explain(tmp_path, otto_home, monkeypatch):
    """Dispatch without explain records nothing and builds nothing, per entry or per field.

    Every site asks :func:`~otto.host.inventory_ref.recording_fields` first, so
    no entry key, entry trace, winner or note is built. Inside
    :func:`~otto.host.provenance.recording` the same build notes every field.
    """
    sut = _with_store(
        tmp_path / "sut",
        records={"edge-17": {"ip": "10.0.0.17", "creds": SU_ROOT}},
        store={"edge-17": [{"login": "root", "password": SENTINEL}]},
        hosts=[{"inventory": "edge-17", "ssh_options": {"port": 2222}}],
        extra='[host_preferences.".*"]\nterm = ["ssh"]\n\n'
        '[host_preferences.".*".ssh_options]\nkeepalive_interval = 30\n\n',
    )
    built: list[str] = []
    for name in ("EntryKey", "EntryTrace", "FieldWinner"):
        real = getattr(provenance, name)
        monkeypatch.setattr(
            provenance, name, lambda real=real, name=name, **kw: built.append(name) or real(**kw)
        )
    for name in ("note", "note_merge", "note_cred_merge", "note_cred_fields", "preference_won"):
        real = getattr(provenance, name)
        monkeypatch.setattr(
            provenance, name, lambda real=real, name=name, **kw: built.append(name) or real(**kw)
        )

    lab = build_lab_and_view([sut], labs=["bench"]).lab
    assert "dut" in lab.hosts
    assert built == []
    assert provenance.trace_position() is None

    with recording() as trace:
        build_lab_and_view([sut], labs=["bench"])
    assert built.count("EntryKey") == built.count("EntryTrace") == 1
    assert built.count("FieldWinner") >= len(trace.entries[0].winners) > 0


def test_a_note_made_in_a_task_created_during_the_build_reaches_the_trace():
    """A task copies the context, and the trace and entry it copies are the shared objects."""
    key = EntryKey(source="s", origin=Path("lab.json"), element="dut", index=0)

    async def child() -> None:
        provenance.note(field="ip", layer=FieldLayer.RECORD)

    async def build() -> None:
        with provenance.entry_scope(key):
            await asyncio.create_task(child())
            provenance.built(host_id="dut")

    with recording() as trace:
        asyncio.run(build())
    entry = trace.entry_for(host_id="dut")
    assert entry is not None
    assert [(w.field, w.layer) for w in entry.winners] == [("ip", FieldLayer.RECORD)]


def test_a_store_password_stays_the_stores_through_both_merges():
    """The cred-merge note itself: a lower-only field keeps the winner an earlier merge recorded."""
    key = EntryKey(source="s", origin=Path("lab.json"), element="dut", index=0)
    store = [{"login": "root", "password": SENTINEL}]
    record = [{"login": "root", "proxy": "su"}, {"login": "admin"}]
    lab = [{"login": "root", "via": "admin"}]
    with recording() as trace, provenance.entry_scope(key):
        provenance.note_cred_merge(
            lower=store,
            higher=record,
            lower_layer=FieldLayer.CREDS_STORE,
            higher_layer=FieldLayer.RECORD,
        )
        provenance.note_cred_merge(
            lower=merge_creds(lower=store, higher=record),
            higher=lab,
            lower_layer=FieldLayer.RECORD,
            higher_layer=FieldLayer.LAB_DATA,
        )
    [entry] = trace.entries
    assert {(w.field, w.detail): w.layer for w in entry.winners} == {
        ("creds.password", "root"): FieldLayer.CREDS_STORE,
        ("creds.proxy", "root"): FieldLayer.RECORD,
        ("creds.via", "root"): FieldLayer.LAB_DATA,
    }


def test_the_merge_and_its_attribution_key_a_layer_by_one_function(monkeypatch):
    """One keying decision: change the identity rule once and both the merge and explain follow.

    The rule is swapped for one that orders ``protocols``. The merge then
    forms one identity from two spellings, and every winner explain records
    names that identity: neither keys a layer on its own.
    """
    from otto.models import base as base_module

    real = base_module.cred_layer_by_identity
    keyed: list[str] = []

    def ordered_scope(layer: list[dict[str, Any]], *, name: str, key: "str | None" = None) -> Any:
        keyed.append(name)
        ordered = [{**e, "protocols": sorted(e.get("protocols") or [])} for e in layer]
        return real(ordered, name=name, key=key)

    monkeypatch.setattr(base_module, "cred_layer_by_identity", ordered_scope)
    lower = [{"login": "root", "protocols": ["telnet", "ssh"], "password": SENTINEL}]
    higher = [{"login": "root", "protocols": ["ssh", "telnet"], "proxy": "su"}]
    merged = merge_creds(lower=lower, higher=higher, lower_name="store", higher_name="record")
    assert len(merged) == 1, "the swapped rule formed one identity"
    key = EntryKey(source="s", origin=Path("lab.json"), element="dut", index=0)
    with recording() as trace, provenance.entry_scope(key):
        provenance.note_cred_merge(
            lower=lower,
            higher=higher,
            lower_layer=FieldLayer.CREDS_STORE,
            higher_layer=FieldLayer.RECORD,
        )
    [entry] = trace.entries
    [identity] = real(merged, name="merged")
    assert {(w.field, w.detail): w.layer for w in entry.winners} == {
        ("creds.password", identity): FieldLayer.CREDS_STORE,
        ("creds.proxy", identity): FieldLayer.RECORD,
    }
    assert keyed == ["store", "record", "attributed", "attributed"], keyed


def test_merge_creds_keeps_its_public_signature():
    """Attribution sits beside the public merge, never in its signature."""
    import inspect as stdlib_inspect

    from otto.inventory import merge_creds as public

    assert list(stdlib_inspect.signature(public).parameters) == [
        "lower",
        "higher",
        "key",
        "lower_name",
        "higher_name",
    ]


def test_layers_are_labelled_by_the_kind_of_entry():
    assert FieldLayer.LAB_DATA.label(rendered=True) == "template"
    assert FieldLayer.LAB_DATA.label(rendered=False) == "lab file"
    assert FieldLayer.RECORD.label(rendered=True) == "inventory record"
    assert json.dumps([layer.value for layer in FieldLayer]) == json.dumps(
        [
            "creds store",
            "os profile",
            "inventory record",
            "lab data",
            "host preferences",
        ]
    )

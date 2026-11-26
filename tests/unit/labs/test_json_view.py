"""The json lab source through the inventory view, and ``unavailable`` labs (lab templating, #1176).

The built-in source implements every view capability: a qualified
``{"name": ..., "key": ...}`` reference reaches its named inventory when a lab
loads and when hosts are summarized, a ``labs`` entry may say why the lab is
unavailable, and the legacy ``load_lab(inventory=)`` keeps today's meaning.
"""

import json
from collections.abc import Iterator, Mapping
from pathlib import Path

import pytest

from otto.host.element import Element
from otto.host.inventory_ref import InventoryRef
from otto.inventory import (
    BareReferenceRefused,
    InventoryError,
    InventoryView,
    RefusedDefault,
    resolve_host_entry,
)
from otto.labs import (
    Diagnostic,
    LabRepositoryError,
    LabStatus,
    SupportsHostSummariesInView,
    SupportsInventoryView,
    SupportsLabStatus,
    host_summaries_in_view,
    load_lab_through,
)
from otto.labs.drops import collecting_drops
from otto.labs.json_repository import _JsonFileLabRepository
from otto.models.lab import LabEntrySpec
from tests._fixtures.named_inventory import FakeInventory

_CREDS = [{"login": "admin", "password": "pw"}]
_PHYSICAL = {"name": "physical", "key": "k"}
_VIRTUAL = {"name": "virtual", "key": "k"}
_NEEDS_VIEW = (
    "references inventory 'physical' (key 'k'), which needs a named-inventory view; "
    "this join resolves through the 'default' inventory only"
)
"""What a legacy ``inventory=`` argument answers for a reference naming ``physical``."""


def _write(path: Path, doc: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc))
    return path


def _source(tmp_path: Path, doc: dict) -> _JsonFileLabRepository:
    _write(tmp_path / "lab.json", doc)
    return _JsonFileLabRepository(search_paths=[tmp_path])


def _view(**inventories: FakeInventory) -> InventoryView:
    return InventoryView(inventories=inventories)


def _one_qualified_lab() -> dict:
    return {
        "labs": {"bench": {}},
        "elements": [
            {
                "name": "sw",
                "labs": ["bench"],
                "hosts": [{"inventory": _PHYSICAL, "creds": _CREDS}],
            }
        ],
    }


def test_the_json_source_implements_every_view_capability(tmp_path):
    source = _source(tmp_path, _one_qualified_lab())
    assert isinstance(source, SupportsInventoryView)
    assert isinstance(source, SupportsHostSummariesInView)
    assert isinstance(source, SupportsLabStatus)


def test_a_qualified_entry_loads_through_the_view(tmp_path):
    source = _source(tmp_path, _one_qualified_lab())
    view = _view(
        default=FakeInventory({"k": {"ip": "10.9.9.9"}}),
        physical=FakeInventory({"k": {"ip": "10.0.0.1"}}, label="fake:physical"),
    )
    lab = load_lab_through(repository=source, name="bench", view=view)
    (host,) = lab.hosts.values()
    assert host.ip == "10.0.0.1"
    assert host.inventory_ref.name == "physical"
    assert host.inventory_ref.qualified is True
    assert host.inventory_ref.backend == "fake:physical"


def test_a_qualified_entry_is_summarized_through_the_view(tmp_path):
    source = _source(tmp_path, _one_qualified_lab())
    view = _view(physical=FakeInventory({"k": {"ip": "10.0.0.1"}}, label="fake:physical"))
    with collecting_drops() as drops:
        (summary,) = host_summaries_in_view(repository=source, view=view)
    assert drops == []
    assert summary.id == "sw"
    assert summary.ip == "10.0.0.1"
    assert summary.labs == ["bench"]
    assert summary.inventory_ref == InventoryRef(
        key="k", backend="fake:physical", name="physical", qualified=True
    )


def test_a_bare_entry_is_summarized_with_a_default_ref(tmp_path):
    doc = _one_qualified_lab()
    doc["elements"][0]["hosts"] = [{"inventory": "k", "creds": _CREDS}]
    source = _source(tmp_path, doc)
    (summary,) = source.list_host_summaries(inventory=FakeInventory({"k": {"ip": "10.0.0.2"}}))
    assert summary.inventory_ref == InventoryRef(key="k", backend="fake:mem")
    assert summary.inventory_ref.name == "default"
    assert summary.inventory_ref.qualified is False


def test_legacy_load_lab_still_accepts_inventory(tmp_path):
    doc = _one_qualified_lab()
    doc["elements"][0]["hosts"] = [
        {"inventory": "k", "creds": _CREDS},
        {"inventory": {"name": "default", "key": "j"}, "creds": _CREDS},
    ]
    source = _source(tmp_path, doc)
    inventory = FakeInventory({"k": {"ip": "10.0.0.3"}, "j": {"ip": "10.0.0.4", "board": "b"}})
    lab = source.load_lab("bench", inventory=inventory)
    assert {h.id: h.ip for h in lab.hosts.values()} == {"sw": "10.0.0.3", "sw-b": "10.0.0.4"}
    # A named inventory needs the view: the legacy call refuses it, naming the file.
    with pytest.raises(LabRepositoryError) as info:
        _source(tmp_path / "q", _one_qualified_lab()).load_lab("bench", inventory=inventory)
    assert str(info.value.__cause__) == _NEEDS_VIEW


def test_legacy_load_lab_keeps_the_refusing_stand_ins_contract(tmp_path):
    """What a legacy caller receives in a D1 project still refuses bare keys, and only those."""
    doc = _one_qualified_lab()
    doc["elements"][0]["hosts"] = [
        {"inventory": {"name": "default", "key": "j"}, "creds": _CREDS},
    ]
    default = FakeInventory({"j": {"ip": "10.0.0.4"}, "k": {"ip": "10.0.0.5"}})
    refused = RefusedDefault(
        origin="/p/.otto/settings.toml", declared=["default"], inventories={"default": default}
    )
    lab = _source(tmp_path, doc).load_lab("bench", inventory=refused)
    assert [h.ip for h in lab.hosts.values()] == ["10.0.0.4"]
    doc["elements"][0]["hosts"] = [{"inventory": "k", "ip": "10.0.0.1", "creds": _CREDS}]
    with pytest.raises(LabRepositoryError) as info:
        _source(tmp_path / "bare", doc).load_lab("bench", inventory=refused)
    assert isinstance(info.value.__cause__, BareReferenceRefused)


def _legacy_arguments() -> dict:
    physical = FakeInventory({"k": {"ip": "10.0.0.1"}}, label="fake:physical")
    default = FakeInventory({"k": {"ip": "10.0.0.2"}})
    return {
        "a stand-in holding physical": RefusedDefault(
            origin="/p/.otto/settings.toml",
            declared=["default", "physical"],
            inventories={"default": default, "physical": physical},
        ),
        "a plain inventory": default,
    }


@pytest.mark.parametrize("argument", sorted(_legacy_arguments()))
def test_a_legacy_argument_reaches_default_only_on_every_entry_point(tmp_path, argument):
    """The json source's legacy ``load_lab`` and ``resolve_host_entry`` answer alike.

    A legacy ``inventory=`` argument stands for ``default`` alone, so a
    reference naming ``physical`` fails with one text on both, even when the
    stand-in's catalog holds ``physical``.
    """
    inventory = _legacy_arguments()[argument]
    entry = {"inventory": _PHYSICAL, "creds": _CREDS}
    with pytest.raises(InventoryError) as direct:
        resolve_host_entry(entry, inventory=inventory, element=Element(name="sw"))
    with pytest.raises(LabRepositoryError) as loaded:
        _source(tmp_path, _one_qualified_lab()).load_lab("bench", inventory=inventory)
    assert str(direct.value) == _NEEDS_VIEW
    assert type(loaded.value.__cause__) is type(direct.value)
    assert str(loaded.value.__cause__) == str(direct.value)


def test_unavailable_is_reported_with_its_reason(tmp_path):
    path = _write(
        tmp_path / "lab.json",
        {"labs": {"bench": {"unavailable": "rack B is being recabled"}, "ok": {}}},
    )
    source = _JsonFileLabRepository(search_paths=[tmp_path])
    statuses = source.list_lab_status(view=_view())
    assert statuses == [
        LabStatus(
            name="bench",
            unavailable=Diagnostic(where=f"{path}: lab 'bench'", reason="rack B is being recabled"),
        ),
        LabStatus(name="ok", unavailable=None),
    ]
    assert source.list_labs() == ["bench", "ok"]


def test_an_ordinary_lab_has_no_diagnostic(tmp_path):
    source = _source(tmp_path, _one_qualified_lab())
    assert source.list_lab_status(view=_view()) == [LabStatus(name="bench", unavailable=None)]
    assert source.list_lab_status(view=_view())[0].available


class _Untouchable(Mapping):
    """An inventories mapping that fails the test on any access."""

    def __getitem__(self, key: str) -> FakeInventory:
        raise AssertionError(f"status read the view ({key!r})")

    def __iter__(self) -> Iterator[str]:
        raise AssertionError("status iterated the view")

    def __len__(self) -> int:
        raise AssertionError("status sized the view")

    def __contains__(self, key: object) -> bool:
        raise AssertionError(f"status probed the view ({key!r})")


def test_status_never_touches_the_view(tmp_path):
    source = _source(tmp_path, {"labs": {"a": {}, "b": {"unavailable": "down"}}})
    statuses = source.list_lab_status(view=InventoryView(inventories=_Untouchable()))
    assert [(s.name, s.available) for s in statuses] == [("a", True), ("b", False)]


def test_two_inventories_at_one_ip_are_two_machines_in_summaries(tmp_path):
    """``physical/k`` in ``e`` and ``virtual/k`` in ``w`` compose one id at one address.

    Today's fold took one address for one machine; two inventories may reuse a
    private address for two machines, as ``Lab.__add__`` says. The first record's
    fields are kept, the second's lab joins the summary (``--lab w`` loads it
    there), and the second is one drop naming both identities.
    """
    path = _write(
        tmp_path / "lab.json",
        {
            "labs": {"e": {}, "w": {}},
            "elements": [
                {
                    "name": "r-x",
                    "labs": ["e"],
                    "hosts": [{"inventory": _PHYSICAL, "creds": _CREDS}],
                },
                {
                    "name": "r",
                    "labs": ["w"],
                    "hosts": [{"inventory": _VIRTUAL, "creds": _CREDS}],
                },
            ],
        },
    )
    source = _JsonFileLabRepository(search_paths=[tmp_path])
    view = _view(
        physical=FakeInventory({"k": {"ip": "10.0.0.9"}}, label="fake:physical"),
        virtual=FakeInventory({"k": {"ip": "10.0.0.9", "board": "x"}}, label="fake:virtual"),
    )
    with collecting_drops() as drops:
        (summary,) = source.list_host_summaries_in_view(view=view)
    assert summary.id == "r-x"
    assert summary.labs == ["e", "w"]
    assert summary.inventory_ref.name == "physical"
    (drop,) = drops
    assert drop.where == f"{path}: element 'r' hosts[0]"
    assert drop.reason == (
        "host id 'r-x': element 'r-x' (10.0.0.9) as physical/k collides with element 'r' "
        "board 'x' (10.0.0.9) as virtual/k — two inventories, or two keys of one, name two "
        "machines at one address; rename one"
    )


def test_one_inventory_restated_at_one_ip_still_folds(tmp_path):
    """One inventory at one address is one machine, as today: a bare key and a qualified one fold.

    Their keys are compared only when both references are qualified.
    """
    _write(
        tmp_path / "lab.json",
        {
            "labs": {"e": {}, "w": {}},
            "elements": [
                {"name": "r-x", "labs": ["e"], "hosts": [{"inventory": "k", "creds": _CREDS}]},
                {
                    "name": "r",
                    "labs": ["w"],
                    "hosts": [{"inventory": {"name": "default", "key": "j"}, "creds": _CREDS}],
                },
            ],
        },
    )
    source = _JsonFileLabRepository(search_paths=[tmp_path])
    with collecting_drops() as drops:
        (summary,) = source.list_host_summaries_in_view(
            view=_view(
                default=FakeInventory(
                    {"k": {"ip": "10.0.0.9"}, "j": {"ip": "10.0.0.9", "board": "x"}}
                )
            )
        )
    assert drops == []
    assert summary.labs == ["e", "w"]


def test_a_refused_bare_entry_is_one_drop_in_summaries(tmp_path):
    doc = _one_qualified_lab()
    doc["elements"][0]["hosts"] = [{"inventory": "k", "creds": _CREDS}]
    source = _source(tmp_path, doc)
    root = Path("/p")
    view = InventoryView(
        inventories={"physical": FakeInventory({})},
        bare_refused_by={root: "/p/.otto/settings.toml"},
    ).for_project(root=root)
    with collecting_drops() as drops:
        assert host_summaries_in_view(repository=source, view=view) == []
    assert [d.where for d in drops] == ["inventory reference 'k'"]


@pytest.mark.parametrize("reason", ["", "   "])
def test_an_empty_reason_is_refused(tmp_path, reason):
    refusal = "'unavailable' must state a reason"
    with pytest.raises(ValueError, match=refusal):
        LabEntrySpec.model_validate({"unavailable": reason})
    source = _source(tmp_path, {"labs": {"bench": {"unavailable": reason}}})
    with pytest.raises(LabRepositoryError, match=rf"labs\['bench'\](.|\n)*{refusal}"):
        source.load_lab("bench")


@pytest.mark.parametrize(
    ("doc", "where"),
    [
        ({"labs": {"bench": {"unavailable": {"password": "hunter2"}}}}, r"labs\['bench'\] "),
        (
            {"labs": {"bench": {}}, "elements": [{"name": {"password": "hunter2"}}]},
            r"elements\[0\] ",
        ),
    ],
    ids=["lab-entry", "element"],
)
def test_a_refused_lab_file_entry_never_prints_what_it_holds(tmp_path, doc, where):
    """Each entry is validated whole: pydantic's echo of its input must not reach the error."""
    source = _source(tmp_path, doc)
    with pytest.raises(LabRepositoryError, match=where) as e:
        source.load_lab("bench")
    assert "hunter2" not in str(e.value)
    assert "\n" not in str(e.value), "one line, not pydantic's block"


def test_status_skips_a_malformed_sibling_as_list_labs_does(tmp_path):
    good = _write(tmp_path / "a.json", {"labs": {"bench": {"unavailable": "down"}}})
    (tmp_path / "b.json").write_text("{not json")
    _write(tmp_path / "c.json", {"labs": {"bench": {}}})  # declared twice: skipped
    source = _JsonFileLabRepository(search_paths=[tmp_path / "*.json"])
    with collecting_drops():
        statuses = source.list_lab_status(view=_view())
        names = source.list_labs()
    assert statuses == [
        LabStatus(name="bench", unavailable=Diagnostic(where=f"{good}: lab 'bench'", reason="down"))
    ]
    assert names == [s.name for s in statuses]


@pytest.mark.parametrize("entry", ["load_lab_in_view", "load_lab(inventory=)", "summaries"])
def test_a_composite_keeps_the_d1_refusal_on_every_entry_point(tmp_path, entry):
    """One refusing project, the same answer through the view and the legacy ``inventory=``.

    The legacy argument is :attr:`InventoryView.bare_inventory`'s stand-in; a
    source with a project root must not re-point it at that root and lose
    the refusal (Fable I2 / Astra C1).
    """
    from otto.labs import CompositeLabRepository, LabSource
    from otto.labs.loader import load_lab
    from tests._fixtures.named_inventory import LookupCounting

    doc = _one_qualified_lab()
    doc["elements"][0]["hosts"] = [{"inventory": "k", "creds": _CREDS}]
    personal = LookupCounting({"k": {"ip": "10.1.0.2"}})
    root = tmp_path / "proj"
    view = InventoryView(
        inventories={"default": personal},
        bare_refused_by={root: str(root / ".otto" / "settings.toml")},
    ).for_project(root=root)
    composite = CompositeLabRepository(
        [LabSource(label="json:x", repository=_source(tmp_path, doc), project_root=root)]
    )
    if entry == "summaries":
        with collecting_drops() as drops:
            summaries = composite.list_host_summaries(inventory=view.bare_inventory)
        assert summaries == []
        assert [d.where for d in drops] == ["inventory reference 'k'"]
    else:

        def load() -> object:
            if entry == "load_lab_in_view":
                return composite.load_lab_in_view("bench", view=view)
            return load_lab("bench", repository=composite, inventory=view.bare_inventory)

        with pytest.raises(LabRepositoryError) as info:
            load()
        cause = info.value
        while cause is not None and not isinstance(cause, BareReferenceRefused):
            cause = cause.__cause__
        assert isinstance(cause, BareReferenceRefused)
    assert personal.lookups == 0


def test_summary_drops_keep_declaration_order(tmp_path: Path) -> None:
    """A summary's drops come out in declaration order, a collision among the entry skips.

    ``a`` + board ``b`` composes element ``a-b``'s id at another address, so it
    is dropped as a collision; the element after it holds an entry that does
    not validate. ``otto cache info`` prints the drops in this order.
    """
    source = _source(
        tmp_path,
        {
            "labs": {"bench": {}},
            "elements": [
                {"name": "a-b", "labs": ["bench"], "hosts": [{"ip": "10.0.0.1", "creds": _CREDS}]},
                {
                    "name": "a",
                    "labs": ["bench"],
                    "hosts": [{"ip": "10.0.0.2", "board": "b", "creds": _CREDS}],
                },
                {"name": "c", "labs": ["bench"], "hosts": [{"creds": _CREDS}]},
            ],
        },
    )
    with collecting_drops() as drops:
        summaries = source.list_host_summaries_in_view(view=_view())
    assert [s.id for s in summaries] == ["a-b"]
    assert [d.where for d in drops] == [
        f"{tmp_path / 'lab.json'}: element 'a' hosts[0]",
        f"{tmp_path / 'lab.json'}: element 'c' hosts[0]",
    ]

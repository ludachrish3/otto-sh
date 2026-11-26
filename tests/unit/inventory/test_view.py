"""The process inventory view and the catalog walk (lab templating, #1176)."""

import dataclasses
import json
import re
from pathlib import Path

import pytest
import tomli as tomllib

from otto.host.inventory_ref import DEFAULT_INVENTORY, InventoryReference, normalize_reference
from otto.inventory import (
    BareReferenceRefused,
    CredsOverlay,
    InventoryDeclaration,
    InventoryError,
    InventoryView,
    build_inventory_view,
    compile_inventory,
    deferred_inventory_view,
    inventory_declaration,
)
from otto.inventory.view import RefusedDefault
from otto.models.settings import InventoryConfigSpec
from tests._fixtures.fake_project import fake_project
from tests._fixtures.named_inventory import register_lookup_counting
from tests._fixtures.paths import CHECKOUT_ROOT, TESTS_ROOT
from tests.unit.inventory.test_resolve import FakeInventory

_GOLDEN = TESTS_ROOT / "_fixtures" / "unchanged_loading.json"
_CHECKOUT = CHECKOUT_ROOT


def _records(path: Path, records: dict) -> Path:
    path.write_text(json.dumps(records))
    return path


def _json(path: Path, ip: str = "10.0.0.1", key: str = "k") -> dict:
    """A json ``[inventory]``-shaped table for a file holding one record *key* at *ip*."""
    _records(path, {key: {"ip": ip}})
    return {"backend": "json", "path": str(path)}


def _creds(path: Path, login: str, key: str = "k") -> dict:
    path.write_text(json.dumps({key: [{"login": login, "password": "p"}]}))
    return {"backend": "json", "path": str(path)}


def _project(tmp_path: Path, name: str, **settings):
    root = tmp_path / name
    root.mkdir(exist_ok=True)
    return fake_project(name, root=root, settings=settings)


def _user_file(dir_: Path, body: str) -> Path:
    """Write a USER-level ``settings.toml``, not a project's."""
    path = dir_ / "user-settings.toml"
    path.write_text(body)
    return path


def _user_inventory(path: Path, ip: str) -> str:
    """A user file's ``[inventory]`` over a json file holding record ``k`` at *ip*."""
    _records(path, {"k": {"ip": ip}})
    return f'[inventory]\nbackend = "json"\npath = {json.dumps(str(path))}\n'


def _user_entry(name: str, path: Path, ip: str) -> str:
    """A user file's ``[[inventories]]`` entry *name* over a json file holding ``k`` at *ip*."""
    _records(path, {"k": {"ip": ip}})
    return f'[[inventories]]\nname = "{name}"\nbackend = "json"\npath = {json.dumps(str(path))}\n'


def _bare(key: str) -> InventoryReference:
    reference = normalize_reference(value=key)
    assert reference is not None
    return reference


def _qualified(name: str, key: str) -> InventoryReference:
    reference = normalize_reference(value={"name": name, "key": key})
    assert reference is not None
    return reference


def _origin(project) -> str:
    return str(project.root / ".otto" / "settings.toml")


# --------------------------------------------------------------------------- #
# The catalog walk
# --------------------------------------------------------------------------- #


def test_a_legacy_configuration_yields_todays_default(tmp_path):
    """Task 0's golden is the oracle: label and addresses recorded with the old code."""
    golden = json.loads(_GOLDEN.read_text())["tech1-inventory"]["unix"]["hosts"]
    tech1 = _CHECKOUT / "tests" / "_fixtures" / "lab_data" / "tech1-inventory"
    tables = tomllib.loads((tech1 / "user-settings.toml").read_text())
    creds_path = (tech1 / tables["creds"]["path"]).resolve()
    user = _user_file(
        tmp_path, f'[creds]\nbackend = "json"\npath = {json.dumps(str(creds_path))}\n'
    )
    project = fake_project(
        "tech1-inventory", root=tech1, settings={"inventory": tables["inventory"]}
    )
    view = build_inventory_view(projects=[project], user_settings_path=user)
    assert view.names() == [DEFAULT_INVENTORY]
    default = view.inventories[DEFAULT_INVENTORY]
    assert isinstance(default, CredsOverlay)
    for host in ("test1", "test2", "test3"):
        assert default.label == golden[host]["inventory_backend"].replace("<root>", str(_CHECKOUT))
        assert default.lookup(host).ip == golden[host]["ip"]
    assert default.store.label == f"json:{creds_path}"
    assert view.bare_inventory is default
    assert view.bare_refused_by == {}


def test_project_a_inventory_with_project_b_creds_still_composes(tmp_path):
    a = _project(tmp_path, "a", inventory=_json(tmp_path / "inv.json"))
    b = _project(tmp_path, "b", creds=_creds(tmp_path / "creds.json", "b-user"))
    view = build_inventory_view(projects=[a, b], user_settings_path=tmp_path / "absent.toml")
    record = view.inventories[DEFAULT_INVENTORY].lookup("k")
    assert record.ip == "10.0.0.1"
    assert [c.login for c in record.creds] == ["b-user"]


def test_one_project_inlining_the_default_store_does_not_conflict(tmp_path):
    table = _json(tmp_path / "inv.json")
    store = _creds(tmp_path / "creds.json", "store-user")
    a = _project(tmp_path, "a", inventories=[{"name": DEFAULT_INVENTORY, **table, "creds": store}])
    b = _project(tmp_path, "b", inventory=table, creds=store)
    for projects in ([a, b], [b, a]):
        view = build_inventory_view(projects=projects, user_settings_path=tmp_path / "absent.toml")
        record = view.inventories[DEFAULT_INVENTORY].lookup("k")
        assert [c.login for c in record.creds] == ["store-user"]
        assert view.bare_refused_by == {}


def test_two_projects_must_agree_on_a_name(tmp_path):
    a = _project(tmp_path, "a", inventories=[{"name": "physical", **_json(tmp_path / "pa.json")}])
    b = _project(tmp_path, "b", inventories=[{"name": "physical", **_json(tmp_path / "pb.json")}])
    with pytest.raises(InventoryError) as e:
        build_inventory_view(projects=[a, b], user_settings_path=tmp_path / "absent.toml")
    assert f"{_origin(a)} [[inventories]] 'physical'" in str(e.value)
    assert f"{_origin(b)} [[inventories]] 'physical'" in str(e.value)
    assert "two active projects declare different" in str(e.value)
    same = _project(
        tmp_path,
        "c",
        inventories=[{"name": "physical", "backend": "json", "path": str(tmp_path / "pa.json")}],
    )
    view = build_inventory_view(projects=[a, same], user_settings_path=tmp_path / "absent.toml")
    assert view.names() == ["physical"]


def test_one_sided_entry_creds_conflict(tmp_path):
    table = _json(tmp_path / "p.json")
    store = _creds(tmp_path / "creds.json", "u")
    a = _project(tmp_path, "a", inventories=[{"name": "physical", **table, "creds": store}])
    b = _project(tmp_path, "b", inventories=[{"name": "physical", **table}])
    for projects in ([a, b], [b, a]):
        with pytest.raises(InventoryError, match=r"\[\[inventories\]\] 'physical'"):
            build_inventory_view(projects=projects, user_settings_path=tmp_path / "absent.toml")
    agreeing = _project(tmp_path, "c", inventories=[{"name": "physical", **table, "creds": store}])
    view = build_inventory_view(projects=[a, agreeing], user_settings_path=tmp_path / "absent.toml")
    assert [c.login for c in view.inventories["physical"].lookup("k").creds] == ["u"]


def test_project_over_user_per_name(tmp_path):
    user = _user_file(
        tmp_path,
        _user_inventory(tmp_path / "ud.json", "10.1.0.1")
        + _user_entry("physical", tmp_path / "up.json", "10.1.0.2")
        + _user_entry("virtual", tmp_path / "uv.json", "10.1.0.3"),
    )
    project = _project(
        tmp_path, "p", inventories=[{"name": "physical", **_json(tmp_path / "pp.json", "10.2.0.2")}]
    )
    view = build_inventory_view(projects=[project], user_settings_path=user)
    assert view.names() == [DEFAULT_INVENTORY, "physical", "virtual"]
    assert view.inventories[DEFAULT_INVENTORY].lookup("k").ip == "10.1.0.1"
    assert view.inventories["physical"].lookup("k").ip == "10.2.0.2"
    assert view.inventories["virtual"].lookup("k").ip == "10.1.0.3"


def test_d6_refuses_at_bootstrap_through_the_view(tmp_path):
    project = _project(
        tmp_path,
        "p",
        inventories=[{"name": "physical", **_json(tmp_path / "p.json")}],
        creds=_creds(tmp_path / "creds.json", "u"),
    )
    with pytest.raises(InventoryError) as e:
        build_inventory_view(projects=[project], user_settings_path=tmp_path / "absent.toml")
    assert _origin(project) in str(e.value)
    assert "[creds] is refused" in str(e.value)
    assert "[inventories.creds]" in str(e.value)


@pytest.mark.parametrize("order", ["physical-first", "default-first"])
def test_a_creds_table_applies_to_default_and_never_to_a_named_inventory(tmp_path, order):
    """Review Focus 3: with D6 satisfied, [creds] binds to `default` only."""
    entries = [
        {"name": "physical", **_json(tmp_path / "physical.json", "10.9.0.1")},
        {"name": DEFAULT_INVENTORY, **_json(tmp_path / "default.json", "10.8.0.1")},
    ]
    if order == "default-first":
        entries.reverse()
    project = _project(
        tmp_path, "p", inventories=entries, creds=_creds(tmp_path / "creds.json", "store-user")
    )
    view = build_inventory_view(projects=[project], user_settings_path=tmp_path / "absent.toml")
    assert view.inventories["default"].lookup("k").creds[0].login == "store-user"
    assert view.inventories["default"].lookup("k").ip == "10.8.0.1"
    assert view.inventories["physical"].lookup("k").creds == []
    assert view.inventories["physical"].lookup("k").ip == "10.9.0.1"


def test_selection_and_credentials_never_depend_on_declaration_order(tmp_path):
    a = _project(
        tmp_path,
        "a",
        inventory=_json(tmp_path / "d.json"),
        inventories=[{"name": "physical", **_json(tmp_path / "p.json", "10.3.0.1")}],
    )
    b = _project(
        tmp_path,
        "b",
        creds=_creds(tmp_path / "creds.json", "store-user"),
        inventories=[
            {"name": DEFAULT_INVENTORY, "backend": "json", "path": str(tmp_path / "d.json")},
            {
                "name": "virtual",
                **_json(tmp_path / "v.json", "10.4.0.1"),
                "creds": _creds(tmp_path / "vcreds.json", "v-user"),
            },
        ],
    )

    def seen(view):
        out = {}
        for name in view.names():
            record = view.select(reference=_qualified(name, "k")).lookup("k")
            out[name] = (record.ip, [c.login for c in record.creds or []])
        return out

    absent = tmp_path / "absent.toml"
    forward = build_inventory_view(projects=[a, b], user_settings_path=absent)
    backward = build_inventory_view(projects=[b, a], user_settings_path=absent)
    assert (
        seen(forward)
        == seen(backward)
        == {
            DEFAULT_INVENTORY: ("10.0.0.1", ["store-user"]),
            "physical": ("10.3.0.1", []),
            "virtual": ("10.4.0.1", ["v-user"]),
        }
    )
    assert forward.memo_identity() == backward.memo_identity()


# --------------------------------------------------------------------------- #
# Selection and D1
# --------------------------------------------------------------------------- #


def test_a_qualified_default_is_allowed_from_a_refusing_project():
    root = Path("/projects/b")
    d = FakeInventory({"k": {"ip": "10.0.0.1"}})
    view = InventoryView(
        inventories={DEFAULT_INVENTORY: d, "physical": FakeInventory({})},
        bare_refused_by={root: "B"},
    ).for_project(root=root)
    assert view.selected_name(reference=_qualified(DEFAULT_INVENTORY, "k")) == DEFAULT_INVENTORY
    assert view.select(reference=_qualified(DEFAULT_INVENTORY, "k")) is d
    with pytest.raises(BareReferenceRefused):
        view.selected_name(reference=_bare("k"))
    refused = view.bare_inventory
    assert isinstance(refused, RefusedDefault)
    assert refused.default is d
    # Another project in the same process is not refused.
    assert view.for_project(root=Path("/projects/a")).bare_inventory is d


def test_bare_inventory_is_a_refusal_for_a_refusing_project():
    root = Path("/projects/b")
    d = FakeInventory({"edge-17": {"ip": "10.0.0.1"}})
    seen = []
    view = (
        InventoryView(inventories={"default": d}, bare_refused_by={root: "B"})
        .for_project(root=root)
        .with_refusal_hook(hook=lambda w, r: seen.append(r))
    )
    with pytest.raises(BareReferenceRefused) as e:
        view.bare_inventory.lookup("edge-17")
    assert json.loads(re.search(r"\{.*\}", str(e.value)).group()) == {
        "name": "<inventory>",
        "key": "edge-17",
    }
    assert seen == [str(e.value)]
    refused = view.bare_inventory
    assert refused.label == "refused:B"
    assert refused.supplies == frozenset({"ip"})
    assert refused.list_keys() == []
    assert refused.fingerprint() is None


def test_selection_errors_name_the_problem():
    empty = InventoryView.of_default(inventory=None)
    with pytest.raises(InventoryError, match=r"references inventory key 'k' but no inventory"):
        empty.selected_name(reference=_bare("k"))
    with pytest.raises(InventoryError, match=r"references inventory key 'k' but no inventory"):
        empty.selected_name(reference=_qualified(DEFAULT_INVENTORY, "k"))
    view = InventoryView(inventories={"physical": FakeInventory({}), "virtual": FakeInventory({})})
    with pytest.raises(InventoryError, match=r"'lab'.*declared inventories: 'physical', 'virtual'"):
        view.selected_name(reference=_qualified("lab", "k"))
    with pytest.raises(InventoryError, match=r"no inventory named 'lab'"):
        view.select_name(name="lab")


def test_of_default_is_keyword_only():
    assert InventoryView.of_default(inventory=None).bare_inventory is None
    d = FakeInventory({})
    assert InventoryView.of_default(inventory=d).bare_inventory is d
    with pytest.raises(TypeError):
        InventoryView.of_default(None)  # ty: ignore[too-many-positional-arguments]


def test_the_bootstrap_view_records_refusing_projects(tmp_path):
    refusing = _project(
        tmp_path, "r", inventories=[{"name": "physical", **_json(tmp_path / "p.json")}]
    )
    binding = _project(
        tmp_path,
        "b",
        inventory=_json(tmp_path / "d.json"),
        inventories=[{"name": "virtual", **_json(tmp_path / "v.json")}],
    )
    view = build_inventory_view(
        projects=[refusing, binding], user_settings_path=tmp_path / "absent.toml"
    )
    assert view.bare_refused_by == {refusing.root: _origin(refusing)}
    with pytest.raises(BareReferenceRefused, match=re.escape(_origin(refusing))):
        view.for_project(root=refusing.root).select(reference=_bare("k"))
    assert view.for_project(root=binding.root).select(reference=_bare("k")).lookup("k").ip


# --------------------------------------------------------------------------- #
# The deferred view
# --------------------------------------------------------------------------- #


def test_a_deferred_view_constructs_nothing_until_used(tmp_path):
    project = _project(tmp_path, "p", inventory={"backend": "unregistered-backend"})
    view = deferred_inventory_view(projects=[project], user_settings_path=tmp_path / "absent.toml")
    assert view.names() == [DEFAULT_INVENTORY]
    assert DEFAULT_INVENTORY in view.inventories
    with pytest.raises(InventoryError) as first:
        view.inventories[DEFAULT_INVENTORY]
    with pytest.raises(InventoryError) as second:
        view.inventories[DEFAULT_INVENTORY]
    assert first.value is second.value
    assert "unregistered-backend" in str(first.value)


def test_the_deferred_view_reads_user_settings_once_and_late(tmp_path, monkeypatch):
    from otto.config import user_settings

    calls = []
    real = user_settings.load_user_settings

    def counting(**kw):
        calls.append(kw)
        return real(**kw)

    monkeypatch.setattr(user_settings, "load_user_settings", counting)

    def body(tag: str) -> str:
        return _user_inventory(tmp_path / f"d-{tag}.json", f"10.{tag}.0.1") + _user_entry(
            "physical", tmp_path / f"p-{tag}.json", f"10.{tag}.0.2"
        )

    user = _user_file(tmp_path, body("1"))
    view = deferred_inventory_view(projects=[], user_settings_path=user)
    assert calls == []
    user.write_text(body("2"))  # the user-level file, not a project
    assert view.inventories["physical"].lookup("k").ip == "10.2.0.2"
    assert view.inventories[DEFAULT_INVENTORY].lookup("k").ip == "10.2.0.1"
    assert view.names() == [DEFAULT_INVENTORY, "physical"]
    assert len(calls) == 1


def test_a_deferred_view_retains_a_user_file_read_error(tmp_path):
    user = _user_file(tmp_path, "[inventory\n")
    project = _project(
        tmp_path,
        "p",
        inventory=_json(tmp_path / "d.json"),
        inventories=[{"name": "physical", **_json(tmp_path / "p.json")}],
    )
    view = deferred_inventory_view(projects=[project], user_settings_path=user)
    errors = []
    for name in (DEFAULT_INVENTORY, "physical", DEFAULT_INVENTORY):
        with pytest.raises(InventoryError) as e:
            view.inventories[name]
        errors.append(e.value)
    assert errors[0] is errors[1] is errors[2]
    assert str(user) in str(errors[0])


def test_a_deferred_name_ignores_an_unrelated_default_conflict(tmp_path):
    a = _project(tmp_path, "a", inventory=_json(tmp_path / "a.json"))
    b = _project(
        tmp_path,
        "b",
        inventory=_json(tmp_path / "b.json"),
        inventories=[{"name": "physical", **_json(tmp_path / "p.json", "10.5.0.1")}],
    )
    view = deferred_inventory_view(projects=[a, b], user_settings_path=tmp_path / "absent.toml")
    assert view.inventories["physical"].lookup("k").ip == "10.5.0.1"
    with pytest.raises(InventoryError, match=r"different \[inventory\] tables"):
        view.inventories[DEFAULT_INVENTORY]


def test_a_deferred_view_refuses_bare_keys_before_building_anything(tmp_path):
    """A broken user file and an unbuildable entry never pre-empt the D1 refusal."""
    user = _user_file(tmp_path, "[inventory\n")
    project = _project(tmp_path, "p", inventories=[{"name": "physical", "backend": "nope"}])
    view = deferred_inventory_view(projects=[project], user_settings_path=user)
    assert view.bare_refused_by == {project.root: _origin(project)}
    with pytest.raises(BareReferenceRefused, match=r"declared inventories: 'physical'"):
        view.for_project(root=project.root).selected_name(reference=_bare("k"))


def test_a_deferred_views_bare_inventory_refuses_before_reading_default(tmp_path):
    """D1 wins on ``bare_inventory`` too: an unreadable user file surfaces only where used."""
    user = _user_file(tmp_path, "[inventory\n")
    project = _project(tmp_path, "p", inventories=[{"name": "physical", "backend": "nope"}])
    view = deferred_inventory_view(projects=[project], user_settings_path=user)
    refused = view.for_project(root=project.root).bare_inventory
    assert isinstance(refused, RefusedDefault)
    with pytest.raises(BareReferenceRefused, match=r"declared inventories: 'physical'"):
        refused.lookup("k")
    # A qualified reference to ``default`` still meets the read error, where it is used.
    with pytest.raises(InventoryError, match=re.escape(str(user))):
        _ = refused.default


# --------------------------------------------------------------------------- #
# Fingerprint, stat paths and memo identity
# --------------------------------------------------------------------------- #


class _Fp:
    def __init__(self, fingerprint, paths=None):
        self._fingerprint = fingerprint
        self._paths = paths

    def fingerprint(self):
        return self._fingerprint

    def stat_paths(self):
        return self._paths


def test_fingerprint_and_stat_paths_three_cases(tmp_path):
    empty = InventoryView(inventories={})
    assert empty.fingerprint() == "none"
    assert empty.stat_paths() == []

    only = _Fp("fp-1", [tmp_path / "a"])
    alone = InventoryView.of_default(inventory=only)
    assert alone.fingerprint() == "fp-1"
    assert alone.stat_paths() == [tmp_path / "a"]
    assert InventoryView.of_default(inventory=_Fp(None, None)).stat_paths() is None

    named = InventoryView(
        inventories={
            "physical": _Fp("p", [tmp_path / "b", tmp_path / "a"]),
            DEFAULT_INVENTORY: _Fp("d", [tmp_path / "a"]),
        }
    )
    assert named.fingerprint() == json.dumps([[DEFAULT_INVENTORY, "d"], ["physical", "p"]])
    assert named.stat_paths() == [tmp_path / "a", tmp_path / "b"]
    # A "|" inside a fingerprint cannot make two catalogs read alike.
    one = InventoryView(inventories={"a": _Fp("1|b=2")})
    two = InventoryView(inventories={"a": _Fp("1"), "b": _Fp("2")})
    assert one.fingerprint() != two.fingerprint()
    opaque = InventoryView(inventories={"a": _Fp(None, [tmp_path / "a"]), "b": object()})
    assert opaque.fingerprint() is None
    assert opaque.stat_paths() is None


def _netbox_view(tmp_path, project_name, filter_):
    project = _project(
        tmp_path,
        project_name,
        inventories=[
            {
                "name": "physical",
                "backend": "netbox",
                "url": "https://netbox.example",
                "filter": filter_,
                "cache_ttl": "0",
            }
        ],
    )
    return build_inventory_view(projects=[project], user_settings_path=tmp_path / "absent.toml")


def test_memo_identity_tells_same_label_bindings_apart(tmp_path):
    one = _netbox_view(tmp_path, "a", {"site": "a"})
    two = _netbox_view(tmp_path, "b", {"site": "b"})
    assert one.keys["physical"].label == two.keys["physical"].label
    assert one.memo_identity() != two.memo_identity()
    assert hash(one.memo_identity()) is not None
    # Another project binding the same settings binds the same key (its root,
    # which refuses bare references, is the only difference).
    same = _netbox_view(tmp_path, "c", {"site": "a"})
    assert same.keys["physical"] == one.keys["physical"]
    assert same.memo_identity() != one.memo_identity()


def test_memo_identity_follows_a_switched_symlink(tmp_path):
    _records(tmp_path / "a.json", {"k": {"ip": "10.0.0.1"}})
    _records(tmp_path / "b.json", {"k": {"ip": "10.0.0.2"}})
    current = tmp_path / "current.json"
    current.symlink_to(tmp_path / "a.json")
    project = _project(tmp_path, "p", inventory={"backend": "json", "path": str(current)})
    absent = tmp_path / "absent.toml"
    before = build_inventory_view(projects=[project], user_settings_path=absent)
    current.unlink()
    current.symlink_to(tmp_path / "b.json")
    after = build_inventory_view(projects=[project], user_settings_path=absent)
    assert before.keys[DEFAULT_INVENTORY].binding == after.keys[DEFAULT_INVENTORY].binding
    assert before.keys[DEFAULT_INVENTORY].label != after.keys[DEFAULT_INVENTORY].label
    assert before.memo_identity() != after.memo_identity()


def test_memo_identity_follows_a_switched_creds_symlink(tmp_path):
    _creds(tmp_path / "ca.json", "a")
    _creds(tmp_path / "cb.json", "b")
    current = tmp_path / "creds.json"
    current.symlink_to(tmp_path / "ca.json")
    project = _project(
        tmp_path,
        "p",
        inventory=_json(tmp_path / "d.json"),
        creds={"backend": "json", "path": str(current)},
    )
    absent = tmp_path / "absent.toml"
    before = build_inventory_view(projects=[project], user_settings_path=absent)
    current.unlink()
    current.symlink_to(tmp_path / "cb.json")
    after = build_inventory_view(projects=[project], user_settings_path=absent)
    assert before.keys[DEFAULT_INVENTORY].creds == after.keys[DEFAULT_INVENTORY].creds
    assert before.keys[DEFAULT_INVENTORY].label == after.keys[DEFAULT_INVENTORY].label
    assert before.memo_identity() != after.memo_identity()


def test_same_as_stays_structural(tmp_path):
    def compiled(value):
        return compile_inventory(
            cfg=InventoryConfigSpec(backend="netbox", url="https://nb", filter={"site_id": value}),
            anchor_dir=tmp_path,
            origin="o",
        )

    one, other = compiled(1), compiled(1.0)
    assert one.same_as(other=other)
    assert one.binding_identity() == other.binding_identity()
    assert hash(one.binding_identity()) == hash(other.binding_identity())
    assert not one.same_as(other=compiled(2))


def test_default_is_walked_once(tmp_path, monkeypatch):
    from otto.config import user_settings
    from otto.inventory import config

    reads = []
    walks = []
    real_read = user_settings.load_user_settings
    real_walk = config.build_binding_from_declarations

    def counting_read(**kw):
        reads.append(kw)
        return real_read(**kw)

    def counting_walk(**kw):
        walks.append(kw)
        return real_walk(**kw)

    monkeypatch.setattr(user_settings, "load_user_settings", counting_read)
    monkeypatch.setattr(config, "build_binding_from_declarations", counting_walk)
    user = _user_file(
        tmp_path,
        _user_inventory(tmp_path / "d.json", "10.0.0.1")
        + _user_entry("physical", tmp_path / "p.json", "10.0.0.2"),
    )
    view = build_inventory_view(projects=[], user_settings_path=user)
    assert view.names() == [DEFAULT_INVENTORY, "physical"]
    assert len(reads) == 1
    assert len(walks) == 1


# --------------------------------------------------------------------------- #
# Review Focus 6, and the one declaration reader
# --------------------------------------------------------------------------- #


def test_a_default_entry_without_creds_is_the_projects_default(tmp_path, monkeypatch):
    counting = register_lookup_counting(monkeypatch, records={"k": {"ip": "10.66.0.1"}})
    user = _user_file(tmp_path, '[inventory]\nbackend = "fake:counting"\n')
    project = _project(
        tmp_path,
        "p",
        inventories=[{"name": DEFAULT_INVENTORY, **_json(tmp_path / "own.json", "10.7.0.1")}],
    )
    view = build_inventory_view(projects=[project], user_settings_path=user)
    assert view.bare_refused_by == {}
    selected = view.for_project(root=project.root).select(reference=_bare("k"))
    assert selected.lookup("k").ip == "10.7.0.1"
    assert counting.lookups == 0


def test_the_doctor_and_bootstrap_read_one_declaration(tmp_path, monkeypatch):
    from otto.config import user_settings
    from otto.config.project import Project
    from otto.init.doctor import _inventory_for
    from otto.inventory import config
    from tests._fixtures.project_scaffold import make_project

    monkeypatch.setattr(user_settings, "load_user_settings", lambda **kw: None)
    root = make_project(
        tmp_path / "p",
        name="p",
        extra=(
            '[inventory]\nbackend = "json"\npath = "d.json"\n\n'
            '[[inventories]]\nname = "physical"\nbackend = "json"\npath = "p.json"\n'
        ),
    )
    _records(root / "d.json", {"k": {"ip": "10.0.0.1"}})
    _records(root / "p.json", {"k": {"ip": "10.0.0.2"}})
    seen: list[InventoryDeclaration | None] = []
    real = config.inventory_declaration

    def recording(**kw):
        seen.append(real(**kw))
        return seen[-1]

    monkeypatch.setattr(config, "inventory_declaration", recording)
    config.build_inventory(
        projects=[Project(root=root)], user_settings_path=tmp_path / "absent.toml"
    )
    _inventory_for(root)
    assert len(seen) == 2
    assert seen[0] == seen[1]
    assert seen[0] is not None
    assert [e["name"] for e in seen[0].inventories] == ["physical"]


@pytest.mark.parametrize(
    "toml", ["[inventories]\nname = 'vms'", "inventories = ['vms']", "inventory = 'x'"]
)
def test_a_malformed_table_is_a_value_error(toml):
    origin = "/p/.otto/settings.toml"
    with pytest.raises(ValueError, match=re.escape(origin)) as e:
        inventory_declaration(data=tomllib.loads(toml), root=Path("/p"), origin=origin)
    assert origin in str(e.value)
    key = "inventory" if toml.startswith("inventory =") else "inventories"
    assert key in str(e.value)


@pytest.mark.parametrize(
    "data",
    [
        {"inventory": [{"token": "hunter2"}]},
        {"creds": ["hunter2"]},
        {"inventories": [["netbox", "hunter2"]]},
    ],
    ids=["inventory", "creds", "inventories-entry"],
)
def test_a_malformed_table_never_prints_what_it_holds(data):
    """A table of the wrong shape is refused by its type, never by its contents."""
    with pytest.raises(ValueError, match="must be a table") as e:
        inventory_declaration(data=data, root=Path("/p"), origin="/p/.otto/settings.toml")
    assert "hunter2" not in str(e.value)


def test_a_declaration_is_kept_for_any_inventory_table():
    root = Path("/p")
    origin = "/p/.otto/settings.toml"
    assert inventory_declaration(data={"name": "p"}, root=root, origin=origin) is None
    assert inventory_declaration(data={"inventory": {}}, root=root, origin=origin) is None
    entry = {"name": DEFAULT_INVENTORY, "backend": "json", "path": "d.json"}
    decl = inventory_declaration(data={"inventories": [entry]}, root=root, origin=origin)
    assert decl == InventoryDeclaration(origin=origin, anchor_dir=root, inventories=[entry])
    with pytest.raises(ValueError, match=r"creds"):
        inventory_declaration(data={"creds": "x"}, root=root, origin=origin)


def test_a_broken_user_file_is_the_error_for_names_only_it_could_supply(tmp_path):
    """No project declares anything: a bare and a qualified reference name the broken file."""
    user = _user_file(tmp_path, "[inventory\n")
    bystander = _project(tmp_path, "p")
    view = deferred_inventory_view(projects=[bystander], user_settings_path=user).for_project(
        root=bystander.root
    )
    assert view.names() == []
    errors = []
    for reference in (_bare("k"), _qualified("physical", "k")):
        with pytest.raises(InventoryError) as e:
            view.select(reference=reference)
        errors.append(e.value)
    assert errors[0] is errors[1]
    assert str(user) in str(errors[0])
    assert "no inventory is configured" not in str(errors[0])


def test_memo_identity_needs_a_key_for_every_name():
    """A hand-built view has no binding keys, so it has no identity to collide on."""
    assert InventoryView(inventories={}).memo_identity() is not None
    for inventory in (FakeInventory({}, label="x"), FakeInventory({}, label="y")):
        with pytest.raises(ValueError, match=r"no binding key for 'default'"):
            InventoryView.of_default(inventory=inventory).memo_identity()


def test_views_compare_by_identity():
    """Value equality would build every name of a deferred view; memo_identity compares bindings."""
    one = InventoryView(inventories={})
    assert one != dataclasses.replace(one)
    assert one != InventoryView(inventories={})

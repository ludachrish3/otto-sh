"""A json inventory whose path names a ``.jsonnet`` file: the document is what it renders.

The file renders with no top-level argument, its imports served under the
table's ``import_paths`` only and never from the protected catalog of the
view (or the table) that built it. The rendered document is read as a written
one is. Its fingerprint stats every file the render may read, and never
renders.
"""

import threading
from pathlib import Path
from typing import Any

import pytest

from otto.inventory import (
    CredsOverlay,
    InventoryDataError,
    InventoryView,
    JsonInventory,
    below_creds_overlay,
    build_inventory_view,
    compile_inventory,
    construct_inventory,
    discover_inventory,
)
from otto.inventory.json_backend import is_templated_inventory
from otto.inventory.registry import INVENTORY_BACKENDS, InventoryEnv
from otto.models.settings import InventoryConfigSpec, NamedInventorySpec
from otto.templating import render as render_module
from otto.templating.errors import RenderCancelled
from otto.templating.imports import library_file
from otto.templating.memo import RenderMemo, cancellation
from tests._fixtures.named_inventory import FakeInventory, assert_same_discovery, json_twin

from ._lab_project import project, table, write
from ._render_helpers import IN_ROOT_FORMS, count_evaluations, hand_built_env, record_opens

WAIT = 30.0
"""How long a test waits for a worker thread; never reached when the code is right."""

FLEET = """
local fleet = import 'fleet.libsonnet';
{ [h.name]: { ip: h.ip, site: 'lab-a' } for h in fleet }
"""
"""An inventory of the hosts ``fleet.libsonnet`` lists, one record each."""

FLEET_LIB = "[{ name: 'test' + n, ip: '10.10.200.' + (10 + n) } for n in [1, 2, 3]]\n"


def _fleet(root: Path) -> tuple[Path, Path]:
    """Write ``root/virtual.jsonnet`` over ``root/lib/fleet.libsonnet``; return both paths."""
    lib = root / "lib"
    write(lib / "fleet.libsonnet", FLEET_LIB)
    return write(root / "virtual.jsonnet", FLEET), lib


def _hand_built(root: Path, text: str, *, roots: "tuple[Path, ...]" = ()) -> JsonInventory:
    """A hand-built templated inventory over ``root/inv.jsonnet`` holding *text*, with its env."""
    path = write(root / "inv.jsonnet", text).resolve()
    return JsonInventory(path, import_paths=roots, env=hand_built_env(root))


def _standalone(path: Path, lib: Path, **keys: Any) -> Any:
    """The inventory ``compile_inventory`` and ``construct_inventory`` build for one table."""
    cfg = InventoryConfigSpec.model_validate(
        {"backend": "json", "path": str(path), "import_paths": [str(lib)], **keys}
    )
    compiled = compile_inventory(cfg=cfg, anchor_dir=path.parent, origin="standalone")
    return construct_inventory(compiled=compiled)


def _virtual_table(path: Path, lib: Path, **keys: Any) -> str:
    return table(
        "[[inventories]]",
        name="virtual",
        backend="json",
        path=str(path),
        import_paths=[str(lib)],
        **keys,
    )


def _view(tmp_path: Path, projects: list[Any], user_file: "Path | None" = None) -> InventoryView:
    return build_inventory_view(
        projects=projects, user_settings_path=user_file or tmp_path / "absent.toml"
    )


# --------------------------------------------------------------------------- #
# The render.
# --------------------------------------------------------------------------- #


def test_jsonnet_inventory_gets_no_inputs(tmp_path):
    """No top-level argument and no external variable: a default is all a parameter gets."""
    defaulted = _hand_built(tmp_path, "function(otto='none') { [otto]: { ip: '10.0.0.1' } }")
    assert defaulted.list_keys() == ["none"]
    needs_otto = _hand_built(tmp_path / "a", "function(otto) { k: { ip: '10.0.0.1' } }")
    with pytest.raises(InventoryDataError, match="could not be rendered"):
        needs_otto.list_keys()
    ext = _hand_built(tmp_path / "b", "{ k: { ip: std.extVar('ip') } }")
    with pytest.raises(InventoryDataError, match="could not be rendered"):
        ext.list_keys()


def test_a_render_failure_names_the_inventory_file_once(tmp_path):
    lib = tmp_path / "lib"
    lib.mkdir()
    inventory = _hand_built(tmp_path, "importstr '/etc/hostname'", roots=(lib,))
    with pytest.raises(InventoryDataError) as caught:
        inventory.list_keys()
    message = str(caught.value)
    assert message.startswith(f"{inventory.path}: could not be rendered: import '/etc/hostname'")
    assert message.count(str(inventory.path)) == 1, message


def test_otto_libsonnet_is_importable_from_an_inventory(tmp_path):
    inventory = _hand_built(
        tmp_path,
        "local o = import 'otto.libsonnet';\n"
        "{ edge: { ip: '10.0.0.1', groups: if o.inGroup('dut')({ groups: ['dut'] })"
        " then ['dut'] else [] } }",
    )
    assert inventory.lookup("edge").groups == ["dut"]


def test_two_agreeing_declarations_render_the_same_records(tmp_path, otto_home):
    path, lib = _fleet(tmp_path / "shared")
    a = project(tmp_path / "a", _virtual_table(path, lib), name="a")
    b = project(tmp_path / "b", _virtual_table(path, lib), name="b")
    standalone = discover_inventory(inventory=_standalone(path, lib)).records
    assert sorted(standalone) == ["test1", "test2", "test3"]
    for projects in ([a, b], [b, a]):
        view = _view(tmp_path, projects)
        assert view.names() == ["virtual"]
        assert discover_inventory(inventory=view.inventories["virtual"]).records == standalone


def test_render_failure_is_a_data_error_never_a_stale_hit(tmp_path, otto_home):
    path, lib = _fleet(tmp_path / "shared")
    sut = project(tmp_path / "sut", _virtual_table(path, lib, cache_ttl="7d"))
    first = _view(tmp_path, [sut]).inventories["virtual"]
    assert first.list_keys() == ["test1", "test2", "test3"]
    write(path, "{ k: error 'the fleet is broken' }")
    broken = _view(tmp_path, [sut]).inventories["virtual"]
    for _ in range(2):  # a failure is never kept, and nothing stands in for it
        with pytest.raises(InventoryDataError, match="the fleet is broken") as caught:
            broken.list_keys()
        assert str(caught.value).startswith(f"{path.resolve()}: could not be rendered: ")
    write(path, FLEET)
    assert broken.list_keys() == ["test1", "test2", "test3"]


def test_records_and_instances_round_trip_through_export(tmp_path):
    inventory = _hand_built(
        tmp_path,
        """
        {
          '$format': 'otto.inventory.v2',
          records: { ['test' + n]: { ip: '10.10.200.' + (10 + n), site: 'lab-a' } for n in [1, 2] },
          instances: { 'lab-a': { members: ['test1', 'test2'] } },
        }
        """,
    )
    found = discover_inventory(inventory=inventory)
    assert sorted(found.records) == ["test1", "test2"]
    assert found.instances is not None
    assert sorted(found.instances) == ["lab-a"]
    assert_same_discovery(left=inventory, right=json_twin(tmp_path=tmp_path, discovery=found))


def test_secrets_in_a_rendered_document_are_wrapped(tmp_path):
    inventory = _hand_built(
        tmp_path, "{ k: { ip: '10.0.0.1', creds: [{ login: 'u', password: 's3cret' }] } }"
    )
    [cred] = inventory.lookup("k").creds
    assert "s3cret" not in repr(cred)
    assert cred.password is not None
    assert cred.password.get_secret_value() == "s3cret"


# --------------------------------------------------------------------------- #
# Freshness.
# --------------------------------------------------------------------------- #


def test_fingerprint_never_evaluates(tmp_path, monkeypatch):
    path, lib = _fleet(tmp_path)
    inventory = JsonInventory(path, import_paths=(lib,), env=hand_built_env(tmp_path))
    n = count_evaluations(monkeypatch)
    inventory.fingerprint()
    assert inventory.stat_paths() == [path, *sorted([lib, lib / "fleet.libsonnet", library_file()])]
    assert n() == 0
    inventory.list_keys()
    inventory.lookup("test1")
    assert n() == 1  # once per instance


def test_editing_an_import_moves_the_fingerprint(tmp_path):
    path, lib = _fleet(tmp_path)
    inventory = JsonInventory(path, import_paths=(lib,))
    before = inventory.fingerprint()
    assert before == inventory.fingerprint()
    write(lib / "fleet.libsonnet", FLEET_LIB.replace("[1, 2, 3]", "[1, 2, 3, 4]"))
    assert inventory.fingerprint() != before


def test_a_new_file_under_a_root_moves_the_fingerprint(tmp_path):
    path, lib = _fleet(tmp_path)
    later = tmp_path / "later"  # a root that does not exist yet is listed itself
    inventory = JsonInventory(path, import_paths=(lib, later))
    before = inventory.fingerprint()
    write(lib / "nested" / "shadow.libsonnet", "[]")
    moved = inventory.fingerprint()
    assert moved != before
    assert lib / "nested" / "shadow.libsonnet" in (inventory.stat_paths() or [])
    write(later / "fleet.libsonnet", "[]")
    assert inventory.fingerprint() != moved


def test_a_written_json_inventory_keeps_its_one_file_fingerprint(tmp_path):
    path = write(tmp_path / "inv.json", {"k": {"ip": "10.0.0.1"}})
    inventory = JsonInventory(path, import_paths=(tmp_path / "lib",))
    assert not inventory.templated
    assert inventory.stat_paths() == [path]
    assert inventory.fingerprint() == JsonInventory(path).fingerprint()


# --------------------------------------------------------------------------- #
# The protected catalog.
# --------------------------------------------------------------------------- #

PROTECTED_KINDS = ("another-projects-creds", "user-files-inventory", "settings-toml")


def _protected_target(kind: str, tmp_path: Path, *, other: Path, user_file: Path) -> Path:
    return {
        "another-projects-creds": other / "lab_data" / "creds.json",
        "user-files-inventory": user_file.parent / "user-inv.json",
        "settings-toml": other / ".otto" / "settings.toml",
    }[kind]


@pytest.mark.parametrize("form", IN_ROOT_FORMS)
@pytest.mark.parametrize("kind", PROTECTED_KINDS)
@pytest.mark.parametrize("consumer", ["inventory"])
def test_protected_file_is_refused_before_open(
    consumer, kind, form, tmp_path, monkeypatch, otto_home
):
    """Review Focus 1's inventory leg: each protected class, each in-root spelling."""
    del consumer
    user_file = write(
        tmp_path / "user-home" / "settings.toml",
        table("[inventory]", backend="json", path="user-inv.json"),
    )
    write(user_file.parent / "user-inv.json", {"u": {"ip": "10.0.0.9"}})
    other_root = tmp_path / "other"
    write(other_root / "lab_data" / "phys.json", {"p": {"ip": "10.0.0.8"}})
    write(other_root / "lab_data" / "creds.json", {"p": [{"login": "u", "password": "s3cret"}]})
    other = project(
        other_root,
        table(
            "[[inventories]]",
            name="phys",
            backend="json",
            path=str(other_root / "lab_data" / "phys.json"),
            creds={"backend": "json", "path": str(other_root / "lab_data" / "creds.json")},
        ),
        name="other",
    )
    target = _protected_target(kind, tmp_path, other=other_root, user_file=user_file)
    if kind != "another-projects-creds":
        target.write_text(target.read_text() + '\n# "s3cret"\n')
    root = target.parent
    (root / "alias").symlink_to(target)
    spelled = {
        "relative": target.name,
        "dotdot": f"../{root.name}/{target.name}",
        "absolute": str(target),
        "symlink": "alias",
    }[form]
    entry = write(tmp_path / "sut" / "virtual.jsonnet", f"{{ k: {{ ip: importstr '{spelled}' }} }}")
    sut = project(tmp_path / "sut", _virtual_table(entry, root))
    inventory = _view(tmp_path, [sut, other], user_file).inventories["virtual"]
    opened = record_opens(monkeypatch)
    with pytest.raises(InventoryDataError, match="refused") as caught:
        inventory.list_keys()
    assert not [p for p in opened() if p.resolve() == target.resolve()]
    assert "s3cret" not in str(caught.value)


@pytest.mark.parametrize("target", ["creds.json", "settings.toml"])
def test_a_standalone_table_protects_its_own_paths(target, tmp_path, monkeypatch):
    """No view built the table: the render runs under its own catalog, origin file included."""
    lib = tmp_path / "lib"
    write(lib / "creds.json", {"k": [{"login": "u", "password": "s3cret"}]})
    settings = write(lib / "settings.toml", '# "s3cret"\n')
    entry = write(tmp_path / "inv.jsonnet", f"{{ k: {{ ip: importstr '{target}' }} }}")
    cfg = NamedInventorySpec(
        name="virtual",
        backend="json",
        path=str(entry),
        import_paths=[str(lib)],
        creds={"backend": "json", "path": str(lib / "creds.json")},
    )
    compiled = compile_inventory(cfg=cfg, anchor_dir=tmp_path, origin=str(settings))
    inventory = construct_inventory(compiled=compiled)
    opened = record_opens(monkeypatch)
    with pytest.raises(InventoryDataError, match="refused") as caught:
        inventory.list_keys()
    assert not [p for p in opened() if p.name == target]
    assert "s3cret" not in str(caught.value)


def test_an_inventory_with_no_env_is_refused_never_rendered_unprotected(tmp_path, monkeypatch):
    """No env, no catalog: the render is refused before evaluating; a written file still reads."""
    path, lib = _fleet(tmp_path)
    n = count_evaluations(monkeypatch)
    with pytest.raises(InventoryDataError, match="no protected-file catalog") as caught:
        JsonInventory(path, import_paths=(lib,)).list_keys()
    assert str(caught.value).startswith(f"{path}: could not be rendered: ")
    assert n() == 0
    written = write(tmp_path / "inv.json", {"k": {"ip": "10.0.0.1"}})
    assert JsonInventory(written).list_keys() == ["k"]


def test_a_view_built_inventory_never_takes_a_views_empty_default(tmp_path, otto_home):
    """The catalog rides the table's env: read through a hand-built view, it still refuses."""
    lib = tmp_path / "lib"
    write(lib / "creds.json", {"k": [{"login": "u", "password": "s3cret"}]})
    entry = write(tmp_path / "sut" / "virtual.jsonnet", "{ k: { ip: importstr 'creds.json' } }")
    sut = project(
        tmp_path / "sut",
        _virtual_table(entry, lib, creds={"backend": "json", "path": str(lib / "creds.json")}),
    )
    built = _view(tmp_path, [sut]).inventories["virtual"]
    legacy = InventoryView.of_default(inventory=built)
    with pytest.raises(InventoryDataError, match="refused"):
        legacy.inventories["default"].list_keys()


# --------------------------------------------------------------------------- #
# Wrappers.
# --------------------------------------------------------------------------- #


def test_is_templated_inventory_sees_through_creds_overlay(tmp_path, otto_home):
    path, lib = _fleet(tmp_path / "shared")
    creds = write(tmp_path / "creds.json", {"test1": [{"login": "u", "password": "p"}]})
    templated = JsonInventory(path, import_paths=(lib,))
    written = JsonInventory(write(tmp_path / "inv.json", {"k": {"ip": "10.0.0.1"}}))
    store = _store(creds)
    assert is_templated_inventory(templated)
    assert is_templated_inventory(CredsOverlay(templated, store=store))
    assert is_templated_inventory(CredsOverlay(CredsOverlay(templated, store=store), store=store))
    assert not is_templated_inventory(written)
    assert not is_templated_inventory(CredsOverlay(written, store=store))
    assert not is_templated_inventory(FakeInventory({"k": {"ip": "10.0.0.1"}}))
    sut = project(
        tmp_path / "sut", _virtual_table(path, lib, creds={"backend": "json", "path": str(creds)})
    )
    built = _view(tmp_path, [sut]).inventories["virtual"]
    assert isinstance(built, CredsOverlay)
    assert is_templated_inventory(built)


def _store(path: Path) -> Any:
    from otto.creds.json_store import _JsonCredsStore

    return _JsonCredsStore(path)


def test_jsonnet_inventory_is_never_snapshot_cached(tmp_path, otto_home):
    """The built-in json states ``snapshot_cache=False``, so no cache wrapper is ever peeled."""
    assert INVENTORY_BACKENDS.peek(name="json").metadata.snapshot_cache is False
    path, lib = _fleet(tmp_path / "shared")
    sut = project(tmp_path / "sut", _virtual_table(path, lib, cache_ttl="30d"))
    built = _view(tmp_path, [sut]).inventories["virtual"]
    assert type(below_creds_overlay(inventory=built)) is JsonInventory
    assert type(below_creds_overlay(inventory=_standalone(path, lib, cache_ttl="30d"))) is (
        JsonInventory
    )


# --------------------------------------------------------------------------- #
# Cancellation.
# --------------------------------------------------------------------------- #


def test_cancelled_inventory_render_never_evaluates_and_stays_shareable(tmp_path, monkeypatch):
    path, lib = _fleet(tmp_path)
    shared = JsonInventory(path, import_paths=(lib,), env=InventoryEnv(tmp_path, "o.toml"))
    view_a = InventoryView(inventories={"virtual": shared})
    view_b = InventoryView(inventories={"virtual": shared})
    memo_a: RenderMemo = view_a.render_memo
    memo_b: RenderMemo = view_b.render_memo
    a_root, b_root = tmp_path / "a", tmp_path / "b"
    reached = threading.Event()
    release = threading.Event()
    real_load = render_module.load_binding

    def blocking_load() -> Any:
        """Hold project A's render just before the cancellation check and the evaluator."""
        binding = real_load()
        if threading.current_thread().name == "project-a":
            reached.set()
            assert release.wait(WAIT)
        return binding

    monkeypatch.setattr(render_module, "load_binding", blocking_load)
    n = count_evaluations(monkeypatch)
    outcomes: list[object] = []

    def project_a() -> None:
        with cancellation(memo_a.cancelled(a_root)):
            try:
                outcomes.append(view_a.inventories["virtual"].list_keys())
            except RenderCancelled as e:
                outcomes.append(e)

    worker = threading.Thread(target=project_a, name="project-a", daemon=True)
    worker.start()
    assert reached.wait(WAIT)
    memo_a.cancel(scope=a_root, reason="the completion deadline passed")
    release.set()
    worker.join(WAIT)
    assert not worker.is_alive()
    [outcome] = outcomes
    assert isinstance(outcome, RenderCancelled)
    assert n() == 0
    with cancellation(memo_b.cancelled(b_root)):
        assert view_b.inventories["virtual"].list_keys() == ["test1", "test2", "test3"]
    assert n() == 1


# --------------------------------------------------------------------------- #
# The table.
# --------------------------------------------------------------------------- #


def test_import_paths_are_anchored_like_the_path(tmp_path):
    prepared = INVENTORY_BACKENDS.prepare(
        name="json",
        raw={"path": "inv.jsonnet", "import_paths": ["lib", "~/x", str(tmp_path / "abs")]},
        env=InventoryEnv(tmp_path, "o.toml"),
    )
    built = INVENTORY_BACKENDS.build(prepared=prepared)
    assert isinstance(built, JsonInventory)
    assert built.templated
    assert built.import_paths == (tmp_path / "lib", Path("~/x").expanduser(), tmp_path / "abs")
    assert built.env == InventoryEnv(tmp_path, "o.toml")


def test_import_paths_default_to_none_and_refuse_an_empty_entry(tmp_path):
    env = InventoryEnv(tmp_path, "o.toml")
    prepared = INVENTORY_BACKENDS.prepare(name="json", raw={"path": "inv.jsonnet"}, env=env)
    assert prepared.config.import_paths == ()
    with pytest.raises(Exception, match="an entry is empty; name a directory"):
        INVENTORY_BACKENDS.prepare(
            name="json", raw={"path": "inv.jsonnet", "import_paths": [""]}, env=env
        )

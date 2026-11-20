"""The ``[[inventories]]`` settings envelope and the D6 ``[creds]`` refusal (#1176)."""

import pytest
from pydantic import ValidationError

from otto.host.inventory_ref import DEFAULT_INVENTORY
from otto.inventory import compile_inventory
from otto.models.base import OttoModel
from otto.models.settings import (
    InventoryConfigSpec,
    NamedInventorySpec,
    SettingsModel,
    UserSettingsModel,
    binds_default,
    bound_table,
)

_PHYSICAL = {"name": "physical", "backend": "json", "path": "physical.json"}
_DEFAULT_ENTRY = {"name": DEFAULT_INVENTORY, "backend": "json", "path": "default.json"}
_INVENTORY = {"backend": "json", "path": "inventory.json"}
_CREDS = {"backend": "json", "path": "creds.json"}


def _project(**tables):
    return SettingsModel.model_validate({"name": "x", "version": "0.1.0", **tables})


def test_the_envelope_reserves_exactly_four_keys():
    spec = NamedInventorySpec.model_validate(
        {"name": "v", "backend": "teamdb", "cache_ttl": "1h", "creds_file": "x", "path": "i"}
    )
    assert spec.model_extra == {"creds_file": "x", "path": "i"}
    with_creds = NamedInventorySpec.model_validate({**_PHYSICAL, "creds": _CREDS})
    assert with_creds.creds is not None
    assert with_creds.creds.backend == "json"
    assert with_creds.model_extra == {"path": "physical.json"}


def test_an_entry_needs_a_non_empty_name():
    with pytest.raises(ValidationError, match="name"):
        NamedInventorySpec.model_validate({"backend": "json"})
    with pytest.raises(ValidationError, match="name"):
        NamedInventorySpec.model_validate({"name": "", "backend": "json"})


class _TeamDbConfig(OttoModel, frozen=True):
    path: str
    creds_file: str


class _TeamDb:
    supplies = frozenset({"ip"})
    label = "teamdb"

    def __init__(self, c):
        self.config = c.config

    def lookup(self, key):
        raise NotImplementedError

    def list_keys(self):
        return []

    def fingerprint(self):
        return "teamdb"


def test_creds_file_reaches_a_registered_factory(tmp_path):
    """``creds_file`` is the backend's key in a named entry: no legacy guard eats it."""
    from otto.inventory import register_inventory_backend

    register_inventory_backend("teamdb", config=_TeamDbConfig, factory=_TeamDb, overwrite=True)
    named = NamedInventorySpec.model_validate(
        {"name": "v", "backend": "teamdb", "creds_file": "x", "path": "i"}
    )
    compiled = compile_inventory(cfg=named, anchor_dir=tmp_path, origin="o")
    assert compiled.prepared.normalized["creds_file"] == "x"
    assert "name" not in compiled.prepared.normalized


@pytest.mark.parametrize("key", ["name", "creds"])
def test_a_legacy_backend_key_named_like_an_envelope_field_still_reaches_it(key, tmp_path):
    """On ``[inventory]``, ``name`` and ``creds`` are not envelope keys: they are the backend's."""
    from otto.inventory import register_inventory_backend

    config = type(f"_Needs_{key}", (OttoModel,), {"__annotations__": {key: str}})
    register_inventory_backend(f"needs-{key}", config=config, factory=_TeamDb, overwrite=True)
    cfg = InventoryConfigSpec.model_validate({"backend": f"needs-{key}", key: "kept"})
    compiled = compile_inventory(cfg=cfg, anchor_dir=tmp_path, origin="o")
    assert compiled.prepared.normalized[key] == "kept"


_D6 = r"\[creds\].*\[\[inventories\]\].*`creds` sub-table"


def test_d6_refuses_creds_beside_named_inventories_without_default():
    with pytest.raises(ValidationError, match=_D6):
        _project(inventories=[_PHYSICAL], creds=_CREDS)


@pytest.mark.parametrize(
    "binding",
    [{"inventory": _INVENTORY}, {"inventories": [_PHYSICAL, _DEFAULT_ENTRY]}],
    ids=["inventory-table", "default-entry"],
)
def test_d6_allows_creds_when_the_project_binds_default(binding):
    tables = {"inventories": [_PHYSICAL], **binding, "creds": _CREDS}
    model = _project(**tables)
    assert model.creds is not None
    assert [e.name for e in model.inventories] == [e["name"] for e in tables["inventories"]]


def test_creds_without_named_inventories_is_todays_behaviour():
    assert _project(creds=_CREDS).creds is not None


@pytest.mark.parametrize(
    ("inventory", "inventories", "binds"),
    [
        (None, [], False),
        (_INVENTORY, [], True),
        (None, [_PHYSICAL, _DEFAULT_ENTRY], True),
        (None, [_PHYSICAL], False),
    ],
    ids=["none", "table", "default-entry", "only-physical"],
)
def test_binds_default(inventory, inventories, binds):
    assert binds_default(inventory=inventory, inventories=inventories) is binds
    specs = [NamedInventorySpec.model_validate(e) for e in inventories]
    assert binds_default(inventory=inventory, inventories=specs) is binds


def test_an_empty_raw_table_does_not_bind_default():
    assert bound_table({}) is None
    assert bound_table(None) is None
    assert bound_table({"backend": "json"}) == {"backend": "json"}
    assert not binds_default(inventory=bound_table({}), inventories=[_PHYSICAL])
    # No [inventory] table at all: D6 refuses. (An explicit empty [inventory]
    # fails nested validation first, so the raw {} case is bound_table's alone.)
    with pytest.raises(ValidationError, match=_D6):
        _project(inventories=[_PHYSICAL], creds=_CREDS)


def test_a_duplicate_name_in_one_file_is_refused():
    with pytest.raises(ValidationError, match=r"'physical' more than once"):
        _project(inventories=[_PHYSICAL, {**_PHYSICAL, "path": "other.json"}])


def test_inventory_and_a_default_entry_in_one_file_are_refused():
    with pytest.raises(ValidationError, match=r"\[inventory\] and an \[\[inventories\]\] entry"):
        _project(inventory=_INVENTORY, inventories=[_DEFAULT_ENTRY])


def test_a_default_entry_creds_and_a_creds_table_are_refused():
    with pytest.raises(ValidationError, match=r"\[creds\] and the 'default' entry's `creds`"):
        _project(inventories=[{**_DEFAULT_ENTRY, "creds": _CREDS}], creds=_CREDS)


def test_the_user_file_runs_the_same_rules_except_d6():
    with pytest.raises(ValidationError, match=r"'physical' more than once"):
        UserSettingsModel.model_validate({"inventories": [_PHYSICAL, _PHYSICAL]})
    with pytest.raises(ValidationError, match=r"\[inventory\] and an \[\[inventories\]\] entry"):
        UserSettingsModel.model_validate({"inventory": _INVENTORY, "inventories": [_DEFAULT_ENTRY]})
    with pytest.raises(ValidationError, match=r"\[creds\] and the 'default' entry's `creds`"):
        UserSettingsModel.model_validate(
            {"inventories": [{**_DEFAULT_ENTRY, "creds": _CREDS}], "creds": _CREDS}
        )
    # D6 is project-only: the user file's [creds] beside named entries is today's default store.
    user = UserSettingsModel.model_validate({"inventories": [_PHYSICAL], "creds": _CREDS})
    assert user.creds is not None
    assert [e.name for e in user.inventories] == ["physical"]


def test_both_models_default_to_no_named_inventories():
    assert _project().inventories == []
    assert UserSettingsModel().inventories == []

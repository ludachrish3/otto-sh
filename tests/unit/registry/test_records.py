"""The record contract, ``FrozenMap``, the errors, the constructor's rules and capabilities."""

import ast
import dataclasses
import re
import subprocess
import sys
from dataclasses import dataclass

import pydantic
import pytest

from otto import registry as reg
from otto.errors import OttoError
from otto.registry import (
    ClassEntry,
    DuplicateRegistration,
    FrozenMap,
    IncompleteRegistration,
    Justified,
    Ref,
    Registry,
    RequireRepo,
    resolved,
)
from tests._fixtures.paths import PROJECT_ROOT


@dataclass(frozen=True)
class Rec:
    value: object


@dataclass(frozen=True)
class Pair:
    left: object
    right: object


@dataclass
class Unfrozen:
    value: object


class DuckModel:
    """A frozen model by duck typing: no pydantic involved."""

    model_config = {"frozen": True}  # noqa: RUF012 — the duck-typed model shape
    model_fields = {"x": None}  # noqa: RUF012

    def __init__(self, x: object) -> None:
        object.__setattr__(self, "x", x)


class UnfrozenDuckModel:
    model_config = {"frozen": False}  # noqa: RUF012
    model_fields = {"x": None}  # noqa: RUF012

    def __init__(self, x: object) -> None:
        self.x = x


def _reg(entry=Rec, **kw) -> Registry:
    return Registry("widget", entry=entry, register_hint="register_widget()", **kw)


def _checked(entry=Rec) -> Registry:
    return _reg(entry, check_resolved=lambda name, record: None)


# --- what counts as a record -----------------------------------------------


def test_a_frozen_dataclass_is_a_record():
    r = _reg()
    r.register("w", Rec(1))
    assert r.get("w") == Rec(1)


def test_a_frozen_model_is_a_record_by_duck_typing():
    r = _reg(DuckModel)
    r.register("w", DuckModel(1))
    assert r.get("w").x == 1


def test_the_entry_type_must_be_a_frozen_record_type():
    with pytest.raises(TypeError, match="frozen dataclass or a frozen model"):
        _reg(Unfrozen)
    with pytest.raises(TypeError, match="frozen dataclass or a frozen model"):
        _reg(UnfrozenDuckModel)


def test_the_engine_never_imports_pydantic():
    """No import of pydantic at any scope, and a full round trip loads none.

    Red: add ``from pydantic import BaseModel`` inside any engine function.
    """
    tree = ast.parse((PROJECT_ROOT / "src" / "otto" / "registry.py").read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.partition(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.partition(".")[0])
    assert not imported & {"pydantic", "pydantic_core"}

    code = (
        "import sys, dataclasses\n"
        "import otto.registry as r\n"
        "r.FrozenMap.freeze_json({'a': [1]})\n"
        "r.Registry('x', entry=r.ClassEntry, register_hint='x')\n"
        "@dataclasses.dataclass(frozen=True)\n"
        "class Config:\n"
        "    n: int\n"
        "    @classmethod\n"
        "    def model_validate(cls, raw, context=None):\n"
        "        return cls(**raw)\n"
        "    def model_dump(self, mode='python'):\n"
        "        return {'n': self.n}\n"
        "b = r.BackendRegistry('thing', register_hint='x', error=ValueError,\n"
        "    describe_parse_error=str, result=lambda name, obj: None)\n"
        "b.register('t', r.configured_backend(config=Config, factory=lambda c: c.config.n,\n"
        "    metadata=None))\n"
        "assert b.build(b.prepare('t', {'n': 3}, None)) == 3\n"
        "print('pydantic' in sys.modules)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=str(PROJECT_ROOT),
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "False"


def test_a_wrong_record_type_is_incomplete():
    with pytest.raises(IncompleteRegistration, match="expected a Rec record"):
        _reg().register("w", Pair(1, 2))


@pytest.mark.parametrize("value", [Rec, {"value": 1}, 42], ids=["class", "dict", "value"])
def test_a_bare_class_dict_or_value_is_incomplete(value):
    with pytest.raises(IncompleteRegistration):
        _reg().register("w", value)


def test_a_bare_ref_is_incomplete_and_names_the_fix():
    with pytest.raises(IncompleteRegistration, match="put it in a field of a Rec record"):
        _checked().register("w", Ref("json:dumps"))


@pytest.mark.parametrize(
    "bad", [[1], {"a": 1}, {1}, bytearray(b"x")], ids=lambda b: type(b).__name__
)
@pytest.mark.parametrize("where", ["top", "tuple", "frozen_map", "nested_record"])
def test_a_list_dict_set_or_bytearray_anywhere_is_incomplete(bad, where):
    value = {
        "top": bad,
        "tuple": (1, bad),
        "frozen_map": FrozenMap({"k": bad}),
        "nested_record": Pair(1, Rec(bad)),
    }[where]
    r = _reg()
    with pytest.raises(IncompleteRegistration, match=type(bad).__name__):
        r.register("w", Rec(value))
    assert "w" not in r


@pytest.mark.parametrize("value", [Unfrozen(1), UnfrozenDuckModel(1)], ids=["dataclass", "model"])
def test_an_unfrozen_nested_dataclass_or_model_is_incomplete(value):
    with pytest.raises(IncompleteRegistration, match="not frozen"):
        _reg().register("w", Rec(value))


class FrozenModel(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(frozen=True)

    x: int


class MutableModel(FrozenModel):
    """A subclass of a frozen model that thaws itself: an instance of the entry type."""

    model_config = pydantic.ConfigDict(frozen=False)


@dataclass
class MutableDataclassDuck(DuckModel):
    """A plain dataclass subclass of a frozen (duck-typed) model: an instance, never frozen."""

    x: object


@pytest.mark.parametrize(
    ("entry_type", "mutable"),
    [(FrozenModel, MutableModel(x=1)), (DuckModel, MutableDataclassDuck(1))],
    ids=["pydantic_subclass", "dataclass_subclass"],
)
@pytest.mark.parametrize("overwrite", [False, True], ids=["register", "overwrite"])
def test_a_mutable_subclass_of_the_entry_type_is_incomplete(entry_type, mutable, overwrite):
    """``isinstance`` is not frozenness: the record's own type must be frozen.

    Mutation: check only ``isinstance(entry, entry_type)``.
    """
    r = _reg(entry_type)
    if overwrite:
        r.register("w", entry_type(x=1))
    with pytest.raises(IncompleteRegistration, match=f"{type(mutable).__name__} is not frozen"):
        r.register("w", mutable, overwrite=overwrite)
    assert ("w" in r) is overwrite
    if overwrite:
        assert type(r.get("w")) is entry_type


def test_the_walk_stops_at_classes_callables_refs_patterns_and_opaque_objects():
    class Holder:
        def __init__(self) -> None:
            self.items = [1, 2]

    r = _checked()
    for name, value in {
        "class": Unfrozen,
        "callable": lambda: [1],
        "pattern": re.compile("x"),
        "opaque": Holder(),
        "ref": Ref("json:dumps"),
    }.items():
        r.register(name, Rec(value))
    assert len(r) == 5


def test_a_ref_nested_below_a_field_is_opaque(monkeypatch):
    """A nested Ref is neither refused nor resolved by get.

    Mutations: make the walk raise on a nested Ref, or make get resolve nested ones.
    """
    monkeypatch.delitem(sys.modules, "never_imported_mod", raising=False)
    r = _checked()
    nested = Ref("never_imported_mod:X")
    r.register("tuple", Rec((nested,)))
    r.register("record", Rec(Pair(1, Rec(nested))))
    assert r.get("tuple").value == (nested,)
    assert r.get("record").value.right.value is nested
    assert "never_imported_mod" not in sys.modules


def test_a_nested_ref_still_needs_check_resolved():
    """Mutation: only look for top-level Refs when applying the structural rule."""
    r = _reg()
    nested = Ref("never_imported_mod:X")
    for name, record in {"tuple": Rec((nested,)), "record": Rec(Pair(1, Rec(nested)))}.items():
        with pytest.raises(IncompleteRegistration, match="check_resolved"):
            r.register(name, record)


def test_a_registry_without_check_resolved_refuses_a_ref_field():
    """Mutation: skip the structural rule."""
    r = _reg()
    with pytest.raises(IncompleteRegistration, match="check_resolved"):
        r.register("w", Rec(Ref("json:dumps")))
    assert "w" not in r


# --- FrozenMap -------------------------------------------------------------


def test_frozen_map_is_immutable_hashable_and_equal_by_content():
    m = FrozenMap({"a": 1, "b": (2, 3)})
    assert m == FrozenMap({"b": (2, 3), "a": 1})
    assert m == {"a": 1, "b": (2, 3)}
    assert hash(m) == hash(FrozenMap({"b": (2, 3), "a": 1}))
    assert {m: "ok"}[FrozenMap({"a": 1, "b": (2, 3)})] == "ok"
    with pytest.raises(TypeError):
        m["c"] = 1
    assert FrozenMap() == {} and len(FrozenMap()) == 0  # noqa: PT018


def test_freeze_json_and_thaw_json_round_trip_json_data():
    data = {"valid_transfers": ["shell", "nc"], "nested": {"a": [1, {"b": [2]}]}, "n": 3}
    frozen = FrozenMap.freeze_json(data)
    assert frozen["valid_transfers"] == ("shell", "nc")
    assert isinstance(frozen["nested"], FrozenMap)
    assert frozen.thaw_json() == data
    assert isinstance(frozen.thaw_json()["valid_transfers"], list)
    _reg().register("w", Rec(frozen))  # a frozen JSON value is a valid field


def test_frozen_map_with_an_unhashable_value_is_not_hashable():
    m = FrozenMap({"a": [1]})
    with pytest.raises(TypeError):
        hash(m)


# --- errors ----------------------------------------------------------------


def test_duplicate_and_incomplete_are_otto_errors_and_value_errors():
    for error in (DuplicateRegistration, IncompleteRegistration, reg.RegistrationRefused):
        assert issubclass(error, OttoError)
        assert issubclass(error, ValueError)


# --- the constructor's rules -----------------------------------------------


def test_entry_is_required():
    with pytest.raises(TypeError, match="entry"):
        Registry("widget", register_hint="x")  # ty: ignore[missing-argument]
    with pytest.raises(TypeError, match="entry= must be a frozen dataclass or a frozen model"):
        Registry("widget", entry=None, register_hint="x")  # ty: ignore[invalid-argument-type]


def test_a_subclass_is_refused():
    """``Registry`` is final for ty, and refuses a subclass when one is constructed."""

    class Sub(Registry):  # ty: ignore[subclass-of-final-class]
        pass

    with pytest.raises(TypeError, match="Registry cannot be subclassed"):
        Sub("widget", entry=Rec, register_hint="x")


def test_origin_is_not_a_registration_keyword():
    """The engine attributes every registration; a caller cannot supply its origin."""
    r = _reg()
    with pytest.raises(TypeError, match="origin"):
        r.register("w", Rec(1), origin="elsewhere")  # ty: ignore[unknown-argument]
    with pytest.raises(TypeError, match="origin"):
        r.register_many([("w", Rec(1))], origin="elsewhere")  # ty: ignore[unknown-argument]
    assert "w" not in r


# --- capabilities ----------------------------------------------------------


def test_capability_is_a_closed_set():
    with pytest.raises(TypeError, match="closed set"):

        class MyCapability(reg.Capability):
            pass


def test_justified_refuses_a_blank_reason():
    with pytest.raises(ValueError, match="reason"):
        Justified(RequireRepo(), reason="  ")
    with pytest.raises(TypeError, match="Capability"):
        Justified("RequireRepo", reason="r")


def test_capabilities_must_be_justified():
    with pytest.raises(TypeError, match="Justified"):
        _reg(capabilities=[RequireRepo()])
    r = _reg(capabilities=[Justified(RequireRepo(), reason="r")])
    assert r.capabilities == [Justified(RequireRepo(), reason="r")]


# --- reads -----------------------------------------------------------------


def test_resolved_refuses_a_ref():
    with pytest.raises(TypeError, match="read it through get"):
        resolved(Ref("a:b"))
    assert resolved(int) is int


def test_find_returns_none_only_for_an_absent_name():
    r = _checked(ClassEntry)
    assert r.find("absent") is None
    r.register("broken", ClassEntry(Ref("otto_no_such_module_anywhere:X")))
    with pytest.raises(ModuleNotFoundError):
        r.find("broken")
    r.register("ok", ClassEntry(int))
    assert r.find("ok") == ClassEntry(int)


def test_a_class_entry_resolves_its_ref():
    r = _checked(ClassEntry)
    r.register("dumps", ClassEntry(Ref("json:JSONDecoder")))
    import json

    assert resolved(r.get("dumps").cls) is json.JSONDecoder
    assert dataclasses.is_dataclass(r.peek("dumps"))

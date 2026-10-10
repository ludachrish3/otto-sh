"""The conformance suite: the five case kinds run on engine-built tables, and coverage.

Each seam owns a case module under ``tests/unit/registry/cases/`` that runs the
case kinds against its live tables and lists them in a module-level ``COVERS``.
Every live otto table must be covered by one.

Each class-valued and backend seam also owns a replacement differential,
``tests/unit/registry/test_replacement_<seam>.py``, whose module-level
``REPLACES`` lists the ``("module:NAME", built-in name)`` pairs it replaces
through the real product path; together they must name every built-in.
"""

import importlib
import json
import pathlib
import subprocess
import sys
from dataclasses import dataclass

import pytest

from otto import registry as reg
from otto.registry import (
    BackendRegistry,
    Configured,
    Derived,
    Justified,
    Ref,
    Registry,
    RegistryView,
    RequireRepo,
    Subscription,
    configured_backend,
)
from tests.e2e._otto_subprocess import PROJECT_ROOT, coverage_subprocess_env
from tests.unit.registry import conformance

CASES = pathlib.Path(__file__).parent / "cases"
REPLACEMENTS = pathlib.Path(__file__).parent


@dataclass(frozen=True)
class Rec:
    value: object


@dataclass(frozen=True)
class LazyRec:
    value: "object | Ref"


def _reject_negative(name, entry, proposed):
    if isinstance(entry.value, int) and entry.value < 0:
        raise ValueError(f"{name}: negative")


def _check_int(name, entry):
    if not isinstance(entry.value, int):
        raise TypeError(f"{name}: not an int")


def test_the_raw_case_holds_for_an_engine_registry(monkeypatch, tmp_path):
    (tmp_path / "conformance_lazy_mod.py").write_text("VALUE = 1\nTEXT = 'not an int'\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delitem(sys.modules, "conformance_lazy_mod", raising=False)
    table = Registry(
        "conformance thing",
        entry=LazyRec,
        register_hint="register_thing()",
        validate=_reject_negative,
        check_resolved=_check_int,
    )
    conformance.assert_raw_registry(
        table,
        make=lambda i: ("thing", LazyRec(i)),
        invalid=("thing", LazyRec(-1)),
        lazy=("lazy-thing", LazyRec(Ref("conformance_lazy_mod:VALUE"))),
        wrongly_typed_lazy=("wrong-thing", LazyRec(Ref("conformance_lazy_mod:TEXT"))),
    )
    assert table.names() == []


def test_the_raw_case_holds_with_a_lazy_record_under_the_case_name(monkeypatch, tmp_path):
    """A class seam's lazy record shares the case's one name; the case makes room for it."""
    (tmp_path / "conformance_same_name_mod.py").write_text("VALUE = 1\nTEXT = 'not an int'\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delitem(sys.modules, "conformance_same_name_mod", raising=False)
    table = Registry(
        "conformance thing",
        entry=LazyRec,
        register_hint="register_thing()",
        check_resolved=_check_int,
    )
    conformance.assert_raw_registry(
        table,
        make=lambda i: ("thing", LazyRec(i)),
        lazy=("thing", LazyRec(Ref("conformance_same_name_mod:VALUE"))),
        wrongly_typed_lazy=("thing", LazyRec(Ref("conformance_same_name_mod:TEXT"))),
    )
    assert table.names() == []


def test_the_raw_case_holds_for_a_require_repo_registry():
    table = Registry(
        "conformance per-repo thing",
        entry=Rec,
        register_hint="register_thing()",
        capabilities=[Justified(RequireRepo(), reason="one per repo")],
    )
    conformance.assert_raw_registry(
        table, make=lambda i: ("thing", Rec(i)), require_repo="conformance_repo"
    )


def test_the_wrapper_case_holds_for_a_marked_wrapper():
    table = Registry("conformance thing", entry=Rec, register_hint="register_thing()")

    @reg.registration_boundary
    def register_thing(*, overwrite: bool = False) -> None:
        table.register("thing", Rec("THING"), overwrite=overwrite)

    conformance.assert_wrapper_matches_raw(
        table, via_wrapper=register_thing, record_for=lambda: Rec("THING")
    )


@dataclass
class _Spy:
    parses: int = 0
    builds: int = 0


def _make_spy_entry():
    """A configured entry whose config model and factory count on their own spy."""
    spy = _Spy()

    @dataclass
    class SpyConfig:
        @classmethod
        def model_validate(cls, raw, context=None):
            spy.parses += 1
            if "secret" in raw:
                raise ValueError(f"bad value {raw['secret']}")
            return cls()

        def model_dump(self, mode="python"):
            return {}

    def factory(c: "Configured[SpyConfig, None]") -> object:
        spy.builds += 1
        return object()

    return configured_backend(config=SpyConfig, factory=factory, metadata=None), spy


def test_the_backend_case_holds_for_an_engine_backend_registry():
    table = BackendRegistry(
        "conformance backend",
        register_hint="register_backend()",
        error=ValueError,
        describe_parse_error=lambda exc: "the configuration did not parse",
        result=lambda name, obj: None,
    )
    conformance.assert_backend_registry(
        table,
        env=None,
        make_spy_entry=_make_spy_entry,
        good_raw={},
        bad_raw={"secret": "s3cr3t-value"},
    )
    assert table.names() == []


def test_the_subscription_case_holds_for_an_engine_subscription():
    conformance.assert_subscription(
        Subscription("conformance hook", register_hint="subscribe_hook()"),
        value_a=len,
        value_b=abs,
    )


def test_the_view_case_holds_for_an_engine_view():
    right = Registry("right", entry=Rec, register_hint="register_right()")

    def no_clash(name, entry, proposed):
        if name in right:
            raise ValueError(f"{name} is already a right entry")

    left = Registry("left", entry=Rec, register_hint="register_left()", validate=no_clash)
    right.register("taken", Rec(0))

    def derive():
        for source in (left, right):
            for name, entry in source.raw_items():
                yield Derived(name, entry, source.origin(name), source.repo(name))

    view = RegistryView("both", register_hint="x", sources=[left, right], derive=derive)
    conformance.assert_view(
        view,
        sources=[left, right],
        contribute=lambda name: left.register(name, Rec(1)),
        clash=lambda: left.register("taken", Rec(2)),
    )
    assert left.names() == [] and right.names() == ["taken"]  # noqa: PT018


def _discovered_cases() -> "list[str]":
    return sorted(p.stem for p in CASES.glob("test_case_*.py"))


def test_every_registry_has_a_case():
    """Red: add a key to a scratch COVERS, or drop one from a case module's."""
    covered: set[str] = set()
    for stem in _discovered_cases():
        module = importlib.import_module(f"tests.unit.registry.cases.{stem}")
        covered.update(module.COVERS)
    assert covered == set(_live_otto_tables())


def _live_otto_tables() -> "list[str]":
    """Every live otto table, keyed ``"module:ATTR"``."""
    from tests.unit.test_registry_loading import (
        _import_every_module_that_builds_a_registry,
        _otto_registries,
    )

    bound = {(r.module, r.kind): r.bound for r in _import_every_module_that_builds_a_registry()}
    return [f"{t.defined_in}:{bound[(t.defined_in, t.kind)]}" for t in _otto_registries()]


_BUILT_INS = """
import json

from otto.host.os_profile import HostClassEntry
from otto.registry import BackendRegistry, ClassEntry, Registry
from tests.unit.test_registry_loading import (
    _import_every_module_that_builds_a_registry,
    _otto_registries,
)

bound = {(r.module, r.kind): r.bound for r in _import_every_module_that_builds_a_registry()}
pairs = []
for table in _otto_registries():
    if isinstance(table, BackendRegistry) or (
        isinstance(table, Registry) and table._entry in (ClassEntry, HostClassEntry)
    ):
        key = f"{table.defined_in}:{bound[(table.defined_in, table.kind)]}"
        pairs += [
            [key, name] for name in table.names() if table.origin(name).startswith("otto.")
        ]
print(json.dumps(sorted(pairs)))
"""
"""Every built-in of every class-valued and backend table, read in a fresh interpreter.

Fresh, so the names are exactly what otto's modules register at import: no
entry a test registered (or a replacement left behind) can leak in."""


def _built_in_pairs() -> "set[tuple[str, str]]":
    result = subprocess.run(
        [sys.executable, "-c", _BUILT_INS],
        env=coverage_subprocess_env(),
        capture_output=True,
        text=True,
        cwd=str(PROJECT_ROOT),
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    return {(key, name) for key, name in json.loads(result.stdout.strip().splitlines()[-1])}


def _replaced_pairs() -> "set[tuple[str, str]]":
    replaced: set[tuple[str, str]] = set()
    for path in sorted(REPLACEMENTS.glob("test_replacement_*.py")):
        module = importlib.import_module(f"tests.unit.registry.{path.stem}")
        replaced.update(module.REPLACES)
    return replaced


def test_every_built_in_of_every_class_valued_and_backend_registry_has_a_replacement_differential():
    """Red: delete one pair from one ``REPLACES``.

    The set is computed, never listed: every ``BackendRegistry`` and every
    ``Registry`` whose entry is a ``ClassEntry`` or a ``HostClassEntry``, and
    every name an otto module registers in it at import.
    """
    built_ins = _built_in_pairs()
    assert built_ins, "the fresh interpreter found no built-ins"
    replaced = _replaced_pairs()
    assert (sorted(built_ins - replaced), sorted(replaced - built_ins)) == ([], []), {
        "built-in with no replacement differential": sorted(built_ins - replaced),
        "replaced but not a built-in": sorted(replaced - built_ins),
    }


@pytest.mark.parametrize("stem", _discovered_cases() or ["<none yet>"])
def test_each_case_module_declares_what_it_covers(stem):
    if stem == "<none yet>":
        pytest.skip("no case modules yet; each seam commit adds its own")
    module = importlib.import_module(f"tests.unit.registry.cases.{stem}")
    assert isinstance(module.COVERS, list) and module.COVERS  # noqa: PT018

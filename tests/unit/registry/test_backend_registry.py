"""``BackendRegistry``: prepare parses once, build constructs once, one error pipeline."""

import dataclasses
import sys
from dataclasses import dataclass, field

import pytest

from otto import registry as reg
from otto.registry import (
    BackendRegistry,
    Configured,
    FrozenMap,
    IncompleteRegistration,
    Prepared,
    Ref,
    class_backend,
    configured_backend,
)


class SeamConstructionError(ValueError):
    """The seam's construction error, handed to the registry."""


@dataclass
class Spy:
    parses: int = 0
    builds: int = 0


@dataclass
class SpyConfig:
    """A configuration model by duck typing: ``model_validate`` and ``model_dump``."""

    path: str = "default"
    items: list = field(default_factory=list)

    spy: "Spy | None" = None

    @classmethod
    def model_validate(cls, raw, context=None):
        spy = context["env"]["spy"]
        spy.parses += 1
        if "token" in raw:
            raise ValueError(f"rejected token {raw['token']!r}")
        return cls(path=raw.get("path", "default"), spy=spy)

    def model_dump(self, mode="python"):
        return {"path": self.path, "items": list(self.items)}

    def prepared_facts(self):
        return ("facts", self.path)


def _describe(exc: Exception) -> str:
    return f"invalid configuration ({type(exc).__name__})"


def _built(c: "Configured[SpyConfig, dict]") -> "tuple[str, list]":
    c.env["spy"].builds += 1
    c.config.items.append("mutated")
    return (c.config.path, list(c.config.items))


def _backends(result=None) -> "BackendRegistry[dict, object, None]":
    return BackendRegistry(
        "thing backend",
        register_hint="register_thing_backend()",
        error=SeamConstructionError,
        describe_parse_error=_describe,
        result=result or (lambda name, obj: None),
    )


def _from_module(module: str, fn, *args, **kwargs):
    code = compile("result = fn(*args, **kwargs)", f"<{module}>", "exec")
    scope = {"__name__": module, "fn": fn, "args": args, "kwargs": kwargs}
    exec(code, scope)  # noqa: S102 — a synthetic registrant module
    return scope["result"]


@pytest.fixture
def spy() -> Spy:
    return Spy()


@pytest.fixture
def table() -> "BackendRegistry[dict, object, None]":
    t = _backends()
    _from_module(
        "repo.init",
        t.register,
        "thing",
        configured_backend(config=SpyConfig, factory=_built, metadata=None),
    )
    return t


def test_prepare_parses_once_and_never_calls_the_factory(table, spy):
    prepared = table.prepare("thing", {"path": "/p"}, {"spy": spy})
    assert (spy.parses, spy.builds) == (1, 0)
    assert prepared.backend == "thing"
    assert prepared.normalized == FrozenMap.freeze_json({"path": "/p", "items": []})
    assert prepared.facts == ("facts", "/p")
    assert prepared.source is None


def test_build_never_parses_and_calls_the_factory_once(table, spy):
    prepared = table.prepare("thing", {"path": "/p"}, {"spy": spy})
    assert table.build(prepared) == ("/p", ["mutated"])
    assert (spy.parses, spy.builds) == (1, 1)


def test_one_prepared_builds_twice(table, spy):
    prepared = table.prepare("thing", {}, {"spy": spy})
    table.build(prepared)
    table.build(prepared)
    assert (spy.parses, spy.builds) == (1, 2)


def test_build_hands_each_call_a_deep_copy(table, spy):
    prepared = table.prepare("thing", {}, {"spy": spy})
    first = table.build(prepared)
    second = table.build(prepared)
    assert first == second == ("default", ["mutated"])
    assert prepared.config.items == []


def test_a_config_that_cannot_be_copied_fails_construction_without_calling_the_factory(spy):
    class Uncopyable(SpyConfig):
        def __deepcopy__(self, memo):
            raise TypeError("cannot copy this")

    t = _backends()
    t.register("thing", configured_backend(config=Uncopyable, factory=_built, metadata=None))
    prepared = t.prepare("thing", {}, {"spy": spy})
    with pytest.raises(SeamConstructionError, match="construction failed") as err:
        t.build(prepared)
    assert isinstance(err.value.__cause__, TypeError)
    assert spy.builds == 0


def test_a_foreign_prepared_is_refused_without_any_call(table, spy):
    other = _backends()
    other.register("thing", configured_backend(config=SpyConfig, factory=_built, metadata=None))
    prepared = other.prepare("thing", {}, {"spy": spy})
    with pytest.raises(
        SeamConstructionError, match=r"stale preparation failed: prepared by another registry"
    ):
        table.build(prepared)
    assert (spy.parses, spy.builds) == (1, 0)


def test_a_stale_prepared_is_refused_after_overwrite(table, spy):
    prepared = table.prepare("thing", {}, {"spy": spy})
    table.register(
        "thing", configured_backend(config=SpyConfig, factory=_built, metadata=None), overwrite=True
    )
    with pytest.raises(SeamConstructionError, match="stale preparation failed"):
        table.build(prepared)
    assert spy.builds == 0


def test_a_stale_prepared_is_refused_after_unregister_and_reinsert(table, spy):
    prepared = table.prepare("thing", {}, {"spy": spy})
    entry = table.peek("thing")
    table.unregister("thing")
    table.register("thing", entry)
    with pytest.raises(SeamConstructionError, match="stale preparation failed"):
        table.build(prepared)
    assert spy.builds == 0


def test_a_stale_prepared_is_refused_after_a_test_restore(table, spy):
    state = table._snapshot()
    prepared = table.prepare("thing", {}, {"spy": spy})
    table._restore(state)
    with pytest.raises(SeamConstructionError, match="stale preparation failed"):
        table.build(prepared)
    assert spy.builds == 0


def _stage_error(stage: str, spy: Spy) -> SeamConstructionError:
    def result(name, obj):
        if obj == "wrong":
            raise TypeError("not a thing")

    t = _backends(result)

    def register(name, overwrite=False, **kw):
        entry = configured_backend(metadata=None, **kw)
        _from_module("repo.init", t.register, name, entry, overwrite=overwrite)

    def boom(c):
        raise RuntimeError("factory exploded")

    register("thing", config=SpyConfig, factory=_built)
    register("missing", config=Ref("otto_no_such_module_anywhere:Config"), factory=_built)
    register("explodes", config=SpyConfig, factory=boom)
    register("wrong", config=SpyConfig, factory=lambda c: "wrong")
    env = {"spy": spy}

    def stale() -> None:
        prepared = t.prepare("thing", {}, env)
        register("thing", config=SpyConfig, factory=_built, overwrite=True)
        t.build(prepared)

    actions = {
        "lookup": lambda: t.prepare("unknown", {}, env),
        "resolution": lambda: t.prepare("missing", {}, env),
        "parse": lambda: t.prepare("thing", {"token": "x"}, env),
        "stale preparation": stale,
        "construction": lambda: t.build(t.prepare("explodes", {}, env)),
        "result": lambda: t.build(t.prepare("wrong", {}, env)),
    }
    with pytest.raises(SeamConstructionError) as err:
        actions[stage]()
    return err.value


@pytest.mark.parametrize(
    "stage", ["lookup", "resolution", "parse", "stale preparation", "construction", "result"]
)
def test_each_stage_names_itself_the_origin_and_the_backend(stage, spy):
    error = _stage_error(stage, spy)
    text = str(error)
    assert f"{stage} failed" in text
    assert "thing backend" in text
    if stage == "lookup":
        assert "'unknown'" in text and "not registered" in text  # noqa: PT018
    else:
        assert "registered by repo.init" in text
    if stage != "stale preparation":
        assert error.__cause__ is not None


def test_a_prepare_source_is_named_in_parse_and_construction_errors(spy):
    """Mutation: drop ``where`` from the message."""
    t = _backends()

    def boom(c):
        raise RuntimeError("factory exploded")

    _from_module(
        "repo.init",
        t.register,
        "b",
        configured_backend(config=SpyConfig, factory=boom, metadata=None),
    )
    source = "/r/.otto/settings.toml"
    with pytest.raises(SeamConstructionError) as err:
        t.prepare("b", {"token": "x"}, {"spy": spy}, source=source)
    assert "repo.init" in str(err.value) and source in str(err.value)  # noqa: PT018
    prepared = t.prepare("b", {}, {"spy": spy}, source=source)
    with pytest.raises(SeamConstructionError) as err:
        t.build(prepared)
    assert "repo.init" in str(err.value) and source in str(err.value)  # noqa: PT018
    with pytest.raises(SeamConstructionError) as err:
        t.build(t.prepare("b", {}, {"spy": spy}))
    assert "configured in" not in str(err.value)


def test_a_parse_failure_never_echoes_the_rejected_value(table, spy):
    with pytest.raises(SeamConstructionError) as err:
        table.prepare("thing", {"token": "s3cr3t-value"}, {"spy": spy})
    assert "s3cr3t-value" not in str(err.value)
    assert "invalid configuration (ValueError)" in str(err.value)


class FakeValidationError(ValueError):
    """Shaped like a pydantic ValidationError: ``errors()`` and ``title``."""

    title = "Config"

    def errors(self):
        return [{"input": "s3cr3t-value"}]


def test_construction_error_never_echoes_a_validation_errors_input(spy):
    def validating_factory(c):
        raise FakeValidationError("1 validation error: input_value='s3cr3t-value'")

    t = _backends()
    t.register("v", configured_backend(config=SpyConfig, factory=validating_factory, metadata=None))
    with pytest.raises(SeamConstructionError) as err:
        t.build(t.prepare("v", {}, {"spy": spy}))
    assert "s3cr3t-value" not in str(err.value)
    assert "invalid configuration (FakeValidationError)" in str(err.value)


def test_a_duck_typed_non_pydantic_config_model_prepares_and_builds():
    @dataclass(frozen=True)
    class Plain:
        port: int

        @classmethod
        def model_validate(cls, raw, context=None):
            return cls(**raw)

        def model_dump(self, mode="json"):
            return dataclasses.asdict(self)

    t = _backends()
    t.register(
        "p", configured_backend(config=Plain, factory=lambda c: c.config.port + 1, metadata=None)
    )
    prepared = t.prepare("p", {"port": 41}, None)
    assert prepared.normalized == {"port": 41}
    assert prepared.facts is None
    assert t.build(prepared) == 42


def test_process_control_exceptions_pass_through(spy):
    def interrupt(c):
        raise KeyboardInterrupt

    t = _backends()
    t.register("k", configured_backend(config=SpyConfig, factory=interrupt, metadata=None))
    with pytest.raises(KeyboardInterrupt):
        t.build(t.prepare("k", {}, {"spy": spy}))


def test_a_class_backend_takes_no_configuration_and_builds_with_create():
    created = []

    class Term:
        @classmethod
        def create(cls, env):
            created.append(env)
            return cls()

    t = _backends()
    t.register("ssh", class_backend(cls=Term, metadata=("unix",)))
    with pytest.raises(SeamConstructionError, match="parse failed: takes no configuration"):
        t.prepare("ssh", {"anything": 1}, "ctx")
    prepared = t.prepare("ssh", {}, "ctx")
    assert prepared.config is None and prepared.normalized == {}  # noqa: PT018
    assert isinstance(t.build(prepared), Term)
    assert created == ["ctx"]


def test_static_metadata_reads_import_nothing(monkeypatch):
    monkeypatch.delitem(sys.modules, "never_imported_backend", raising=False)
    t = BackendRegistry(
        "thing backend",
        register_hint="x",
        error=SeamConstructionError,
        describe_parse_error=_describe,
        result=lambda name, obj: None,
        check_resolved=lambda name, entry: None,
    )
    t.register(
        "lazy",
        configured_backend(
            config=Ref("never_imported_backend:Config"),
            factory=Ref("never_imported_backend:build"),
            metadata=("snapshot_cache", False),
        ),
    )
    assert t.peek("lazy").metadata == ("snapshot_cache", False)
    assert t.names() == ["lazy"] and "lazy" in t  # noqa: PT018
    assert "never_imported_backend" not in sys.modules


def test_the_inner_table_is_not_listed_separately():
    t = _backends()
    listed = [x for x in reg.instances() if x is t or x is t._table]
    assert listed == [t]


def test_only_the_helpers_build_entries():
    t = _backends()
    hand_made = reg.BackendEntry(config=SpyConfig, factory=_built, cls=None, metadata=None)
    with pytest.raises(IncompleteRegistration, match="configured_backend"):
        t.register("hand", hand_made)
    assert "hand" not in t


def test_the_prepared_record_carries_its_provenance(table, spy):
    prepared = table.prepare("thing", {}, {"spy": spy}, source="host h1")
    assert isinstance(prepared, Prepared)
    assert prepared.registry is table
    assert prepared.generation == table._generation("thing")
    assert prepared.env == {"spy": spy}
    assert prepared.source == "host h1"

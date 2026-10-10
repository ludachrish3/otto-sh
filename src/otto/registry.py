"""The registry engine behind every otto extension seam.

Every otto extension seam (term/transfer backends, host classes, lab
repositories, CLI commands, ...) stores its entries in a table built by this
module: a :class:`Registry` of named records, a :class:`BackendRegistry` that
also builds what it stores, an ordered :class:`Subscription`, or a read-only
:class:`RegistryView` derived from other tables. One engine gives every seam the
same rules:

- **A taken name raises** :class:`DuplicateRegistration` unless the caller
  passes ``overwrite=True``, and the message names both registering modules.
- **A registration is a complete record** of the registry's declared ``entry``
  type: a frozen dataclass or a frozen model. A bare value, a bare :class:`Ref`
  or a record holding a ``list``, ``dict`` or ``set`` raises
  :class:`IncompleteRegistration`.
- **Attribution is captured by the engine.** The first calling frame outside
  the engine and outside every ``register_*`` wrapper is the registrant; the
  registering repo comes from :func:`registering_repo`.
- **Every change bumps a revision** that derived caches key on.

Domain modules keep their public ``register_*``/``build_*`` wrapper functions;
the wrappers build records, and the engine applies the rules.

The module also holds the *registering-repo marker* (:func:`registering_repo`,
:func:`get_registering_repo`): the context variable ``bootstrap()`` sets around
each repo's init imports, so registrations record *which repo* an entry came
from rather than only which module.

Every table refuses entries registered from outside otto while repo test files
load (:func:`loading_test_files`): test files and conftests load only inside a
pytest session, so anything they registered would exist for some commands and
not others. A registered :class:`Ref` resolves outside that phase, wherever its
first lookup happens: an init module named it, so what its module registers on
import is the init module's.

>>> from otto.registry import ClassEntry, Registry
>>> class Demo:
...     pass
>>> r: Registry[ClassEntry[Demo]] = Registry(
...     "demo backend", entry=ClassEntry, register_hint="register_demo()"
... )
>>> r.register("json", ClassEntry(Demo))
>>> r.get("json").cls is Demo
True
>>> r.names()
['json']

References
----------
A :class:`Ref` wraps a ``"package.module:attribute"`` string that names an
object without importing it. A ``Ref`` lives in a record field, never on its
own: ``ClassEntry(Ref("my_plugin.frames:FishFrame"))`` is a complete
registration, a bare ``Ref`` is refused. A registry whose records may hold one
declares a ``check_resolved`` hook, which runs on the record once its ``Ref``
fields are imported. :meth:`Registry.names`, ``in``, ``len()``,
:meth:`Registry.origin` and :meth:`Registry.peek` never import a ``Ref``'s
target; :meth:`Registry.get` (and :meth:`Registry.items`) resolve each ``Ref``
field on first read, run ``check_resolved`` and cache the resolved record.
"""

import contextlib
import contextvars
import copy
import dataclasses
import difflib
import enum
import inspect
import itertools
import sys
import weakref
from collections.abc import Callable, Collection, Iterable, Iterator, Mapping, Sequence
from types import CodeType
from typing import Any, Generic, TypeVar, cast, final

from typing_extensions import override

from otto.errors import OttoError

__all__ = [
    "BackendRegistry",
    "ClassEntry",
    "Configured",
    "DuplicateRegistration",
    "FrozenMap",
    "IncompleteRegistration",
    "Justified",
    "Prepared",
    "Ref",
    "RegistrationRefused",
    "Registry",
    "RegistryView",
    "RequireRepo",
    "Subscription",
    "class_backend",
    "configured_backend",
    "registering_repo",
]

T = TypeVar("T")
"""Type variable for a stored value or a built object."""

E = TypeVar("E")
"""Type variable for the record type a :class:`Registry` stores."""

C = TypeVar("C")
"""Type variable for a backend's configuration model."""

Env = TypeVar("Env")
"""Type variable for the environment a backend seam supplies at preparation."""

M = TypeVar("M")
"""Type variable for a backend entry's static metadata."""

K = TypeVar("K")
"""Type variable for a :class:`FrozenMap` key."""

V = TypeVar("V")
"""Type variable for a :class:`FrozenMap` value."""

CapT = TypeVar("CapT", bound="Capability")
"""Type variable for the capability a :class:`Justified` carries."""

F = TypeVar("F", bound=Callable[..., object])
"""Type variable for a function marked by :func:`registration_boundary`."""


@dataclasses.dataclass(frozen=True)
class Ref:
    """A ``"package.module:attribute"`` reference to an object, imported on first lookup."""

    target: str

    def __post_init__(self) -> None:
        module, sep, attr = self.target.partition(":")
        if not sep or not module or not attr:
            raise ValueError(f"Ref target must be 'package.module:attribute', got {self.target!r}")

    def resolve(self) -> object:
        """Import the module and return the attribute.

        The import runs outside the test-file loading phase
        (:func:`loading_test_files`), even when the first lookup happens
        inside a pytest session. A registered ``Ref`` was named by an init
        module or by otto (the same registration from a test file is
        refused), so whatever its module registers at import belongs to that
        init module, not to the test file whose lookup triggered it. The
        phase is back in force as soon as the import returns.

        Import and attribute errors propagate unchanged.
        """
        import importlib

        module, _, attr = self.target.partition(":")
        token = _LOADING_TEST_FILES.set(False)
        try:
            imported = importlib.import_module(module)
        finally:
            _LOADING_TEST_FILES.reset(token)
        return getattr(imported, attr)


class RegistrationRefused(OttoError, ValueError):  # noqa: N818 — interface-fixed name the docs refer to
    """A registration the engine refuses whatever the entry.

    Raised when a test file or conftest registers something: test files and
    conftests load only inside a pytest session (``otto test``'s collection
    and run), so anything they registered would silently exist for some
    commands and not others. Extensions belong in an init module, which loads
    for every command. Registrations made by otto's own modules are exempt: a
    test file is often the first thing to import one.

    Also raised for a write attempted while a registry check runs, and for a
    registration a registry's ``RequireRepo`` capability refuses outside a
    repo's init import.
    """


class DuplicateRegistration(OttoError, ValueError):  # noqa: N818 — interface-fixed name the docs refer to
    """A name is already registered, or repeated within one registration.

    The message names both registering modules. ``overwrite=True`` replaces a
    stored entry deliberately; it never covers a name repeated within one
    batch.
    """


class IncompleteRegistration(OttoError, ValueError):  # noqa: N818 — interface-fixed name the docs refer to
    """A registration is not a complete, frozen record of the registry's entry type.

    Raised for a bare value or a bare :class:`Ref` where a record is expected,
    for a record of the wrong type, for a ``list``, ``dict``, ``set`` or
    ``bytearray`` anywhere in a record, for a record that is not frozen, and
    for a record holding a :class:`Ref` in a registry with no
    ``check_resolved`` hook.
    """


_LOADING_TEST_FILES: "contextvars.ContextVar[bool]" = contextvars.ContextVar(
    "otto_loading_test_files", default=False
)


@contextlib.contextmanager
def loading_test_files() -> "Iterator[None]":
    """Mark the block as importing repo test files: only otto's modules may register in it."""
    token = _LOADING_TEST_FILES.set(True)
    try:
        yield
    finally:
        _LOADING_TEST_FILES.reset(token)


def is_loading_test_files() -> bool:
    """Whether the current context is importing repo test files."""
    return _LOADING_TEST_FILES.get()


def _is_otto_origin(origin: str) -> bool:
    return origin == "otto" or origin.startswith("otto.")


def refuse_during_test_load(kind: str, name: str, origin: str) -> None:
    """Raise :class:`RegistrationRefused` for a non-otto registration made while test files load.

    Keyed on ORIGIN, not on the phase alone: a test file is often the first thing
    to import an otto module that registers its own entries at import time
    (``otto.project.actions`` registers the built-in project instructions), and
    that registration is otto's, not the test file's.
    """
    if _LOADING_TEST_FILES.get() and not _is_otto_origin(origin):
        raise RegistrationRefused(
            f"{kind} {name!r} is registered from {origin!r} while repo test files load; "
            f"register it from an init module listed in .otto/settings.toml, not a test "
            f"file or conftest (those load only inside a pytest session)"
        )


# ── Registering-repo marker ─────────────────────────────────────────────
# Bootstrap wraps each repo's init-module imports in registering_repo(name)
# so the engine can attribute what they register to the repo whose import is
# running. A ContextVar, not a module global: exception-safe restore for free,
# and nested use (a repo importing another's init helper) unwinds correctly.

_REGISTERING_REPO: "contextvars.ContextVar[str | None]" = contextvars.ContextVar(
    "otto_registering_repo", default=None
)


def get_registering_repo() -> str | None:
    """Name of the repo whose init modules are currently being imported, if any."""
    return _REGISTERING_REPO.get()


@contextlib.contextmanager
def registering_repo(name: str) -> "Iterator[None]":
    """Attribute registrations inside this block to repo *name* (bootstrap-only)."""
    token = _REGISTERING_REPO.set(name)
    try:
        yield
    finally:
        _REGISTERING_REPO.reset(token)


# ── Engine-wide state ───────────────────────────────────────────────────

_CHECKING: "contextvars.ContextVar[bool]" = contextvars.ContextVar(
    "otto_registry_checking", default=False
)
_GENERATIONS = itertools.count(1)
_BOUNDARY_CODES: "set[CodeType]" = set()
_INSTANCES: "weakref.WeakSet[_Table]" = weakref.WeakSet()


def registration_boundary(fn: F) -> F:
    """Mark *fn* as a transparent registration boundary: the engine credits its caller.

    Every ``register_*`` wrapper and registering decorator carries this mark,
    so an entry it registers is attributed to the module that called the
    wrapper, not to the wrapper's own module. A helper that wraps a marked
    wrapper without the mark is itself credited as the registrant.
    """
    _BOUNDARY_CODES.add(cast("CodeType", fn.__code__))  # ty: ignore[unresolved-attribute]
    return fn


def _attributed_module() -> str:
    """Return the first frame's module outside the engine and every marked wrapper."""
    frame = sys._getframe(1)  # noqa: SLF001 — the frame walk IS the attribution
    while frame is not None:
        module = frame.f_globals.get("__name__", "<unknown>")
        if module != __name__ and frame.f_code not in _BOUNDARY_CODES:
            return module
        frame = frame.f_back
    return "<unknown>"  # pragma: no cover - every call has a caller outside the engine


# ── The step matrix and its one executor ────────────────────────────────


class _Op(enum.Enum):
    REGISTER = "register / register_many"
    UNREGISTER = "unregister"
    RESTORE = "test restore"
    PUBLISH = "publish (from get)"
    SUBSCRIBE = "subscribe"
    CANCEL = "Token.cancel"


class _Step(enum.Enum):  # declaration order IS execution order
    REFUSE_DURING_TEST_LOAD = "2"
    NESTED_WRITE_GUARD = "3"
    CAPABILITIES = "4"
    RECORD = "5"
    COLLISIONS = "6"
    VALIDATE = "7a"
    CHECK_RESOLVED = "7b"
    BUMP_REVISION = "8"
    BUMP_GENERATION = "9"


_S = _Step
_STEPS: "dict[_Op, frozenset[_Step]]" = {
    _Op.REGISTER: frozenset(
        {
            _S.REFUSE_DURING_TEST_LOAD,
            _S.NESTED_WRITE_GUARD,
            _S.CAPABILITIES,
            _S.RECORD,
            _S.COLLISIONS,
            _S.VALIDATE,
            _S.BUMP_REVISION,
            _S.BUMP_GENERATION,
        }
    ),
    _Op.UNREGISTER: frozenset({_S.NESTED_WRITE_GUARD, _S.BUMP_REVISION, _S.BUMP_GENERATION}),
    _Op.RESTORE: frozenset({_S.BUMP_REVISION, _S.BUMP_GENERATION}),
    _Op.PUBLISH: frozenset({_S.RECORD, _S.CHECK_RESOLVED}),
    _Op.SUBSCRIBE: frozenset({_S.REFUSE_DURING_TEST_LOAD, _S.NESTED_WRITE_GUARD, _S.BUMP_REVISION}),
    _Op.CANCEL: frozenset({_S.NESTED_WRITE_GUARD, _S.BUMP_REVISION}),
}
"""Which steps each operation runs. Step 1, attribution, is captured by the public
entry point before ``_commit`` and is pinned by the attribution tests."""


def _unwired(step: str) -> "Callable[[], None]":
    """Return the default for a step an operation does not supply: running it raises.

    A no-op default would let a step added to the wrong _STEPS row pass silently; this makes
    every such mutation observable in the behavioural tests, not only in the table test.
    """

    def run() -> None:
        raise RuntimeError(f"registry step {step!r} ran for an operation that does not wire it")

    return run


@dataclasses.dataclass
class _Change:
    """One change to a table's stored state, with a closure per step its operation wires."""

    op: _Op
    kind: str
    names: list[str]
    origin: str
    write: "Callable[[bool], None]"
    """``write(bump_generation)``: the one write, after every check."""
    bump_revision: "Callable[[], None]"
    capabilities: "Callable[[], None]" = dataclasses.field(
        default_factory=lambda: _unwired("capabilities")
    )
    records: "Callable[[], None]" = dataclasses.field(default_factory=lambda: _unwired("records"))
    collisions: "Callable[[], None]" = dataclasses.field(
        default_factory=lambda: _unwired("collisions")
    )
    validate: "Callable[[], None]" = dataclasses.field(default_factory=lambda: _unwired("validate"))
    check_resolved: "Callable[[], None]" = dataclasses.field(
        default_factory=lambda: _unwired("check_resolved")
    )


@contextlib.contextmanager
def _checking() -> "Iterator[None]":
    token = _CHECKING.set(True)
    try:
        yield
    finally:
        _CHECKING.reset(token)


def _commit(change: _Change) -> None:
    """Execute one change to stored state, running _STEPS[change.op] in order."""
    steps = _STEPS[change.op]
    for step in _Step:
        if step not in steps:
            continue
        if step is _Step.REFUSE_DURING_TEST_LOAD:
            for name in change.names:
                refuse_during_test_load(change.kind, name, change.origin)
        elif step is _Step.NESTED_WRITE_GUARD:
            if _CHECKING.get():
                first = change.names[0] if change.names else ""
                raise RegistrationRefused(
                    f"{change.kind} {first!r}: {change.op.value} is refused while a registry "
                    "check runs; a check may read registries but never write one"
                )
        elif step is _Step.CAPABILITIES:
            change.capabilities()
        elif step is _Step.RECORD:
            change.records()
        elif step is _Step.COLLISIONS:
            change.collisions()
        elif step is _Step.VALIDATE:
            with _checking():
                change.validate()
        elif step is _Step.CHECK_RESOLVED:
            with _checking():
                change.check_resolved()
    # Every operation writes exactly once, after every check, and the generation flag is read
    # from the row for EVERY op, so adding BUMP_GENERATION (or BUMP_REVISION) to a row that
    # does not carry it changes observable behaviour, never just the table.
    change.write(_Step.BUMP_GENERATION in steps)
    if _Step.BUMP_REVISION in steps:
        change.bump_revision()


# ── Records ─────────────────────────────────────────────────────────────


@final
class FrozenMap(Mapping[K, V]):
    """An immutable mapping, equal by content and hashable when its values are.

    Records hold a ``FrozenMap`` where they would hold a ``dict``.
    :meth:`freeze_json` and :meth:`thaw_json` convert JSON-shaped data (lists
    and dicts, as JSON and TOML produce) to and from the frozen form exactly.
    """

    __slots__ = ("_data",)

    def __init__(self, mapping: "Mapping[K, V] | None" = None) -> None:
        self._data: dict[K, V] = dict(mapping) if mapping is not None else {}

    @override
    def __getitem__(self, key: K) -> V:
        return self._data[key]

    @override
    def __iter__(self) -> "Iterator[K]":
        return iter(self._data)

    @override
    def __len__(self) -> int:
        return len(self._data)

    @override
    def __hash__(self) -> int:
        return hash(frozenset(self._data.items()))

    @override
    def __eq__(self, other: object) -> bool:
        if isinstance(other, Mapping):
            return dict(self._data) == dict(other.items())
        return NotImplemented

    @override
    def __repr__(self) -> str:
        return f"FrozenMap({self._data!r})"

    @classmethod
    def freeze_json(cls, value: "Mapping[str, object]") -> "FrozenMap[str, object]":
        """Freeze JSON-shaped *value*: every list becomes a tuple, every mapping a ``FrozenMap``."""
        return FrozenMap({key: _freeze_json(item) for key, item in value.items()})

    def thaw_json(self) -> dict[str, object]:
        """Invert :meth:`freeze_json`: tuples become lists, and frozen maps dicts."""
        return {str(key): _thaw_json(item) for key, item in self._data.items()}


def _freeze_json(value: object) -> object:
    if isinstance(value, Mapping):
        return FrozenMap({key: _freeze_json(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item) for item in value)
    return value


def _thaw_json(value: object) -> object:
    if isinstance(value, FrozenMap):
        return value.thaw_json()
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


def _is_model_type(cls: type) -> bool:
    """Return whether *cls* is a pydantic-style model class (duck-typed: no pydantic import)."""
    config = getattr(cls, "model_config", None)
    return isinstance(config, Mapping) and hasattr(cls, "model_fields")


def _is_frozen_record_type(cls: type) -> bool:
    if dataclasses.is_dataclass(cls):
        return bool(cls.__dataclass_params__.frozen)  # ty: ignore[unresolved-attribute]
    if _is_model_type(cls):
        return cls.model_config.get("frozen") is True  # ty: ignore[unresolved-attribute]
    return False


def _record_fields(record: object) -> "list[tuple[str, object]]":
    """``(field name, value)`` for every field of a dataclass or model record."""
    if dataclasses.is_dataclass(record):
        return [(f.name, getattr(record, f.name)) for f in dataclasses.fields(record)]
    names = type(record).model_fields  # ty: ignore[unresolved-attribute]
    return [(name, getattr(record, name)) for name in names]


def _walk(value: object, path: str) -> bool:
    """Walk one field value for the record contract; return whether a :class:`Ref` was met.

    Raises :class:`IncompleteRegistration` for a ``list``, ``dict``, ``set`` or
    ``bytearray`` anywhere, or a dataclass or model that is not frozen. Recurses
    into tuples, frozensets, frozen maps, dataclasses and models; stops at a
    ``Ref``, a class, a callable, a compiled pattern and any other object.

    A callable is behaviour, not record data, so the walk never opens one:
    a function, a method, a partial, or an instance whose type defines
    ``__call__`` (a dataclass or model included, frozen or not).
    """
    if isinstance(value, Ref):
        return True
    if isinstance(value, type) or callable(value):
        return False
    if isinstance(value, (list, dict, set, bytearray)):
        raise IncompleteRegistration(
            f"{path} holds a {type(value).__name__}; a record holds only frozen values "
            "(a tuple, a frozenset or a FrozenMap; FrozenMap.freeze_json for JSON data)"
        )
    if isinstance(value, (tuple, frozenset)):
        found = False
        for index, item in enumerate(value):
            found = _walk(item, f"{path}[{index}]") or found
        return found
    if isinstance(value, FrozenMap):
        found = False
        for key, item in value.items():
            found = _walk(key, f"{path} key {key!r}") or found
            found = _walk(item, f"{path}[{key!r}]") or found
        return found
    if dataclasses.is_dataclass(value) or _is_model_type(type(value)):
        if not _is_frozen_record_type(type(value)):
            raise IncompleteRegistration(
                f"{path} holds a {type(value).__name__} that is not frozen; "
                "a record holds only frozen values"
            )
        found = False
        for name, item in _record_fields(value):
            found = _walk(item, f"{path}.{name}") or found
        return found
    return False


def _replace_fields(record: E, updates: "dict[str, object]") -> E:
    """Return a copy of *record* with *updates* applied (a dataclass or a model)."""
    if dataclasses.is_dataclass(record):
        return cast("E", dataclasses.replace(record, **updates))  # ty: ignore[invalid-argument-type]
    return cast("E", record.model_copy(update=updates))  # ty: ignore[unresolved-attribute]


class Capability:
    """A sanctioned deviation from a registry's default behaviour: a closed set.

    Only the engine defines capability types; a registry declares the ones it
    uses with :class:`Justified`, beside its reason.
    """

    def __init_subclass__(cls, **kwargs: object) -> None:
        if cls.__module__ != __name__:
            raise TypeError(
                f"{cls.__qualname__}: the registry capabilities are a closed set defined by "
                "otto.registry; a new capability is a change to the engine"
            )
        super().__init_subclass__(**kwargs)


@final
@dataclasses.dataclass(frozen=True)
class RequireRepo(Capability):
    """Refuse a registration made outside a repo's init import.

    A register or register_many made while :func:`get_registering_repo` is
    ``None`` raises :class:`RegistrationRefused`. It does not apply to
    unregister, test restore or publish.
    """


@final
@dataclasses.dataclass(frozen=True)
class Justified(Generic[CapT]):
    """A capability a registry declares, with the reason it deviates from the default."""

    capability: CapT
    """The capability the registry uses."""

    reason: str
    """Why this registry needs it; a blank reason is refused."""

    def __post_init__(self) -> None:
        if not isinstance(self.capability, Capability):
            raise TypeError(f"Justified needs a Capability, got {self.capability!r}")
        if not self.reason.strip():
            raise ValueError(
                f"Justified({type(self.capability).__name__}()) needs a reason that says "
                "why this registry deviates from the default"
            )


@final
@dataclasses.dataclass(frozen=True)
class ClassEntry(Generic[T]):
    """The record of a class-valued registry: the class, or a :class:`Ref` naming it."""

    cls: "type[T] | Ref"
    """The registered class; a ``Ref`` is imported at the entry's first ``get``."""


def resolved(value: "T | Ref") -> T:
    """Narrow a ``Ref``-able field of a record read through :meth:`Registry.get`.

    ``get`` resolves every top-level ``Ref`` field, but the field's declared
    type still includes ``Ref``; this states the narrowing, and raises
    :class:`TypeError` if the value is still a ``Ref``.
    """
    if isinstance(value, Ref):
        raise TypeError(
            f"{value!r} is unresolved; read it through get(), which resolves Ref fields"
        )
    return value


_ABSENT = object()


def _static_member(obj: object, member: str) -> object:
    """Return *member* of *obj* without running a property getter, or ``_ABSENT``.

    Static lookup finds instance attributes, class attributes, methods and
    properties alike; a ``staticmethod``/``classmethod`` is unwrapped to the
    function it calls.
    """
    found = inspect.getattr_static(obj, member, _ABSENT)
    if isinstance(found, (staticmethod, classmethod)):
        return found.__func__
    return found


def missing_member(
    obj: object, *, methods: "Sequence[str]" = (), attributes: "Sequence[str]" = ()
) -> str | None:
    """Name the first protocol member *obj* lacks, or return ``None``.

    For a backend seam's result check: each of *methods* must be callable and
    each of *attributes* present. The lookup is static, so a property is
    found without its getter running (a getter that fetches or raises is
    never triggered by the check). The answer reads ``"a callable <name>"``
    for a method and ``"<name>"`` for an attribute.

    >>> from otto.registry import missing_member
    >>> class Store:
    ...     label = "s"
    ...
    ...     def lookup(self, key): ...
    >>> missing_member(Store(), methods=["lookup", "fingerprint"], attributes=["label"])
    'a callable fingerprint'
    >>> missing_member(Store(), methods=["lookup"], attributes=["label"]) is None
    True
    """
    for member in methods:
        if not callable(_static_member(obj, member)):
            return f"a callable {member}"
    for member in attributes:
        if _static_member(obj, member) is _ABSENT:
            return member
    return None


# ── Checks ──────────────────────────────────────────────────────────────


@final
class Proposed(Mapping[str, E]):
    """The stored entries with a registration's batch applied: read-only.

    Handed to a registry's ``validate`` hook so a rule spanning entries can
    read the table as it would be after the write.
    """

    __slots__ = ("_data",)

    def __init__(self, stored: "Mapping[str, E]", batch: "Sequence[tuple[str, E]]") -> None:
        self._data: dict[str, E] = dict(stored)
        for name, entry in batch:
            self._data[name] = entry

    @override
    def __getitem__(self, name: str) -> E:
        return self._data[name]

    @override
    def __iter__(self) -> "Iterator[str]":
        return iter(self._data)

    @override
    def __len__(self) -> int:
        return len(self._data)


EntryCheck = Callable[[str, E, Proposed[E]], None]
"""A registry's ``validate`` hook: ``(name, entry, proposed)``, run at registration."""

ResolvedCheck = Callable[[str, E], None]
"""A registry's ``check_resolved`` hook: ``(name, resolved entry)``, run at the first ``get``."""

ResultCheck = Callable[[str, object], None]
"""A backend seam's ``result`` check: ``(backend name, built object)``; raises on a wrong result."""


# ── Registry ────────────────────────────────────────────────────────────


@dataclasses.dataclass(frozen=True)
class _Stored:
    value: object
    origin: str
    repo: str | None
    generation: int
    owes_check: bool = False
    """The record holds a Ref and its check_resolved has not passed yet."""


_SnapshotRow = tuple[str, object, str, "str | None", int, bool]


@final
class Registry(Generic[E]):
    """A named table of frozen records, with one set of rules for every seam.

    It cannot be subclassed: a seam's own rules live in its record type and
    its *validate* and *check_resolved* hooks.
    """

    def __init__(
        self,
        kind: str,
        *,
        entry: "type[E]",
        register_hint: str,
        validate: "EntryCheck[E] | None" = None,
        check_resolved: "ResolvedCheck[E] | None" = None,
        capabilities: "Sequence[Justified[Capability]]" = (),
        _owned: bool = False,
    ) -> None:
        """Create a registry of *kind* entries (e.g. ``"term backend"``).

        *entry* is the record type every registration must be an instance
        of: a frozen dataclass or a frozen model.

        *register_hint* names the public registration function shown in lookup
        errors (e.g. ``"otto.register_term_backend()"``).

        *validate* runs at registration with the name, the record (its
        ``Ref`` fields unresolved) and the proposed table; *check_resolved*
        runs once, at the first :meth:`get` of a record holding a
        :class:`Ref`, after its top-level ``Ref`` fields are imported (a
        nested ``Ref`` stays unresolved). A registry with no
        *check_resolved* refuses a record holding a :class:`Ref`.

        *capabilities* declares the registry's deviations from the default,
        each with its reason (:class:`Justified`).

        Raises:
            TypeError: If the registry is a subclass (``Registry`` is final),
                *entry* is not a frozen record type, or a capability is not
                :class:`Justified`.
        """
        if type(self) is not Registry:
            raise TypeError(f"{type(self).__name__}: Registry cannot be subclassed")
        if not isinstance(entry, type) or not _is_frozen_record_type(entry):
            raise TypeError(
                f"{kind} registry: entry= must be a frozen dataclass or a frozen model "
                f"type, got {entry!r}"
            )
        for justified in capabilities:
            if not isinstance(justified, Justified):
                raise TypeError(
                    f"{kind} registry: a capability is declared as Justified(capability, "
                    f"reason=...), got {justified!r}"
                )
        self.defined_in = _attributed_module()
        """The module that constructed this registry.

        A guard over :func:`instances` reads it to tell otto's own seams from
        tables built elsewhere."""
        self._kind = kind
        self._entry = entry
        self._register_hint = register_hint
        self._validate = validate
        self._check_resolved = check_resolved
        self._capabilities: list[Justified[Capability]] = list(capabilities)
        self._entries: dict[str, _Stored] = {}
        self._revision = 0
        if not _owned:
            _INSTANCES.add(self)

    # -- properties ---------------------------------------------------------

    @property
    def kind(self) -> str:
        """What one entry is, in words (``"term backend"``)."""
        return self._kind

    @property
    def revision(self) -> int:
        """A counter bumped by every change to what the registry holds.

        A register, an unregister and a test restore bump it once each; the
        caching publish of a first :meth:`get` does not. A derived cache keys
        on it.
        """
        return self._revision

    @property
    def capabilities(self) -> "list[Justified[Capability]]":
        """The deviations this registry declares, each with its reason."""
        return list(self._capabilities)

    # -- writes -------------------------------------------------------------

    def register(self, name: str, entry: E, *, overwrite: bool = False) -> None:
        """Register *entry* under *name*; a taken name raises unless *overwrite*.

        Raises:
            RegistrationRefused: If repo test files are loading and the
                registrant is outside the ``otto`` package, if a registry check
                is running, or if a ``RequireRepo`` registry is written outside
                a repo's init import.
            IncompleteRegistration: If *entry* is not a complete, frozen
                record of the registry's entry type.
            DuplicateRegistration: If *name* is already registered and
                *overwrite* is false; the message names both registering
                modules.
            ValueError: From the registry's *validate* hook; nothing is stored.
        """
        self._register_batch([(name, entry)], overwrite=overwrite)

    def register_many(self, entries: "Iterable[tuple[str, E]]", *, overwrite: bool = False) -> None:
        """Register every ``(name, entry)`` pair of *entries* atomically.

        Every check runs on the whole batch before anything is written, so a
        failure commits nothing. ``overwrite=True`` covers stored names, never
        a name repeated within the batch.

        It raises what :meth:`register` raises.
        """
        self._register_batch(list(entries), overwrite=overwrite)

    def _register_batch(self, batch: "list[tuple[str, E]]", *, overwrite: bool) -> None:
        if not batch:
            return  # nothing to register: no change, so no commit and no revision bump
        registrant = _attributed_module()
        repo = get_registering_repo()
        names = [name for name, _ in batch]
        holding_refs: set[str] = set()

        def write(bump_generation: bool) -> None:
            for name, entry in batch:
                old = self._entries.get(name)
                if bump_generation:
                    generation = next(_GENERATIONS)
                else:
                    generation = old.generation if old is not None else 0
                self._entries[name] = _Stored(
                    entry, registrant, repo, generation, owes_check=name in holding_refs
                )

        _commit(
            _Change(
                _Op.REGISTER,
                self._kind,
                names,
                registrant,
                write=write,
                bump_revision=self._bump,
                capabilities=lambda: self._check_capabilities(names, repo),
                records=lambda: holding_refs.update(self._check_records(batch)),
                collisions=lambda: self._check_collisions(names, overwrite, registrant),
                validate=lambda: self._run_validate(batch),
            )
        )

    def _check_capabilities(self, names: list[str], repo: str | None) -> None:
        """Apply each declared capability (step 4)."""
        for justified in self._capabilities:
            if isinstance(justified.capability, RequireRepo) and repo is None:
                raise RegistrationRefused(
                    f"{self._kind} {names[0] if names else ''!r} must be registered from "
                    "a repo init module (listed in .otto/settings.toml [init]); no repo's "
                    "init import is running"
                )

    def _check_records(self, batch: "list[tuple[str, E]]") -> set[str]:
        """Check every entry is a complete, frozen record (step 5 of a registration).

        Return the names whose record holds a :class:`Ref`, so owes its
        *check_resolved* at the first :meth:`get`.
        """
        return {name for name, entry in batch if self._check_record(name, entry)}

    def _check_collisions(self, names: list[str], overwrite: bool, registrant: str) -> None:
        """Refuse a taken name without *overwrite*, and any repeat within the batch (step 6)."""
        seen: set[str] = set()
        for name in names:
            if name in seen:
                raise DuplicateRegistration(
                    f"{self._kind} {name!r} is repeated within one registration; "
                    "overwrite=True never covers that"
                )
            seen.add(name)
            if name in self._entries and not overwrite:
                raise DuplicateRegistration(
                    f"{self._kind} {name!r} is already registered by "
                    f"{self._entries[name].origin!r}; second registration from "
                    f"{registrant!r}. Pass overwrite=True to replace it deliberately."
                )

    def _run_validate(self, batch: "list[tuple[str, E]]") -> None:
        """Run the registry's validate hook on the proposed table (step 7 of a registration)."""
        if self._validate is None:
            return
        proposed = Proposed({n: cast("E", s.value) for n, s in self._entries.items()}, batch)
        for name, entry in batch:
            self._validate(name, entry, proposed)

    def _check_record(self, name: str, entry: object) -> bool:
        """Step 5: *entry* is a frozen record of the entry type, frozen all the way down.

        Return whether it holds a :class:`Ref` anywhere.
        """
        entry_type = self._entry
        if isinstance(entry, Ref):
            raise IncompleteRegistration(
                f"{self._kind} {name!r}: a bare Ref is not a registration; put it in a "
                f"field of a {entry_type.__name__} record"
            )
        if not isinstance(entry, entry_type):
            raise IncompleteRegistration(
                f"{self._kind} {name!r}: expected a {entry_type.__name__} record, got "
                f"{type(entry).__name__} {entry!r}"
            )
        if not _is_frozen_record_type(type(entry)):
            # isinstance is not frozenness: a subclass may thaw itself.
            raise IncompleteRegistration(
                f"{self._kind} {name!r}: a {type(entry).__name__} is not frozen; a record's "
                f"own type must be frozen, a subclass of {entry_type.__name__} included"
            )
        holds_ref = False
        for field, value in _record_fields(entry):
            holds_ref = _walk(value, f"{self._kind} {name!r} field {field!r}") or holds_ref
        if holds_ref and self._check_resolved is None:
            raise IncompleteRegistration(
                f"{self._kind} {name!r} holds a Ref, but the {self._kind} registry has no "
                "check_resolved hook to check what it names; register the object itself"
            )
        return holds_ref

    def unregister(self, name: str) -> None:
        """Remove the entry registered under *name*, without resolving a :class:`Ref`.

        Raises:
            RegistrationRefused: If a registry check is running (whatever the
                name).
            ValueError: If *name* is unknown (same rich error as :meth:`get`).
        """

        def write(bump_generation: bool) -> None:
            # The name is looked up here, after the nested-write guard, so a write
            # inside a check is refused the same way whether or not the name exists.
            self._ensure_known(name)
            if not bump_generation:
                raise RuntimeError("an unregister always retires the entry's generation")
            del self._entries[name]

        _commit(
            _Change(
                _Op.UNREGISTER,
                self._kind,
                [name],
                _attributed_module(),
                write=write,
                bump_revision=self._bump,
            )
        )

    def _bump(self) -> None:
        self._revision += 1

    # -- reads --------------------------------------------------------------

    def _ensure_known(self, name: str) -> None:
        """Raise the rich unknown-name error unless *name* is registered."""
        if name in self._entries:
            return
        known = ", ".join(self._entries) or "<none>"
        close = difflib.get_close_matches(name, list(self._entries), n=1)
        suggestion = f" Did you mean {close[0]!r}?" if close else ""
        raise ValueError(
            f"Unknown {self._kind} {name!r}.{suggestion} Registered: {known}. "
            f"Custom entries can be added via {self._register_hint}."
        )

    def _resolve(self, name: str, fields: "Collection[str] | None") -> E:
        """Return *name*'s record with its ``Ref`` fields in *fields* (all when None) resolved.

        The resolved record is published (cached in place of the stored one)
        only when the entry was not replaced while the ``Ref`` imported.

        A record that holds a ``Ref`` owes one *check_resolved*, run once no
        top-level ``Ref`` remains. A nested ``Ref`` (inside a tuple or a
        frozen map) is never resolved here, so a record holding only nested
        ``Ref`` values is checked as stored, at its first :meth:`get`.
        """
        self._ensure_known(name)
        stored = self._entries[name]
        record = stored.value
        updates: dict[str, object] = {}
        for field, value in _record_fields(record):
            if isinstance(value, Ref) and (fields is None or field in fields):
                updates[field] = value.resolve()
        result = _replace_fields(cast("E", record), updates) if updates else cast("E", record)
        complete = not any(isinstance(v, Ref) for _, v in _record_fields(result))
        run_check = stored.owes_check and complete
        if not updates and not run_check:
            return result

        def records() -> None:
            for field, value in updates.items():
                _walk(value, f"{self._kind} {name!r} field {field!r}")

        def check_resolved() -> None:
            if run_check and self._check_resolved is not None:
                self._check_resolved(name, result)

        self._publish(
            name,
            stored,
            result,
            records=records,
            check_resolved=check_resolved,
            owes_check=stored.owes_check and not run_check,
        )
        return result

    def _publish(
        self,
        name: str,
        stored: _Stored,
        result: object,
        *,
        records: "Callable[[], None]",
        check_resolved: "Callable[[], None]",
        owes_check: bool = False,
    ) -> None:
        def write(bump_generation: bool) -> None:
            if self._entries.get(name) is not stored:
                return  # replaced while the Ref imported: the newer entry stays
            generation = next(_GENERATIONS) if bump_generation else stored.generation
            self._entries[name] = _Stored(
                result, stored.origin, stored.repo, generation, owes_check=owes_check
            )

        _commit(
            _Change(
                _Op.PUBLISH,
                self._kind,
                [name],
                stored.origin,
                write=write,
                bump_revision=self._bump,
                records=records,
                check_resolved=check_resolved,
            )
        )

    def get(self, name: str) -> E:
        """Return the record registered under *name*, resolving its ``Ref`` fields once.

        The first ``get`` of a record holding a :class:`Ref` imports each
        ``Ref`` field's target, checks the resolved record (the freeze walk and
        the registry's *check_resolved*), and caches it, so later reads are
        free. A failed import or check propagates and caches nothing; the
        next ``get`` retries.

        Raises:
            ValueError: If *name* is unknown; the message lists registered
                names, adds a did-you-mean suggestion, and points at the
                registration function.
        """
        return self._resolve(name, None)

    def find(self, name: str) -> "E | None":
        """Like :meth:`get`, but ``None`` for an absent name (a failing ``Ref`` still raises)."""
        if name not in self._entries:
            return None
        return self.get(name)

    def peek(self, name: str) -> E:
        """Return the record stored for *name*, never resolving a :class:`Ref`.

        That is the record as registered until a read resolves its ``Ref``
        fields (:meth:`get`, :meth:`find`, :meth:`items`, or a backend
        registry's ``prepare``/``build``); from then on it is the resolved
        record that read cached in its place (possibly with only the fields
        that read resolved: ``prepare`` resolves ``config`` alone), so a
        ``Ref`` field may read as the class it named.

        Raises:
            ValueError: If *name* is unknown (same rich error as :meth:`get`).
        """
        self._ensure_known(name)
        return cast("E", self._entries[name].value)

    def raw_items(self) -> "list[tuple[str, E]]":
        """``(name, stored record)`` pairs in registration order, never resolving.

        Each record is what :meth:`peek` returns for its name.
        """
        return [(name, cast("E", stored.value)) for name, stored in self._entries.items()]

    def items(self) -> "list[tuple[str, E]]":
        """``(name, record)`` pairs in registration order, resolving each as :meth:`get` does."""
        return [(name, self.get(name)) for name in list(self._entries)]

    def names(self) -> list[str]:
        """Return registered names in registration order."""
        return list(self._entries)

    def origin(self, name: str) -> str:
        """Return the module that registered *name*, without resolving a :class:`Ref`.

        Raises:
            ValueError: If *name* is unknown (same rich error as :meth:`get`).
        """
        self._ensure_known(name)
        return self._entries[name].origin

    def repo(self, name: str) -> str | None:
        """Return the repo whose init import registered *name*, or ``None`` outside one.

        Raises:
            ValueError: If *name* is unknown (same rich error as :meth:`get`).
        """
        self._ensure_known(name)
        return self._entries[name].repo

    def __contains__(self, name: object) -> bool:
        """Return whether *name* is registered."""
        return name in self._entries

    def __len__(self) -> int:
        """Return the number of registered entries."""
        return len(self._entries)

    # -- test support -------------------------------------------------------

    def _generation(self, name: str) -> int:
        """Return the generation of *name*'s current registration (test support)."""
        self._ensure_known(name)
        return self._entries[name].generation

    def _snapshot(self) -> "list[_SnapshotRow]":
        """Return the stored state, in order, with nothing resolved (test support)."""
        return [
            (name, s.value, s.origin, s.repo, s.generation, s.owes_check)
            for name, s in self._entries.items()
        ]

    def _restore(self, state: "list[_SnapshotRow]") -> None:
        """Reinstall *state* exactly, running no check (test support).

        Every entry gets a new generation, so a :class:`Prepared` held across
        tests (by a module- or session-scoped fixture) is stale from the next
        test on, once the per-test isolation restores the tables.
        """

        def write(bump_generation: bool) -> None:
            self._entries = {
                name: _Stored(
                    value,
                    origin,
                    repo,
                    next(_GENERATIONS) if bump_generation else generation,
                    owes_check=owes_check,
                )
                for name, value, origin, repo, generation, owes_check in state
            }

        _commit(
            _Change(
                _Op.RESTORE,
                self._kind,
                [row[0] for row in state],
                _attributed_module(),
                write=write,
                bump_revision=self._bump,
            )
        )

    def _origins_added_since(self, state: "list[_SnapshotRow]") -> set[str]:
        """Origins of entries registered (or replaced) since *state* (test support)."""
        before = {(row[0], row[4]) for row in state}
        return {s.origin for n, s in self._entries.items() if (n, s.generation) not in before}

    def _drop_non_otto(self) -> set[str]:
        """Drop every entry not registered by an otto module; return their origins."""
        state = self._snapshot()
        dropped = {row[2] for row in state if not _is_otto_origin(row[2])}
        if dropped:
            self._restore([row for row in state if _is_otto_origin(row[2])])
        return dropped


# ── Subscription ────────────────────────────────────────────────────────


@final
@dataclasses.dataclass(frozen=True)
class Subscribed(Generic[T]):
    """One occurrence in a :class:`Subscription`: the value, who subscribed it, from which repo."""

    value: T
    origin: str
    repo: str | None


@dataclasses.dataclass(frozen=True)
class _Occurrence:
    id: int
    value: object
    origin: str
    repo: str | None


_OCCURRENCES = itertools.count(1)


@final
class Token:
    """Cancels exactly the one occurrence its :meth:`Subscription.subscribe` added."""

    __slots__ = ("_id", "_subscription")

    def __init__(self, subscription: "Subscription[Any]", occurrence: int) -> None:
        self._subscription = weakref.ref(subscription)
        self._id = occurrence

    def cancel(self) -> None:
        """Remove this token's occurrence; a second cancel is a no-op.

        Raises:
            RegistrationRefused: If a registry check is running.
        """
        subscription = self._subscription()
        if subscription is not None:
            subscription._cancel(self._id)  # noqa: SLF001 — the subscription's own token


@final
class Subscription(Generic[T]):
    """An ordered collection of subscribed values; repeats stay repeated occurrences."""

    def __init__(self, kind: str, *, register_hint: str) -> None:
        """Create a subscription of *kind* values (e.g. ``"product provider"``)."""
        self.defined_in = _attributed_module()
        """The module that constructed this subscription."""
        self._kind = kind
        self._register_hint = register_hint
        self._occurrences: list[_Occurrence] = []
        self._revision = 0
        _INSTANCES.add(self)

    @property
    def kind(self) -> str:
        """What one value is, in words."""
        return self._kind

    @property
    def revision(self) -> int:
        """A counter bumped by every subscribe, effective cancel and test restore."""
        return self._revision

    def subscribe(self, value: T) -> Token:
        """Append *value*, recording its origin and repo; return the token that cancels it.

        Raises:
            RegistrationRefused: If repo test files are loading and the
                subscriber is outside the ``otto`` package, or a registry check
                is running.
        """
        origin = _attributed_module()
        repo = get_registering_repo()
        occurrence = next(_OCCURRENCES)
        label = getattr(value, "__qualname__", None) or repr(value)

        def write(bump_generation: bool) -> None:
            if bump_generation:
                raise RuntimeError("subscriptions have no generations")
            self._occurrences.append(_Occurrence(occurrence, value, origin, repo))

        _commit(
            _Change(
                _Op.SUBSCRIBE,
                self._kind,
                [label],
                origin,
                write=write,
                bump_revision=self._bump,
            )
        )
        return Token(self, occurrence)

    def _cancel(self, occurrence: int) -> None:
        index = next((i for i, o in enumerate(self._occurrences) if o.id == occurrence), None)
        if index is None:
            return

        def write(bump_generation: bool) -> None:
            if bump_generation:
                raise RuntimeError("subscriptions have no generations")
            del self._occurrences[index]

        _commit(
            _Change(
                _Op.CANCEL,
                self._kind,
                [repr(self._occurrences[index].value)],
                _attributed_module(),
                write=write,
                bump_revision=self._bump,
            )
        )

    def _bump(self) -> None:
        self._revision += 1

    def items(self) -> "list[Subscribed[T]]":
        """Every occurrence, in subscription order."""
        return [Subscribed(cast("T", o.value), o.origin, o.repo) for o in self._occurrences]

    def __len__(self) -> int:
        """Return the number of occurrences."""
        return len(self._occurrences)

    def _snapshot(self) -> "list[_Occurrence]":
        """Return the occurrences, in order (test support)."""
        return list(self._occurrences)

    def _restore(self, state: "list[_Occurrence]") -> None:
        """Reinstall *state*'s occurrences exactly (test support).

        Subscriptions carry no generations, so the restore's generation step
        has nothing to renew here.
        """

        def write(bump_generation: bool) -> None:  # noqa: ARG001 — no generations to renew
            self._occurrences = list(state)

        _commit(
            _Change(
                _Op.RESTORE,
                self._kind,
                [],
                _attributed_module(),
                write=write,
                bump_revision=self._bump,
            )
        )

    def _origins_added_since(self, state: "list[_Occurrence]") -> set[str]:
        before = {o.id for o in state}
        return {o.origin for o in self._occurrences if o.id not in before}

    def _drop_non_otto(self) -> set[str]:
        dropped = {o.origin for o in self._occurrences if not _is_otto_origin(o.origin)}
        if dropped:
            self._restore([o for o in self._occurrences if _is_otto_origin(o.origin)])
        return dropped


# ── RegistryView ────────────────────────────────────────────────────────


@final
@dataclasses.dataclass(frozen=True)
class Derived(Generic[E]):
    """One row a :class:`RegistryView` derives: the name, the entry and its attribution."""

    name: str
    entry: E
    origin: str
    repo: str | None


@final
class RegistryView(Generic[E]):
    """A read-only table derived from source tables, recomputed when a source changes."""

    def __init__(
        self,
        kind: str,
        *,
        register_hint: str,
        sources: "Sequence[_Table]",
        derive: "Callable[[], Iterable[Derived[E]]]",
    ) -> None:
        """Create a view of *kind* entries over *sources*, whose rows *derive* computes.

        A source may itself be a view; its revision moves with its own sources.
        """
        self.defined_in = _attributed_module()
        """The module that constructed this view."""
        self._kind = kind
        self._register_hint = register_hint
        self._sources = list(sources)
        self._derive = derive
        self._seen = self._sources_token()
        self._revision = 0
        self._derived_at: int | None = None
        self._rows: dict[str, Derived[E]] = {}
        _INSTANCES.add(self)

    @property
    def kind(self) -> str:
        """What one entry is, in words."""
        return self._kind

    def _sources_token(self) -> tuple[int, ...]:
        return tuple(source.revision for source in self._sources)

    @property
    def revision(self) -> int:
        """A counter over the sources' revisions; it moves whenever a source changes.

        It is an ``int`` like every table's revision, so a cache keys on a registry and a
        view alike. A source change is observed when the view is next read.
        """
        token = self._sources_token()
        if token != self._seen:
            self._seen = token
            self._revision += 1
        return self._revision

    def _current(self) -> "dict[str, Derived[E]]":
        revision = self.revision
        if revision != self._derived_at:
            rows: dict[str, Derived[E]] = {}
            for row in self._derive():
                if row.name in rows:
                    raise DuplicateRegistration(
                        f"{self._kind} {row.name!r} is derived twice: from "
                        f"{rows[row.name].origin!r} and from {row.origin!r}"
                    )
                rows[row.name] = row
            self._rows = rows
            self._derived_at = revision
        return self._rows

    def _row(self, name: str) -> "Derived[E]":
        rows = self._current()
        if name in rows:
            return rows[name]
        known = ", ".join(rows) or "<none>"
        close = difflib.get_close_matches(name, list(rows), n=1)
        suggestion = f" Did you mean {close[0]!r}?" if close else ""
        raise ValueError(
            f"Unknown {self._kind} {name!r}.{suggestion} Registered: {known}. "
            f"Custom entries can be added via {self._register_hint}."
        )

    def names(self) -> list[str]:
        """Return the derived names in derivation order."""
        return list(self._current())

    def get(self, name: str) -> E:
        """Return the entry derived under *name*.

        Raises:
            ValueError: If *name* is unknown (the rich unknown-name error).
        """
        return self._row(name).entry

    def find(self, name: str) -> "E | None":
        """Like :meth:`get`, but ``None`` for an absent name."""
        row = self._current().get(name)
        return None if row is None else row.entry

    def items(self) -> "list[tuple[str, E]]":
        """``(name, entry)`` pairs in derivation order."""
        return [(name, row.entry) for name, row in self._current().items()]

    def origin(self, name: str) -> str:
        """Return the module credited with *name*'s row."""
        return self._row(name).origin

    def repo(self, name: str) -> str | None:
        """Return the repo credited with *name*'s row, or ``None``."""
        return self._row(name).repo

    def __contains__(self, name: object) -> bool:
        """Return whether *name* is derived."""
        return name in self._current()

    def __len__(self) -> int:
        """Return the number of derived rows."""
        return len(self._current())


# ── BackendRegistry ─────────────────────────────────────────────────────


@final
@dataclasses.dataclass(frozen=True)
class Configured(Generic[C, Env]):
    """What a configured backend's factory receives: its parsed config and the seam's env."""

    config: C
    """The configuration, parsed by the entry's own config model (a per-build copy)."""

    env: Env
    """The environment the seam supplied at preparation."""


_HELPER_MADE = object()


@final
@dataclasses.dataclass(frozen=True)
class BackendEntry(Generic[Env, T, M]):
    """The record a :class:`BackendRegistry` stores; built only by the two helpers.

    Use :func:`configured_backend` or :func:`class_backend`.
    """

    config: "type[Any] | Ref | None"
    factory: "Callable[[Configured[Any, Env]], T] | Ref | None"
    cls: "type[T] | Ref | None"
    metadata: M
    _made: object = None


def configured_backend(
    *,
    config: "type[C] | Ref",
    factory: "Callable[[Configured[C, Env]], T] | Ref",
    metadata: M,
) -> "BackendEntry[Env, T, M]":
    """Build the entry of a backend constructed from configuration.

    *config* is the configuration model (or a :class:`Ref` to it): ``prepare``
    calls its ``model_validate(raw, context={"env": env})`` once, and it must be
    deep-copyable. *factory* (or a ``Ref`` to it) receives
    ``Configured(config, env)`` at each build. *metadata* is static: readable
    with :meth:`Registry.peek` without importing anything.
    """
    return BackendEntry(
        config=config,
        factory=factory,
        cls=None,
        metadata=metadata,
        _made=_HELPER_MADE,
    )


def class_backend(*, cls: "type[T] | Ref", metadata: M) -> "BackendEntry[Env, T, M]":
    """Build the entry of a backend with no configuration: ``build`` calls ``cls.create(env)``."""
    return BackendEntry(config=None, factory=None, cls=cls, metadata=metadata, _made=_HELPER_MADE)


def _helper_made_check(
    kind: str, seam_validate: "EntryCheck[BackendEntry[Env, T, M]] | None"
) -> "EntryCheck[BackendEntry[Env, T, M]]":
    """Return the inner table's validate: only the helpers build entries, then the seam's check."""

    def validate(
        name: str,
        entry: "BackendEntry[Env, T, M]",
        proposed: "Proposed[BackendEntry[Env, T, M]]",
    ) -> None:
        if entry._made is not _HELPER_MADE:  # noqa: SLF001 — the engine's own marker
            raise IncompleteRegistration(
                f"{kind} {name!r}: build the entry with configured_backend() or class_backend()"
            )
        if seam_validate is not None:
            seam_validate(name, entry, proposed)

    return validate


def _no_check(name: str, entry: object) -> None:
    """Check nothing: a backend seam with no ``check_resolved`` of its own."""


@final
@dataclasses.dataclass(frozen=True)
class Prepared(Generic[Env, T, M]):
    """A backend's configuration, parsed once, ready for :meth:`BackendRegistry.build`."""

    registry: "BackendRegistry[Env, T, M]"
    """The registry that prepared it; another registry's ``build`` refuses it."""

    backend: str
    """The backend name."""

    generation: int
    """The entry generation it was prepared against; a newer registration makes it stale."""

    env: Env
    """The environment the seam supplied."""

    source: str | None
    """Where the configuration came from (a settings file, a host), named in errors."""

    config: object
    """The parsed configuration instance (``None`` for a class backend)."""

    normalized: "FrozenMap[str, object]"
    """The configuration's JSON form, frozen; identity compares this."""

    metadata: M
    """The entry's static metadata."""

    facts: object
    """The config model's ``prepared_facts()``, or ``None`` when it defines none."""


@final
class BackendRegistry(Generic[Env, T, M]):
    """A registry of backends that also prepares and builds them."""

    def __init__(
        self,
        kind: str,
        *,
        register_hint: str,
        error: "type[Exception]",
        describe_parse_error: "Callable[[Exception], str]",
        result: ResultCheck,
        validate: "EntryCheck[BackendEntry[Env, T, M]] | None" = None,
        check_resolved: "ResolvedCheck[BackendEntry[Env, T, M]] | None" = None,
    ) -> None:
        """Create a backend registry of *kind* backends.

        *error* is the seam's construction error, raised for every failed
        stage with the cause chained. *describe_parse_error* describes a
        configuration that failed to parse without echoing the rejected value.
        *result* checks every built object.
        """
        self.defined_in = _attributed_module()
        """The module that constructed this registry."""
        self._error = error
        self._describe_parse_error = describe_parse_error
        self._result = result
        # The inner table's hooks are closures over the seam's hooks, never over
        # self: a reference back to this registry would make a cycle, and a
        # table in a cycle outlives its last reference in instances().
        self._table: Registry[BackendEntry[Env, T, M]] = Registry(
            kind,
            entry=BackendEntry,
            register_hint=register_hint,
            validate=_helper_made_check(kind, validate),
            check_resolved=check_resolved or _no_check,
            _owned=True,
        )
        _INSTANCES.add(self)

    # -- the Registry surface, delegated -----------------------------------

    @property
    def kind(self) -> str:
        """What one backend is, in words."""
        return self._table.kind

    @property
    def revision(self) -> int:
        """The inner table's revision."""
        return self._table.revision

    @property
    def capabilities(self) -> "list[Justified[Capability]]":
        """A backend registry declares no capabilities."""
        return self._table.capabilities

    def register(
        self, name: str, entry: "BackendEntry[Env, T, M]", *, overwrite: bool = False
    ) -> None:
        """Register a backend entry; see :meth:`Registry.register`."""
        self._table.register(name, entry, overwrite=overwrite)

    def register_many(
        self, entries: "Iterable[tuple[str, BackendEntry[Env, T, M]]]", *, overwrite: bool = False
    ) -> None:
        """Register several backend entries atomically; see :meth:`Registry.register_many`."""
        self._table.register_many(entries, overwrite=overwrite)

    def unregister(self, name: str) -> None:
        """Remove a backend; see :meth:`Registry.unregister`."""
        self._table.unregister(name)

    def get(self, name: str) -> "BackendEntry[Env, T, M]":
        """Return the resolved entry; see :meth:`Registry.get`."""
        return self._table.get(name)

    def find(self, name: str) -> "BackendEntry[Env, T, M] | None":
        """Return the resolved entry or ``None``; see :meth:`Registry.find`."""
        return self._table.find(name)

    def peek(self, name: str) -> "BackendEntry[Env, T, M]":
        """Return the entry stored for *name*, never resolving it (see :meth:`Registry.peek`).

        Static metadata reads use this.
        """
        return self._table.peek(name)

    def raw_items(self) -> "list[tuple[str, BackendEntry[Env, T, M]]]":
        """Return the stored entries, never resolving them (see :meth:`Registry.peek`)."""
        return self._table.raw_items()

    def items(self) -> "list[tuple[str, BackendEntry[Env, T, M]]]":
        """Return the resolved entries."""
        return self._table.items()

    def names(self) -> list[str]:
        """Return registered backend names in registration order."""
        return self._table.names()

    def origin(self, name: str) -> str:
        """Return the module that registered *name*."""
        return self._table.origin(name)

    def repo(self, name: str) -> str | None:
        """Return the repo whose init import registered *name*, or ``None``."""
        return self._table.repo(name)

    def __contains__(self, name: object) -> bool:
        """Return whether *name* is registered."""
        return name in self._table

    def __len__(self) -> int:
        """Return the number of registered backends."""
        return len(self._table)

    def _generation(self, name: str) -> int:
        return self._table._generation(name)  # noqa: SLF001 — the owned inner table

    def _snapshot(self) -> "list[_SnapshotRow]":
        return self._table._snapshot()  # noqa: SLF001 — the owned inner table

    def _restore(self, state: "list[_SnapshotRow]") -> None:
        self._table._restore(state)  # noqa: SLF001 — the owned inner table

    def _origins_added_since(self, state: "list[_SnapshotRow]") -> set[str]:
        return self._table._origins_added_since(state)  # noqa: SLF001 — the owned inner table

    def _drop_non_otto(self) -> set[str]:
        return self._table._drop_non_otto()  # noqa: SLF001 — the owned inner table

    # -- preparation and construction --------------------------------------

    def _fail(
        self, name: str, origin: str, source: str | None, stage: str, detail: str
    ) -> Exception:
        where = f"; configured in {source}" if source is not None else ""
        return self._error(
            f"{self.kind} {name!r} (registered by {origin}{where}): {stage} failed: {detail}"
        )

    def _describe(self, exc: Exception) -> str:
        """Describe a construction cause; one shaped like a validation error is never echoed."""
        if callable(getattr(exc, "errors", None)) and hasattr(exc, "title"):
            return self._describe_parse_error(exc)
        return f"{type(exc).__name__}: {exc}"

    def prepare(
        self, name: str, raw: "Mapping[str, object]", env: Env, *, source: str | None = None
    ) -> "Prepared[Env, T, M]":
        """Parse *raw* once with backend *name*'s config model; never call its factory.

        *source* names where the configuration came from (a settings file, a
        host); every stage's error names it beside the registering module.

        A failure raises the seam's construction error (the registry's
        *error*), naming the stage (lookup, resolution or parse), the
        registering module and the backend, with the cause chained.
        """
        try:
            self._table._ensure_known(name)  # noqa: SLF001 — the owned inner table
        except ValueError as exc:
            where = f"; configured in {source}" if source is not None else ""
            raise self._error(
                f"{self.kind} {name!r} (not registered{where}): lookup failed: {exc}"
            ) from exc
        origin = self._table.origin(name)
        generation = self._table._generation(name)  # noqa: SLF001 — the owned inner table
        try:
            entry = self._table._resolve(name, {"config"})  # noqa: SLF001 — the owned inner table
        except Exception as exc:
            raise self._fail(
                name, origin, source, "resolution", f"{type(exc).__name__}: {exc}"
            ) from exc
        if entry.config is None:
            if raw:
                keys = ", ".join(sorted(str(k) for k in raw))
                raise self._fail(
                    name, origin, source, "parse", f"takes no configuration; got keys {keys}"
                )
            return Prepared(
                self, name, generation, env, source, None, FrozenMap(), entry.metadata, None
            )
        model = cast("Any", entry.config)
        try:
            parsed = model.model_validate(raw, context={"env": env})
            normalized = FrozenMap.freeze_json(parsed.model_dump(mode="json"))
            facts_of = getattr(parsed, "prepared_facts", None)
            facts = facts_of() if callable(facts_of) else None
        except Exception as exc:
            raise self._fail(
                name, origin, source, "parse", self._describe_parse_error(exc)
            ) from exc
        return Prepared(
            self, name, generation, env, source, parsed, normalized, entry.metadata, facts
        )

    def build(self, prepared: "Prepared[Env, T, M]") -> T:
        """Build the backend *prepared* names: parse nothing, call the factory once.

        The factory receives a per-call deep copy of the parsed configuration,
        so one ``Prepared`` builds any number of times.

        A failure raises the seam's construction error (the registry's
        *error*), naming the stage (stale preparation, resolution,
        construction or result), the registering module, the backend and the
        preparation's source, with the cause chained.
        """
        name = prepared.backend
        source = prepared.source
        origin = self._table.origin(name) if name in self._table else "<not registered>"
        if prepared.registry is not self:
            raise self._fail(
                name,
                origin,
                source,
                "stale preparation",
                f"prepared by another registry ({prepared.registry.kind})",
            )
        if name not in self._table or self._table._generation(name) != prepared.generation:  # noqa: SLF001 — the owned inner table
            raise self._fail(
                name,
                origin,
                source,
                "stale preparation",
                "the backend was registered again since this configuration was prepared",
            )
        try:
            config = copy.deepcopy(prepared.config)
        except Exception as exc:
            raise self._fail(
                name,
                origin,
                source,
                "construction",
                f"the configuration cannot be copied: {self._describe(exc)}",
            ) from exc
        try:
            entry = self._table._resolve(name, {"factory", "cls"})  # noqa: SLF001 — the owned inner table
        except Exception as exc:
            raise self._fail(
                name, origin, source, "resolution", f"{type(exc).__name__}: {exc}"
            ) from exc
        try:
            if entry.factory is not None:
                factory = cast("Callable[[Configured[Any, Env]], T]", entry.factory)
                built = factory(Configured(config, prepared.env))
            else:
                built = cast("T", cast("Any", entry.cls).create(prepared.env))
        except Exception as exc:
            raise self._fail(name, origin, source, "construction", self._describe(exc)) from exc
        try:
            self._result(name, built)
        except Exception as exc:
            raise self._fail(
                name, origin, source, "result", f"{type(exc).__name__}: {exc}"
            ) from exc
        return built


_Table = Registry[Any] | BackendRegistry[Any, Any, Any] | Subscription[Any] | RegistryView[Any]
"""Any top-level engine table."""


def instances() -> "list[_Table]":
    """Every live top-level engine table: the catalog, for test support and guards.

    A :class:`BackendRegistry`'s inner table is not listed separately.
    """
    return list(_INSTANCES)

"""How a host spec's fields reach the host constructor.

A host is declared twice: by its runtime class (a dataclass under
:class:`~otto.host.remote_host.RemoteHost`) and by its boundary spec (a
:class:`~otto.host.HostSpec` subclass). Each spec field declares its *kind*
beside it as ``Annotated`` metadata: :class:`Pass` (the default),
:class:`Convert`, :class:`Consume` or :class:`Resolve`. A class field with no
spec counterpart says why with ``field(metadata={"otto": RuntimeOnly(...)})``.
:func:`check_coverage` proves, at host-class registration, that the two
declarations cover each other.

At runtime this module imports nothing from otto but :mod:`otto.errors`.
Every other otto type it names (the host and spec types, the element and
profile types, and :class:`~otto.registry.Prepared`) is imported for type
checking only.
"""

import copy
import dataclasses
import inspect
from collections.abc import Callable, Mapping
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Literal, final

from typing_extensions import override

from ..errors import OttoError

if TYPE_CHECKING:
    from ..registry import Prepared
    from .element import Element
    from .os_profile import HostClassEntry, OsProfile
    from .remote_host import RemoteHost
    from .specs import HostSpec


class _Marker:
    """The base of the spec-field kinds; its repr names the marker, never a callable."""

    __slots__ = ()

    @override
    def __repr__(self) -> str:
        return f"otto.host.{type(self).__name__}"


@final
class Pass(_Marker):
    """Forward the field to the constructor under its own name, when the entry set it.

    The default kind: a field with no marker is ``Pass``. An omitted field is
    not forwarded, so the host class's own default applies.
    """

    __slots__ = ()


@final
class Convert(_Marker):
    """Forward ``fn(value, build)`` when the entry set the field; :data:`Omit` forwards nothing.

    A conversion is not a configuration check: refuse bad input in the field's
    type or in the spec's ``check_in``, so the doctor reports it.
    """

    __slots__ = ("fn",)

    def __init__(self, fn: "Callable[[Any, HostBuild], object]") -> None:
        if not callable(fn):
            raise TypeError(f"Convert needs a callable, got {fn!r}")
        self.fn = fn


@final
class Consume(_Marker):
    """Use the field before construction and never forward it; *reason* says what uses it."""

    __slots__ = ("reason",)

    def __init__(self, reason: str) -> None:
        if not isinstance(reason, str) or not reason:
            raise ValueError("Consume needs a non-empty reason")
        self.reason = reason


@final
class Resolve(_Marker):
    """Always forward ``fn(build)``, whether or not the entry set the field."""

    __slots__ = ("fn",)

    def __init__(self, fn: "Callable[[HostBuild], object]") -> None:
        if not callable(fn):
            raise TypeError(f"Resolve needs a callable, got {fn!r}")
        self.fn = fn


@final
@dataclasses.dataclass(frozen=True)
class RuntimeOnly:
    """Marks a host-class field no spec field supplies, in ``field(metadata={"otto": ...})``.

    *supplied_by* ``"element"`` means the stock builder passes the factory's
    element; ``None`` means the field keeps its runtime default and is never
    passed.
    """

    reason: str
    supplied_by: Literal["element"] | None = None


@final
class _OmitType:
    __slots__ = ()
    _instance: "_OmitType | None" = None

    def __new__(cls) -> "_OmitType":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    @override
    def __repr__(self) -> str:
        return "otto.host.Omit"

    @override
    def __reduce__(self) -> str:
        return "Omit"


Omit = _OmitType()
"""What a :class:`Convert` callback returns to leave the constructor argument out."""


def _declared(klass: type, name: str) -> "list[_Marker]":
    info = getattr(klass, "model_fields", {}).get(name)
    return [m for m in (info.metadata if info is not None else []) if isinstance(m, _Marker)]


def field_kind(spec: type, name: str) -> "Pass | Convert | Consume | Resolve":
    """Return field *name*'s kind on *spec*: its own marker, else its nearest base's, else Pass.

    An override that declares no marker inherits its parent's (pydantic keeps
    only the overriding declaration's metadata, so the walk is along the MRO).

    Raises:
        ValueError: The nearest declaration carries two markers.
    """
    for klass in spec.__mro__:
        markers = _declared(klass, name)
        if len(markers) > 1:
            raise ValueError(
                f"{spec.__name__}.{name} declares two field kinds: {markers}; keep one"
            )
        if markers:
            return markers[0]  # ty: ignore[invalid-return-type] — the four kinds are the only markers
    return Pass()


def class_marker(cls: type, name: str) -> object | None:
    """Return *name*'s ``"otto"`` field metadata, from the nearest class in *cls*'s MRO with one."""
    for klass in cls.__mro__:
        declared = getattr(klass, "__dataclass_fields__", {}).get(name)
        if declared is not None and "otto" in declared.metadata:
            return declared.metadata["otto"]
    return None


def _required_params(cls: type) -> list[str]:
    """Return the constructor parameters of *cls* that have no default.

    A ``**kwargs`` wrapper hides its contract, so behind one the dataclass
    fields decide: every ``init`` field the signature does not name, with
    neither a default nor a default factory, is required too.
    """
    params = inspect.signature(cls).parameters
    named = [
        n
        for n, p in params.items()
        if p.kind not in (p.VAR_POSITIONAL, p.VAR_KEYWORD) and p.default is p.empty
    ]
    if any(p.kind is p.VAR_KEYWORD for p in params.values()):
        named += [
            f.name
            for f in dataclasses.fields(cls)  # ty: ignore[invalid-argument-type] — a host dataclass
            if f.init
            and f.name not in params
            and f.default is dataclasses.MISSING
            and f.default_factory is dataclasses.MISSING
        ]
    return named


def check_coverage(where: str, cls: type, spec: type) -> None:
    """Refuse a class/spec pair whose fields do not cover each other.

    Every forwarded spec field (``Pass``, ``Convert``, ``Resolve``) needs a
    constructor field of the same name; every public constructor field is a
    spec field or marked ``RuntimeOnly``; no name is both a spec field and
    ``RuntimeOnly``; every marker is well formed; and every constructor
    parameter without a default has a supplier: a required ``Pass`` or
    ``Convert`` spec field, a ``Resolve`` field, or
    ``RuntimeOnly(supplied_by="element")``.

    Raises:
        ValueError: Naming *where*, both classes, and every gap with its fix.
    """
    problems: list[str] = []
    kinds: dict[str, object] = {}
    for name in spec.model_fields:  # ty: ignore[unresolved-attribute] — a HostSpec subclass
        try:
            kinds[name] = field_kind(spec, name)
        except ValueError as exc:  # noqa: PERF203 — once per field at registration; every gap is reported
            problems.append(str(exc))
            kinds[name] = None  # declared, so no "neither a spec field" report below
    cls_fields = dataclasses.fields(cls)  # ty: ignore[invalid-argument-type] — a host dataclass
    init = {f.name for f in cls_fields if f.init}
    for name, kind in kinds.items():
        if kind is None:
            continue
        if isinstance(class_marker(cls, name), RuntimeOnly):
            problems.append(
                f"{cls.__name__}.{name} is marked RuntimeOnly but {spec.__name__} declares it"
            )
        elif not isinstance(kind, Consume) and name not in init:
            problems.append(
                f"spec field {name!r} has no {cls.__name__} constructor field; add the field to "
                f"the class, or declare it Consume(reason) on {spec.__name__}"
            )
    for f in cls_fields:
        marker = class_marker(cls, f.name)
        if marker is None:
            if f.init and not f.name.startswith("_") and f.name not in kinds:
                problems.append(
                    f"{cls.__name__}.{f.name} is neither a {spec.__name__} field nor marked "
                    f"RuntimeOnly; declare it on the spec, or mark it "
                    f"field(metadata={{'otto': RuntimeOnly(reason=...)}})"
                )
            continue
        if not isinstance(marker, RuntimeOnly):
            problems.append(f"{cls.__name__}.{f.name}: its 'otto' metadata must be a RuntimeOnly")
            continue
        if not isinstance(marker.reason, str) or not marker.reason:
            problems.append(f"{cls.__name__}.{f.name}: RuntimeOnly needs a non-empty reason")
        if marker.supplied_by not in (None, "element"):
            problems.append(
                f"{cls.__name__}.{f.name}: RuntimeOnly supplied_by must be None or 'element', "
                f"got {marker.supplied_by!r}"
            )
    spec_fields = spec.model_fields  # ty: ignore[unresolved-attribute] — a HostSpec subclass
    for name in _required_params(cls):
        if name in kinds and kinds[name] is None:
            continue  # its two kinds are already reported
        kind = kinds.get(name)
        marker = class_marker(cls, name)
        supplied = (
            isinstance(kind, Resolve)
            or (isinstance(kind, (Pass, Convert)) and spec_fields[name].is_required())
            or (isinstance(marker, RuntimeOnly) and marker.supplied_by == "element")
        )
        if not supplied:
            problems.append(
                f"{cls.__name__} requires {name!r}, which nothing supplies: make it a required "
                f"{spec.__name__} field, a Resolve field, or give it a default"
            )
    if problems:
        raise ValueError(
            f"{where}: {cls.__name__} and {spec.__name__} do not cover each other: "
            + "; ".join(problems)
        )


def _declared_default(cls: object, name: str) -> object:
    """Return the plain default *cls* declares for dataclass field *name*, else ``None``.

    ``None`` too when the field is absent or declares a default factory.
    """
    declared = getattr(cls, "__dataclass_fields__", {}).get(name)
    if declared is None or declared.default is dataclasses.MISSING:
        return None
    return declared.default


def _effective_default(spec: object, cls: object, name: str) -> object:
    """Return the value field *name* reaches the host with: the entry's, else *cls*'s default.

    A field the entry set (directly or through its profile) is forwarded, so
    its value wins; an omitted one is not, so the host class's own declared
    default applies (:func:`_declared_default`).
    """
    if name in getattr(spec, "model_fields_set", ()):
        return getattr(spec, name, None)
    return _declared_default(cls, name)


def profile_inputs(spec: type) -> frozenset[str]:
    """Return the fields a profile default may set on *spec*: all but ``os_type`` and Consume."""
    return frozenset(
        name
        for name in spec.model_fields  # ty: ignore[unresolved-attribute] — a HostSpec subclass
        if name != "os_type" and not isinstance(field_kind(spec, name), Consume)
    )


FlatPreferences = dict[str, list[str]]
"""One host's capability selections from the preferences table, e.g. ``{"term": ["ssh"]}``."""


class HostValidationError(OttoError, ValueError):
    """A host entry failed contextual validation, or its readiness record cannot build.

    The message names the host id.
    """


@final
@dataclasses.dataclass(frozen=True)
class HostContext:
    """Everything contextual validation reads about one entry beyond the entry itself."""

    element: "Element"
    """The element the host belongs to; the built host carries this very object."""

    entry: "HostClassEntry"
    """The host class registration the entry's ``os_type`` resolved to (classes, never refs)."""

    profile: "OsProfile"
    """The whole profile the ``os_type`` resolved to."""

    host_id: str
    """The id the built host will report."""

    preferences: "FlatPreferences | None"
    """The host's selections, or ``None`` when preferences are unknown (no-preference mode)."""


@final
@dataclasses.dataclass(frozen=True)
class HostSpecInContext:
    """A host entry's validated spec and the context it validates in.

    Returned by :func:`~otto.host.host_context`. Build with
    ``hc.spec.to_host(ready=hc.spec.validate_in(hc.context))``.
    """

    spec: "HostSpec"
    """The entry's structurally validated spec, of the class its ``os_type`` resolved to."""

    context: HostContext
    """The context :meth:`~otto.host.HostSpec.validate_in` reads."""


@dataclasses.dataclass(frozen=True)
class _Payload:
    context: HostContext
    values: "dict[str, object]"
    fields_set: frozenset[str]
    selections: "dict[str, str] | None"
    power: "Prepared[Any, Any, Any] | None"


@final
@dataclasses.dataclass(frozen=True, eq=False)
class HostReadiness:
    """The record contextual validation issues and construction consumes, once.

    Issued only by :meth:`~otto.host.HostSpec.validate_in`. Constructing,
    mutating, copying or pickling a record is unsupported.
    """

    host_id: str
    """The id of the host this record builds."""

    cls: "type[RemoteHost]"
    """The host class this record builds."""

    selections: "Mapping[str, str] | None"
    """The effective term/transfer/impairer selections; ``None`` for a no-preference record."""

    _owner: object = dataclasses.field(init=False, repr=False)
    _probe: bool = dataclasses.field(init=False, repr=False)
    _witness: "Mapping[str, str] | None" = dataclasses.field(init=False, repr=False)
    _payload: _Payload = dataclasses.field(init=False, repr=False)
    _consumed: bool = dataclasses.field(init=False, repr=False)


@final
@dataclasses.dataclass(frozen=True)
class HostBuild:
    """What :class:`Convert` and :class:`Resolve` callbacks read while a record builds."""

    inputs: "Mapping[str, object]"
    """The validated field values, read-only."""

    context: HostContext
    """The context the record was issued in (a copy; the element is the original)."""

    selections: "Mapping[str, str]"
    """The effective selections, read-only."""

    power: "Prepared[Any, Any, Any] | None"
    """The record's prepared power configuration, or ``None`` when the entry declares none."""


def _issue_readiness(
    *,
    owner: object,
    context: HostContext,
    values: "dict[str, object]",
    fields_set: frozenset[str],
    selections: "dict[str, str] | None",
    witness: "dict[str, str] | None",
    power: "Prepared[Any, Any, Any] | None",
) -> HostReadiness:
    """Copy the construction payload once and issue the record.

    The element, and the registry that prepared *power*, are kept by identity.
    """
    memo: dict[int, object] = {id(context.element): context.element}
    if power is not None:
        memo[id(power.registry)] = power.registry
    try:
        payload = copy.deepcopy(_Payload(context, values, fields_set, selections, power), memo)
    except Exception as exc:
        # Named by type only: a copy hook's message (a ValidationError's above
        # all) may quote the rejected value. The cause stays chained.
        raise HostValidationError(
            f"host {context.host_id!r}: its validated configuration cannot be copied for "
            f"construction: {type(exc).__name__} (its message is not shown: it may quote "
            f"a rejected value)"
        ) from exc
    ready = HostReadiness(
        host_id=context.host_id,
        cls=context.entry.cls,  # ty: ignore[invalid-argument-type] — HOST_CLASSES.get resolved the entry, so cls is a class
        selections=None if payload.selections is None else MappingProxyType(payload.selections),
    )
    for name, value in [
        ("_owner", owner),
        ("_probe", selections is None),
        ("_witness", None if witness is None else MappingProxyType(dict(witness))),
        ("_payload", payload),
        ("_consumed", False),
    ]:
        object.__setattr__(ready, name, value)
    return ready


def _open_record(ready: HostReadiness, owner: object) -> HostBuild:
    """Refuse a foreign, probe or consumed record; consume it; return the build view."""
    host_id = getattr(ready, "host_id", "?")
    if not isinstance(ready, HostReadiness) or getattr(ready, "_owner", None) is not owner:
        raise HostValidationError(
            f"host {host_id!r}: this readiness record was issued for another spec; build with "
            f"the record this spec's validate_in returned"
        )
    if ready._probe:  # noqa: SLF001 — this module owns the record
        witness = dict(ready._witness or {})  # noqa: SLF001 — this module owns the record
        raise HostValidationError(
            f"host {host_id!r}: a record from no-preference validation (validate_host_dict, "
            f"host_identity) cannot build (it validated through {witness!r}); validate "
            f"through otto.host.host_context or create_host_from_dict"
        )
    if ready._consumed:  # noqa: SLF001 — this module owns the record
        raise HostValidationError(
            f"host {host_id!r}: record already consumed; validate again to build again"
        )
    object.__setattr__(ready, "_consumed", True)
    payload = ready._payload  # noqa: SLF001 — this module owns the record
    return HostBuild(
        inputs=MappingProxyType(payload.values),
        context=payload.context,
        selections=MappingProxyType(payload.selections or {}),
        power=payload.power,
    )

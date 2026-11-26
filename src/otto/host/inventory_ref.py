"""Inventory references: what a host entry's ``inventory`` names, and where its facts came from.

:func:`normalize_reference` is the one reading of a host entry's ``inventory``
value, bare (``"edge-17"``) or qualified (``{"name": "physical", "key":
"edge-17"}``). The host spec, the runtime join
(:func:`otto.inventory.resolve_host_entry`), the doctor and the emitted
``lab.json`` schema (:func:`inventory_reference_schema`) all decide through it.

``InventoryRef`` is stamped by :func:`otto.host.factory.create_host_from_dict`
at build, before the product providers run, like the host's ``element``. An
inline host, a container and the builtin ``local`` host carry an empty ref.

:class:`MachineIdentity` pairs a host's address with its ref, and
:func:`same_machine` is the one judgement of whether two declarations of one
host id are one machine; every merge asks it through :func:`first_conflict`,
and a whole element replacing another through :func:`same_machines`.
:func:`spell_machine` is how a message names one.

:data:`FIELD_RECORDING` and :func:`recording_fields` are the one seam of
``otto lab explain``'s recording (:mod:`otto.host.provenance`): every merge
that could note where a value came from asks :func:`recording_fields` first,
and imports the recording module only when it says yes. They live here
because every lab build has loaded this module already, so a build that
explains nothing loads nothing more.

Pure Python: this module imports no pydantic, so the bootstrap path can read a
reference without building a model.
"""

import contextlib
from collections.abc import Mapping, Sequence
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from ..errors import describe_refused

FIELD_RECORDING: "ContextVar[Any]" = ContextVar("otto_field_recording", default=None)
"""The :class:`~otto.host.provenance.FieldTrace` of the live recording, or ``None``.

Set only by :func:`otto.host.provenance.recording`; read through
:func:`recording_fields`."""


def recording_fields() -> bool:
    """Return whether a recording of where each host value comes from is live.

    The question every recording site asks before it imports
    :mod:`otto.host.provenance` or builds anything for it.
    """
    return FIELD_RECORDING.get() is not None


NOT_RECORDING = contextlib.nullcontext()
"""The scope of a load no recording attributes, one shared object that costs nothing.

What a recording site enters instead of its scope when :func:`recording_fields`
says no (a source's load in the composite, a host entry's in a lab file)."""


DEFAULT_INVENTORY = "default"
"""The inventory a bare reference resolves through, and the name a legacy
``[inventory]`` table binds."""

_QUALIFIED_KEYS = frozenset({"name", "key"})
"""Exactly the keys of a qualified reference."""

_REFUSAL = "'inventory' must name a key (a non-empty string)"
"""How every refused reference begins; what was given is described at the end."""


@dataclass(frozen=True, kw_only=True, order=True)
class InventoryReference:
    """One host entry's ``inventory`` value, normalized.

    Ordered by ``(name, key, qualified)``, so a sorted list of references is
    stable; hashable, so a set of them de-duplicates.
    """

    name: str
    """The inventory consulted; :data:`DEFAULT_INVENTORY` for a bare reference."""

    key: str
    """The record's key within that inventory."""

    qualified: bool
    """Whether the entry stated the inventory's name (the ``{"name", "key"}`` form)."""


def _qualified_problem(value: Mapping[Any, Any]) -> "str | None":
    """Say what is wrong with a mapping meant as a qualified reference, or ``None``."""
    unknown = sorted(describe_refused(k) for k in value if k not in _QUALIFIED_KEYS)
    if unknown:
        return f"unknown key {', '.join(unknown)}"
    missing = sorted(_QUALIFIED_KEYS - set(value))
    if missing:
        return f"missing {', '.join(repr(k) for k in missing)}"
    for name in ("name", "key"):
        if not isinstance(value[name], str) or not value[name]:
            return f"{name!r} must be a non-empty string"
    return None


def normalize_reference(*, value: object) -> "InventoryReference | None":
    """Return what *value*, a host entry's ``inventory``, references; ``None`` for nothing.

    ``None`` (and so an absent key, read with ``.get``) references nothing: the
    entry is inline. A non-empty string is a bare reference through
    :data:`DEFAULT_INVENTORY`. A mapping of exactly ``name`` and ``key``, both
    non-empty strings, is a qualified reference to that inventory.

    Raises:
        ValueError: For any other value, the empty string included. The message
            begins ``'inventory' must name a key (a non-empty string)`` and ends
            ``, got <the value>``, the value described by
            ``otto.errors.describe_refused``: a mapping by its keys and
            any other container by its type, never by what it holds.
    """
    if value is None:
        return None
    if isinstance(value, str) and value:
        return InventoryReference(name=DEFAULT_INVENTORY, key=value, qualified=False)
    problem = _qualified_problem(value) if isinstance(value, Mapping) else None
    if isinstance(value, Mapping) and problem is None:
        return InventoryReference(name=value["name"], key=value["key"], qualified=True)
    detail = ' or an inventory and a key, {"name": "<inventory>", "key": "<key>"}'
    if problem is not None:
        detail += f" ({problem})"
    raise ValueError(f"{_REFUSAL}{detail}, got {describe_refused(value)}")


def inventory_reference_schema(*, nullable: bool) -> dict[str, Any]:
    """Return the JSON Schema of an ``inventory`` value :func:`normalize_reference` accepts.

    With *nullable*, ``null`` (references nothing) is accepted too: that is the
    field's own schema. Without it, the schema is a reference proper, which is
    what an entry carrying no inline address must state. A fresh document each
    call, so a caller that decorates one never edits another's.
    """
    nonempty = {"type": "string", "minLength": 1}
    arms: list[dict[str, Any]] = [
        dict(nonempty),
        {
            "type": "object",
            "properties": {"name": dict(nonempty), "key": dict(nonempty)},
            "required": sorted(_QUALIFIED_KEYS),
            "additionalProperties": False,
        },
    ]
    if nullable:
        arms.append({"type": "null"})
    return {"anyOf": arms}


INVENTORY_REFERENCE_SCHEMA: dict[str, Any] = inventory_reference_schema(nullable=False)
"""The schema of a reference proper (never ``null``); call
:func:`inventory_reference_schema` for a copy to embed."""


@dataclass(frozen=True)
class InventoryRef:
    """Inventory provenance of one host.

    Hashing raises ``TypeError`` (``extra`` is a dict); key collections by
    ``key``.
    """

    key: str = ""
    """The inventory key the entry referenced; ``""`` for an inline host."""

    backend: str = ""
    """The inventory's label, e.g. ``json:/home/me/lab/inventory.json``."""

    extra: dict[str, Any] = field(default_factory=dict)
    """The record's opaque ``extra`` table — a per-host copy; otto never reads it."""

    name: str = ""
    """The inventory consulted, e.g. ``physical``; ``""`` for an inline host.

    A referenced ref built without one names :data:`DEFAULT_INVENTORY`, so a
    third-party lab source that builds ``InventoryRef(key=..., backend=...)``
    equals what the built-in join stamps for a bare reference.
    """

    qualified: bool = False
    """Whether the entry named its inventory (``{"name": ..., "key": ...}``)."""

    def __post_init__(self) -> None:
        # frozen=True blocks rebinding, not mutation of the dict behind the
        # field (the LabInfo lesson): copy, so no host aliases the record.
        object.__setattr__(self, "extra", dict(self.extra))
        if self.key and not self.name:
            object.__setattr__(self, "name", DEFAULT_INVENTORY)

    @property
    def referenced(self) -> bool:
        """Whether this host was built from an inventory record."""
        return bool(self.key)


def spell_ref(*, ref: InventoryRef) -> str:
    """Spell *ref* as ``name/key`` for a collision message, or ``inline`` when unreferenced."""
    return f"{ref.name}/{ref.key}" if ref.referenced else "inline"


@dataclass(frozen=True, kw_only=True)
class MachineIdentity:
    """What a host declaration says about which machine it is: its address and provenance."""

    ip: str
    """The host's address."""

    ref: InventoryRef
    """Where its facts came from; empty for an inline host."""


def same_machine(*, a: MachineIdentity, b: MachineIdentity) -> bool:
    """Return whether *a* and *b* describe one machine: the one judgement every merge makes.

    The addresses must be equal. An inline declaration (no key) at that
    address is the same machine as anything there, as today. Two referenced
    declarations must name the same inventory: two isolated inventories may
    reuse one private address for two machines. Their keys are compared only
    when both references are qualified; a bare key stays an alias, as two bare
    keys at one address always were.
    """
    if a.ip != b.ip:
        return False
    if not a.ref.referenced or not b.ref.referenced:
        return True
    if a.ref.name != b.ref.name:
        return False
    return not (a.ref.qualified and b.ref.qualified) or a.ref.key == b.ref.key


def first_conflict(
    *, incoming: MachineIdentity, seen: "Sequence[MachineIdentity]"
) -> "MachineIdentity | None":
    """Return the first of *seen* that is not :func:`same_machine` with *incoming*.

    ``None`` when every one is. Every merge decides a collision through it.
    """
    return next((s for s in seen if not same_machine(a=incoming, b=s)), None)


def same_machines(
    *, replaced: "Sequence[MachineIdentity]", incoming: "Sequence[MachineIdentity]"
) -> bool:
    """Return whether *incoming* declares the machines *replaced* did, as a whole.

    Each incoming identity is :func:`same_machine` with some replaced one, and
    each replaced identity with some incoming one; which host id each is
    declared under is not compared, so an element whose boards moved still
    holds the same machines.
    """
    return all(any(same_machine(a=i, b=r) for r in replaced) for i in incoming) and all(
        any(same_machine(a=r, b=i) for i in incoming) for r in replaced
    )


def spell_machine(*, identity: MachineIdentity) -> str:
    """Spell *identity* as a message names a machine: ``<ip> as <name>/<key>``, or ``as inline``."""
    return f"{identity.ip} as {spell_ref(ref=identity.ref)}"

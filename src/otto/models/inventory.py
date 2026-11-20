"""Boundary spec for one inventory record (spec 2026-08-28 host-inventory §4).

An inventory record is the tool-agnostic half of a host: what is true about
the machine regardless of otto. Its field names are
:class:`~otto.host.specs.HostSpec`-family field names — the same name the
referencing host's own concrete spec (:class:`~otto.host.specs.UnixHostSpec`,
:class:`~otto.host.specs.EmbeddedHostSpec`) declares — so the join
(:func:`otto.inventory.resolve_host_entry`) is a plain key copy and
no mapping table exists in otto core: a mapping is where drift hides.
Every field below is on the shared ``HostSpec`` base, so no record can be
refused by the host family it lands on: ``hw_version``/``sw_version`` were the
last two declared on ``UnixHostSpec`` alone, and §4 widened them. The
deployment's ``supplies`` declaration
(:func:`~otto.inventory.protocol.check_supplies`) is therefore the only control
on which fields a given record may carry. Three record fields have no
host-spec twin at all. ``extra`` is an opaque table otto never reads, carried
onto the host as ``host.inventory_ref.extra``. ``groups`` and ``labels`` are
the discovery vocabulary (spec 2026-11-18 lab-templating §6.2): facts about the
instance the machine belongs to, read by discovery and templates, never copied
onto a host and never otto behaviour.
"""

from typing import Annotated, Any

from pydantic import Field, StrictInt, StrictStr, field_validator, model_validator
from typing_extensions import final

from ..host.address import validate_address
from ..host.specs import InterfaceSpec, IntOrStr
from ..host.specs import (
    coerce_digit_string as coerce_digit_string,  # noqa: PLC0414 — explicit re-export
)
from .base import CredSpec, OttoModel, nonnegative, strip_comment_keys


@final
class InventoryRecord(OttoModel):
    """One inventory record: machine facts, keyed by an opaque inventory key."""

    ip: str
    """Management address — the one field every record must have."""

    interfaces: dict[str, InterfaceSpec] = Field(default_factory=dict)
    creds: list[CredSpec] = Field(default_factory=list)
    hw_version: str | None = None
    sw_version: str | None = None
    """A declaration of what the device should run; the probe reports what it does."""
    os_name: str | None = None
    os_version: str | None = None
    board: str | None = None
    site: IntOrStr | None = None
    rack: IntOrStr | None = None
    shelf: int | None = None
    slot: int | None = None
    is_virtual: bool = False
    element_id: int | None = None
    """A KEY rather than data — asserted here and on the element, never filled (spec §2)."""
    groups: list[StrictStr] = Field(default_factory=list)
    """Memberships, such as a role, a tag or a group; discovery data, never a host field."""
    labels: dict[str, StrictStr | StrictInt] = Field(default_factory=dict)
    """Scalar facts about the instance (``pod``, ``vlan``): discovery data, never a host field.

    Strict, so ``"007"`` stays a string and a boolean, float, array or object
    is refused naming the label rather than coerced.
    """
    extra: dict[str, Any] = Field(default_factory=dict)
    """Opaque; otto never reads it."""

    @model_validator(mode="before")
    @classmethod
    def _strip(cls, data: object) -> object:
        return strip_comment_keys(data)

    @field_validator("ip")
    @classmethod
    def _validate_ip(cls, v: str) -> str:
        # The host field's own rule: a record's ip becomes the host's ip.
        return validate_address(v, allow_hostname=True)

    @field_validator("interfaces", mode="before")
    @classmethod
    def _coerce_interface_shorthand(cls, v: object) -> object:
        # "eth0": "10.0.0.5" -> "eth0": {"ip": "10.0.0.5"}, as HostSpec accepts.
        if isinstance(v, dict):
            return {k: ({"ip": e} if isinstance(e, str) else e) for k, e in v.items()}
        return v

    @field_validator("element_id", "slot", "shelf")
    @classmethod
    def _nonnegative(cls, v: int | None) -> int | None:
        return nonnegative(v)


INVENTORY_KEY_FIELDS: frozenset[str] = frozenset({"element_id"})
"""Record fields that are identity keys — cross-checked against the lab file, never copied."""

MERGED_INVENTORY_FIELDS: frozenset[str] = frozenset({"creds"})
"""Supplied fields the join COMPOSES by login instead of refusing inline (spec 2026-09-06 §6.2).

The one exception to "data lives in exactly one layer": a referenced entry
may state ``creds`` beside a reference, and the lab file's entries layer over
the record's (which already layer over the creds store's). ONE definition,
so every consumer that reasons about the partition names the same set.
"""

SUPPLIES_EXEMPT_FIELDS: frozenset[str] = INVENTORY_KEY_FIELDS | {"extra", "groups", "labels"}
"""Record fields the ``supplies`` partition does not govern (spec §2, §4).

A record may carry these whatever its inventory declares, and no reader may
treat one as a field the inventory "supplied": an identity key (``element_id``)
is ASSERTED rather than filled — the join cross-checks it against the lab file
— ``extra`` is opaque, and ``groups``/``labels`` are discovery data (spec
2026-11-18 lab-templating §6.2). None has a host-spec twin to collide with, so
the join never copies one onto a host.

ONE definition, because three readers apply the rule: the stage-1 document
parser (:func:`otto.inventory.json_backend.parse_inventory_document`, which the
snapshot cache reads its snapshots back through), the backend conformance
suite (:func:`otto.testing.assert_inventory_conforms`), and
:data:`FILLABLE_INVENTORY_FIELDS` right below. A partition rule spelled three
times is a partition rule that will eventually disagree with itself.
"""

FILLABLE_INVENTORY_FIELDS: frozenset[str] = (
    frozenset[str](InventoryRecord.model_fields) - SUPPLIES_EXEMPT_FIELDS
)
"""The MOST a backend may supply (spec §4). Derived, so a new field cannot dodge enforcement."""


INVENTORY_V2_FORMAT = "otto.inventory.v2"
"""The json inventory's ``$format`` marker for the document that declares instances.

A marker rather than a shape test, because ``instances`` and every other bare
top-level name are already legal inventory keys in a flat document.
"""

InstanceKey = Annotated[str, Field(min_length=1)]
"""An instance member, which is one of the declaring backend's keys and never empty."""


def _canonical_keys(keys: list[str], *, where: str = "") -> list[str]:
    """Return *keys* sorted, refusing a repeated key (the message prefixed by *where*)."""
    seen: set[str] = set()
    for key in keys:
        if key in seen:
            raise ValueError(f"{where}duplicate key {key!r}")
        seen.add(key)
    return sorted(keys)


@final
class InventoryInstance(OttoModel):
    """One instance an inventory declares: the keys that form, say, one rig.

    Members are the declaring backend's OWN keys (spec 2026-11-18
    lab-templating §6.2) and need no record there: only an explicit instance
    map can report an instance whose machines have all gone. ``roles`` names
    the part a key plays (``dut``, ``tgen``); ``members`` holds keys that
    belong without one, so a key in ``members`` may not also play a role; one
    key may play several roles. Every list is canonical, sorted with no
    repeat, so two documents that state the same instance compare equal; an
    instance with no member is legal.
    """

    members: list[InstanceKey] = Field(default_factory=list)
    """Keys that belong to the instance without a role."""
    roles: dict[str, list[InstanceKey]] = Field(default_factory=dict)
    """Role name to the keys that play it."""

    @model_validator(mode="before")
    @classmethod
    def _strip(cls, data: object) -> object:
        return strip_comment_keys(data)

    @field_validator("members")
    @classmethod
    def _canonical_members(cls, v: list[str]) -> list[str]:
        return _canonical_keys(v)

    @field_validator("roles")
    @classmethod
    def _canonical_roles(cls, v: dict[str, list[str]]) -> dict[str, list[str]]:
        if "" in v:
            raise ValueError("a role name must be a non-empty string")
        return {role: _canonical_keys(v[role], where=f"role {role!r}: ") for role in sorted(v)}

    @model_validator(mode="after")
    def _members_have_no_role(self) -> "InventoryInstance":
        # ``members`` means "without a role": a key in both is stated twice.
        for role, keys in self.roles.items():
            both = sorted(set(keys) & set(self.members))
            if both:
                raise ValueError(
                    f"key {both[0]!r} is in 'members' and plays role {role!r}; "
                    "'members' holds keys without a role, so list it once"
                )
        return self

    def all_members(self) -> list[str]:
        """Return every key in the instance, with or without a role, sorted and once each."""
        return sorted({*self.members, *(key for keys in self.roles.values() for key in keys)})

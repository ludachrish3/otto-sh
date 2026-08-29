"""Boundary spec for one inventory record (spec 2026-08-28 host-inventory §4).

An inventory record is the tool-agnostic half of a host: what is true about
the machine regardless of otto. Its field names are
:class:`~otto.models.host.HostSpec`-family field names — the same name the
referencing host's own concrete spec (:class:`~otto.models.host.UnixHostSpec`,
:class:`~otto.models.host.EmbeddedHostSpec`) declares — so the join
(:func:`otto.inventory.resolve_host_entry`) is a plain key copy and
no mapping table exists in otto core: a mapping is where drift hides.
Every field below is on the shared ``HostSpec`` base, so no record can be
refused by the host family it lands on: ``hw_version``/``sw_version`` were the
last two declared on ``UnixHostSpec`` alone, and §4 widened them. The
deployment's ``supplies`` declaration
(:func:`~otto.inventory.protocol.check_supplies`) is therefore the only control
on which fields a given record may carry. ``extra`` is the one record field
with no host-spec twin at all: an opaque table otto never reads, carried onto
the host as ``host.inventory_ref.extra``.
"""

import re
from datetime import timedelta
from typing import Any

from pydantic import Field, field_validator, model_validator

from .base import OttoModel
from .host import CredSpec, InterfaceSpec, IntOrStr
from .host import coerce_digit_string as coerce_digit_string  # noqa: PLC0414 — explicit re-export
from .lab import _strip_comment_keys


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
    extra: dict[str, Any] = Field(default_factory=dict)
    """Opaque; otto never reads it."""

    @model_validator(mode="before")
    @classmethod
    def _strip(cls, data: object) -> object:
        return _strip_comment_keys(data)

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
        if v is not None and v < 0:
            raise ValueError(f"must be >= 0, got {v}")
        return v


INVENTORY_KEY_FIELDS: frozenset[str] = frozenset({"element_id"})
"""Record fields that are identity keys — cross-checked against the lab file, never copied."""

SUPPLIES_EXEMPT_FIELDS: frozenset[str] = INVENTORY_KEY_FIELDS | {"extra"}
"""Record fields the ``supplies`` partition does not govern (spec §2, §4).

A record may carry these whatever its inventory declares, and no reader may
treat one as a field the inventory "supplied": an identity key (``element_id``)
is ASSERTED rather than filled — the join cross-checks it against the lab file
— and ``extra`` is opaque, with no host-spec twin to collide with.

ONE definition, because three readers apply the rule: the stage-1 document
parser (:func:`otto.inventory.json_backend.parse_inventory_document`, which the
snapshot cache reads its snapshots back through), the backend conformance
suite (:func:`otto.testing.assert_inventory_conforms`), and
:data:`FILLABLE_INVENTORY_FIELDS` right below. A partition rule spelled three
times is a partition rule that will eventually disagree with itself.
"""

FILLABLE_INVENTORY_FIELDS: frozenset[str] = (
    frozenset(InventoryRecord.model_fields) - SUPPLIES_EXEMPT_FIELDS
)
"""The MOST a backend may supply (spec §4). Derived, so a new field cannot dodge enforcement."""


_TTL = re.compile(r"\A(?:0|([1-9]\d*)([mhd]))\Z")
"""``\\Z``, not ``$``: ``$`` also matches before a trailing newline, so ``"24h\\n"``
would parse — and a settings value with a stray newline must be refused, not guessed at."""

_TTL_UNIT = {"m": timedelta(minutes=1), "h": timedelta(hours=1), "d": timedelta(days=1)}


def parse_cache_ttl(text: str) -> timedelta:
    """``"24h"`` → 24 hours; ``"0"`` → no caching (spec §9.5). Units: ``m``, ``h``, ``d``.

    Lives here rather than in the inventory package because
    :class:`~otto.models.settings.InventoryConfigSpec` validates ``cache_ttl``
    at the settings boundary, and a boundary model may not import a runtime
    package. Deliberately narrow: no leading zeros, no whitespace, no
    fractions, no week/second units — one spelling per duration, so two
    settings files that mean the same thing look the same.

    Every rejection is a ``ValueError``, including an out-of-range one. The
    grammar admits arbitrarily many digits but ``timedelta`` does not, and an
    ``OverflowError`` escaping here would sail through every caller's
    ``except ValueError`` (the pydantic validator, ``load_user_settings``,
    ``build_inventory``) and reach the user as a bare traceback naming no file.
    """
    m = _TTL.match(text)
    if m is None:
        raise ValueError(f"cache_ttl must be '0' or <n>m / <n>h / <n>d, got {text!r}")
    if m.group(1) is None:
        return timedelta(0)
    try:
        return int(m.group(1)) * _TTL_UNIT[m.group(2)]
    except OverflowError as e:
        raise ValueError(f"cache_ttl {text!r} is out of range: {e}") from e

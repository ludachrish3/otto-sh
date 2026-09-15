"""The one pysnmp GET in otto, with a raw outcome and no opinion.

``otto.host`` (the probe survey) and ``otto.monitor`` (the collector) both
need to send one SNMP GET and learn what happened. The monitor package
depends on the host package, so the GET cannot live in the monitor without
handing the host package a cycle; it lives here, a leaf, and both import it.

This function reports exactly four things -- the agent ANSWERED, there was
SILENCE (a transport error, which for v2c also covers a refused community:
agents drop a wrong community without a reply), pysnmp raised an
ERROR-INDICATION, or the agent returned an ERROR-STATUS -- and leaves
coercion, logging and verdicts to the caller. pysnmp is imported inside the
function so ``import otto.snmp`` costs nothing but the stdlib.
"""

from dataclasses import dataclass, field
from typing import Literal

SnmpOutcome = Literal["answered", "silence", "error-indication", "error-status"]

__all__ = ["SnmpGet", "SnmpOutcome", "snmp_get"]


@dataclass(frozen=True, slots=True)
class SnmpGet:
    """What one GET produced."""

    outcome: SnmpOutcome
    values: dict[str, object] = field(default_factory=dict)
    """Requested OID -> raw varbind value; only populated when ``answered``."""
    detail: str = ""
    """Empty when answered; the reason text otherwise."""


def _match_requested(oid_str: str, requested: list[str]) -> str | None:
    """Map a returned OID back to a requested key, tolerating a trailing ``.0``.

    An exact match always wins before a tolerant one is attempted — otherwise
    a returned ``...10.10`` can be mis-matched to a requested ``...10.1`` by
    an indiscriminate suffix strip.
    """
    if oid_str in requested:
        return oid_str
    for key in requested:
        if key == oid_str or key == oid_str + ".0" or oid_str == key + ".0":
            return key
    return None


async def snmp_get(
    address: str,
    port: int,
    community: str,
    version: Literal["1", "2c"],
    oids: list[str],
    *,
    timeout: float = 2.0,
    retries: int = 1,
) -> SnmpGet:
    """GET *oids* from the agent at ``(address, port)`` in one PDU."""
    if not oids:
        return SnmpGet(outcome="answered")

    # Lazy: this module must import without pysnmp, and the unit tests fake
    # exactly this module path.
    from pysnmp.hlapi.v1arch.asyncio import (  # type: ignore[import-untyped]
        CommunityData,
        ObjectIdentity,
        ObjectType,
        SnmpDispatcher,
        UdpTransportTarget,
        get_cmd,
    )

    mp_model = 1 if version == "2c" else 0
    dispatcher = SnmpDispatcher()
    try:
        transport = await UdpTransportTarget.create(
            (address, port), timeout=timeout, retries=retries
        )
        error_indication, error_status, _error_index, var_binds = await get_cmd(
            dispatcher,
            CommunityData(community, mpModel=mp_model),
            transport,
            *(ObjectType(ObjectIdentity(oid)) for oid in oids),
        )
    except Exception as exc:  # noqa: BLE001 — pysnmp raises heterogeneous errors; every one is "silence" with its text
        return SnmpGet(outcome="silence", detail=f"{type(exc).__name__}: {exc}")
    finally:
        dispatcher.close()

    if error_indication:
        return SnmpGet(outcome="error-indication", detail=str(error_indication))
    if error_status:
        return SnmpGet(outcome="error-status", detail=error_status.prettyPrint())

    values: dict[str, object] = {}
    for oid_obj, value in var_binds:
        key = _match_requested(str(oid_obj), oids)
        if key is not None:
            values[key] = value
    return SnmpGet(outcome="answered", values=values)

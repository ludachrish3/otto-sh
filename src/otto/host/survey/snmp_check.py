"""snmp: one sysUpTime GET against the host's own `snmp` block, or the client defaults."""

from typing import Literal, SupportsInt, cast

from ...snmp import snmp_get
from .verdict import SNMP_SYSUPTIME_OID, ProtocolVerdict


async def check_snmp(
    *,
    address: str,
    port: int,
    community: str,
    version: str,
    vantage: str,
    timeout: float,
    retries: int,
) -> ProtocolVerdict:
    """``supported`` on an answer, ``timeout`` on silence, ``service-mismatch`` on an error-status.

    *version* arrives as ``str`` because ``SnmpOptions.version`` is one; the
    spec's two values are the only ones the options layer accepts.
    """
    got = await snmp_get(
        address,
        port,
        community,
        cast("Literal['1', '2c']", version),
        [SNMP_SYSUPTIME_OID],
        timeout=timeout,
        retries=retries,
    )
    if got.outcome == "answered":
        # The varbind is an ``object`` here because the leaf reports raw pysnmp
        # values without coercion. sysUpTime's TimeTicks converts through
        # ``__int__`` -- but an agent that does not implement the OID answers
        # with error-status 0 and a ``NoSuchObject``/``NoSuchInstance`` varbind,
        # modelled on ``univ.Null``, which has none. That is the service
        # answering with something otto did not ask for, so it is the
        # mismatch arm, not an exception escaping the check.
        value = got.values.get(SNMP_SYSUPTIME_OID)
        try:
            ticks = int(cast("SupportsInt", value))
        except (TypeError, ValueError):
            return ProtocolVerdict(
                protocol="snmp",
                kind="monitor",
                port=port,
                state="service-mismatch",
                tier="dial",
                vantage=vantage,
                detail=f"unexpected sysUpTime value {value!r}",
            )
        return ProtocolVerdict(
            protocol="snmp",
            kind="monitor",
            port=port,
            state="supported",
            tier="dial",
            vantage=vantage,
            detail=f"sysUpTime {ticks} via community {community!r}",
        )
    if got.outcome == "error-status":
        return ProtocolVerdict(
            protocol="snmp",
            kind="monitor",
            port=port,
            state="service-mismatch",
            tier="dial",
            vantage=vantage,
            detail=got.detail,
        )
    detail = (
        f"no reply from :{port} (no agent, or the community was refused: "
        f"v2c agents drop a wrong community silently)"
    )
    return ProtocolVerdict(
        protocol="snmp",
        kind="monitor",
        port=port,
        state="timeout",
        tier="dial",
        vantage=vantage,
        detail=detail,
    )

"""snmp: one sysUpTime GET; silence is timeout, never login-failed."""

import pytest

from otto.host.survey import snmp_check
from otto.host.survey.verdict import SNMP_SYSUPTIME_OID
from otto.snmp import SnmpGet

pytestmark = pytest.mark.asyncio


def _fake(got: SnmpGet, calls: list):
    async def snmp_get(address, port, community, version, oids, *, timeout, retries):
        calls.append((address, port, community, version, oids, timeout, retries))
        return got

    return snmp_get


async def test_an_answer_is_supported_with_the_uptime_and_community(monkeypatch):
    calls = []
    monkeypatch.setattr(
        snmp_check,
        "snmp_get",
        _fake(SnmpGet(outcome="answered", values={SNMP_SYSUPTIME_OID: 12345}), calls),
    )
    v = await snmp_check.check_snmp(
        address="10.10.200.14",
        port=16101,
        community="public",
        version="2c",
        vantage="controller",
        timeout=0.3,
        retries=0,
    )
    assert (v.protocol, v.kind, v.port, v.state, v.tier, v.vantage) == (
        "snmp",
        "monitor",
        16101,
        "supported",
        "dial",
        "controller",
    )
    assert v.detail == "sysUpTime 12345 via community 'public'"
    assert calls == [("10.10.200.14", 16101, "public", "2c", [SNMP_SYSUPTIME_OID], 0.3, 0)]


@pytest.mark.parametrize("outcome", ["silence", "error-indication"])
async def test_silence_is_timeout_and_says_a_community_may_have_been_refused(monkeypatch, outcome):
    """Mutation: report login-failed on silence and a missing agent is blamed on the community."""
    monkeypatch.setattr(
        snmp_check,
        "snmp_get",
        _fake(SnmpGet(outcome=outcome, detail="No SNMP response received before timeout"), []),
    )
    v = await snmp_check.check_snmp(
        address="h",
        port=161,
        community="public",
        version="2c",
        vantage="controller",
        timeout=2.0,
        retries=1,
    )
    assert v.state == "timeout"
    assert v.detail == (
        "no reply from :161 (no agent, or the community was refused: "
        "v2c agents drop a wrong community silently)"
    )


async def test_an_error_status_is_service_mismatch(monkeypatch):
    monkeypatch.setattr(
        snmp_check, "snmp_get", _fake(SnmpGet(outcome="error-status", detail="noSuchName"), [])
    )
    v = await snmp_check.check_snmp(
        address="h",
        port=161,
        community="public",
        version="2c",
        vantage="hop:test4",
        timeout=2.0,
        retries=1,
    )
    assert v.state == "service-mismatch"
    assert v.detail == "noSuchName"
    assert v.vantage == "hop:test4"


class _NoSuchObject:
    """Stands in for pysnmp's ``univ.Null`` subclasses: answered, but no value."""

    def __repr__(self) -> str:
        return "NoSuchObject"


@pytest.mark.parametrize("value", [_NoSuchObject(), None, "up"])
async def test_an_answer_otto_cannot_read_as_ticks_is_service_mismatch(monkeypatch, value):
    """An agent that does not implement sysUpTime answers with a Null varbind, not an error-status.

    Mutation: let ``int()`` run unguarded and the TypeError escapes ``check_snmp``,
    losing the candidate's verdict instead of reporting it.
    """
    values = {} if value is None else {SNMP_SYSUPTIME_OID: value}
    monkeypatch.setattr(
        snmp_check, "snmp_get", _fake(SnmpGet(outcome="answered", values=values), [])
    )
    v = await snmp_check.check_snmp(
        address="h",
        port=161,
        community="public",
        version="2c",
        vantage="controller",
        timeout=2.0,
        retries=1,
    )
    assert v.state == "service-mismatch"
    assert v.detail == f"unexpected sysUpTime value {value!r}"

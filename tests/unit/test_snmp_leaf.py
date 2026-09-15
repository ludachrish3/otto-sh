"""``otto.snmp`` — the one pysnmp GET, returning a raw outcome and no opinion.

The leaf exists so ``otto.host`` (the probe survey) and ``otto.monitor`` (the
collector) can share the dispatcher dance without a module cycle. It reports
WHAT HAPPENED — answered / silence / error-indication / error-status — and
leaves coercion and logging to its callers. The pysnmp boundary is faked as a
module, the way tests/unit/monitor/test_snmp.py already does.
"""

import sys
import types

import pytest

from otto.snmp import SnmpGet, snmp_get

OID_UPTIME = "1.3.6.1.2.1.1.3.0"


def _install_fake_pysnmp(
    monkeypatch, *, var_binds, error_indication=None, error_status=0, raise_exc=None
):
    fake = types.ModuleType("pysnmp.hlapi.v1arch.asyncio")
    calls: dict[str, object] = {}

    async def get_cmd(_dispatcher, auth, transport, *var_binds_req, **_opts):
        calls["auth"] = auth
        calls["transport"] = transport
        calls["requested"] = list(var_binds_req)
        if raise_exc is not None:
            raise raise_exc
        return error_indication, error_status, 0, var_binds

    class _Status:
        def prettyPrint(self):  # noqa: N802 — pysnmp's spelling
            return "noSuchName"

    class UdpTransportTarget:
        @staticmethod
        async def create(addr, **kwargs):
            return ("transport", addr, kwargs)

    class _Dispatcher:
        closed = False

        def close(self):
            _Dispatcher.closed = True

    fake.get_cmd = get_cmd
    fake.SnmpDispatcher = lambda *a, **k: _Dispatcher()
    fake.CommunityData = lambda *a, **k: ("community", a, k)
    fake.ObjectType = lambda x: x
    fake.ObjectIdentity = lambda oid: oid
    fake.UdpTransportTarget = UdpTransportTarget
    fake._status = _Status
    fake._dispatcher = _Dispatcher
    monkeypatch.setitem(sys.modules, "pysnmp.hlapi.v1arch.asyncio", fake)
    return fake, calls


# Explicit per-function @pytest.mark.asyncio rather than a module-level
# pytestmark: the module's last test is sync, and strict mode turns a
# pytest.mark.asyncio on a non-coroutine test into a hard warning-as-error.


@pytest.mark.asyncio
async def test_an_answer_is_answered_with_the_raw_value(monkeypatch):
    """Mutation: drop the `.0` match-back and the value lands under the wrong key."""
    _install_fake_pysnmp(monkeypatch, var_binds=[("1.3.6.1.2.1.1.3", 555)])
    got = await snmp_get("10.0.0.1", 161, "public", "2c", [OID_UPTIME])
    assert got == SnmpGet(outcome="answered", values={OID_UPTIME: 555}, detail="")


@pytest.mark.asyncio
async def test_an_exact_match_wins_over_a_tolerant_one(monkeypatch):
    """Mutation: drop the exact-match short-circuit and ...10.10 mis-matches to ...10.1."""
    oid_1 = "1.3.6.1.2.1.2.2.1.10.1"
    oid_10 = "1.3.6.1.2.1.2.2.1.10.10"
    _install_fake_pysnmp(monkeypatch, var_binds=[(oid_1, 111), (oid_10, 1010)])
    got = await snmp_get("10.0.0.1", 161, "public", "2c", [oid_1, oid_10])
    assert got.values == {oid_1: 111, oid_10: 1010}


@pytest.mark.asyncio
async def test_a_transport_exception_is_silence_with_the_text(monkeypatch):
    """Mutation: swallow the exception text and the report cannot say why."""
    _install_fake_pysnmp(monkeypatch, var_binds=[], raise_exc=OSError("no route to host"))
    got = await snmp_get("10.0.0.1", 161, "public", "2c", [OID_UPTIME])
    assert got.outcome == "silence"
    assert got.values == {}
    assert "no route to host" in got.detail


@pytest.mark.asyncio
async def test_an_error_indication_is_its_own_outcome(monkeypatch):
    _install_fake_pysnmp(
        monkeypatch, var_binds=[], error_indication="No SNMP response received before timeout"
    )
    got = await snmp_get("10.0.0.1", 161, "public", "2c", [OID_UPTIME])
    assert got.outcome == "error-indication"
    assert got.detail == "No SNMP response received before timeout"


@pytest.mark.asyncio
async def test_an_error_status_is_pretty_printed(monkeypatch):
    fake, _ = _install_fake_pysnmp(monkeypatch, var_binds=[])
    fake.get_cmd_status = fake._status()

    async def get_cmd(_d, _a, _t, *_v, **_o):
        return None, fake.get_cmd_status, 0, []

    fake.get_cmd = get_cmd
    got = await snmp_get("10.0.0.1", 161, "public", "2c", [OID_UPTIME])
    assert got == SnmpGet(outcome="error-status", values={}, detail="noSuchName")


@pytest.mark.asyncio
async def test_the_dispatcher_is_closed_even_when_the_get_raises(monkeypatch):
    """Mutation: drop the finally and the UDP socket leaks (ResourceWarning under -W error)."""
    fake, _ = _install_fake_pysnmp(monkeypatch, var_binds=[], raise_exc=RuntimeError("boom"))
    fake._dispatcher.closed = False
    await snmp_get("10.0.0.1", 161, "public", "2c", [OID_UPTIME])
    assert fake._dispatcher.closed is True


@pytest.mark.asyncio
async def test_version_timeout_and_retries_reach_pysnmp(monkeypatch):
    """Mutation: hard-code mpModel=1 and v1 agents stop answering."""
    _, calls = _install_fake_pysnmp(monkeypatch, var_binds=[(OID_UPTIME, 1)])
    await snmp_get("10.0.0.1", 1161, "secret", "1", [OID_UPTIME], timeout=0.7, retries=0)
    assert calls["auth"] == ("community", ("secret",), {"mpModel": 0})
    assert calls["transport"] == ("transport", ("10.0.0.1", 1161), {"timeout": 0.7, "retries": 0})


@pytest.mark.asyncio
async def test_no_oids_asks_nothing():
    got = await snmp_get("10.0.0.1", 161, "public", "2c", [])
    assert got == SnmpGet(outcome="answered", values={}, detail="")


def test_the_leaf_imports_nothing_from_pysnmp_at_module_scope():
    """The import-budget guard and the monitor's own docstring both rely on this."""
    import subprocess
    import sys

    code = (
        "import sys, otto.snmp; "
        "assert not any(m.startswith('pysnmp') for m in sys.modules), "
        "sorted(m for m in sys.modules if m.startswith('pysnmp'))"
    )
    subprocess.run([sys.executable, "-c", code], check=True)

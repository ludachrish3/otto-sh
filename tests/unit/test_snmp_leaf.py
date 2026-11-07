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
import pytest_asyncio

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


# --- #742: cancelling a real pysnmp GET -------------------------------------


class _SilentAgent:
    """A 127.0.0.1 UDP endpoint that receives datagrams and never answers."""

    def __init__(self) -> None:
        import asyncio

        self.received = asyncio.Event()

    def connection_made(self, transport) -> None:
        pass

    def datagram_received(self, data, addr) -> None:
        self.received.set()

    def error_received(self, exc) -> None:
        pass

    def connection_lost(self, exc) -> None:
        pass


@pytest_asyncio.fixture
async def silent_agent():
    """``(port, agent)`` for a silent agent; ``agent.received`` is set once a request lands."""
    import asyncio

    agent = _SilentAgent()
    transport, _ = await asyncio.get_running_loop().create_datagram_endpoint(
        lambda: agent, local_addr=("127.0.0.1", 0)
    )
    try:
        yield transport.get_extra_info("sockname")[1], agent
    finally:
        transport.close()


@pytest.mark.asyncio
async def test_cancelling_a_pending_real_get_propagates_the_cancel(silent_agent, monkeypatch):
    """pysnmp 7.1.29-7.1.30 ``close()`` calls a pending cbFun with 4 args.

    The ``TypeError`` that raises would replace the ``CancelledError``. The
    cancel lands only once the agent has the request, so the request is
    pending in the dispatcher when ``close()`` runs.
    """
    import asyncio

    from pysnmp.hlapi.v1arch.asyncio import SnmpDispatcher

    closed: list[dict] = []
    real_close = SnmpDispatcher.close

    def spy_close(self) -> None:
        real_close(self)
        closed.append(dict(self._pendingReqs))

    monkeypatch.setattr(SnmpDispatcher, "close", spy_close)
    port, agent = silent_agent
    task = asyncio.ensure_future(
        snmp_get("127.0.0.1", port, "public", "2c", [OID_UPTIME], timeout=2.0)
    )
    await asyncio.wait_for(agent.received.wait(), 2.0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed == [{}], "the dispatcher was not closed, or kept a pending request"


@pytest.mark.asyncio
async def test_the_survey_reports_a_timeout_row_for_a_cancelled_real_get(silent_agent, monkeypatch):
    """``_bounded`` catches only the timeout; no ``TypeError`` may replace it.

    The survey's ``wait_for`` must expire while the GET is pending, so the
    timeout here fires only once the request is on the wire. A wall-clock
    deadline cannot promise that. The window also holds the lazy pysnmp
    import, which is synchronous: cold in a fresh venv, it outlasted a 0.5 s
    deadline, and the cancel landed before anything was sent (#960).

    So pysnmp is imported here first, outside any deadline. The patched
    ``wait_for`` intercepts only the survey's own call, matched by identity
    with the coroutine its ``check_snmp`` returned, and that check runs with
    pysnmp's own timeout off, so nothing but the survey can end the GET. It
    waits for the agent to receive the request, then lets the real
    ``wait_for`` time out at once.
    That is equivalent to a spent budget: the pending GET is cancelled, the
    timeout is caught, and a ``TypeError`` from pysnmp's ``close()`` would
    still surface. The 30 s bound on the wait is reached only when the
    request is never sent.
    """
    import asyncio

    import pysnmp.hlapi.v1arch.asyncio  # noqa: F401 — warm, outside every deadline

    from otto.host.survey import engine

    port, agent = silent_agent
    checks: list[object] = []
    expired: list[object] = []
    real_check = engine.check_snmp
    real_wait_for = asyncio.wait_for

    def _recording_check(*args, **kwargs):
        # pysnmp's own expiry is off, so the GET is still pending when the
        # survey's timeout fires, however late that is.
        checks.append(real_check(*args, **{**kwargs, "timeout": float("inf")}))
        return checks[-1]

    async def _expire_once_sent(awaitable, timeout):
        if not checks or awaitable is not checks[-1]:
            return await real_wait_for(awaitable, timeout)
        expired.append(awaitable)
        get = asyncio.ensure_future(awaitable)
        try:
            await real_wait_for(agent.received.wait(), 30.0)
        except BaseException:
            get.cancel()
            await asyncio.gather(get, return_exceptions=True)
            raise
        return await real_wait_for(get, 0)

    monkeypatch.setattr(engine, "check_snmp", _recording_check)
    monkeypatch.setattr(asyncio, "wait_for", _expire_once_sent)
    verdict = await engine._snmp_row(
        port, engine._Deadline(30.0), address="127.0.0.1", community="public", version="2c"
    )
    assert len(checks) == 1, "the survey did not run its check through check_snmp"
    assert expired == checks, "the survey's wait_for on its check was not the one intercepted"
    assert agent.received.is_set(), "the request never reached the agent"
    assert (verdict.state, verdict.detail) == ("timeout", "survey budget exhausted"), (
        "the row did not come from the survey's own timeout"
    )


@pytest.mark.asyncio
async def test_pysnmps_dispatcher_keeps_its_pending_requests_in_a_dict():
    """Pins the private ``_pendingReqs`` coupling in ``snmp_get``'s ``finally``."""
    from pysnmp.hlapi.v1arch.asyncio import SnmpDispatcher

    dispatcher = SnmpDispatcher()
    try:
        assert isinstance(dispatcher._pendingReqs, dict)
    finally:
        dispatcher.close()

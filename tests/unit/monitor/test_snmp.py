"""Unit tests for the otto SNMP manager core (acquisition + presentation)."""

import sys
import types
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from otto.monitor import snmp
from otto.monitor.rates import RateTracker
from otto.monitor.snmp import (
    OID_SYS_UPTIME,
    SnmpClient,
    SnmpMetric,
    get_snmp_metric,
    process_snmp_values,
    register_snmp_metric,
    resolve_snmp_metric,
)

T0 = datetime(2026, 7, 3, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def clean_registry():
    """Unregister any test-added descriptor after the test.

    Tests using this fixture only ever register a *new* oid (never overwrite
    a built-in), so diff-based cleanup — mirroring the term/transfer registry
    fixtures — is sufficient.
    """
    before = set(snmp.SNMP_METRICS.names())
    try:
        yield
    finally:
        for oid in set(snmp.SNMP_METRICS.names()) - before:
            snmp.SNMP_METRICS.unregister(oid)


# ---------------------------------------------------------------------------
# SnmpMetric — presentation + scaling
# ---------------------------------------------------------------------------


class TestSnmpMetric:
    def test_scale_applied(self):
        m = SnmpMetric(oid="1.2.3", label="CPU", chart="CPU", unit="%", scale=0.01)
        assert m.to_point(4250).value == 42.5

    def test_unit_scale_is_identity(self):
        m = SnmpMetric(oid="1.2.3", label="Heap", chart="Memory", unit="B")
        assert m.to_point(8192).value == 8192.0

    def test_rounds_to_two_places(self):
        m = SnmpMetric(oid="1.2.3", label="X", chart="X", scale=1 / 3)
        assert m.to_point(1).value == 0.33


# ---------------------------------------------------------------------------
# Descriptor registry
# ---------------------------------------------------------------------------


class TestRegistry:
    def test_builtin_sysuptime_present(self):
        m = get_snmp_metric(OID_SYS_UPTIME)
        assert m is not None
        assert m.label == "Uptime"
        assert m.scale == 0.01  # TimeTicks → seconds

    def test_unknown_oid_returns_none(self):
        assert get_snmp_metric("1.3.6.1.4.1.99999.42") is None

    def test_resolve_falls_back_to_default_styling(self):
        m = resolve_snmp_metric("1.3.6.1.4.1.99999.42")
        assert m.label == "1.3.6.1.4.1.99999.42"
        assert m.chart == "1.3.6.1.4.1.99999.42"
        assert m.tab == "metrics"
        assert m.scale == 1.0

    def test_register_overrides(self, clean_registry):
        register_snmp_metric(SnmpMetric(oid="9.9.9", label="Custom", chart="Widgets", unit="w"))
        m = resolve_snmp_metric("9.9.9")
        assert m.label == "Custom"
        assert m.chart == "Widgets"
        assert m.unit == "w"

    def test_register_restored_after_fixture(self):
        # The override from the previous test must not leak.
        assert get_snmp_metric("9.9.9") is None

    def test_builtins_registered_through_public_path(self):
        # Every built-in descriptor must be retrievable via the same getter a
        # third-party registration would populate — i.e. _register_builtin_metrics()
        # used register_snmp_metric(), not a private dict literal.
        from otto.monitor.snmp import _OTTO_BASE, OID_SYS_UPTIME

        for oid in (
            OID_SYS_UPTIME,
            f"{_OTTO_BASE}.1.1.0",
            f"{_OTTO_BASE}.1.2.0",
            f"{_OTTO_BASE}.1.3.0",
            f"{_OTTO_BASE}.1.4.0",
        ):
            assert get_snmp_metric(oid) is not None, f"built-in {oid} not registered"

    def test_snmp_metric_is_frozen(self):
        m = get_snmp_metric(OID_SYS_UPTIME)
        assert m is not None
        with pytest.raises(ValidationError):
            m.scale = 2.0  # frozen → mutation rejected


# ---------------------------------------------------------------------------
# process_snmp_values — OID → (label, point, descriptor)
# ---------------------------------------------------------------------------


class TestProcessSnmpValues:
    def test_gauge_known_oid_is_scaled_and_labelled(self):
        triples = process_snmp_values({OID_SYS_UPTIME: 12345}, rates=RateTracker(), ts=T0)
        assert len(triples) == 1
        label, point, metric = triples[0]
        assert label == "Uptime"
        assert point.value == 123.45  # sysUpTime is 1/100 s
        assert metric.oid == OID_SYS_UPTIME

    def test_none_values_skipped(self):
        triples = process_snmp_values(
            {OID_SYS_UPTIME: 12345, "1.2.3.4": None}, rates=RateTracker(), ts=T0
        )
        assert [t[0] for t in triples] == ["Uptime"]

    def test_unknown_oid_gets_fallback_descriptor(self):
        triples = process_snmp_values({"1.2.3.4": 7}, rates=RateTracker(), ts=T0)
        label, point, _metric = triples[0]
        assert label == "1.2.3.4"
        assert point.value == 7.0

    def test_counter_first_tick_baselines_and_emits_nothing(self):
        register_snmp_metric(
            SnmpMetric(oid="1.2.3.9.1", label="rx test", chart="Net", kind="counter", unit="B/s")
        )
        rates = RateTracker()
        assert process_snmp_values({"1.2.3.9.1": 1000}, rates=rates, ts=T0) == []
        triples = process_snmp_values(
            {"1.2.3.9.1": 6000}, rates=rates, ts=T0 + timedelta(seconds=5)
        )
        assert triples[0][1].value == 1000.0  # (6000-1000)/5

    def test_counter_reset_skips_tick(self):
        register_snmp_metric(SnmpMetric(oid="1.2.3.9.2", label="rx r", chart="Net", kind="counter"))
        rates = RateTracker()
        process_snmp_values({"1.2.3.9.2": 9000}, rates=rates, ts=T0)
        assert (
            process_snmp_values({"1.2.3.9.2": 10}, rates=rates, ts=T0 + timedelta(seconds=5)) == []
        )

    def test_meta_of_attaches_to_target_series_not_own(self):
        register_snmp_metric(
            SnmpMetric(oid="1.2.3.9.3", label="fs0 used", chart="Filesystem", unit="B")
        )
        register_snmp_metric(
            SnmpMetric(
                oid="1.2.3.9.4", label="Total", chart="Filesystem", unit="B", meta_of="1.2.3.9.3"
            )
        )
        triples = process_snmp_values(
            {"1.2.3.9.3": 1048576, "1.2.3.9.4": 2097152}, rates=RateTracker(), ts=T0
        )
        assert [t[0] for t in triples] == ["fs0 used"]  # meta_of OID charts no series
        assert triples[0][1].meta == {"Total": "2 M"}  # human-readable for unit "B"

    def test_meta_of_target_absent_drops_meta_never_errors(self):
        register_snmp_metric(
            SnmpMetric(oid="1.2.3.9.5", label="Orphan", chart="X", meta_of="1.2.3.9.99")
        )
        assert process_snmp_values({"1.2.3.9.5": 5}, rates=RateTracker(), ts=T0) == []

    def test_gauge_default_kind_unchanged(self):
        assert SnmpMetric(oid="1.2.3", label="x", chart="x").kind == "gauge"
        assert SnmpMetric(oid="1.2.3", label="x", chart="x").meta_of is None


# ---------------------------------------------------------------------------
# SnmpClient.get — pysnmp boundary mocked via a fake module
# ---------------------------------------------------------------------------


def _install_fake_pysnmp(monkeypatch, *, var_binds, error_indication=None, error_status=0):
    """Inject a fake ``pysnmp.hlapi.v1arch.asyncio`` so get() needs no network.

    ``var_binds`` is a list of ``(oid_str, value)`` pairs the fake ``get_cmd``
    returns; the client coerces and maps them back to requested OIDs.
    """
    fake = types.ModuleType("pysnmp.hlapi.v1arch.asyncio")

    async def get_cmd(_dispatcher, _auth, _transport, *_var_binds, **_opts):
        return error_indication, error_status, 0, var_binds

    class UdpTransportTarget:
        @staticmethod
        async def create(addr, **kwargs):
            return ("transport", addr, kwargs)

    class _Dispatcher:
        def close(self):
            pass

    fake.get_cmd = get_cmd
    fake.SnmpDispatcher = lambda *a, **k: _Dispatcher()
    fake.CommunityData = lambda *a, **k: ("community", a, k)
    fake.ObjectType = lambda x: x
    fake.ObjectIdentity = lambda oid: oid
    fake.UdpTransportTarget = UdpTransportTarget
    monkeypatch.setitem(sys.modules, "pysnmp.hlapi.v1arch.asyncio", fake)


class TestSnmpClient:
    pytestmark = pytest.mark.asyncio

    async def test_empty_oids_short_circuits(self):
        # No oids → no import, no network.
        assert await SnmpClient("10.0.0.1").get([]) == {}

    async def test_successful_get(self, monkeypatch):
        _install_fake_pysnmp(monkeypatch, var_binds=[(OID_SYS_UPTIME, 555)])
        res = await SnmpClient("10.0.0.1").get([OID_SYS_UPTIME])
        assert res == {OID_SYS_UPTIME: 555.0}

    async def test_trailing_zero_oid_matched_back(self, monkeypatch):
        # Requested without instance suffix; agent answers with .0
        _install_fake_pysnmp(monkeypatch, var_binds=[("1.3.6.1.2.1.1.3.0", 42)])
        res = await SnmpClient("10.0.0.1").get(["1.3.6.1.2.1.1.3"])
        assert res == {"1.3.6.1.2.1.1.3": 42.0}

    async def test_non_numeric_value_is_none(self, monkeypatch):
        _install_fake_pysnmp(monkeypatch, var_binds=[(OID_SYS_UPTIME, "a string")])
        res = await SnmpClient("10.0.0.1").get([OID_SYS_UPTIME])
        assert res == {OID_SYS_UPTIME: None}

    async def test_error_indication_returns_all_none(self, monkeypatch):
        _install_fake_pysnmp(monkeypatch, var_binds=[], error_indication="timeout")
        res = await SnmpClient("10.0.0.1").get([OID_SYS_UPTIME])
        assert res == {OID_SYS_UPTIME: None}

    async def test_v1_uses_mpmodel_zero(self, monkeypatch):
        captured = {}
        _install_fake_pysnmp(monkeypatch, var_binds=[(OID_SYS_UPTIME, 1)])
        fake = sys.modules["pysnmp.hlapi.v1arch.asyncio"]
        orig = fake.CommunityData
        fake.CommunityData = lambda *a, **k: captured.update(k) or orig(*a, **k)
        await SnmpClient("10.0.0.1", version="1").get([OID_SYS_UPTIME])
        assert captured.get("mpModel") == 0

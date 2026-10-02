"""Tests for build_monitor_collector — choosing SNMP vs shell collection mode."""

from otto.host.element import Element
from otto.host.factory import create_host_from_dict
from otto.host.login_proxy import Cred
from otto.host.unix_host import UnixHost
from otto.logger.mode import LogMode
from otto.monitor.factory import build_monitor_collector, is_monitorable, monitorable
from otto.monitor.parsers import LoadParser


class TestBuildMonitorCollector:
    def test_snmp_host_becomes_snmp_target(self):
        host = create_host_from_dict(
            {
                "ip": "192.0.2.1",
                "os_type": "embedded",
                "command_frame": "zephyr",
                "snmp": {"port": 16101, "oids": ["1.3.6.1.2.1.1.3.0"]},
            },
            element=Element("zephyr37_fat"),
        )
        collector = build_monitor_collector([host])
        target = collector._targets[0]

        assert target.snmp is not None
        # address omitted in lab data -> defaults to the host's own ip
        assert target.snmp.client.address == "192.0.2.1"
        assert target.snmp.client.port == 16101
        assert target.snmp.oids == ["1.3.6.1.2.1.1.3.0"]

    def test_snmp_address_override_is_used(self):
        host = create_host_from_dict(
            {
                "ip": "192.0.2.1",
                "os_type": "embedded",
                "command_frame": "zephyr",
                "snmp": {"address": "10.10.200.14", "port": 16101, "oids": ["1.3.6.1.2.1.1.3.0"]},
            },
            element=Element("zephyr37_fat"),
        )
        collector = build_monitor_collector([host])
        # the relay endpoint, not the host's telnet ip
        assert collector._targets[0].snmp.client.address == "10.10.200.14"

    def test_snmp_address_resolves_named_interface(self):
        # snmp.address names a secondary interface -> resolved via address_for
        host = create_host_from_dict(
            {
                "ip": "192.0.2.1",
                "os_type": "embedded",
                "command_frame": "zephyr",
                "interfaces": {"mgmt": "10.9.9.9", "data": "192.168.5.5"},
                "snmp": {"address": "mgmt", "port": 16101, "oids": ["1.3.6.1.2.1.1.3.0"]},
            },
            element=Element("zephyr37_fat"),
        )
        collector = build_monitor_collector([host])
        assert collector._targets[0].snmp.client.address == "10.9.9.9"

    def test_snmp_address_literal_passes_through(self):
        # snmp.address is a literal IP (not an interface name) -> unchanged
        host = create_host_from_dict(
            {
                "ip": "192.0.2.1",
                "os_type": "embedded",
                "command_frame": "zephyr",
                "interfaces": {"mgmt": "10.9.9.9"},
                "snmp": {"address": "203.0.113.5", "port": 16101, "oids": ["1.3.6.1.2.1.1.3.0"]},
            },
            element=Element("zephyr37_fat"),
        )
        collector = build_monitor_collector([host])
        assert collector._targets[0].snmp.client.address == "203.0.113.5"

    def test_snmp_address_defaults_to_host_ip(self):
        # snmp.address omitted -> falls back to host.ip (a literal, unchanged)
        host = create_host_from_dict(
            {
                "ip": "192.0.2.1",
                "os_type": "embedded",
                "command_frame": "zephyr",
                "interfaces": {"mgmt": "10.9.9.9"},
                "snmp": {"port": 16101, "oids": ["1.3.6.1.2.1.1.3.0"]},
            },
            element=Element("zephyr37_fat"),
        )
        collector = build_monitor_collector([host])
        assert collector._targets[0].snmp.client.address == "192.0.2.1"

    def test_host_without_snmp_is_shell_target(self):
        host = create_host_from_dict(
            {
                "ip": "10.10.200.11",
                "creds": [{"login": "v", "password": "v"}],
            },
            element=Element("alt1"),
        )
        collector = build_monitor_collector([host])
        target = collector._targets[0]

        assert target.snmp is None
        assert target.parsers  # shell parsers resolved from the registry

    def test_bundle_names_expand_at_target_construction(self):
        from otto.monitor.snmp import CORE_OIDS

        host = create_host_from_dict(
            {
                "ip": "192.0.2.1",
                "os_type": "embedded",
                "command_frame": "zephyr",
                "snmp": {
                    "port": 161,
                    "oids": ("otto-core",),
                    "community": "public",
                    "version": "2c",
                },
            },
            element=Element("zephyr37_fat"),
        )
        collector = build_monitor_collector([host])
        assert collector._targets[0].snmp.oids == list(CORE_OIDS)


def test_factory_passes_tunnel_source_through() -> None:
    from otto.models.monitor import TunnelRecord

    async def source() -> list[TunnelRecord]:
        return []

    collector = build_monitor_collector(hosts=[], tunnel_source=source)
    assert collector._tunnel_source is source


def test_factory_defaults_to_no_tunnel_source() -> None:
    assert build_monitor_collector(hosts=[])._tunnel_source is None


def _unix(name: str = "box") -> UnixHost:
    return UnixHost(
        ip="10.0.0.1",
        element=Element(name),
        creds=[Cred(login="a", password="b")],
        log=LogMode.NORMAL,
    )


def _embedded(name: str, *, snmp: bool) -> "object":
    spec = {"ip": "192.0.2.1", "os_type": "embedded", "command_frame": "zephyr"}
    if snmp:
        spec["snmp"] = {"oids": ["1.3.6.1.2.1.1.3.0"]}
    return create_host_from_dict(spec, element=Element(name))


class TestMonitorable:
    def test_unix_host_is_monitorable(self):
        assert is_monitorable(_unix())

    def test_snmp_only_host_is_monitorable(self):
        assert is_monitorable(_embedded("z1", snmp=True))

    def test_host_offering_neither_is_not(self):
        assert not is_monitorable(_embedded("z2", snmp=False))

    def test_monitorable_filters_and_keeps_order(self):
        a, b, c = _unix("a"), _embedded("b", snmp=False), _embedded("c", snmp=True)
        assert monitorable([a, b, c]) == [a, c]


def test_building_a_collector_leaves_the_hosts_log_mode_alone():
    host = _unix()
    build_monitor_collector([host])
    assert host.log is LogMode.NORMAL


def test_explicit_parsers_replace_the_registered_set_for_shell_hosts():
    collector = build_monitor_collector([_unix()], parsers=[LoadParser()])
    assert list(collector._targets[0].parsers) == [LoadParser().command]


def test_explicit_parsers_do_not_touch_snmp_targets():
    collector = build_monitor_collector([_embedded("z", snmp=True)], parsers=[LoadParser()])
    assert collector._targets[0].parsers == {}


def test_explicit_parsers_are_copied_per_host_because_parsers_keep_state():
    mine = LoadParser()
    collector = build_monitor_collector([_unix("a"), _unix("b")], parsers=[mine])
    first, second = (next(iter(t.parsers.values())) for t in collector._targets)
    assert first is not second
    assert first is not mine
    assert second is not mine

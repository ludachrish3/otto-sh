"""Tests for build_monitor_collector — choosing SNMP vs shell collection mode."""

from otto.host.factory import create_host_from_dict
from otto.monitor.factory import build_monitor_collector


class TestBuildMonitorCollector:
    def test_snmp_host_becomes_snmp_target(self):
        host = create_host_from_dict(
            {
                "ip": "192.0.2.1",
                "element": "sprout",
                "os_type": "embedded",
                "command_frame": "zephyr",
                "snmp": {"port": 16101, "oids": ["1.3.6.1.2.1.1.3.0"]},
            }
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
                "element": "sprout",
                "os_type": "embedded",
                "command_frame": "zephyr",
                "snmp": {"address": "10.10.200.14", "port": 16101, "oids": ["1.3.6.1.2.1.1.3.0"]},
            }
        )
        collector = build_monitor_collector([host])
        # the relay endpoint, not the host's telnet ip
        assert collector._targets[0].snmp.client.address == "10.10.200.14"

    def test_snmp_address_resolves_named_interface(self):
        # snmp.address names a secondary interface -> resolved via address_for
        host = create_host_from_dict(
            {
                "ip": "192.0.2.1",
                "element": "sprout",
                "os_type": "embedded",
                "command_frame": "zephyr",
                "interfaces": {"mgmt": "10.9.9.9", "data": "192.168.5.5"},
                "snmp": {"address": "mgmt", "port": 16101, "oids": ["1.3.6.1.2.1.1.3.0"]},
            }
        )
        collector = build_monitor_collector([host])
        assert collector._targets[0].snmp.client.address == "10.9.9.9"

    def test_snmp_address_literal_passes_through(self):
        # snmp.address is a literal IP (not an interface name) -> unchanged
        host = create_host_from_dict(
            {
                "ip": "192.0.2.1",
                "element": "sprout",
                "os_type": "embedded",
                "command_frame": "zephyr",
                "interfaces": {"mgmt": "10.9.9.9"},
                "snmp": {"address": "203.0.113.5", "port": 16101, "oids": ["1.3.6.1.2.1.1.3.0"]},
            }
        )
        collector = build_monitor_collector([host])
        assert collector._targets[0].snmp.client.address == "203.0.113.5"

    def test_snmp_address_defaults_to_host_ip(self):
        # snmp.address omitted -> falls back to host.ip (a literal, unchanged)
        host = create_host_from_dict(
            {
                "ip": "192.0.2.1",
                "element": "sprout",
                "os_type": "embedded",
                "command_frame": "zephyr",
                "interfaces": {"mgmt": "10.9.9.9"},
                "snmp": {"port": 16101, "oids": ["1.3.6.1.2.1.1.3.0"]},
            }
        )
        collector = build_monitor_collector([host])
        assert collector._targets[0].snmp.client.address == "192.0.2.1"

    def test_host_without_snmp_is_shell_target(self):
        host = create_host_from_dict(
            {
                "ip": "10.10.200.11",
                "element": "orange",
                "creds": [{"login": "v", "password": "v"}],
            }
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
                "element": "sprout",
                "os_type": "embedded",
                "command_frame": "zephyr",
                "snmp": {
                    "port": 161,
                    "oids": ("otto-core",),
                    "community": "public",
                    "version": "2c",
                },
            }
        )
        collector = build_monitor_collector([host])
        assert collector._targets[0].snmp.oids == list(CORE_OIDS)

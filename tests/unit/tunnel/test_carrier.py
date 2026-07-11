"""The TunnelCarrier contract + CARRIERS registry (mirrors the impairer seam)."""

import pytest

from otto.tunnel.carrier import (
    CARRIERS,
    DEFAULT_CARRIER,
    TunnelCarrier,
    build_carrier,
    register_carrier,
)
from otto.tunnel.socat import (
    SocatCarrier,
    egress_socat_args,
    ingress_socat_args,
    relay_socat_args,
)


class TestRegistry:
    def test_socat_is_registered_first_party(self):
        assert build_carrier("socat") is SocatCarrier

    def test_unknown_name_is_a_rich_error(self):
        with pytest.raises(ValueError, match="Unknown carrier 'wireguard'"):
            build_carrier("wireguard")
        with pytest.raises(ValueError, match="register_carrier"):
            build_carrier("wireguard")

    def test_register_rejects_empty_supported_protocols(self):
        class NoProtocols(TunnelCarrier):
            requirements_command = "true"
            tools_description = "nothing"

        with pytest.raises(ValueError, match="supported_protocols is empty"):
            register_carrier("broken", NoProtocols)

    def test_custom_carrier_registers_and_resolves(self):
        class FakeCarrier(TunnelCarrier):
            supported_protocols = frozenset({"tcp"})
            requirements_command = "command -v fake >/dev/null 2>&1 && echo ok || echo no"
            tools_description = "fake"

            def ingress_args(self, protocol, service_port, bind_ip, next_ip, carrier_port):
                return ["fake", "ingress"]

            def relay_args(self, carrier_port, next_ip):
                return ["fake", "relay"]

            def egress_args(self, protocol, service_port, deliver_ip, carrier_port):
                return ["fake", "egress"]

        register_carrier("fake", FakeCarrier)
        try:
            assert build_carrier("fake") is FakeCarrier
            assert "fake" in CARRIERS
        finally:
            CARRIERS.unregister("fake")


class TestSocatCarrier:
    def test_delegates_to_the_proven_builders(self):
        c = SocatCarrier()
        assert c.ingress_args("tcp", 8080, "10.0.0.1", "10.0.0.2", 50000) == ingress_socat_args(
            "tcp", 8080, "10.0.0.1", "10.0.0.2", 50000
        )
        assert c.relay_args(50000, "10.0.0.3") == relay_socat_args(50000, "10.0.0.3")
        assert c.egress_args("udp", 53, "127.0.0.1", 50001) == egress_socat_args(
            "udp", 53, "127.0.0.1", 50001
        )

    def test_ingress_argv_golden(self):
        # Pin the exact argv shipped by #2b — the carrier must not change it.
        assert SocatCarrier().ingress_args("tcp", 8080, "10.0.0.1", "10.0.0.2", 50000) == [
            "socat",
            "TCP4-LISTEN:8080,bind=10.0.0.1,fork,reuseaddr",
            "TCP4:10.0.0.2:50000",
        ]

    def test_protocols_and_requirements(self):
        assert SocatCarrier.supported_protocols == frozenset({"tcp", "udp"})
        # STABILITY GOLDEN: this exact probe line runs on every chain host at
        # add time — byte-identical to the retired inline probe in
        # manage._require_tools. Never change these bytes casually.
        assert SocatCarrier.requirements_command == (
            "command -v socat >/dev/null 2>&1 && command -v bash >/dev/null 2>&1 "
            "&& echo ok || echo no"
        )
        assert SocatCarrier.tools_description == "socat and/or bash"

    def test_default_carrier_resolves_to_socat(self):
        # The one source of truth the CLI option and add_tunnel default share.
        assert DEFAULT_CARRIER == "socat"
        assert build_carrier(DEFAULT_CARRIER) is SocatCarrier

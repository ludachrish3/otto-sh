"""Boundary validation for lab.json ``links`` entries."""

import pytest
from pydantic import ValidationError

from otto.models.link import LinkSpec


def _entry(**overrides) -> dict:
    base = {
        "endpoints": [
            {"host": "carrot_seed", "interface": "eth1"},
            {"host": "tomato_seed", "interface": "eth1"},
        ],
        "protocol": "udp",
    }
    return {**base, **overrides}


class TestLinkSpec:
    def test_full_entry_parses(self):
        spec = LinkSpec.model_validate(_entry(name="data-plane-a"))
        assert spec.endpoints[0].host == "carrot_seed"
        assert spec.protocol == "udp"
        assert spec.name == "data-plane-a"

    def test_protocol_defaults_to_tcp(self):
        entry = _entry()
        del entry["protocol"]
        assert LinkSpec.model_validate(entry).protocol == "tcp"

    def test_protocol_lowercased(self):
        assert LinkSpec.model_validate(_entry(protocol="UDP")).protocol == "udp"

    def test_interface_optional(self):
        entry = _entry(endpoints=[{"host": "a"}, {"host": "b"}])
        spec = LinkSpec.model_validate(entry)
        assert spec.endpoints[0].interface is None

    @pytest.mark.parametrize("count", [1, 3])
    def test_exactly_two_endpoints(self, count):
        entry = _entry(endpoints=[{"host": f"h{i}"} for i in range(count)])
        with pytest.raises(ValidationError):
            LinkSpec.model_validate(entry)

    def test_self_link_rejected(self):
        entry = _entry(endpoints=[{"host": "a", "interface": "eth0"}] * 2)
        with pytest.raises(ValidationError, match="must differ"):
            LinkSpec.model_validate(entry)

    def test_same_host_different_interface_allowed(self):
        entry = _entry(
            endpoints=[
                {"host": "a", "interface": "eth0"},
                {"host": "a", "interface": "eth1"},
            ]
        )
        LinkSpec.model_validate(entry)  # loopback cabling: legal

    def test_unknown_key_rejected(self):
        with pytest.raises(ValidationError):
            LinkSpec.model_validate(_entry(bandwidth="10G"))

    def test_underscore_comment_keys_stripped(self):
        LinkSpec.model_validate(_entry(_comment="a note"))

    def test_reserved_fields_accepted(self):
        spec = LinkSpec.model_validate(_entry(impair="netem", management="mgmt-01"))
        assert (spec.impair, spec.management) == ("netem", "mgmt-01")

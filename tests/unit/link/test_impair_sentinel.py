"""otto-impair sentinel codec + ps-scan parsing (mirrors tunnel sentinel style)."""

from otto.link.params import Selector
from otto.link.sentinel import (
    IMPAIR_PS_COMMAND,
    ImpairTimer,
    encode_impair_sentinel,
    encode_impair_sentinel_v2,
    parse_impair_ps,
    parse_impair_sentinel,
)


class TestCodec:
    def test_v1_roundtrip(self) -> None:
        token = encode_impair_sentinel("lnk-abc123", "eth1.100")
        assert token == "otto-impair:v1:lnk-abc123:eth1.100"
        assert parse_impair_sentinel(token) == ("lnk-abc123", "eth1.100", None)

    def test_v2_roundtrip_with_proto(self) -> None:
        token = encode_impair_sentinel_v2("lnk-abc123", "eth1.100", Selector(5201, "tcp"))
        assert token == "otto-impair:v2:lnk-abc123:eth1.100:5201:tcp"
        assert parse_impair_sentinel(token) == ("lnk-abc123", "eth1.100", Selector(5201, "tcp"))

    def test_v2_roundtrip_proto_none_empty_segment(self) -> None:
        token = encode_impair_sentinel_v2("lnk-abc123", "eth1.100", Selector(53))
        assert token == "otto-impair:v2:lnk-abc123:eth1.100:53:"
        assert parse_impair_sentinel(token) == ("lnk-abc123", "eth1.100", Selector(53))

    def test_percent_encoding_of_separator(self) -> None:
        token = encode_impair_sentinel("name:with:colons", "eth1")
        assert parse_impair_sentinel(token) == ("name:with:colons", "eth1", None)
        token2 = encode_impair_sentinel_v2("name:with:colons", "eth1", Selector(80))
        assert parse_impair_sentinel(token2) == ("name:with:colons", "eth1", Selector(80))

    def test_reject_foreign_and_malformed(self) -> None:
        assert parse_impair_sentinel("otto-tunnel:v1:x:y") is None
        assert parse_impair_sentinel("otto-impair:v3:a:b:1:tcp") is None
        assert parse_impair_sentinel("otto-impair:v1:onlyone") is None
        assert parse_impair_sentinel("otto-impair:v2:a:b:notaport:tcp") is None
        assert parse_impair_sentinel("otto-impair:v2:a:b:80:icmp") is None
        assert parse_impair_sentinel("otto-impair:v2:a:b:0:tcp") is None


class TestPsScan:
    def test_ps_command_uses_separate_eo_flags(self) -> None:
        # procps-ng 3.3.10 mis-parses the comma-joined form (#2b lesson)
        assert "-eo pid= -eo etime= -eo args=" in IMPAIR_PS_COMMAND
        assert "grep -a ' otto-impair:'" in IMPAIR_PS_COMMAND

    def test_parse_ps_extracts_both_versions(self) -> None:
        v1 = encode_impair_sentinel("lnk-abc123", "eth1.100")
        v2 = encode_impair_sentinel_v2("lnk-abc123", "eth1.100", Selector(5201, "tcp"))
        text = "\n".join(
            [
                f"  4242 05:00 {v1} -c sleep 30 && tc qdisc del dev eth1.100 root",
                f"  4243 06:00 {v2} -c sleep 30 && tc filter del ...",
                "  4244 05:00 otto-impair:v1:mangled",
                "  10 01:00 socat TCP4-LISTEN:5000 STDIO",
                "garbage",
            ]
        )
        assert parse_impair_ps(text) == [
            ImpairTimer(4242, "lnk-abc123", "eth1.100", None),
            ImpairTimer(4243, "lnk-abc123", "eth1.100", Selector(5201, "tcp")),
        ]


class TestWireGolden:
    def test_ps_command_golden(self):
        # `\grep` bypasses the interactive-shell color alias that blinds the
        # scan on telnet-term hosts — see TestPsScanCommand in
        # tests/unit/host/test_daemon.py for the full story.
        assert IMPAIR_PS_COMMAND == (
            "ps -eo pid= -eo etime= -eo args= 2>/dev/null | \\grep -a ' otto-impair:' || true"
        )

    def test_encode_produces_the_exact_v1_bytes(self):
        assert encode_impair_sentinel("lnk-1", "eth0.100") == "otto-impair:v1:lnk-1:eth0.100"
        assert encode_impair_sentinel("a:b", "e/th") == "otto-impair:v1:a%3Ab:e%2Fth"

    def test_encode_produces_the_exact_v2_bytes(self):
        assert (
            encode_impair_sentinel_v2("a:b", "e/th", Selector(5201, "udp"))
            == "otto-impair:v2:a%3Ab:e%2Fth:5201:udp"
        )

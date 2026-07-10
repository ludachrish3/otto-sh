"""otto-impair sentinel codec + ps-scan parsing (mirrors tunnel sentinel style)."""

from otto.link.sentinel import (
    IMPAIR_PS_COMMAND,
    encode_impair_sentinel,
    parse_impair_ps,
    parse_impair_sentinel,
)


class TestCodec:
    def test_roundtrip(self) -> None:
        token = encode_impair_sentinel("lnk-abc123", "eth1.100")
        assert token == "otto-impair:v1:lnk-abc123:eth1.100"
        assert parse_impair_sentinel(token) == ("lnk-abc123", "eth1.100")

    def test_percent_encoding_of_separator(self) -> None:
        token = encode_impair_sentinel("name:with:colons", "eth1")
        assert parse_impair_sentinel(token) == ("name:with:colons", "eth1")

    def test_reject_foreign_and_malformed(self) -> None:
        assert parse_impair_sentinel("otto-tunnel:v1:x:y") is None
        assert parse_impair_sentinel("otto-impair:v2:x:y") is None
        assert parse_impair_sentinel("otto-impair:v1:onlyone") is None


class TestPsScan:
    def test_ps_command_uses_separate_eo_flags(self) -> None:
        # procps-ng 3.3.10 mis-parses the comma-joined form (#2b lesson)
        assert "-eo pid= -eo etime= -eo args=" in IMPAIR_PS_COMMAND
        assert "grep -a ' otto-impair:'" in IMPAIR_PS_COMMAND

    def test_parse_ps_extracts_timer_pids(self) -> None:
        token = encode_impair_sentinel("lnk-abc123", "eth1.100")
        text = "\n".join(
            [
                f"  4242 05:00 {token} -c sleep 30 && tc qdisc del dev eth1.100 root",
                "  4243 05:00 otto-impair:v1:mangled",
                "  10 01:00 socat TCP4-LISTEN:5000 STDIO",
                "garbage",
            ]
        )
        assert parse_impair_ps(text) == [(4242, "lnk-abc123", "eth1.100")]

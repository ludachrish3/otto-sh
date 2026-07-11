"""otto.host.daemon: the sentinel-tagged daemon toolkit shared by tunnels and
link-impairment timers — launch, ps scan/parse, etime parsing, kill, and the
argv-tag sentinel framing codec."""

import pytest

from otto.host.daemon import (
    DaemonProcess,
    dec,
    enc,
    encode_token,
    kill_command,
    launch_command,
    parse_etime,
    parse_ps_output,
    ps_scan_command,
    split_token,
)


class TestLaunchCommand:
    def test_survival_template_shape(self) -> None:
        cmd = launch_command(
            "otto-impair:v1:lnk:eth1", ["bash", "-c", "sleep 5 && tc qdisc del dev eth1 root"]
        )
        # Whole if/then/else/fi conditional wrapped in an outer `bash -c` so the
        # returned string is one opaque word — safe for a caller to splice into
        # a larger command by naive textual prefixing (e.g. sudo). The real
        # systemd-run invocation is folded INTO the if condition (falls through
        # to setsid when systemd-run is present but unusable — no dbus session)
        # and bounded by `timeout 5` so a hang-shaped failure also folds through.
        assert cmd.startswith("bash -c ")
        assert "if command -v systemd-run >/dev/null 2>&1 && timeout 5 systemd-run --user" in cmd
        assert "setsid bash -c" in cmd
        assert "otto-impair:v1:lnk:eth1" in cmd


class TestPsScanCommand:
    def test_tunnel_prefix_is_byte_identical_to_the_retired_literal(self):
        # STABILITY CONTRACT: this exact command string is what shipped in
        # otto.tunnel.socat.DISCOVERY_PS_COMMAND. Never change these bytes.
        assert ps_scan_command("otto-tunnel") == (
            "ps -eo pid= -eo etime= -eo args= 2>/dev/null | grep -a ' otto-tunnel:' || true"
        )

    @pytest.mark.parametrize("bad", ["o'tto", "otto tunnel", "otto.tunnel", "otto[x]", "", "-x"])
    def test_rejects_prefixes_that_break_shell_quoting_or_grep_regex(self, bad: str) -> None:
        # The prefix is spliced into a single-quoted grep BRE: a quote breaks
        # the shell line, a regex metachar changes match semantics, and the
        # trailing `|| true` would mask the resulting grep failure as an empty
        # (falsely clean) scan. Fail loud at build time instead.
        with pytest.raises(ValueError, match="prefix"):
            ps_scan_command(bad)

    def test_impair_prefix_is_byte_identical_to_the_retired_literal(self):
        assert ps_scan_command("otto-impair") == (
            "ps -eo pid= -eo etime= -eo args= 2>/dev/null | grep -a ' otto-impair:' || true"
        )


class TestParseEtime:
    # Moved from tests/unit/tunnel/test_discovery.py (the function moved).
    def test_bare_seconds(self):
        assert parse_etime("42") == 42

    def test_mm_ss(self):
        assert parse_etime("02:03") == 123

    def test_hh_mm_ss(self):
        assert parse_etime("01:02:03") == 3723

    def test_days(self):
        assert parse_etime("2-01:02:03") == 2 * 86400 + 3723

    def test_garbage_is_zero_not_an_error(self):
        assert parse_etime("garbage") == 0

    def test_empty_string_is_zero(self):
        assert parse_etime("") == 0


class TestParsePsOutput:
    PREFIX = "otto-test"

    def test_extracts_pid_age_and_token(self):
        out = parse_ps_output(f"  123 01:00 bash {self.PREFIX}:v1:a:b extra", self.PREFIX)
        assert out == [DaemonProcess(pid=123, age_seconds=60, token=f"{self.PREFIX}:v1:a:b")]

    def test_skips_short_lines(self):
        assert parse_ps_output("123 01:00", self.PREFIX) == []

    def test_skips_non_numeric_pid(self):
        assert parse_ps_output(f"abc 01:00 {self.PREFIX}:v1:a", self.PREFIX) == []

    def test_skips_lines_without_our_token(self):
        assert parse_ps_output("123 01:00 socat TCP4-LISTEN:9 TCP4:h:9", self.PREFIX) == []

    def test_foreign_prefix_not_matched(self):
        assert parse_ps_output("123 01:00 other-tool:v1:a", self.PREFIX) == []

    def test_token_must_start_a_word(self):
        # The token is found by str.startswith on whitespace-split words.
        assert parse_ps_output(f"123 01:00 x{self.PREFIX}:v1:a", self.PREFIX) == []

    def test_multiple_lines(self):
        text = f"1 00:01 {self.PREFIX}:v1:a\n\n2 00:02 {self.PREFIX}:v1:b\n"
        assert [p.pid for p in parse_ps_output(text, self.PREFIX)] == [1, 2]


class TestKillCommand:
    def test_sorts_pids(self):
        assert kill_command([30, 10, 20]) == "kill 10 20 30"

    def test_empty_pids_is_a_loud_error(self):
        # A bare "kill " is a usage error on the host; fail at build time.
        with pytest.raises(ValueError, match="at least one pid"):
            kill_command([])

    def test_single_pid(self):
        assert kill_command([7]) == "kill 7"


class TestSentinelFraming:
    def test_enc_percent_encodes_everything(self):
        assert enc("a:b c/d") == "a%3Ab%20c%2Fd"

    def test_enc_none_is_empty(self):
        assert enc(None) == ""

    def test_enc_int(self):
        assert enc(8080) == "8080"

    def test_dec_reverses_enc(self):
        assert dec(enc("a:b c/d")) == "a:b c/d"

    def test_encode_token_layout(self):
        assert encode_token("otto-x", "v1", ("a", "b")) == "otto-x:v1:a:b"

    def test_encode_does_not_reencode_segments(self):
        # Framing must pass final segment strings through verbatim —
        # otto-tunnel's path segment is double-encoded by its OWN codec.
        assert encode_token("otto-x", "v1", ("a%3Ab",)) == "otto-x:v1:a%3Ab"

    def test_split_round_trip(self):
        assert split_token("otto-x:v1:a:b", "otto-x", "v1", 2) == ["a", "b"]

    def test_split_wrong_prefix_is_none(self):
        assert split_token("otto-y:v1:a:b", "otto-x", "v1", 2) is None

    def test_split_wrong_version_is_none(self):
        assert split_token("otto-x:v2:a:b", "otto-x", "v1", 2) is None

    def test_split_wrong_count_is_none(self):
        assert split_token("otto-x:v1:a", "otto-x", "v1", 2) is None
        assert split_token("otto-x:v1:a:b:c", "otto-x", "v1", 2) is None

    def test_split_preserves_empty_segments(self):
        assert split_token("otto-x:v1::b", "otto-x", "v1", 2) == ["", "b"]

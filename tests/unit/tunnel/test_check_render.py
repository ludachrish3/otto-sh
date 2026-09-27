"""``tunnel_sections``: rendering a completed ``check_tunnel`` report from a simulated bed."""

import dataclasses
import json

import pytest
from rich.console import Console

from otto.check import (
    FeatureResult,
    ProvenEntry,
    ProvenRange,
    UnmeasuredReason,
    Verdict,
    render_sections,
    report_to_json,
)
from otto.tunnel.check import (
    TunnelCheckColumn,
    TunnelCheckHop,
    TunnelCheckReport,
    check_tunnel,
    tunnel_sections,
)

from ._check_fakes import bed
from ._check_helpers import (
    AB,
    ABC,
    DEST,
    EMBEDDED,
    HANDSHAKE_ONLY,
    LAST_SEGMENT,
    PORT,
    SCRATCH,
    _dest_bed,
)


def _section(report: TunnelCheckReport):
    [section] = tunnel_sections(report)
    return section


def _render(report: TunnelCheckReport) -> str:
    console = Console(record=True, width=120, color_system=None)
    render_sections(console, tunnel_sections(report))
    return console.export_text()


_HOP_FACTS_TAIL = (
    "socat 1.8.0.0 · bash 5.2.21 · clock yes · launcher systemd-run · "
    "kernel 6.8.0-86-generic · aarch64 · gnu"
)
_PINNED_RANGE = ProvenRange(
    revision="test",
    components={
        name: [ProvenEntry(version, "the fake bed", "2026-09-26")]
        for name, version in {
            "kernel": "6.8.0-86-generic",
            "isa": "aarch64",
            "userland": "gnu",
            "socat": "1.8.0.0",
            "bash": "5.2.21",
            "launcher": "systemd-run",
        }.items()
    },
)
"""The render goldens' own proven range: exactly the fake bed's versions, whatever
``proven.json`` records, so every label reads ``within``."""


@pytest.fixture
def pinned_proven_range(monkeypatch) -> ProvenRange:
    """Label every hop against :data:`_PINNED_RANGE` instead of the packaged file."""
    monkeypatch.setattr("otto.check.proven.load_proven_range", lambda: _PINNED_RANGE)
    return _PINNED_RANGE


_PROVEN_RANGE_LINE = (
    "proven range: kernel within · aarch64 within · gnu within · socat within · "
    "bash within · launcher within"
)
_TCP_TUNNEL_ID = "tun-9c0e6b395c42-61000"
_UDP_TUNNEL_ID = "tun-b3d7dad22a5e-61000"

GOLDEN_ABC_BOTH = [
    "a 10.0.0.1 → b 10.0.0.2 → c 10.0.0.3  :8080 (scratch 61000)",
    f"a  {_HOP_FACTS_TAIL}",
    f"   {_PROVEN_RANGE_LINE}",
    f"b  {_HOP_FACTS_TAIL}",
    f"   {_PROVEN_RANGE_LINE}",
    f"c  {_HOP_FACTS_TAIL}",
    f"   {_PROVEN_RANGE_LINE}",
    "bulk: tcp 64 KiB · udp 65000 B",
    "proven: hop chain: payload-verified (tcp, udp)",
    "",
    "feature        tcp   udp   detail",
    "service port   pass  pass  free",
    "segment a → b  pass  pass  5/5 echoed, median 1.0 ms round trip",
    "segment b → c  pass  pass  5/5 echoed, median 1.0 ms round trip",
    f"build          pass  pass  tcp carriers 61001/61002 · {_TCP_TUNNEL_ID}",
    f"                           udp carriers 61001/61002 · {_UDP_TUNNEL_ID}",
    "fwd 1 B        pass  pass  5/5 echoed byte for byte",
    "fwd 1400 B     pass  pass  5/5 echoed byte for byte",
    "fwd bulk       pass  pass  tcp got 65536 of 65536 B",
    "                           udp got 65000 of 65000 B",
    "rev 1 B        pass  pass  5/5 echoed byte for byte",
    "rev 1400 B     pass  pass  5/5 echoed byte for byte",
    "rev bulk       pass  pass  tcp got 65536 of 65536 B",
    "                           udp got 65000 of 65000 B",
    "rtt            pass  pass  through tunnel 1.0 ms; segments sum 2.0 ms",
    "list           pass  pass  ok",
    "teardown       pass  pass  nothing tagged survived",
    "path: 26 pass",
]


@pytest.mark.usefixtures("pinned_proven_range")
class TestTunnelSectionsHeadingAndSubheadings:
    @pytest.mark.asyncio
    async def test_sections_heading_names_every_hop_and_address(self, monkeypatch) -> None:
        the_bed = bed().install(monkeypatch)
        report = await check_tunnel(the_bed, ABC, port=PORT)
        section = _section(report)
        assert section.heading == "a 10.0.0.1 → b 10.0.0.2 → c 10.0.0.3  :8080 (scratch 61000)"
        assert section.columns == ["tcp", "udp"]
        assert section.summary_name == "path"
        assert section.subheadings[0] == f"a  {_HOP_FACTS_TAIL}"

    @pytest.mark.asyncio
    async def test_a_hop_s_proven_range_sits_directly_under_its_own_facts_line(
        self, monkeypatch
    ) -> None:
        """K1: the range line is indented ``len(host_id) + 2`` and uses link's value labels
        (``aarch64 within``/``gnu within``, not ``isa within``/``userland within``)."""
        the_bed = bed().install(monkeypatch)
        report = await check_tunnel(the_bed, ABC, port=PORT)
        section = _section(report)
        assert section.subheadings[0] == f"a  {_HOP_FACTS_TAIL}"
        assert section.subheadings[1] == f"   {_PROVEN_RANGE_LINE}"
        assert section.subheadings[2] == f"b  {_HOP_FACTS_TAIL}"
        assert section.subheadings[3] == f"   {_PROVEN_RANGE_LINE}"
        assert section.subheadings[4] == f"c  {_HOP_FACTS_TAIL}"
        assert section.subheadings[5] == f"   {_PROVEN_RANGE_LINE}"

    @pytest.mark.asyncio
    async def test_proven_line_is_a_subheading(self, monkeypatch) -> None:
        the_bed = bed().install(monkeypatch)
        report = await check_tunnel(the_bed, ABC, port=PORT)
        section = _section(report)
        assert section.subheadings[-1] == f"proven: {report.proven}"
        assert report.proven == "hop chain: payload-verified (tcp, udp)"

    @pytest.mark.asyncio
    async def test_a_missing_scratch_port_drops_the_scratch_clause(self, monkeypatch) -> None:
        # The missing-tools path never builds a tunnel, so scratch_port stays None.
        the_bed = bed(a={"missing": ["socat"]}).install(monkeypatch)
        report = await check_tunnel(the_bed, ABC, port=PORT)
        assert report.scratch_port is None
        section = _section(report)
        assert section.heading == "a 10.0.0.1 → b 10.0.0.2 → c 10.0.0.3  :8080"

    @pytest.mark.asyncio
    async def test_a_full_proof_dest_extends_the_heading_and_gets_its_own_facts(
        self, monkeypatch
    ) -> None:
        the_bed = bed("a", "b", "d").install(monkeypatch)
        report = await check_tunnel(the_bed, AB, port=PORT, protocol="tcp", dest=DEST)
        section = _section(report)
        assert section.heading == "a 10.0.0.1 → b 10.0.0.2 → dest d 10.0.0.3  :8080 (scratch 61000)"
        # Each of a, b and the dest: a facts line + its own indented proven-range line,
        # then bulk, then proven: 8 lines. The dest comes straight after the hop pairs
        # (K1's subheading order: hop pairs, dest, bulk, swept, proven).
        assert len(section.subheadings) == 8
        assert section.subheadings[0].startswith("a  socat")
        assert section.subheadings[1].startswith("   proven range:")
        assert section.subheadings[2].startswith("b  socat")
        assert section.subheadings[3].startswith("   proven range:")
        assert section.subheadings[4] == f"d  {_HOP_FACTS_TAIL}"
        assert section.subheadings[5] == f"   {_PROVEN_RANGE_LINE}"
        assert section.subheadings[6] == "bulk: tcp 64 KiB"
        assert section.subheadings[7] == f"proven: {report.proven}"

    @pytest.mark.asyncio
    async def test_a_split_proof_dest_with_a_fingerprint_still_gets_a_facts_line(
        self, monkeypatch
    ) -> None:
        the_bed = bed("a", "b", "d", d={"missing": ["socat"], "serves": [PORT]}).install(
            monkeypatch
        )
        report = await check_tunnel(the_bed, AB, port=PORT, protocol="tcp", dest=DEST)
        assert report.dest_proof == "split"
        assert report.destination is not None
        assert report.destination.fingerprint is not None
        section = _section(report)
        # The dest's facts line, and its own proven-range line directly under it (K1/M4).
        assert section.subheadings[4].startswith("d  socat")
        assert section.subheadings[5].startswith("   proven range:")

    @pytest.mark.asyncio
    async def test_a_has_bash_false_dest_says_not_fingerprinted(self, monkeypatch) -> None:
        the_bed = bed("a", "b", "d", d={"has_bash": False, "serves": [PORT]}).install(monkeypatch)
        report = await check_tunnel(the_bed, AB, port=PORT, protocol="tcp", dest=DEST)
        assert report.dest_proof == "split"
        assert report.destination is not None
        assert report.destination.fingerprint is None
        section = _section(report)
        # The dest is the one line right after the two hops' own facts+range pairs;
        # it never gets a proven-range line of its own, since it was never fingerprinted.
        assert section.subheadings[4] == "d: runs nothing of otto's — not fingerprinted"
        assert sum(line.startswith("proven range:") for line in section.subheadings) == 0
        assert sum(line.strip().startswith("proven range:") for line in section.subheadings) == 2


@pytest.mark.usefixtures("pinned_proven_range")
class TestTunnelSectionsTable:
    @pytest.mark.asyncio
    async def test_bulk_rows_display_protocol_neutral_but_json_keeps_the_real_name(
        self, monkeypatch
    ) -> None:
        the_bed = bed().install(monkeypatch)
        report = await check_tunnel(the_bed, ABC, port=PORT)
        section = _section(report)
        labels = [row.label for row in section.rows]
        assert "fwd bulk" in labels
        assert "rev bulk" in labels
        assert "fwd 64 KiB" not in labels
        assert "fwd 65000 B" not in labels
        # The underlying FeatureResult still carries its own protocol's exact name.
        tcp_bulk = next(r for r in report.columns[0].results if r.feature == "fwd 64 KiB")
        udp_bulk = next(r for r in report.columns[1].results if r.feature == "fwd 65000 B")
        bulk_row = next(row for row in section.rows if row.label == "fwd bulk")
        assert bulk_row.cells == [tcp_bulk, udp_bulk]

    def test_missing_rows_render_na(self) -> None:
        """Two columns with asymmetric rows (a full-proof extra segment vs. a split's
        last-segment row): a column with no row of a given label gets ``None``, and
        ``render_sections`` turns that into ``n/a`` — proving the mechanism generically,
        not any one real run's shape."""
        seg_row = FeatureResult("segment b → dest1", Verdict.PASS, measured="echoed")
        last_row = FeatureResult(
            "last segment → dest1",
            Verdict.UNMEASURED,
            reason=UnmeasuredReason.NO_REPLY_ORACLE,
            detail="no reply oracle",
        )
        tcp = TunnelCheckColumn(
            "tcp",
            [FeatureResult("service port", Verdict.PASS, measured="free"), seg_row],
        )
        udp = TunnelCheckColumn(
            "udp",
            [FeatureResult("service port", Verdict.PASS, measured="free"), last_row],
        )
        report = TunnelCheckReport(
            path=["a"],
            port=PORT,
            dest="dest1",
            carrier="socat",
            protocols=["tcp", "udp"],
            scratch_port=SCRATCH,
            hops=[TunnelCheckHop("a", "10.0.0.1", None, {})],
            columns=[tcp, udp],
            proven="hop chain: payload-verified",
        )
        section = _section(report)
        labels = [row.label for row in section.rows]
        assert labels == ["service port", "segment b → dest1", "last segment → dest1"]
        seg = next(r for r in section.rows if r.label == "segment b → dest1")
        assert seg.cells == [seg_row, None]
        last = next(r for r in section.rows if r.label == "last segment → dest1")
        assert last.cells == [None, last_row]
        text = _render(report)
        seg_line = next(line for line in text.splitlines() if line.startswith("segment b"))
        assert "n/a" in seg_line
        last_line = next(line for line in text.splitlines() if line.startswith("last segment"))
        assert "n/a" in last_line

    @pytest.mark.asyncio
    async def test_row_order_is_the_union_of_the_columns_first_seen(self, monkeypatch) -> None:
        the_bed = bed().install(monkeypatch)
        report = await check_tunnel(the_bed, ABC, port=PORT)
        section = _section(report)
        assert [row.label for row in section.rows] == [
            "service port",
            "segment a → b",
            "segment b → c",
            "build",
            "fwd 1 B",
            "fwd 1400 B",
            "fwd bulk",
            "rev 1 B",
            "rev 1400 B",
            "rev bulk",
            "rtt",
            "list",
            "teardown",
        ]


@pytest.mark.usefixtures("pinned_proven_range")
class TestTunnelSectionsSayTheDetailACellCarries:
    @pytest.mark.asyncio
    async def test_a_split_proof_s_passing_handshake_says_it_proves_no_payload(
        self, monkeypatch
    ) -> None:
        the_bed = _dest_bed(monkeypatch, **EMBEDDED)
        report = await check_tunnel(the_bed, AB, port=PORT, protocol="tcp", dest=DEST)
        line = next(line for line in _render(report).splitlines() if line.startswith(LAST_SEGMENT))
        assert line.split(maxsplit=4)[-1].strip() == f"pass  connected — {HANDSHAKE_ONLY}"

    @pytest.mark.asyncio
    async def test_a_service_port_held_by_an_otto_tunnel_names_it(self, monkeypatch) -> None:
        the_bed = bed().install(monkeypatch)
        user = the_bed.plant_tunnel(["a", "c"], "tcp", PORT)
        report = await check_tunnel(the_bed, ABC, port=PORT, protocol="tcp")
        text = " ".join(line.strip() for line in _render(report).splitlines())
        assert (
            f"want free — an otto tunnel already binds it: {user.id} on a, {user.id} on c" in text
        )


@pytest.mark.usefixtures("pinned_proven_range")
class TestTunnelSectionsMarkupAndJSON:
    @pytest.mark.asyncio
    async def test_a_version_string_with_markup_syntax_renders_literally(self, monkeypatch) -> None:
        the_bed = bed().install(monkeypatch)
        report = await check_tunnel(the_bed, ABC, port=PORT)
        hop0 = report.hops[0]
        assert hop0.fingerprint is not None
        rogue = dataclasses.replace(
            hop0.fingerprint, versions={**hop0.fingerprint.versions, "bash": "5.2[bold]"}
        )
        hops = [dataclasses.replace(hop0, fingerprint=rogue), *report.hops[1:]]
        report = dataclasses.replace(report, hops=hops)
        text = _render(report)
        line = next(line for line in text.splitlines() if line.startswith("a  socat"))
        assert "bash 5.2[bold]" in line

    @pytest.mark.asyncio
    async def test_report_json_kind_tunnel(self, monkeypatch) -> None:
        the_bed = bed().install(monkeypatch)
        report = await check_tunnel(the_bed, ABC, port=PORT)
        text = report_to_json(report, kind="tunnel")
        doc = json.loads(text)
        assert doc["kind"] == "tunnel"
        assert doc["result"]["columns"][0]["results"][0]["verdict"] == "pass"
        # The round trip keeps each row's own protocol-specific name.
        names = {r["feature"] for r in doc["result"]["columns"][0]["results"]}
        assert "fwd 64 KiB" in names

    @pytest.mark.asyncio
    async def test_report_json_kind_tunnel_names_a_split_dest(self, monkeypatch) -> None:
        the_bed = bed("a", "b", "d", d={"has_bash": False, "serves": [PORT]}).install(monkeypatch)
        report = await check_tunnel(the_bed, AB, port=PORT, protocol="tcp", dest=DEST)
        text = report_to_json(report, kind="tunnel")
        doc = json.loads(text)
        assert doc["result"]["dest_proof"] == "split"
        assert doc["result"]["destination"]["host_id"] == "d"
        assert doc["result"]["destination"]["fingerprint"] is None

    @pytest.mark.asyncio
    async def test_golden_two_protocol_three_hop_render(self, monkeypatch) -> None:
        the_bed = bed().install(monkeypatch)
        report = await check_tunnel(the_bed, ABC, port=PORT)
        text = _render(report)
        lines = [line.rstrip() for line in text.splitlines()]
        assert lines == GOLDEN_ABC_BOTH

    @pytest.mark.asyncio
    async def test_golden_dead_segment_fails_both_protocols_with_shared_evidence_and_hint(
        self, monkeypatch
    ) -> None:
        """G2: the fail row's own detail, per-protocol ``ran:`` evidence, and the
        cascade's single unprefixed ``hint:`` line (M3) on every downstream skipped row.

        The two ``ran:`` commands are otto's real trips scripts — one-liners so
        long that Rich wraps each across many physical lines at width 120.
        Pinning that wrap character-for-character blows past this repo's
        100-column lint limit on every wrapped fragment (checked: it does,
        with no ``# noqa`` available to silence it), and would really be
        pinning ``trips_script``'s wording, not this task's rendering. So the
        structural lines — heading, subheadings, table rows, hint lines,
        summary — are pinned exactly, and the two long evidence blocks are
        checked by their own first line and their protocol-naming content.
        """
        the_bed = bed().install(monkeypatch)
        the_bed.dead.add(("b", "c"))
        report = await check_tunnel(the_bed, ABC, port=PORT)
        text = _render(report)
        lines = [line.rstrip() for line in text.splitlines()]

        assert lines[:9] == [
            "a 10.0.0.1 → b 10.0.0.2 → c 10.0.0.3  :8080 (scratch 61000)",
            f"a  {_HOP_FACTS_TAIL}",
            f"   {_PROVEN_RANGE_LINE}",
            f"b  {_HOP_FACTS_TAIL}",
            f"   {_PROVEN_RANGE_LINE}",
            f"c  {_HOP_FACTS_TAIL}",
            f"   {_PROVEN_RANGE_LINE}",
            "bulk: tcp 64 KiB · udp 65000 B",
            "proven: hop chain: not proven — see the failing rows",
        ]
        assert lines[9] == ""

        table = lines[10:]
        assert table[0] == "feature        tcp      udp      detail"
        assert table[1] == "service port   pass     pass     free"
        assert table[2] == "segment a → b  pass     pass     5/5 echoed, median 1.0 ms round trip"
        assert table[3] == "segment b → c  fail     fail     b → c: no echo from 10.0.0.3:61000"

        # The evidence block between the fail row and "build": one ran: command
        # per protocol, tcp before udp (column order), each naming its own
        # target address. The exact shell text is otto's trips_script's own
        # concern, not this renderer's.
        build_index = next(n for n, line in enumerate(table) if line.startswith("build "))
        evidence = table[4:build_index]
        tcp_at = next(n for n, line in enumerate(evidence) if line.strip().startswith("tcp ran:"))
        udp_at = next(n for n, line in enumerate(evidence) if line.strip().startswith("udp ran:"))
        assert tcp_at < udp_at
        assert "TCP4:10.0.0.3:61000" in " ".join(evidence[tcp_at:udp_at])
        assert "UDP4:10.0.0.3:61000" in " ".join(evidence[udp_at:])
        # Each script is cut on screen, saying where the whole command is.
        cut = "chars; full command with -v or in --report)"
        assert cut in " ".join(line.strip() for line in evidence[tcp_at:udp_at])
        assert cut in " ".join(line.strip() for line in evidence[udp_at:])

        # Every downstream row is skipped on both columns. The cascade's one cause is
        # ONE unprefixed hint line (never a "tcp hint:"/"udp hint:" pair), printed
        # once, under the first skipped row: the rows after it do not repeat it.
        downstream = [
            "build", "fwd 1 B", "fwd 1400 B", "fwd bulk",
            "rev 1 B", "rev 1400 B", "rev bulk", "rtt", "list",
        ]  # fmt: skip
        hint = "hint: segment b → c failed, so no tunnel was built across it"
        at = [
            next(n for n, line in enumerate(table) if line.startswith(f"{name} "))
            for name in downstream
        ]
        for name, i in zip(downstream, at, strict=True):
            assert table[i].split()[-2:] == ["skipped", "skipped"], name
        assert table[at[0] + 1].strip() == hint
        assert at == list(range(at[0], at[0] + 1)) + list(range(at[0] + 2, at[0] + len(at) + 1))
        assert [line.strip() for line in table].count(hint) == 1

        assert table[-2] == "teardown       pass     pass     nothing tagged survived"
        assert table[-1] == "path: 6 pass · 2 fail · 18 skipped"

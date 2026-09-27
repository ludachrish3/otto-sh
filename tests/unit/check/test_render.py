"""Stdout rendering for checks (spec 2026-09-24 §3.5)."""

import dataclasses
import json

import pytest
from rich.console import Console

from otto.check import FeatureResult, HostFingerprint, UnmeasuredReason, Verdict, report_to_json
from otto.check.render import (
    RAN_LINE_CAP,
    CheckRow,
    CheckSection,
    proven_range_line,
    render_sections,
    section_counts,
)

PASS = Verdict.PASS


def _text(sections: list[CheckSection], *, verbose: bool = False) -> str:
    console = Console(record=True, width=140, color_system=None)
    render_sections(console, sections, verbose=verbose)
    return console.export_text()


SECTION = CheckSection(
    heading="test1  eth1.100 10.10.201.11  (a->b)   iproute2 6.1.0 · kernel 6.8.0 · aarch64 · gnu",
    subheadings=["proven range: iproute2 within · kernel unknown · aarch64 within"],
    columns=["sandbox", "live"],
    rows=[
        CheckRow("delay", [FeatureResult("delay", PASS, measured="+200.4ms"), None]),
        CheckRow(
            "rate",
            [
                FeatureResult(
                    "rate",
                    Verdict.FAIL,
                    measured="48 kbit/s",
                    wanted="1 mbit/s ±25%",
                    commands=["tc qdisc replace dev ock1a2b3c root netem rate 1mbit"],
                    hint="see docs/cli/link/check#rate",
                ),
                None,
            ],
        ),
        CheckRow(
            "reorder",
            [
                FeatureResult(
                    "reorder",
                    Verdict.UNMEASURED,
                    reason=UnmeasuredReason.MISSING_TOOL,
                    detail="needs socat or python3",
                ),
                None,
            ],
        ),
        CheckRow(
            "corrupt",
            [
                FeatureResult(
                    "corrupt",
                    Verdict.UNSUPPORTED,
                    commands=["tc qdisc replace dev ock1a2b3c root netem corrupt 50%"],
                    output="Error: netem: unknown parameter corrupt",
                ),
                None,
            ],
        ),
    ],
    summary_name="test1",
    legend=["n/a: sandbox result applies; a real link doesn't change this feature"],
)


def test_heading_table_legend_and_summary() -> None:
    out = _text([SECTION])
    assert out.startswith("test1  eth1.100 10.10.201.11  (a->b)")
    assert "proven range: iproute2 within" in out
    for header in ("feature", "sandbox", "live", "detail"):
        assert header in out
    assert "n/a: sandbox result applies; a real link doesn't change this feature" in out
    assert out.rstrip().endswith("test1: 1 pass · 1 fail · 1 unsupported · 1 unmeasured")


def test_cells_show_verdict_words_and_na() -> None:
    out = _text([SECTION])
    delay_line = next(line for line in out.splitlines() if line.startswith("delay"))
    assert "pass" in delay_line
    assert "n/a" in delay_line
    assert "+200.4ms" in delay_line


def test_failing_row_shows_measured_want_command_and_hint_in_order() -> None:
    # SECTION declares two columns (sandbox, live), so its evidence lines carry
    # the column-name prefix even though this row's "live" cell is n/a.
    lines = _text([SECTION]).splitlines()
    i = next(n for n, line in enumerate(lines) if line.startswith("rate"))
    assert "measured 48 kbit/s, want 1 mbit/s ±25%" in lines[i]
    line = "sandbox ran: tc qdisc replace dev ock1a2b3c root netem rate 1mbit"
    assert lines[i + 1].strip() == line
    assert lines[i + 2].strip() == "sandbox hint: see docs/cli/link/check#rate"


def test_unmeasured_detail_names_its_reason() -> None:
    out = _text([SECTION])
    line = next(line for line in out.splitlines() if line.startswith("reorder"))
    assert "missing-tool: needs socat or python3" in line


def test_unsupported_quotes_what_the_tool_said() -> None:
    out = _text([SECTION])
    assert "said: Error: netem: unknown parameter corrupt" in out


def test_verbose_adds_output_blocks_and_default_does_not() -> None:
    section = CheckSection(
        heading="h",
        subheadings=[],
        columns=["sandbox"],
        rows=[CheckRow("delay", [FeatureResult("delay", PASS, output="rtt line 1\nrtt line 2")])],
        summary_name="h",
    )
    assert "rtt line 1" not in _text([section])
    verbose = _text([section], verbose=True)
    assert "output:" in verbose
    assert "  rtt line 2" in verbose


def test_section_counts_ignore_na_cells() -> None:
    counts = section_counts(SECTION)
    assert counts[Verdict.PASS] == 1
    assert sum(counts.values()) == 4


def test_brackets_print_verbatim_everywhere() -> None:
    """Rich markup must not eat ``[...]`` in measured/wanted, commands, hints or output."""
    section = CheckSection(
        heading="h",
        subheadings=[],
        columns=["sandbox"],
        rows=[
            CheckRow(
                "addr",
                [
                    FeatureResult(
                        "addr",
                        Verdict.FAIL,
                        measured="host [fe80::1]:22 unreachable",
                        wanted="a[0] reachable",
                        commands=["ip -6 route get a[0] via [fe80::1]"],
                        hint="see [docs] at [bold]link/check[/bold]",
                    ),
                ],
            ),
            CheckRow(
                "probe",
                [
                    FeatureResult(
                        "probe",
                        Verdict.UNSUPPORTED,
                        commands=["tc qdisc replace dev x root netem corrupt 50%"],
                        output="Error: netem [unknown] parameter a[0]",
                    ),
                ],
            ),
        ],
        summary_name="h",
    )
    out = _text([section])
    assert "host [fe80::1]:22 unreachable" in out
    assert "want a[0] reachable" in out
    assert "ran: ip -6 route get a[0] via [fe80::1]" in out
    assert "hint: see [docs] at [bold]link/check[/bold]" in out
    assert "said: Error: netem [unknown] parameter a[0]" in out


def test_evidence_for_every_non_pass_cell_is_column_prefixed_in_order() -> None:
    """Every non-pass, non-n/a cell gets its own evidence, not just the first one."""
    section = CheckSection(
        heading="h",
        subheadings=[],
        columns=["sandbox", "live"],
        rows=[
            CheckRow(
                "reorder",
                [
                    FeatureResult(
                        "reorder",
                        Verdict.UNMEASURED,
                        reason=UnmeasuredReason.MISSING_TOOL,
                        detail="needs socat or python3",
                        commands=["which socat"],
                    ),
                    FeatureResult(
                        "reorder",
                        Verdict.FAIL,
                        measured="0% reordered",
                        wanted="5% ±2%",
                        commands=["tc qdisc replace dev eth0 root netem reorder 5%"],
                        hint="see docs/cli/link/check#reorder",
                    ),
                ],
            ),
        ],
        summary_name="h",
    )
    lines = _text([section]).splitlines()
    i = next(n for n, line in enumerate(lines) if line.startswith("reorder"))
    tail = (line.strip() for line in lines[i + 1 :])
    evidence = [line for line in tail if line.startswith(("sandbox", "live"))]
    # The two cells' own detail texts differ ("missing-tool: ..." vs. "measured
    # 0% reordered, ..."), so the row prints "sandbox ..." on its own line (not
    # captured by `tail`, which starts after the row) and "live ..." as a
    # continuation line, before either cell's ran:/hint: evidence.
    assert evidence == [
        "live measured 0% reordered, want 5% ±2%",
        "sandbox ran: which socat",
        "live ran: tc qdisc replace dev eth0 root netem reorder 5%",
        "live hint: see docs/cli/link/check#reorder",
    ]


def test_two_columns_that_both_pass_with_the_same_value_print_once_unprefixed() -> None:
    """The common case (spec 2026-09-24 §3.5): agreeing columns collapse to one line."""
    section = CheckSection(
        heading="h",
        subheadings=[],
        columns=["tcp", "udp"],
        rows=[
            CheckRow(
                "service port",
                [
                    FeatureResult("service port", PASS, measured="free"),
                    FeatureResult("service port", PASS, measured="free"),
                ],
            ),
        ],
        summary_name="h",
    )
    line = next(line for line in _text([section]).splitlines() if line.startswith("service port"))
    assert line.rstrip().endswith("free")
    assert "tcp free" not in line
    assert "udp free" not in line


def test_two_columns_that_both_pass_with_different_values_print_one_line_each() -> None:
    """K2: two columns passing with DIFFERENT values are never silently collapsed to one.

    Reproduces the tunnel check's own bug: ``otto tunnel check``'s "fwd bulk"
    row passes on both tcp and udp with different byte counts, and the old
    single-``target``-cell renderer showed only whichever cell it happened to
    pick, hiding the other column's real value.
    """
    section = CheckSection(
        heading="h",
        subheadings=[],
        columns=["tcp", "udp"],
        rows=[
            CheckRow(
                "fwd bulk",
                [
                    FeatureResult("fwd 64 KiB", PASS, measured="got 65536 of 65536 B"),
                    FeatureResult("fwd 65000 B", PASS, measured="got 65000 of 65000 B"),
                ],
            ),
        ],
        summary_name="h",
    )
    lines = _text([section]).splitlines()
    i = next(n for n, line in enumerate(lines) if line.startswith("fwd bulk"))
    assert lines[i].rstrip().endswith("tcp got 65536 of 65536 B")
    assert lines[i + 1].strip() == "udp got 65000 of 65000 B"


def test_a_hint_every_non_pass_cell_of_one_row_shares_prints_once_unprefixed() -> None:
    """M3: a per-row shared hint (a cascade of skipped rows) collapses like the section-wide one.

    The section carries a SECOND row with a different (here: absent) hint, so
    ``shared_hint``'s section-wide dedup does not itself apply here — only the
    per-row mechanism under test can collapse "build"'s two identical hints.
    """
    section = CheckSection(
        heading="h",
        subheadings=[],
        columns=["tcp", "udp"],
        rows=[
            CheckRow(
                "segment",
                [
                    FeatureResult("segment", Verdict.FAIL, detail="no echo"),
                    FeatureResult("segment", Verdict.FAIL, detail="no echo"),
                ],
            ),
            CheckRow(
                "build",
                [
                    FeatureResult("build", Verdict.SKIPPED, hint="segment a → b failed"),
                    FeatureResult("build", Verdict.SKIPPED, hint="segment a → b failed"),
                ],
            ),
        ],
        summary_name="h",
    )
    lines = _text([section]).splitlines()
    hint_lines = [line.strip() for line in lines if "segment a → b failed" in line]
    assert hint_lines == ["hint: segment a → b failed"]


def test_a_hint_only_one_non_pass_cell_carries_keeps_its_own_column_prefix() -> None:
    """A single non-passing cell is not a cascade: its hint stays column-prefixed."""
    section = CheckSection(
        heading="h",
        subheadings=[],
        columns=["tcp", "udp"],
        rows=[
            CheckRow(
                "build",
                [
                    FeatureResult("build", Verdict.FAIL, detail="boom", hint="see the docs"),
                    FeatureResult("build", PASS, measured="ok"),
                ],
            ),
        ],
        summary_name="h",
    )
    lines = _text([section]).splitlines()
    hint_lines = [line.strip() for line in lines if "see the docs" in line]
    assert hint_lines == ["tcp hint: see the docs"]


_UNMEASURED_SHAPES = [
    ({"measured": "loss 20%, σ 9.5 ms"}, "{code}: measured loss 20%, σ 9.5 ms"),  # noqa: RUF001 — sigma is the SI symbol for standard deviation
    ({"measured": "+3.1ms", "wanted": "100 ±10"}, "{code}: measured +3.1ms, want 100 ±10"),
    ({"detail": "needs ping on test1"}, "{code}: needs ping on test1"),
    ({}, "{code}"),
]


@pytest.mark.parametrize("reason", list(UnmeasuredReason), ids=lambda r: r.value)
@pytest.mark.parametrize(
    ("fields", "expected"),
    _UNMEASURED_SHAPES,
    ids=["measured", "measured+wanted", "detail", "bare"],
)
def test_unmeasured_detail_leads_with_its_reason_code_in_every_shape(
    reason: UnmeasuredReason, fields: dict, expected: str
) -> None:
    """The reason code is the lookup key, so a measurement never displaces it."""
    cell = FeatureResult("delay", Verdict.UNMEASURED, reason=reason, **fields)
    section = CheckSection("h", [], ["sandbox"], [CheckRow("delay", [cell])], "h")
    line = next(line for line in _text([section]).splitlines() if line.startswith("delay"))
    assert line.split("unmeasured", 1)[1].strip() == expected.format(code=reason.value)


def _skipped_section(hints: list[str | None]) -> CheckSection:
    return CheckSection(
        heading="test1  eth1.100  (a->b)",
        subheadings=["proven range: iproute2 within"],
        columns=["sandbox"],
        rows=[
            CheckRow(f"f{i}", [FeatureResult(f"f{i}", Verdict.SKIPPED, hint=hint)])
            for i, hint in enumerate(hints)
        ],
        summary_name="test1",
    )


def test_a_hint_every_row_shares_prints_once_under_the_heading() -> None:
    hint = "otto could not become root on test1"
    lines = _text([_skipped_section([hint] * 11)]).splitlines()
    assert [line.strip() for line in lines if hint in line] == [f"hint: {hint}"]
    assert lines.index(f"hint: {hint}") < next(
        n for n, line in enumerate(lines) if line.startswith("feature")
    ), "it reads as the section's one cause, before the table"


def test_a_hint_only_some_rows_share_stays_on_each_row() -> None:
    """Not every row shares it, so nothing is hoisted; rows apart each say it."""
    hint = "needs bash on test1"
    out = _text([_skipped_section([hint, None, hint])])
    assert out.count(f"hint: {hint}") == 2
    assert f"\nhint: {hint}" not in out, "nothing is hoisted under the heading"


def test_a_hint_the_row_before_printed_is_not_repeated() -> None:
    """A cascade of skipped rows says its one cause once, under the first of them."""
    hint = "segment a → b failed, so no tunnel was built across it"
    lines = _text([_skipped_section([None, hint, hint, hint, None, hint])]).splitlines()
    hint_at = [n for n, line in enumerate(lines) if line.strip() == f"hint: {hint}"]
    rows = {line.split()[0]: n for n, line in enumerate(lines) if line.startswith("f")}
    # Once under f1 (f2 and f3 follow it), and again under f5, after f4 broke the run.
    assert hint_at == [rows["f1"] + 1, rows["f5"] + 1]


def _two_column_section(rows: list[tuple[FeatureResult, FeatureResult]]) -> CheckSection:
    return CheckSection(
        heading="h",
        subheadings=[],
        columns=["tcp", "udp"],
        rows=[CheckRow(tcp.feature, [tcp, udp]) for tcp, udp in rows],
        summary_name="h",
    )


def test_a_row_shared_hint_the_row_before_printed_is_not_repeated() -> None:
    hint = "segment a → b failed"
    skipped = [(FeatureResult(f"r{i}", Verdict.SKIPPED, hint=hint),) * 2 for i in range(3)]
    section = _two_column_section([
        (FeatureResult("seg", Verdict.FAIL, detail="no echo"),) * 2,
        *skipped,
    ])  # fmt: skip
    lines = [line.strip() for line in _text([section]).splitlines()]
    assert lines.count(f"hint: {hint}") == 1
    assert lines[lines.index(f"hint: {hint}") - 1].startswith("r0")


def test_a_single_cell_hint_repeats_only_in_the_same_column() -> None:
    hint = "see the docs"
    ok = FeatureResult("x", PASS, measured="ok")
    section = _two_column_section([
        (FeatureResult("r0", Verdict.FAIL, detail="boom", hint=hint), ok),
        (FeatureResult("r1", Verdict.FAIL, detail="boom", hint=hint), ok),
        (ok, FeatureResult("r2", Verdict.FAIL, detail="boom", hint=hint)),
    ])  # fmt: skip
    lines = [line.strip() for line in _text([section]).splitlines()]
    assert [line for line in lines if hint in line] == [f"tcp hint: {hint}", f"udp hint: {hint}"]


def test_a_two_hint_cascade_is_not_repeated_per_row() -> None:
    """N3: the collapse compares the row's WHOLE hint list, any length — not just one hint.

    Two columns failing for two DIFFERENT causes give each row two hint
    lines (``tcp hint: …`` and ``udp hint: …``), never a single shared row
    hint. When several rows in a row repeat that identical pair, it must
    still print once, under the first of them, exactly like the one-hint
    cascade does.
    """
    tcp_hint = "segment a → b failed, so no tcp tunnel was built across it"
    udp_hint = "segment a → b failed, so no udp tunnel was built across it"

    def skipped() -> tuple[FeatureResult, FeatureResult]:
        return (
            FeatureResult("f", Verdict.SKIPPED, hint=tcp_hint),
            FeatureResult("f", Verdict.SKIPPED, hint=udp_hint),
        )

    section = _two_column_section([
        (FeatureResult("seg", Verdict.FAIL, detail="no echo"),) * 2,
        *(skipped() for _ in range(3)),
    ])  # fmt: skip
    lines = [line.strip() for line in _text([section]).splitlines()]
    assert lines.count(f"tcp hint: {tcp_hint}") == 1
    assert lines.count(f"udp hint: {udp_hint}") == 1


def test_a_passing_row_says_its_measurement_then_the_caveat_it_carries() -> None:
    cell = FeatureResult(
        "last segment → d",
        PASS,
        measured="handshake",
        detail="only the handshake is proven, not the payload (#440)",
    )
    section = CheckSection("h", [], ["tcp"], [CheckRow(cell.feature, [cell])], "h")
    line = next(line for line in _text([section]).splitlines() if line.startswith("last"))
    assert line.rstrip().endswith(
        "pass  handshake — only the handshake is proven, not the payload (#440)"
    )


def test_a_failing_row_says_measured_and_want_then_the_cause_it_names() -> None:
    cell = FeatureResult(
        "service port",
        Verdict.FAIL,
        measured="a: LISTEN 0 5 0.0.0.0:8080",
        wanted="free",
        detail="an otto tunnel already binds it: tun-0123456789ab-8080 on a",
    )
    section = CheckSection("h", [], ["tcp"], [CheckRow(cell.feature, [cell])], "h")
    line = next(line for line in _text([section]).splitlines() if line.startswith("service"))
    assert line.split("fail", 1)[1].strip() == (
        "measured a: LISTEN 0 5 0.0.0.0:8080, want free — "
        "an otto tunnel already binds it: tun-0123456789ab-8080 on a"
    )


def test_a_detail_that_repeats_the_measurement_is_said_once() -> None:
    cell = FeatureResult("list", PASS, measured="ok", detail="ok")
    section = CheckSection("h", [], ["tcp"], [CheckRow("list", [cell])], "h")
    line = next(line for line in _text([section]).splitlines() if line.startswith("list"))
    assert line.split()[-1] == "ok"
    assert line.count("ok") == 1


_LONG = "bash -c '" + "x" * 600 + "'"


def _failing_with(command: str) -> CheckSection:
    cell = FeatureResult("segment", Verdict.FAIL, detail="no echo", commands=[command])
    return CheckSection("h", [], ["tcp"], [CheckRow("segment", [cell])], "h")


def test_a_long_ran_line_is_cut_and_says_where_the_whole_command_is() -> None:
    console = Console(record=True, width=1000, color_system=None)
    render_sections(console, [_failing_with(_LONG)])
    [ran] = [line.strip() for line in console.export_text().splitlines() if "ran:" in line]
    whole = f"ran: {_LONG}"
    assert ran == (
        f"{whole[:RAN_LINE_CAP]} … (+{len(whole) - RAN_LINE_CAP} chars; "
        "full command with -v or in --report)"
    )


def test_verbose_prints_a_long_ran_line_whole() -> None:
    console = Console(record=True, width=1000, color_system=None)
    render_sections(console, [_failing_with(_LONG)], verbose=True)
    [ran] = [line.strip() for line in console.export_text().splitlines() if "ran:" in line]
    assert ran == f"ran: {_LONG}"


def test_a_ran_line_at_the_cap_is_whole() -> None:
    command = "x" * (RAN_LINE_CAP - len("tcp ran: "))
    cell = FeatureResult("segment", Verdict.FAIL, detail="no echo", commands=[command])
    section = _two_column_section([(cell, cell)])
    console = Console(record=True, width=1000, color_system=None)
    render_sections(console, [section])
    rans = [line.strip() for line in console.export_text().splitlines() if "ran:" in line]
    assert rans == [f"tcp ran: {command}", f"udp ran: {command}"]


def test_the_report_keeps_a_cut_command_whole() -> None:
    cell = FeatureResult("segment", Verdict.FAIL, detail="no echo", commands=[_LONG])
    doc = json.loads(report_to_json(_Holder([cell]), kind="tunnel"))
    assert doc["result"]["cells"][0]["commands"] == [_LONG]


@dataclasses.dataclass
class _Holder:
    cells: list[FeatureResult]


_FP = HostFingerprint(
    host_id="h",
    address="10.0.0.1",
    kernel="6.8.0",
    isa="aarch64",
    userland="gnu",
    user="vagrant",
    privileged=False,
    netns=True,
    netem_module=True,
    tools={},
    versions={},
    raw="",
)


def test_proven_range_line_leads_isa_and_userland_with_their_value() -> None:
    labels = {"iproute2": "within", "kernel": "older", "isa": "within", "userland": "outside"}
    line = proven_range_line(_FP, labels, ["iproute2", "kernel", "isa", "userland"])
    assert line == "proven range: iproute2 within · kernel older · aarch64 within · gnu outside"


def test_proven_range_line_follows_the_order_it_is_given() -> None:
    labels = dict.fromkeys(["kernel", "isa", "socat", "bash"], "unknown")
    unread = dataclasses.replace(_FP, isa=None)
    line = proven_range_line(unread, labels, ["socat", "isa", "kernel", "bash"])
    assert line == "proven range: socat unknown · isa unknown · kernel unknown · bash unknown"


def test_a_passing_row_shows_the_caveat_it_carries() -> None:
    caveat = "port-scoped tree not applied: tc rejected it (see port range)"
    section = CheckSection(
        heading="h",
        subheadings=[],
        columns=["sandbox"],
        rows=[CheckRow("read-back", [FeatureResult("read-back", PASS, detail=caveat)])],
        summary_name="h",
    )
    line = next(line for line in _text([section]).splitlines() if line.startswith("read-back"))
    assert caveat in line

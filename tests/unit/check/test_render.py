"""Stdout rendering for checks (spec 2026-09-24 §3.5)."""

import pytest
from rich.console import Console

from otto.check import FeatureResult, UnmeasuredReason, Verdict
from otto.check.render import CheckRow, CheckSection, render_sections, section_counts

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
    assert evidence == [
        "sandbox ran: which socat",
        "live ran: tc qdisc replace dev eth0 root netem reorder 5%",
        "live hint: see docs/cli/link/check#reorder",
    ]


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
    hint = "needs bash on test1"
    out = _text([_skipped_section([hint, hint, None])])
    assert out.count(f"hint: {hint}") == 2
    assert f"\nhint: {hint}" not in out, "nothing is hoisted under the heading"


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

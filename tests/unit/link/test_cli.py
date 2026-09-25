"""``otto link`` CLI: impair/repair/list/check rendering + completion.

Commands are plain ``async def`` leaves bridged by the leaf-invoke wrapper,
so these tests drive ``link_app`` through the production dispatch seam
(``DispatchRunner``) rather than a bare ``CliRunner``.
"""

import json
from unittest.mock import AsyncMock, patch

from rich import get_console

from otto.check import CheckHostUnreachableError, FeatureResult, UnmeasuredReason, Verdict
from otto.cli.link import _link_completer, link_app
from otto.link import (
    AppliedPlacement,
    FlowDirection,
    ImpairmentParams,
    ImpairReport,
    LinkState,
    Placement,
)
from otto.link.check import LinkCheckHost, LinkCheckReport
from otto.link.model import Link, LinkEndpoint
from tests._fixtures.dispatch import DispatchRunner
from tests.conftest import active_context

from .test_manage_impair import INPATH, LINK, MGMT_LINK, _bed

runner = DispatchRunner()


class TestImpairCommand:
    def test_happy_path_prints_placements(self) -> None:
        report = ImpairReport(
            link_id="lnk-abc",
            applied=[
                AppliedPlacement(
                    Placement("test1", "eth1.100", FlowDirection.A_TO_B),
                    ImpairmentParams(delay_ms=50.0),
                ),
            ],
        )
        with (
            patch("otto.cli.link.get_lab", return_value=object()),
            patch("otto.cli.link.impair_link", AsyncMock(return_value=report)),
        ):
            result = runner.invoke(link_app, ["impair", "edge", "--delay", "50"])
        assert result.exit_code == 0, result.output
        assert "impaired lnk-abc" in result.output
        assert "test1/eth1.100" in result.output

    def test_no_param_options_is_usage_error(self) -> None:
        result = runner.invoke(link_app, ["impair", "edge"])
        assert result.exit_code == 2
        assert "at least one parameter option" in result.output

    def test_bad_unit_is_usage_error_2_not_1(self) -> None:
        result = runner.invoke(link_app, ["impair", "edge", "--rate", "10"])
        assert result.exit_code == 2
        assert "explicit unit" in result.output

    def test_known_failure_exits_1(self) -> None:
        with (
            patch("otto.cli.link.get_lab", return_value=object()),
            patch(
                "otto.cli.link.impair_link",
                AsyncMock(side_effect=ValueError("management interface")),
            ),
        ):
            result = runner.invoke(link_app, ["impair", "edge", "--delay", "50"])
        assert result.exit_code == 1
        assert "management interface" in result.output


class TestRepairCommand:
    def test_neither_link_nor_all_exits_2(self) -> None:
        result = runner.invoke(link_app, ["repair"])
        assert result.exit_code == 2

    def test_both_link_and_all_exits_2(self) -> None:
        result = runner.invoke(link_app, ["repair", "edge", "--all"])
        assert result.exit_code == 2

    def test_repair_all_failures_exit_1(self) -> None:
        from otto.link import RepairAllReport

        sweep = RepairAllReport(failures=["lnk-abc: host down"])
        with (
            patch("otto.cli.link.get_lab", return_value=object()),
            patch("otto.cli.link.repair_all", AsyncMock(return_value=sweep)),
        ):
            result = runner.invoke(link_app, ["repair", "--all"])
        assert result.exit_code == 1
        assert "lnk-abc: host down" in result.output

    def test_repair_all_names_skipped_links_without_failing(self) -> None:
        """A skip used to be a silent `continue`: a link carrying a foreign
        qdisc made `repair --all` print "repaired 0 link(s)", exit 0, and say
        nothing at all about the link it had declined to touch."""
        from otto.link import RepairAllReport

        sweep = RepairAllReport(
            skipped=["lnk-abc: test1/eth1.100 has a foreign qdisc otto did not create"]
        )
        with (
            patch("otto.cli.link.get_lab", return_value=object()),
            patch("otto.cli.link.repair_all", AsyncMock(return_value=sweep)),
        ):
            result = runner.invoke(link_app, ["repair", "--all"])
        assert result.exit_code == 0, result.output
        assert "skipped 1 link(s)" in result.output
        assert "foreign qdisc otto did not create" in result.output

    def test_a_partial_repair_names_what_it_could_not_reach_and_exits_1(self) -> None:
        """ "I did not look" is not a clean bill of health.

        The headline is deliberately not a green "repaired" with a warning
        under it: the headline is the part an operator reads, and a repair that
        could not look at one end has not repaired the link.
        """
        from otto.link import RepairReport

        report = RepairReport(
            link_id="lnk-abc",
            cleared=[Placement("test1", "bbeth-1350", FlowDirection.A_TO_B)],
            unreachable=[
                "bb1350_qemu/eth0: link references host 'bb1350_qemu' not in the loaded lab"
            ],
        )
        with (
            patch("otto.cli.link.get_lab", return_value=object()),
            patch("otto.cli.link.repair_link", AsyncMock(return_value=report)),
        ):
            result = runner.invoke(link_app, ["repair", "edge"])
        assert result.exit_code == 1, result.output
        assert "partially repaired" in result.output
        assert "test1/bbeth-1350" in result.output
        assert "could not reach" in result.output
        assert "bb1350_qemu/eth0" in result.output

    def test_a_fully_reached_repair_still_says_repaired_and_exits_0(self) -> None:
        """The discriminator for the test above: the partial rendering must not
        become the only rendering."""
        from otto.link import RepairReport

        report = RepairReport(
            link_id="lnk-abc",
            cleared=[Placement("test1", "bbeth-1350", FlowDirection.A_TO_B)],
        )
        with (
            patch("otto.cli.link.get_lab", return_value=object()),
            patch("otto.cli.link.repair_link", AsyncMock(return_value=report)),
        ):
            result = runner.invoke(link_app, ["repair", "edge"])
        assert result.exit_code == 0, result.output
        assert "partially" not in result.output
        assert "repaired lnk-abc" in result.output


class TestListCommand:
    def test_rows_and_partial_scan_warning(self) -> None:
        from otto.link import DirectionState

        state = LinkState(
            link=LINK,
            impairable=True,
            unreachable=False,
            by_direction={
                FlowDirection.A_TO_B: DirectionState(whole=ImpairmentParams(delay_ms=50.0)),
                FlowDirection.B_TO_A: DirectionState(),
            },
        )
        down = LinkState(
            link=INPATH,
            impairable=True,
            unreachable=True,
            by_direction={FlowDirection.A_TO_B: None, FlowDirection.B_TO_A: None},
        )
        with (
            patch("otto.cli.link.get_lab", return_value=object()),
            patch("otto.cli.link.read_link_states", AsyncMock(return_value=[state, down])),
        ):
            result = runner.invoke(link_app, ["list"])
        assert result.exit_code == 0
        assert "delay 50ms" in result.output
        assert "partial scan" in result.output

    @staticmethod
    def _list_output(state: LinkState) -> str:
        with (
            patch("otto.cli.link.get_lab", return_value=object()),
            patch("otto.cli.link.read_link_states", AsyncMock(return_value=[state])),
        ):
            result = runner.invoke(link_app, ["list"])
        assert result.exit_code == 0, result.output
        return result.output

    def test_unimpairable_row_states_the_reason_once(self) -> None:
        """Every implicit link lands in this branch, so on a lab that declares
        no links the whole table used to be a column of bare `n/a`.

        Once, not in both direction cells: the live refusals are full
        sentences ("...it is the management interface otto reaches the host
        through (self-lockout)") and printing them twice on one soft-wrapped
        line is unreadable."""
        reason = "'zephyr37-fat' has no named interface"
        output = self._list_output(
            LinkState(
                link=LINK,
                impairable=False,
                unreachable=False,
                by_direction={},
                refusal=reason,
            )
        )
        assert f"not impairable: {reason}" in output
        assert output.count(reason) == 1
        assert "a->b: n/a" in output

    def test_brackets_in_user_data_survive_every_row(self) -> None:
        """`eth0[dataplane]` is a legal netdev name and rich reads `[dataplane]`
        as a style tag, printing `eth0` — an interface that does not exist, in
        the message whose only job is to name the one at fault.

        Nothing validates a link `name`, a host id or an interface against
        `[`. Same hazard as 1fbef92c, one column over; the negative control
        below is what keeps this from being a tautology."""
        bracketed = Link(
            a=LinkEndpoint(host="gw", interface="eth0[dataplane]"),
            b=LinkEndpoint(host="dut", interface="eth1"),
            name="wan[primary]",
        )
        output = self._list_output(
            LinkState(
                link=bracketed,
                impairable=False,
                unreachable=False,
                by_direction={},
                refusal="refusing to impair 'eth0[dataplane]' on 'gw' — [bold] mgmt",
            )
        )
        assert "wan[primary]" in output
        assert "eth0[dataplane]" in output
        assert "[bold] mgmt" in output

        # The partial-scan warning keeps markup ON — its emphasis is otto's own
        # — so its interpolated ids are escaped instead. Same exposure, other
        # remedy; both need proving.
        output = self._list_output(
            LinkState(
                link=bracketed,
                impairable=True,
                unreachable=True,
                by_direction={FlowDirection.A_TO_B: None, FlowDirection.B_TO_A: None},
            )
        )
        # On the WARNING line, not merely somewhere in the output — the row
        # above prints the same id through the markup=False path, and asserting
        # on the whole capture passes with the escape removed.
        (warning,) = [ln for ln in output.splitlines() if "partial scan" in ln]
        assert "wan[primary]" in warning

        # Negative control: rich really does eat these when markup is on.
        console = get_console()
        with console.capture() as cap:
            console.print("eth0[dataplane]", soft_wrap=True)
        assert "eth0[dataplane]" not in cap.get()

    def test_unimpairable_row_without_a_reason_prints_no_extra_row(self) -> None:
        """`refusal` defaults to None, and a LinkState built by anything but
        `read_link_state` (a third-party caller, a future backend) must not render
        the string `None` at the user — nor lose its row, which is why the
        `n/a` cells are asserted rather than just the absence of `None`."""
        output = self._list_output(
            LinkState(link=LINK, impairable=False, unreachable=False, by_direction={})
        )
        assert "a->b: n/a" in output
        assert "b->a: n/a" in output
        assert "not impairable" not in output
        assert "None" not in output


from otto.link import DirectionState, Selector


class TestScopedCli:
    def test_impair_with_port_passes_selector(self) -> None:
        report = ImpairReport(link_id="lnk-abc", applied=[])
        mock = AsyncMock(return_value=report)
        with (
            patch("otto.cli.link.get_lab", return_value=object()),
            patch("otto.cli.link.impair_link", mock),
        ):
            result = runner.invoke(
                link_app,
                ["impair", "edge", "--delay", "200", "--port", "5201", "--proto", "tcp"],
            )
        assert result.exit_code == 0, result.output
        assert mock.call_args.kwargs["selector"] == Selector(5201, "tcp")

    def test_impair_report_row_includes_selector(self) -> None:
        report = ImpairReport(
            link_id="lnk-abc",
            applied=[
                AppliedPlacement(
                    Placement("test1", "eth1.100", FlowDirection.A_TO_B),
                    ImpairmentParams(delay_ms=200.0),
                    Selector(5201, "tcp"),
                ),
            ],
        )
        with (
            patch("otto.cli.link.get_lab", return_value=object()),
            patch("otto.cli.link.impair_link", AsyncMock(return_value=report)),
        ):
            result = runner.invoke(link_app, ["impair", "edge", "--delay", "200", "--port", "5201"])
        assert "test1/eth1.100: 5201/tcp delay 200ms" in result.output

    def test_proto_without_port_is_usage_error(self) -> None:
        result = runner.invoke(link_app, ["impair", "edge", "--delay", "1", "--proto", "tcp"])
        assert result.exit_code == 2
        assert "--proto needs --port" in result.output

    def test_bad_proto_is_usage_error(self) -> None:
        result = runner.invoke(
            link_app, ["impair", "edge", "--delay", "1", "--port", "80", "--proto", "icmp"]
        )
        assert result.exit_code == 2

    def test_impair_range_and_side_pass_one_selector(self) -> None:
        mock = AsyncMock(return_value=ImpairReport(link_id="lnk-abc", applied=[]))
        with (
            patch("otto.cli.link.get_lab", return_value=object()),
            patch("otto.cli.link.impair_link", mock),
        ):
            result = runner.invoke(
                link_app,
                [
                    "impair",
                    "edge",
                    "--delay",
                    "5",
                    "--port",
                    "5000:5010",
                    "--proto",
                    "tcp",
                    "--side",
                    "dst",
                ],
            )
        assert result.exit_code == 0, result.output
        assert mock.call_args.kwargs["selector"] == Selector(5000, "tcp", end=5010, side="dst")

    def test_bad_port_text_is_a_usage_error_naming_it(self) -> None:
        result = runner.invoke(link_app, ["impair", "edge", "--delay", "1", "--port", "5010:5000"])
        assert result.exit_code == 2
        assert "5010:5000" in result.output

    def test_side_without_port_is_a_usage_error(self) -> None:
        result = runner.invoke(link_app, ["impair", "edge", "--delay", "1", "--side", "dst"])
        assert result.exit_code == 2
        assert "--side needs --port" in result.output

    def test_bad_side_is_a_usage_error(self) -> None:
        result = runner.invoke(
            link_app, ["impair", "edge", "--delay", "1", "--port", "80", "--side", "both"]
        )
        assert result.exit_code == 2

    def test_collision_refusal_surfaces_as_a_failure(self) -> None:
        refusal = ValueError("5205/tcp collides with 5200:5220/tcp")  # short: Rich wraps at 80 cols
        with (
            patch("otto.cli.link.get_lab", return_value=object()),
            patch("otto.cli.link.impair_link", AsyncMock(side_effect=refusal)),
        ):
            result = runner.invoke(
                link_app,
                ["impair", "edge", "--delay", "1", "--port", "5205", "--proto", "tcp"],
            )
        assert result.exit_code != 0
        assert "collides with 5200:5220/tcp" in result.output

    def test_repair_with_port_passes_selector(self) -> None:
        from otto.link import RepairReport

        mock = AsyncMock(return_value=RepairReport("lnk-abc"))
        with (
            patch("otto.cli.link.get_lab", return_value=object()),
            patch("otto.cli.link.repair_link", mock),
        ):
            result = runner.invoke(link_app, ["repair", "edge", "--port", "53", "--proto", "udp"])
        assert result.exit_code == 0, result.output
        assert mock.call_args.kwargs["selector"] == Selector(53, "udp")

    def test_repair_all_with_port_is_usage_error(self) -> None:
        result = runner.invoke(link_app, ["repair", "--all", "--port", "53"])
        assert result.exit_code == 2

    def test_repair_range_and_side_and_n_colon_n(self) -> None:
        from otto.link import RepairReport

        mock = AsyncMock(return_value=RepairReport("lnk-abc"))
        with (
            patch("otto.cli.link.get_lab", return_value=object()),
            patch("otto.cli.link.repair_link", mock),
        ):
            result = runner.invoke(
                link_app, ["repair", "edge", "--port", "5000:5010", "--side", "src"]
            )
            assert result.exit_code == 0, result.output
            assert mock.call_args.kwargs["selector"] == Selector(5000, end=5010, side="src")
            result = runner.invoke(link_app, ["repair", "edge", "--port", "5000:5000"])
            assert mock.call_args.kwargs["selector"] == Selector(5000)

    def test_repair_side_without_port_is_a_usage_error(self) -> None:
        result = runner.invoke(link_app, ["repair", "edge", "--side", "dst"])
        assert result.exit_code == 2
        assert "--side needs --port" in result.output

    def test_list_renders_selector_rows_and_foreign(self) -> None:
        scoped = LinkState(
            link=LINK,
            impairable=True,
            unreachable=False,
            by_direction={
                FlowDirection.A_TO_B: DirectionState(
                    scoped={
                        Selector(5201, "tcp"): ImpairmentParams(delay_ms=200.0),
                        Selector(53, "udp"): ImpairmentParams(loss_pct=5.0),
                    }
                ),
                FlowDirection.B_TO_A: DirectionState(foreign=True),
            },
        )
        with (
            patch("otto.cli.link.get_lab", return_value=object()),
            patch("otto.cli.link.read_link_states", AsyncMock(return_value=[scoped])),
        ):
            result = runner.invoke(link_app, ["list"])
        assert result.exit_code == 0, result.output
        assert "a->b: port-scoped (2)" in result.output
        assert "b->a: foreign qdisc — not otto's" in result.output
        assert "  a->b  53/udp  loss 5%" in result.output
        assert "  a->b  5201/tcp  delay 200ms" in result.output
        # rows sort by (port, last, proto, side): 53/udp before 5201/tcp, not insertion order
        assert result.output.index("53/udp") < result.output.index("5201/tcp")

    def test_list_rows_sort_by_port_then_last_then_proto_then_side(self) -> None:
        scoped = LinkState(
            link=LINK,
            impairable=True,
            unreachable=False,
            by_direction={
                FlowDirection.A_TO_B: DirectionState(
                    scoped={
                        Selector(6000, "tcp"): ImpairmentParams(delay_ms=1.0),
                        Selector(5000, end=5010, side="src"): ImpairmentParams(delay_ms=2.0),
                        Selector(5000, side="dst"): ImpairmentParams(delay_ms=3.0),
                    }
                ),
            },
        )
        with (
            patch("otto.cli.link.get_lab", return_value=object()),
            patch("otto.cli.link.read_link_states", AsyncMock(return_value=[scoped])),
        ):
            result = runner.invoke(link_app, ["list"])
        assert result.exit_code == 0, result.output
        out = result.output
        assert out.index("5000 dst") < out.index("5000:5010 src") < out.index("6000/tcp")

    def test_list_distinguishes_a_failed_read_from_an_unreachable_host(self) -> None:
        """ "?" and "!" are different news and get different summary lines.

        A host that answered and failed the read must not be listed under
        "could not fully read" — that is the network-fault story, and it is
        the wrong place to send someone whose host simply has no working tc.
        """
        broken = LinkState(
            link=LINK,
            impairable=True,
            unreachable=False,
            by_direction={FlowDirection.A_TO_B: None},
            read_errors={
                FlowDirection.A_TO_B: ("'tc qdisc show dev eth1.100' failed on 'test1': not found")
            },
        )
        with (
            patch("otto.cli.link.get_lab", return_value=object()),
            patch("otto.cli.link.read_link_states", AsyncMock(return_value=[broken])),
        ):
            result = runner.invoke(link_app, ["list"])
        assert result.exit_code == 0, result.output
        assert "a->b: !" in result.output
        assert "read failed (a->b): 'tc qdisc show dev eth1.100' failed on 'test1'" in result.output
        assert "host reachable, read command failed" in result.output
        assert "partial scan" not in result.output

    def test_list_gives_each_direction_its_own_cell_and_story(self) -> None:
        """One endpoint down, the other's tc broken — the shape a link-wide
        read_error string could not render: `unreachable` is per LINK, so it
        claimed "?" for both cells."""
        mixed = LinkState(
            link=LINK,
            impairable=True,
            unreachable=True,
            by_direction={FlowDirection.A_TO_B: None, FlowDirection.B_TO_A: None},
            read_errors={FlowDirection.B_TO_A: "'tc qdisc show' failed on 'test2': nope"},
        )
        with (
            patch("otto.cli.link.get_lab", return_value=object()),
            patch("otto.cli.link.read_link_states", AsyncMock(return_value=[mixed])),
        ):
            result = runner.invoke(link_app, ["list"])
        assert result.exit_code == 0, result.output
        assert "a->b: ?  b->a: !" in result.output
        assert "read failed (b->a): 'tc qdisc show' failed on 'test2'" in result.output
        # Both summary lines fire — the link really is both things at once.
        assert "partial scan" in result.output
        assert "host reachable, read command failed" in result.output

    def test_list_prints_a_whole_link_read_failure_once_not_per_direction(self) -> None:
        """Placement resolution failing records the SAME message under both
        directions; these are full sentences and one of them is enough."""
        both = LinkState(
            link=LINK,
            impairable=True,
            read_errors={
                FlowDirection.A_TO_B: "'ip -o addr show' failed on 'test1': nope",
                FlowDirection.B_TO_A: "'ip -o addr show' failed on 'test1': nope",
            },
        )
        with (
            patch("otto.cli.link.get_lab", return_value=object()),
            patch("otto.cli.link.read_link_states", AsyncMock(return_value=[both])),
        ):
            result = runner.invoke(link_app, ["list"])
        assert result.output.count("ip -o addr show' failed on 'test1'") == 1
        assert "read failed (a->b, b->a):" in result.output

    def test_list_still_marks_an_unreachable_host_with_a_question_mark(self) -> None:
        gone = LinkState(
            link=LINK,
            impairable=True,
            unreachable=True,
            by_direction={FlowDirection.A_TO_B: None},
        )
        with (
            patch("otto.cli.link.get_lab", return_value=object()),
            patch("otto.cli.link.read_link_states", AsyncMock(return_value=[gone])),
        ):
            result = runner.invoke(link_app, ["list"])
        assert "a->b: ?" in result.output
        assert "partial scan" in result.output
        assert "read failed" not in result.output


def _check_host(
    host_id: str = "test1", *, sandbox: list[FeatureResult] | None = None
) -> LinkCheckHost:
    return LinkCheckHost(
        host_id=host_id,
        placements=[Placement(host_id, "eth1.100", FlowDirection.A_TO_B)],
        fingerprint=None,
        range_labels={},
        sandbox=list(sandbox) if sandbox is not None else [],
        live=[],
        swept=[],
    )


def _check_report(
    *,
    hosts: list[LinkCheckHost] | None = None,
    features: list[str] | None = None,
    refusal: str | None = None,
    refusal_hint: str | None = None,
    dry_run_plan: list[str] | None = None,
    live_swept: list[str] | None = None,
) -> LinkCheckReport:
    hosts = hosts if hosts is not None else []
    # `link_sections` builds one row per `features` entry, looking each up by
    # name in the host's sandbox results — so a report whose `features` don't
    # name the rows a test put in `hosts` renders every row as the row-less
    # "n/a", the exact trap this default guards against.
    if features is None:
        named = list(dict.fromkeys(r.feature for h in hosts for r in h.sandbox))
        features = named or ["read-back"]
    return LinkCheckReport(
        link_id="lnk-abc",
        link_name="edge",
        live_requested=False,
        features=features,
        hosts=hosts,
        refusal=refusal,
        refusal_hint=refusal_hint,
        dry_run_plan=dry_run_plan if dry_run_plan is not None else [],
        live_swept=live_swept if live_swept is not None else [],
    )


class TestCheckCommand:
    def test_check_all_pass_exits_0_and_prints_the_table(self) -> None:
        host = _check_host(sandbox=[FeatureResult("read-back", Verdict.PASS)])
        report = _check_report(hosts=[host])
        with (
            patch("otto.cli.link.get_lab", return_value=object()),
            patch("otto.link.check.check_link", AsyncMock(return_value=report)),
        ):
            result = runner.invoke(link_app, ["check", "edge"])
        assert result.exit_code == 0, result.output
        assert "read-back" in result.output
        assert "pass" in result.output
        assert "test1" in result.output

    def test_check_any_fail_exits_1(self) -> None:
        host = _check_host(sandbox=[FeatureResult("delay", Verdict.FAIL)])
        report = _check_report(hosts=[host])
        with (
            patch("otto.cli.link.get_lab", return_value=object()),
            patch("otto.link.check.check_link", AsyncMock(return_value=report)),
        ):
            result = runner.invoke(link_app, ["check", "edge"])
        assert result.exit_code == 1, result.output
        assert "fail" in result.output

    def test_check_unmeasured_only_exits_0(self) -> None:
        host = _check_host(
            sandbox=[
                FeatureResult("rate", Verdict.UNMEASURED, reason=UnmeasuredReason.MISSING_TOOL)
            ]
        )
        report = _check_report(hosts=[host])
        with (
            patch("otto.cli.link.get_lab", return_value=object()),
            patch("otto.link.check.check_link", AsyncMock(return_value=report)),
        ):
            result = runner.invoke(link_app, ["check", "edge"])
        assert result.exit_code == 0, result.output
        assert "unmeasured" in result.output

    def test_check_unknown_feature_exits_2_naming_valid_features(self) -> None:
        """``--feature`` validation runs BEFORE ``check_link`` (real, unmocked
        ``requested_features``), so an unknown name never reaches it at all —
        the mock records zero calls, not just a matching exit code."""
        mock = AsyncMock()
        with (
            patch("otto.cli.link.get_lab", return_value=object()),
            patch("otto.link.check.check_link", mock),
        ):
            result = runner.invoke(link_app, ["check", "edge", "--feature", "bogus"])
        assert result.exit_code == 2, result.output
        assert "unknown feature" in result.output
        assert "'bogus'" in result.output
        assert "valid: read-back" in result.output
        mock.assert_not_called()

    def test_check_unknown_link_from_check_link_exits_1(self) -> None:
        """The sibling case to the test above: ``check_link`` raises
        ``ValueError`` for plenty of things that are NOT a bad ``--feature``
        (an unknown link, a bad ``--from`` host, a lab host it can't find) —
        those are the check's own RESULT, exit 1, same as `impair`/`repair`'s
        `except (ValueError, RuntimeError)`, not a usage error."""
        error = ValueError("no link 'nope' in this lab; known: edge, mgmt-edge")
        with (
            patch("otto.cli.link.get_lab", return_value=object()),
            patch("otto.link.check.check_link", AsyncMock(side_effect=error)),
        ):
            result = runner.invoke(link_app, ["check", "nope"])
        assert result.exit_code == 1, result.output
        assert "no link 'nope' in this lab" in result.output

    def test_check_feature_naming_no_features_exits_2(self) -> None:
        """``--feature ""``/``--feature ","`` parse to an empty list, which
        must not silently become "just run read-back" — that's indistinguishable
        from an operator's genuine mistake."""
        for given in ("", ","):
            result = runner.invoke(link_app, ["check", "edge", "--feature", given])
            assert result.exit_code == 2, (given, result.output)
            assert "--feature named no features" in result.output

    def test_check_refusal_exits_1_with_hint(self) -> None:
        report = _check_report(
            refusal="'edge' has no named interface",
            refusal_hint="declare this link in lab.json with an interface on each endpoint",
        )
        with (
            patch("otto.cli.link.get_lab", return_value=object()),
            patch("otto.link.check.check_link", AsyncMock(return_value=report)),
        ):
            result = runner.invoke(link_app, ["check", "edge"])
        assert result.exit_code == 1, result.output
        assert "cannot check link lnk-abc" in result.output
        assert "no named interface" in result.output
        assert "declare this link in lab.json" in result.output

    def test_check_report_writes_json_only_when_asked(self, tmp_path, monkeypatch) -> None:
        host = _check_host(sandbox=[FeatureResult("read-back", Verdict.PASS)])
        report = _check_report(hosts=[host])
        dest = tmp_path / "out.json"
        with (
            patch("otto.cli.link.get_lab", return_value=object()),
            patch("otto.link.check.check_link", AsyncMock(return_value=report)),
        ):
            result = runner.invoke(link_app, ["check", "edge", "--report", str(dest)])
        assert result.exit_code == 0, result.output
        assert dest.exists()
        payload = json.loads(dest.read_text())
        assert payload["schema"] == "otto-check/1"
        assert payload["kind"] == "link"
        assert str(dest) in result.output

        # `dest` was never passed to the second invoke, so an assertion about
        # SOME other named path is vacuous — it can never fail. `chdir` into
        # `tmp_path` instead and compare its whole listing before/after: any
        # write anywhere under it, named or not, moves that listing.
        before = sorted(tmp_path.iterdir())
        monkeypatch.chdir(tmp_path)
        with (
            patch("otto.cli.link.get_lab", return_value=object()),
            patch("otto.link.check.check_link", AsyncMock(return_value=report)),
        ):
            result = runner.invoke(link_app, ["check", "edge"])
        assert result.exit_code == 0, result.output
        assert sorted(tmp_path.iterdir()) == before, (
            "check without --report wrote something under the cwd"
        )
        assert "report:" not in result.output

    def test_check_report_write_failure_exits_1_no_traceback(self, tmp_path) -> None:
        host = _check_host(sandbox=[FeatureResult("read-back", Verdict.PASS)])
        report = _check_report(hosts=[host])
        dest = tmp_path / "missing-dir" / "out.json"
        with (
            patch("otto.cli.link.get_lab", return_value=object()),
            patch("otto.link.check.check_link", AsyncMock(return_value=report)),
        ):
            result = runner.invoke(link_app, ["check", "edge", "--report", str(dest)])
        assert result.exit_code == 1, result.output
        assert f"cannot write report {dest}" in result.output
        assert "Traceback" not in result.output

    def test_check_refusal_still_writes_the_report(self, tmp_path) -> None:
        """A refusal IS the check's result — `--report` gets one for it too,
        the same `LinkCheckReport` JSON shape the success path writes."""
        dest = tmp_path / "out.json"
        report = _check_report(
            refusal="'edge' has no named interface",
            refusal_hint="declare this link in lab.json with an interface on each endpoint",
        )
        with (
            patch("otto.cli.link.get_lab", return_value=object()),
            patch("otto.link.check.check_link", AsyncMock(return_value=report)),
        ):
            result = runner.invoke(link_app, ["check", "edge", "--report", str(dest)])
        assert result.exit_code == 1, result.output
        assert dest.exists()
        payload = json.loads(dest.read_text())
        assert payload["result"]["refusal"] == "'edge' has no named interface"
        assert str(dest) in result.output

    def test_check_dry_run_with_report_writes_nothing_and_says_so(self, tmp_path) -> None:
        """A dry run measures nothing, so `--report` must not write a file
        that LOOKS like a real result — and must not go silent about why."""
        dest = tmp_path / "out.json"
        plan = ["placement a->b on test1/eth1.100", "no device was contacted"]
        report = _check_report(dry_run_plan=plan)
        with (
            patch("otto.cli.link.get_lab", return_value=object()),
            patch("otto.link.check.check_link", AsyncMock(return_value=report)),
        ):
            result = runner.invoke(link_app, ["check", "edge", "--report", str(dest)])
        assert result.exit_code == 0, result.output
        assert not dest.exists()
        assert "no report was written" in result.output

    def test_check_dry_run_of_a_refused_link_writes_no_report(self, tmp_path) -> None:
        """A refusal is built BEFORE the dry-run plan, so a refused link reaches
        the refusal branch with an empty plan. Under a dry run it must still
        write nothing: the dry run measured nothing, refusal or not."""
        dest = tmp_path / "out.json"
        report = _check_report(
            refusal="'edge' has no named interface",
            refusal_hint="declare this link in lab.json with an interface on each endpoint",
        )
        with (
            active_context(dry_run=True),
            patch("otto.cli.link.get_lab", return_value=object()),
            patch("otto.link.check.check_link", AsyncMock(return_value=report)),
        ):
            result = runner.invoke(link_app, ["check", "edge", "--report", str(dest)])
        assert result.exit_code == 1, result.output
        assert "cannot check link lnk-abc" in result.output
        assert not dest.exists()
        assert "no report was written — a dry run measures nothing" in result.output

    def test_check_refusal_and_live_swept_survive_brackets_end_to_end(self) -> None:
        """A refusal/hint and a `--live` sweep line both carry text a real
        netdev/namespace name can legally contain (`eth0[dataplane]`, a legal
        netdev name rich reads as a style tag and eats). Both are asserted
        through `runner.invoke`'s REAL render path — refusal via
        `print_error`, live_swept via `link_sections`/`render_sections` — not
        via an isolated helper call that could pass while the wiring rots."""
        refusal_report = _check_report(
            refusal="cannot impair 'eth0[dataplane]' on 'gw' — it is the management interface",
            refusal_hint="use a non-management interface, not eth0[dataplane]",
        )
        with (
            patch("otto.cli.link.get_lab", return_value=object()),
            patch("otto.link.check.check_link", AsyncMock(return_value=refusal_report)),
        ):
            result = runner.invoke(link_app, ["check", "edge"])
        assert result.exit_code == 1, result.output
        # Twice, not just "somewhere in the output": the refusal message and
        # the hint each print it through their own `print_error` call, and
        # counting both separately keeps one surviving line from masking the
        # other's regression.
        assert result.output.count("eth0[dataplane]") == 2, result.output

        host = _check_host(sandbox=[FeatureResult("read-back", Verdict.PASS)])
        swept_report = _check_report(
            hosts=[host], live_swept=["swept otto-check-eth0[dataplane] from an earlier run"]
        )
        with (
            patch("otto.cli.link.get_lab", return_value=object()),
            patch("otto.link.check.check_link", AsyncMock(return_value=swept_report)),
        ):
            result = runner.invoke(link_app, ["check", "edge"])
        assert result.exit_code == 0, result.output
        assert "otto-check-eth0[dataplane]" in result.output

    def test_check_passes_live_feature_and_from_through(self) -> None:
        mock = AsyncMock(return_value=_check_report())
        with (
            patch("otto.cli.link.get_lab", return_value=object()),
            patch("otto.link.check.check_link", mock),
        ):
            result = runner.invoke(
                link_app,
                ["check", "edge", "--live", "--feature", "delay, loss", "--from", "test1"],
            )
        assert result.exit_code == 0, result.output
        assert mock.call_args.kwargs["live"] is True
        assert mock.call_args.kwargs["features"] == ["delay", "loss"]
        assert mock.call_args.kwargs["from_host"] == "test1"

    def test_check_dry_run_prints_the_plan_and_exits_0(self) -> None:
        plan = [
            "placement a->b on test1/eth1.100",
            "no device was contacted — nothing was measured",
        ]
        report = _check_report(dry_run_plan=plan)
        with (
            patch("otto.cli.link.get_lab", return_value=object()),
            patch("otto.link.check.check_link", AsyncMock(return_value=report)),
        ):
            result = runner.invoke(link_app, ["check", "edge"])
        assert result.exit_code == 0, result.output
        assert "dry run" in result.output
        assert "placement a->b on test1/eth1.100" in result.output
        assert "no device was contacted" in result.output

    def test_check_host_unreachable_exits_1_with_no_traceback(self) -> None:
        """Every host-side error ``check_link`` raises subclasses ``RuntimeError``
        (``CheckHostUnreachableError``, ``CheckCommandFailedError``,
        ``LinkHostUnreachableError``, ``LinkCommandFailedError``), so the plain
        ``except RuntimeError`` below must turn a down host into the clean
        host-named exit 1 the dispatch seam prints — never a traceback."""
        error = CheckHostUnreachableError("'tc qdisc show' timed out on 'test1'")
        with (
            patch("otto.cli.link.get_lab", return_value=object()),
            patch("otto.link.check.check_link", AsyncMock(side_effect=error)),
        ):
            result = runner.invoke(link_app, ["check", "edge"])
        assert result.exit_code == 1, result.output
        assert "'tc qdisc show' timed out on 'test1'" in result.output
        assert "Traceback" not in result.output


class TestCompleter:
    def test_link_completer_filters_prefix(self) -> None:
        with (
            patch("otto.cli.link.get_repos", return_value=[]),
            patch(
                "otto.cli.link.collect_link_ids",
                return_value=["edge", "dataplane", "lnk-1"],
            ),
        ):
            assert _link_completer(None, "e") == ["edge"]


class TestDryRunRendering:
    """The CLI must never render a preview as a result.

    `test_cli.py` invokes `link_app` directly, so the ROOT `--dry-run` flag is
    not parseable here — the context is installed around the invoke instead,
    which is where the flag would have put it anyway (`main`'s callback →
    `OttoContext(dry_run=...)`).

    The library calls are real (no `AsyncMock`): the whole property under test
    is that `impair_link`/`repair_link` hand back a plan and the renderer
    branches on it, and a patched return value would let either half rot.
    """

    @staticmethod
    def _dry_invoke(lab, args: list[str]):
        with (
            active_context(dry_run=True),
            patch("otto.cli.link.get_lab", return_value=lab),
        ):
            return runner.invoke(link_app, args)

    def test_impair_prints_the_plan_and_no_green_impaired_line(self) -> None:
        lab, *_ = _bed()
        result = self._dry_invoke(lab, ["impair", "edge", "--delay", "50"])
        assert result.exit_code == 0, result.output
        assert "dry run" in result.output
        assert "would: a->b on test1/eth1.100: tc qdisc replace" in result.output
        assert "not checked: " in result.output
        assert "impaired " not in result.output, (
            f"a dry run reported an impairment as applied: {result.output}"
        )

    def test_impair_names_the_lockout_refusals_it_could_not_make(self) -> None:
        """The headline finding, at the surface an operator actually reads."""
        lab, *_ = _bed(link=MGMT_LINK)
        result = self._dry_invoke(lab, ["impair", "mgmt-edge", "--delay", "50"])
        assert result.exit_code == 0, result.output
        assert "self-lockout" in result.output
        assert "management address" in result.output

    def test_repair_never_says_cleared_nothing_to_clear(self) -> None:
        lab, *_ = _bed()
        result = self._dry_invoke(lab, ["repair", "edge"])
        assert result.exit_code == 0, result.output
        assert "would: test1/eth1.100: tc qdisc del" in result.output
        assert "repaired " not in result.output
        assert "nothing to clear" not in result.output
        assert "timers cancelled 0" not in result.output.replace(
            "a dry run reporting `timers cancelled 0`", ""
        ), "the fabricated count reached the console outside the sentence disowning it"

    def test_repair_all_does_not_report_repaired_n_links(self) -> None:
        lab, *_ = _bed()
        result = self._dry_invoke(lab, ["repair", "--all"])
        assert result.exit_code == 0, result.output
        assert "previewed 1 link(s)" in result.output
        assert "repaired 1 link(s)" not in result.output

    def test_a_sweep_that_previews_nothing_still_says_it_was_dry(self) -> None:
        """The vacuous shape: a dry sweep whose output is byte-identical to a real one.

        Every IMPLICIT link is structurally refused, so on a lab that declares
        none the whole sweep lands in `skipped` and previews nothing. A banner
        gated on `sweep.planned` having content then falls through to
        `repaired 0 link(s)` — true, mute, and indistinguishable from the real
        sweep asserted below, which is the exact shape this commit exists to
        remove.
        """
        bare = Link(
            a=LinkEndpoint(host="test1", ip="10.10.201.11"),
            b=LinkEndpoint(host="test2", ip="10.10.202.12"),
            name="bare-edge",
        )
        dry = self._dry_invoke(_bed(link=bare)[0], ["repair", "--all"])
        assert dry.exit_code == 0, dry.output
        assert "dry run" in dry.output
        assert "repaired 0 link(s)" not in dry.output
        assert "no named interface" in dry.output, (
            "the skip itself is lab data and must still be reported"
        )

        with patch("otto.cli.link.get_lab", return_value=_bed(link=bare)[0]):
            real = runner.invoke(link_app, ["repair", "--all"])
        assert "repaired 0 link(s)" in real.output, (
            "the control: without it, a renderer that had lost the success line "
            "entirely would satisfy the assertions above"
        )
        assert "dry run" not in real.output
        assert dry.output != real.output, (
            "the two runs printed the same bytes, which is the whole defect"
        )

    def test_a_real_repair_all_still_reports_repaired_n_links(self) -> None:
        """The positive control for the branch above: without it, a renderer
        that had lost the success line entirely would pass."""
        from otto.link import RepairAllReport, RepairReport

        sweep = RepairAllReport(repaired=[RepairReport("lnk-abc")])
        with (
            patch("otto.cli.link.get_lab", return_value=object()),
            patch("otto.cli.link.repair_all", AsyncMock(return_value=sweep)),
        ):
            result = runner.invoke(link_app, ["repair", "--all"])
        assert "repaired 1 link(s)" in result.output
        assert "dry run" not in result.output

    def test_list_says_not_read_rather_than_showing_a_clean_link(self) -> None:
        lab, *_ = _bed()
        result = self._dry_invoke(lab, ["list"])
        assert result.exit_code == 0, result.output
        assert "a->b: not read  b->a: not read" in result.output
        assert "dry run" in result.output
        assert "management-interface and hop-transit refusals" in result.output
        assert "partial scan" not in result.output, "not_measured is not unreachable"
        assert "read failed" not in result.output, "not_measured is not a failed read"

    def test_a_real_list_of_a_clean_link_still_shows_the_dash(self) -> None:
        """The control that makes the assertion above about the DRY RUN.

        `-` is the clean cell and it is also the `never read` cell, and that
        overload is precisely how a dry run used to report every endpoint-mode
        link as unimpaired. Both spellings have to be observed on the same bed
        for either to mean anything.
        """
        lab, test1, test2, _ = _bed()
        test1.qdisc_texts = [""]
        test2.qdisc_texts = [""]
        with patch("otto.cli.link.get_lab", return_value=lab):
            result = runner.invoke(link_app, ["list"])
        assert result.exit_code == 0, result.output
        assert "a->b: -  b->a: -" in result.output
        assert "not read" not in result.output
        assert "dry run" not in result.output

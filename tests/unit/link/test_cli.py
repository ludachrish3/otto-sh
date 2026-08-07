"""``otto link`` CLI: impair/repair/list rendering + completion.

Commands are plain ``async def`` leaves bridged by the leaf-invoke wrapper,
so these tests drive ``link_app`` through the production dispatch seam
(``DispatchRunner``) rather than a bare ``CliRunner``.
"""

from unittest.mock import AsyncMock, patch

from rich import get_console

from otto.cli.link import _link_completer, link_app
from otto.link import (
    AppliedPlacement,
    FlowDirection,
    ImpairmentParams,
    ImpairReport,
    LinkState,
    Placement,
)
from otto.link.model import Link, LinkEndpoint
from tests._fixtures.dispatch import DispatchRunner

from .test_manage_impair import INPATH, LINK

runner = DispatchRunner()


class TestImpairCommand:
    def test_happy_path_prints_placements(self) -> None:
        report = ImpairReport(
            link_id="lnk-abc",
            applied=[
                AppliedPlacement(
                    Placement("carrot_seed", "eth1.100", FlowDirection.A_TO_B),
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
        assert "carrot_seed/eth1.100" in result.output

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
            skipped=["lnk-abc: carrot_seed/eth1.100 has a foreign qdisc otto did not create"]
        )
        with (
            patch("otto.cli.link.get_lab", return_value=object()),
            patch("otto.cli.link.repair_all", AsyncMock(return_value=sweep)),
        ):
            result = runner.invoke(link_app, ["repair", "--all"])
        assert result.exit_code == 0, result.output
        assert "skipped 1 link(s)" in result.output
        assert "foreign qdisc otto did not create" in result.output


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
        reason = "'sprout' has no named interface"
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
        `_link_state` (a third-party caller, a future backend) must not render
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
                    Placement("carrot_seed", "eth1.100", FlowDirection.A_TO_B),
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
        assert "carrot_seed/eth1.100: 5201/tcp delay 200ms" in result.output

    def test_proto_without_port_is_usage_error(self) -> None:
        result = runner.invoke(link_app, ["impair", "edge", "--delay", "1", "--proto", "tcp"])
        assert result.exit_code == 2
        assert "--proto needs --port" in result.output

    def test_bad_proto_is_usage_error(self) -> None:
        result = runner.invoke(
            link_app, ["impair", "edge", "--delay", "1", "--port", "80", "--proto", "icmp"]
        )
        assert result.exit_code == 2

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
        # rows sort by (port, proto): 53/udp before 5201/tcp, not insertion order
        assert result.output.index("53/udp") < result.output.index("5201/tcp")

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
                FlowDirection.A_TO_B: (
                    "'tc qdisc show dev eth1.100' failed on 'carrot_seed': not found"
                )
            },
        )
        with (
            patch("otto.cli.link.get_lab", return_value=object()),
            patch("otto.cli.link.read_link_states", AsyncMock(return_value=[broken])),
        ):
            result = runner.invoke(link_app, ["list"])
        assert result.exit_code == 0, result.output
        assert "a->b: !" in result.output
        assert (
            "read failed (a->b): 'tc qdisc show dev eth1.100' failed on 'carrot_seed'"
            in result.output
        )
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
            read_errors={FlowDirection.B_TO_A: "'tc qdisc show' failed on 'tomato_seed': nope"},
        )
        with (
            patch("otto.cli.link.get_lab", return_value=object()),
            patch("otto.cli.link.read_link_states", AsyncMock(return_value=[mixed])),
        ):
            result = runner.invoke(link_app, ["list"])
        assert result.exit_code == 0, result.output
        assert "a->b: ?  b->a: !" in result.output
        assert "read failed (b->a): 'tc qdisc show' failed on 'tomato_seed'" in result.output
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
                FlowDirection.A_TO_B: "'ip -o addr show' failed on 'carrot_seed': nope",
                FlowDirection.B_TO_A: "'ip -o addr show' failed on 'carrot_seed': nope",
            },
        )
        with (
            patch("otto.cli.link.get_lab", return_value=object()),
            patch("otto.cli.link.read_link_states", AsyncMock(return_value=[both])),
        ):
            result = runner.invoke(link_app, ["list"])
        assert result.output.count("ip -o addr show' failed on 'carrot_seed'") == 1
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

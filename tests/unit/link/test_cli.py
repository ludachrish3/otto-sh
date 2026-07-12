"""``otto link`` CLI: impair/repair/list rendering + completion."""

from unittest.mock import AsyncMock, patch

from typer.testing import CliRunner

from otto.cli.link import _link_completer, link_app
from otto.link import (
    AppliedPlacement,
    FlowDirection,
    ImpairmentParams,
    ImpairReport,
    LinkState,
    Placement,
)

from .test_manage_impair import INPATH, LINK

runner = CliRunner()


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
        with (
            patch("otto.cli.link.get_lab", return_value=object()),
            patch(
                "otto.cli.link.repair_all",
                AsyncMock(return_value=([], ["lnk-abc: host down"])),
            ),
        ):
            result = runner.invoke(link_app, ["repair", "--all"])
        assert result.exit_code == 1
        assert "lnk-abc: host down" in result.output


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

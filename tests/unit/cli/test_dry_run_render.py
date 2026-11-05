"""One ``--dry-run`` plan renderer and one ``check --dry-run`` renderer for link and tunnel (#844).

``otto link`` and ``otto tunnel`` print the same two previews. Each command
reaches them through its own entry, so a test drives both entries and the
renderer itself with one input and compares lines (one decision, one place).
The renderers are imported inside each test, so a missing one fails that test
alone instead of the whole file at collection.
"""

from unittest.mock import AsyncMock, patch

import pytest

from otto.cli import link as link_cli
from otto.cli import tunnel as tunnel_cli
from otto.link import DryRunPlan as LinkPlan
from otto.link import ImpairReport
from otto.link.check import LinkCheckReport
from otto.tunnel import AddedTunnel, Tunnel, TunnelHop
from otto.tunnel import DryRunPlan as TunnelPlan
from tests._fixtures.dispatch import DispatchRunner
from tests._fixtures.literal_text import HOSTILE, assert_one_verbatim_line
from tests.conftest import active_context

runner = DispatchRunner()


def _plan_lines(out: str) -> list[str]:
    return [line for line in out.splitlines() if line.strip()]


def _invoke_link_impair(header: str, would: list[str], unchecked: list[str]) -> str:
    report = ImpairReport(link_id=header, plan=LinkPlan(would=would, unchecked=unchecked))
    with (
        active_context(dry_run=True),
        patch("otto.fleet.get_lab", return_value=object()),
        patch("otto.cli.link.impair_link", AsyncMock(return_value=report)),
    ):
        result = runner.invoke(link_cli.link_app, ["impair", "edge", "--delay", "50"])
    assert result.exit_code == 0, result.output
    return result.output


def _invoke_tunnel_add(header: str, would: list[str], unchecked: list[str]) -> str:
    # The tunnel header is built from the path's end hosts; the caller's
    # header must be that spelling.
    assert header == "test1 <-> test2"
    tunnel = Tunnel(
        protocol="tcp",
        service_port=161,
        path=(TunnelHop(host="test1"), TunnelHop(host="test2")),
    )
    added = AddedTunnel(
        tunnel=tunnel,
        carrier_fwd=None,
        carrier_rev=None,
        plan=TunnelPlan(would=would, unchecked=unchecked),
    )
    with (
        active_context(dry_run=True),
        patch("otto.fleet.get_lab", return_value=object()),
        patch("otto.cli.tunnel.add_tunnel", AsyncMock(return_value=added)),
    ):
        result = runner.invoke(
            tunnel_cli.tunnel_app, ["add", "--hosts", "test1,test2", "--port", "161"]
        )
    assert result.exit_code == 0, result.output
    return result.output


@pytest.mark.parametrize(
    ("invoke", "header"),
    [
        pytest.param(_invoke_link_impair, "lnk-1", id="link"),
        pytest.param(_invoke_tunnel_add, "test1 <-> test2", id="tunnel"),
    ],
)
def test_dry_run_plan_output_is_the_shared_renderers(invoke, header, capsys):
    from otto.cli.invoke import print_dry_run_plan

    out = invoke(header, [HOSTILE], ["x"])
    print_dry_run_plan(header, [HOSTILE], ["x"])
    direct = capsys.readouterr().out
    assert _plan_lines(out) == _plan_lines(direct)


def _invoke_link_check(header: str, lines: list[str]) -> str:
    report = LinkCheckReport(
        link_id=header,
        link_name="edge",
        live_requested=False,
        features=["read-back"],
        hosts=[],
        refusal=None,
        refusal_hint=None,
        dry_run_plan=lines,
        live_swept=[],
    )
    with (
        patch("otto.fleet.get_lab", return_value=object()),
        patch("otto.link.check.check_link", AsyncMock(return_value=report)),
    ):
        result = runner.invoke(link_cli.link_app, ["check", "edge"])
    assert result.exit_code == 0, result.output
    return result.output


def _invoke_tunnel_check(header: str, lines: list[str]) -> str:
    from tests.unit.tunnel.test_cli import _check_report

    report = _check_report(path=[header], dry_run_plan=lines)
    with (
        patch("otto.fleet.get_lab", return_value=object()),
        patch("otto.tunnel.check.check_tunnel", AsyncMock(return_value=report)),
    ):
        result = runner.invoke(
            tunnel_cli.tunnel_app, ["check", "--hosts", "test1,test2", "--port", "161"]
        )
    assert result.exit_code == 0, result.output
    return result.output


@pytest.mark.parametrize(
    "invoke",
    [pytest.param(_invoke_link_check, id="link"), pytest.param(_invoke_tunnel_check, id="tunnel")],
)
def test_check_dry_run_output_is_the_shared_renderer(invoke, capsys):
    from otto.cli.invoke import print_check_dry_run

    out = invoke("lnk-1", [HOSTILE, "second"])
    print_check_dry_run("lnk-1", [HOSTILE, "second"])
    direct = capsys.readouterr().out
    assert _plan_lines(out) == _plan_lines(direct)


def test_dry_run_renderers_print_hostile_text_verbatim(monkeypatch, capsys):
    from otto.cli.invoke import print_check_dry_run, print_dry_run_plan

    monkeypatch.setenv("COLUMNS", "40")
    print_dry_run_plan(HOSTILE, [HOSTILE], ["x"])
    print_check_dry_run(HOSTILE, [HOSTILE])
    out = capsys.readouterr().out
    assert_one_verbatim_line(
        out,
        f"dry run {HOSTILE}: no device was contacted — nothing was read and nothing was changed",
    )
    assert_one_verbatim_line(out, f"  would: {HOSTILE}")
    assert_one_verbatim_line(out, f"dry run {HOSTILE}:")
    assert_one_verbatim_line(out, f"  {HOSTILE}")


@pytest.mark.parametrize("module", [link_cli, tunnel_cli], ids=["link", "tunnel"])
def test_no_dry_run_twins_remain(module):
    assert not hasattr(module, "_print_dry_run_plan")
    assert not hasattr(module, "_print_check_dry_run")

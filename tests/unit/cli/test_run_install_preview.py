"""``otto -n run install|uninstall|install-tools`` prints the plan before the dry-run block."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from otto.host.product import ProductPlan
from otto.project.plan import HostPlan, ProductPlanEntry, RepoPlan
from tests.unit.cli.conftest import _flat
from tests.unit.cli.test_project_instruction_commands import _publish_the_six


@pytest.fixture(autouse=True)
def _published():
    """Otto's project instructions, ``install`` among them, published."""
    _publish_the_six()


LONG_COMMAND = "tar -xzf /opt/stage/agent.tar.gz -C /opt/agent && " + "/opt/agent/install.sh " * 6
assert len(LONG_COMMAND) > 100


def _canned(name, ctx, kwargs):
    del ctx, kwargs
    return [
        RepoPlan(
            "repo1",
            [
                HostPlan(
                    "test1",
                    [
                        ProductPlanEntry(
                            "agent",
                            ProductPlan(
                                stage=["PUT build/agent.tar.gz -> /opt/stage"],
                                install=[
                                    "sh install",
                                    "echo [bold]x[/bold] :fire:",
                                    LONG_COMMAND,
                                ],
                            ),
                        )
                    ],
                )
            ],
        )
    ]


def test_dry_run_install_prints_the_plan_then_the_standard_block(run_cli):
    with patch("otto.project.plan.plan_instruction", side_effect=_canned) as planned:
        dry = run_cli(["run", "install"], dry_run=True)
    assert dry.exit_code == 0, dry.output
    planned.assert_called_once()
    assert planned.call_args.args[0] == "install"
    out = dry.output
    assert "    stage    agent  PUT build/agent.tar.gz -> /opt/stage" in out
    # Verbatim: no markup read, no emoji, no hard wrap of a long line.
    assert "echo [bold]x[/bold] :fire:" in out
    assert f"  {LONG_COMMAND}\n" in out
    assert out.index("repo1") < out.index("dry run: no command body was run")
    # The harness has no ``otto`` root group, so the command path starts at ``run``.
    assert "would run: run install" in _flat(out)


@pytest.mark.parametrize("name", ["uninstall", "install-tools"])
def test_the_other_two_previewed_verbs_plan_under_their_own_name(run_cli, name):
    with patch("otto.project.plan.plan_instruction", side_effect=_canned) as planned:
        dry = run_cli(["run", name], dry_run=True)
    assert dry.exit_code == 0, dry.output
    assert planned.call_args.args[0] == name


def test_dry_run_status_keeps_the_seam_default(run_cli):
    with patch("otto.project.plan.plan_instruction", side_effect=_canned) as planned:
        dry = run_cli(["run", "status"], dry_run=True)
    assert dry.exit_code == 0, dry.output
    planned.assert_not_called()


def test_a_real_run_never_plans(run_cli):
    with patch("otto.project.plan.plan_instruction", side_effect=_canned) as planned:
        run_cli(["run", "install"])
    planned.assert_not_called()


def test_no_repo_applying_prints_no_blank_line_before_the_block(run_cli):
    with patch("otto.project.plan.plan_instruction", return_value=[]):
        dry = run_cli(["run", "install"], dry_run=True)
    assert dry.exit_code == 0, dry.output
    assert not dry.output.startswith("\n"), repr(dry.output[:40])
    assert "\n\n\n" not in dry.output


def test_the_leaf_drives_the_real_planner_over_a_lab_without_contacting_the_host(
    monkeypatch, tmp_path
):
    """No patching of ``plan_instruction``: a shell product on a double host, end to end."""
    from otto.cli.run import run_app
    from otto.config.lab import Lab
    from otto.context import OttoContext, reset_context, set_context
    from otto.declared import DeclaredEntry
    from otto.host.product import PRODUCT_KINDS
    from tests._fixtures.dispatch import DispatchRunner
    from tests.unit.project.test_plan import _Host

    (tmp_path / "agent.tar.gz").write_bytes(b"x")
    entry = DeclaredEntry(
        name="agent",
        kind="shell",
        seam="products",
        owner="r1",
        base_dir=tmp_path,
        match={},
        params={"artifact": "agent.tar.gz", "stage_dir": "/opt/stage", "install": "sh install"},
    )
    host = _Host("test1")
    product = PRODUCT_KINDS.build([entry], host)[0]
    product.owner = "r1"
    host.products = [product]
    ctx = OttoContext(lab=Lab(name="bench", hosts={host.id: host}), dry_run=True)
    repos = [SimpleNamespace(name="r1", dependencies=[], project_scope=None, sut_dir=tmp_path)]
    monkeypatch.setattr("otto.config.bootstrapped.get_ordered_repos", lambda: repos)
    monkeypatch.setattr("otto.config.bootstrapped.get_repos", lambda: repos)
    token = set_context(ctx)
    try:
        dry = DispatchRunner().invoke(run_app, ["install"], async_leaves=True)
    finally:
        reset_context(token)
    assert dry.exit_code == 0, dry.output
    assert f"PUT {tmp_path}/agent.tar.gz -> /opt/stage" in dry.output
    assert "sh install" in dry.output
    assert dry.output.index("test1") < dry.output.index("dry run: no command body was run")

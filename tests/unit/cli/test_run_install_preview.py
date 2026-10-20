"""``otto -n run install|uninstall|cleanup|install-tools|uninstall-tools`` prints the plan first.

The plan comes before the dry-run block.
"""

from unittest.mock import patch

import pytest

from otto.host.product import ProductPlan
from otto.project.plan import HostPlan, ProductPlanEntry, ProjectPlan
from tests._fixtures.bootstrap_seam import patch_bootstrap
from tests._fixtures.fake_project import fake_project
from tests.unit.cli.conftest import _flat

LONG_COMMAND = "tar -xzf /opt/stage/agent.tar.gz -C /opt/agent && " + "/opt/agent/install.sh " * 6
assert len(LONG_COMMAND) > 100


def _canned(name, ctx, kwargs):
    del ctx, kwargs
    return [
        ProjectPlan(
            "project1",
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
    assert out.index("project1") < out.index("dry run: no command body was run")
    # The harness has no ``otto`` root group, so the command path starts at ``run``.
    assert "would run: run install" in _flat(out)


@pytest.mark.parametrize("name", ["uninstall", "cleanup", "install-tools", "uninstall-tools"])
def test_the_other_previewed_verbs_plan_under_their_own_name(run_cli, name):
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


def test_no_project_applying_prints_no_blank_line_before_the_block(run_cli):
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
    from otto.context import OttoContext, reset_context, set_context
    from otto.declared import DeclaredEntry
    from otto.host.product import PRODUCT_KIND_BUILDER
    from otto.invocation import RunPolicy
    from otto.labs.loader import Lab
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
    product = PRODUCT_KIND_BUILDER.build([entry], host)[0]
    product.owner = "r1"
    host.products = [product]
    ctx = OttoContext(lab=Lab(name="bench", hosts={host.id: host}), policy=RunPolicy(dry_run=True))
    projects = [fake_project("r1", project_scope=None, root=tmp_path)]
    patch_bootstrap(monkeypatch, projects)
    token = set_context(ctx)
    try:
        dry = DispatchRunner().invoke(run_app, ["install"], async_leaves=True)
    finally:
        reset_context(token)
    assert dry.exit_code == 0, dry.output
    assert f"PUT {tmp_path}/agent.tar.gz -> /opt/stage" in dry.output
    assert "sh install" in dry.output
    assert dry.output.index("test1") < dry.output.index("dry run: no command body was run")


def test_a_dry_run_cleanup_previews_every_removal_host_by_host(monkeypatch, tmp_path):
    """The real planner end to end: products, then dev tools, then the lab's own tail.

    A shell product and a scripted dev tool on a double host that fails if
    contacted; the cleanup body never runs.
    """
    from otto.cli.run import run_app
    from otto.context import OttoContext, reset_context, set_context
    from otto.declared import DeclaredEntry
    from otto.host.product import PRODUCT_KIND_BUILDER
    from otto.invocation import RunPolicy
    from otto.labs.loader import Lab
    from tests._fixtures.dispatch import DispatchRunner
    from tests.unit.project.test_plan import _Host, _plan, _Planned

    (tmp_path / "agent.tar.gz").write_bytes(b"x")
    entry = DeclaredEntry(
        name="agent",
        kind="shell",
        seam="products",
        owner="r1",
        base_dir=tmp_path,
        match={},
        params={
            "artifact": "agent.tar.gz",
            "stage_dir": "/opt/stage",
            "install": "sh install",
            "uninstall": "sh uninstall",
        },
    )
    host = _Host("test1", globs=["/var/log/agent.log"])
    product = PRODUCT_KIND_BUILDER.build([entry], host)[0]
    product.owner = "r1"
    host.products = [product]
    host.dev_tools = [_Planned("kmodcov", "r1", _plan(uninstall=["sudo rmmod otto_kmodcov"]))]
    ctx = OttoContext(lab=Lab(name="bench", hosts={host.id: host}), policy=RunPolicy(dry_run=True))
    patch_bootstrap(monkeypatch, [fake_project("r1", project_scope=None, root=tmp_path)])
    token = set_context(ctx)
    try:
        dry = DispatchRunner().invoke(run_app, ["cleanup"], async_leaves=True)
    finally:
        reset_context(token)
    assert dry.exit_code == 0, dry.output
    out = dry.output
    order = [
        "uninstall    agent    sh uninstall",
        "remove tool  kmodcov  sudo rmmod otto_kmodcov",
        "after every project",
        "debug logs   fetch /var/log/agent.log",
        "impairments  would attempt to clear otto-owned impairments",
        "tunnels      would remove otto tunnels",
        "dry run: no command body was run",
    ]
    positions = [out.index(line) for line in order]
    assert positions == sorted(positions), out

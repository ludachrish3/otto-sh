"""THE SPLIT-BRAIN GUARD: `otto run install --ensure --lab-env x` and an
`ensure("installed")` marker under a suite with `--lab-env x` must hand every
repo the SAME options instance. The two paths are different code (flat kwargs
vs. an instance matched by declaring class); this is the one test that can
turn red if either drifts -- and it does turn red both if the fixture path is
switched to defaults (verified when written: swap
`from_instance(SuiteOpts(lab_env="x"))` for `from_instance(None)` and watch it
fail on `lab_env`) and if it is switched to bare name matching instead of
declaring-class matching (verified when written: `SuiteOpts` declares its OWN
`variant`, distinct from `WidgetInstall`'s default -- a name-only match would
copy the suite's `variant` across and the loop below fails on `variant`).
"""

import dataclasses
from types import SimpleNamespace
from typing import Annotated

import pytest
import typer

from otto import options
from otto.cli.run import instruction
from otto.context import OttoContext
from otto.params import OptionsSource
from otto.project import (
    InstallOptions,
    InstallState,
    ProjectActions,
    ProjectStatus,
    orchestrator,
    register_project_actions,
)
from otto.project import actions as actions_mod
from otto.registry import registering_repo
from otto.result import Result
from otto.utils import Status


@options
class RepoBase:
    lab_env: Annotated[str, typer.Option(help="env")] = "staging"


@options
class WidgetInstall(RepoBase, InstallOptions):
    variant: Annotated[str, typer.Option(help="v")] = "field"


@options
class SuiteOpts(RepoBase):
    firmware: Annotated[str, typer.Option(help="fw")] = "latest"
    # OWN field, not shared with WidgetInstall by declaring class -- a
    # name-only match would wrongly copy this across; see the module
    # docstring's second proven red.
    variant: Annotated[str, typer.Option(help="suite's own variant")] = "suite-only"


class _FakeCtx:
    """OttoContext double — copied from test_orchestrator.py's ``_FakeCtx``.

    Only the two seams the orchestrator uses: ``for_repo`` (the real wrapper,
    borrowed rather than reimplemented) and an empty ``scopes`` mapping (the
    whole-lab fallback, the shape this single-repo lab runs under).
    """

    for_repo = OttoContext.for_repo

    def __init__(self, hosts):
        self.hosts = list(hosts)
        self.scopes = {}
        self.include_projects = ()
        self.exclude_projects = ()

    def all_hosts(self, _scope_owner=None):
        return iter(self.hosts)

    async def do_for_all_hosts(self, method, *args, _scope_owner=None, **kwargs):
        out = {}
        for host in self.hosts:
            try:
                out[host.id] = await method(host, *args, **kwargs)
            except Exception as exc:  # noqa: PERF203,BLE001 — mirrors do_for_all_hosts' capture
                out[host.id] = exc
        return out


def _wire_lab(monkeypatch, repo_names, ctx):
    """Point the orchestrator's two lookups at *repo_names* and *ctx*.

    Copied from test_orchestrator.py's ``_wire_lab`` (single-repo shape):
    every fake repo carries ``dependencies`` (empty here) and
    ``inventory_settings = {}`` -- the repo-double rule, since a Mock's
    truthy-but-empty attribute reads as a broken ``[inventory]`` declaration.
    """
    ordered = [
        SimpleNamespace(name=name, dependencies=[], inventory_settings={}) for name in repo_names
    ]
    monkeypatch.setattr("otto.config.get_ordered_repos", lambda: ordered)
    monkeypatch.setattr("otto.config.get_repos", lambda: ordered)
    monkeypatch.setattr("otto.context.get_context", lambda: ctx)
    return ordered


@pytest.fixture
def one_repo_lab(monkeypatch):
    """Wire a one-repo lab (``widget``) around the given actions class."""

    def wire(name, cls):
        del cls  # already attributed to *name* by ``register_project_actions``
        ctx = _FakeCtx([])
        _wire_lab(monkeypatch, [name], ctx)
        return ctx

    return wire


@pytest.fixture
def widget_lab(monkeypatch, one_repo_lab):
    """One repo 'widget' whose install records the options it was handed."""
    actions_mod.register_project_instruction_bodies(ProjectActions, None)
    seen: list = []

    with registering_repo("widget"):

        @register_project_actions
        class Widget(ProjectActions):
            @instruction(options=WidgetInstall)
            async def install(self, opts: WidgetInstall):
                seen.append(opts)
                return Result(Status.Success)

    one_repo_lab("widget", Widget)

    async def uninstalled(source):
        return ProjectStatus(
            overall=InstallState.UNINSTALLED, repos={"widget": InstallState.UNINSTALLED}, scoping={}
        )

    monkeypatch.setattr(orchestrator, "status", uninstalled)
    return seen


@pytest.mark.asyncio
async def test_cli_and_fixture_build_the_same_options(widget_lab) -> None:
    await orchestrator.run_project_instruction(
        "install", {"ensure": True, "recover_partial": True, "lab_env": "x", "variant": "field"}
    )
    await orchestrator.ensure_installed(OptionsSource.from_instance(SuiteOpts(lab_env="x")))
    assert len(widget_lab) == 2
    cli_opts, fixture_opts = widget_lab
    assert type(cli_opts) is WidgetInstall
    assert type(fixture_opts) is WidgetInstall
    # `ensure`/`recover_partial` are orchestration-ROUTING flags, not per-repo
    # content: `_install`'s own docstring says they "reach ensure_installed
    # unchanged" on the CLI path (kwargs flow straight through, `ensure` among
    # them), while the fixture path's source is a suite's own options instance
    # that never carries them at all -- so the two legitimately disagree there
    # regardless of split-brain. The guard this test exists for is that every
    # OTHER field -- what a repo's body actually reads -- lands identically,
    # `lab_env` (declared by both paths' shared base) included. Nothing else
    # may be added to this set: any field a body can read must be compared.
    routing_only = {"ensure", "recover_partial"}
    content_fields = [
        f.name for f in dataclasses.fields(WidgetInstall) if f.name not in routing_only
    ]
    for field in content_fields:
        assert getattr(cli_opts, field) == getattr(fixture_opts, field), field
    assert cli_opts.lab_env == "x"

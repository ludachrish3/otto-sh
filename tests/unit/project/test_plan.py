"""plan_instruction walks like the real run and turns what it cannot know into gaps."""

from types import SimpleNamespace

import pytest

from otto.context import OttoContext
from otto.host.host import BaseHost
from otto.host.product import ProductPlan
from otto.host.toolchain import Toolchain
from otto.project.actions import ProjectActions, register_project_actions
from otto.project.options import InstallOptions
from otto.project.plan import HostPlan, ProductPlanEntry, ProjectPlan, plan_instruction
from otto.registry import registering_project
from otto.result import Result
from otto.utils import Status
from tests._fixtures.bootstrap_seam import seed_scope_verdicts
from tests._fixtures.fake_project import fake_project


class _Planned:
    """A product/dev tool whose plan is scripted."""

    def __init__(self, name, owner, plan):
        self.name, self.owner, self._plan = name, owner, plan

    def plan(self, host):
        del host
        return self._plan


class _Host(BaseHost):
    """The smallest BaseHost: identity, attachments, and nothing overridden."""

    def __init__(self, host_id, products=(), dev_tools=(), globs=()):
        self.id = host_id
        self.products = list(products)
        self.dev_tools = list(dev_tools)
        self.debug_log_globs = list(globs)
        self.toolchain = Toolchain()

    async def _exec_one(self, *a, **k):  # pragma: no cover — never reached by a plan
        raise AssertionError("a plan contacted the host")


class _Overriding(_Host):
    async def install(self, stage_only=False, owner=None):  # pragma: no cover
        return Result(Status.Success)


class _Ctx:
    """OttoContext double: the lab's hosts, the projects and the project view seam the walk uses."""

    for_project = OttoContext.for_project
    _resolve_scopes = OttoContext._resolve_scopes  # what scopes_of(ctx) reads; seeded below

    def __init__(self, hosts, fleets=None, exclude_projects=()):
        self.hosts = list(hosts)
        self.lab = SimpleNamespace(hosts={h.id: h for h in self.hosts})
        seed_scope_verdicts(self, {})
        self.include_projects = ()
        self.exclude_projects = tuple(exclude_projects)
        # {project: [host ids]}: the project view's fleet, narrower than the lab's.
        self.fleets = fleets or {}

    def all_hosts(self, _scope_owner=None, **kw):
        del kw
        if _scope_owner in self.fleets:
            return iter([h for h in self.hosts if h.id in self.fleets[_scope_owner]])
        return iter(self.hosts)


def _wire(monkeypatch, project_names, ctx):
    ordered = [fake_project(n) for n in project_names]
    ctx.projects = ordered
    ctx.ordered_projects = ordered
    monkeypatch.setattr("otto.run_context.get_context", lambda: ctx)
    return ordered


def _plan(stage=(), install=(), uninstall=(), unchecked=()):
    return ProductPlan(list(stage), list(install), list(uninstall), list(unchecked))


def test_install_lists_every_owned_product_in_declaration_order_and_lifts_its_gaps(monkeypatch):
    agent = _Planned("agent", "r1", _plan(stage=["PUT a -> /s"], install=["sh a"]))
    kcov = _Planned("kcov", "r1", _plan(install=["sudo insmod k"], unchecked=["the login home"]))
    other = _Planned("theirs", "r2", _plan(install=["never"]))
    ctx = _Ctx([_Host("h1", [agent, kcov, other])])
    _wire(monkeypatch, ["r1"], ctx)
    plans = plan_instruction("install", ctx, {})
    assert plans == [
        ProjectPlan(
            "r1",
            [
                HostPlan(
                    "h1",
                    [
                        ProductPlanEntry("agent", agent.plan(None)),
                        ProductPlanEntry("kcov", kcov.plan(None)),
                    ],
                    ["kcov: the login home"],
                )
            ],
            [],
        )
    ]


def test_uninstall_walks_projects_in_reverse(monkeypatch):
    ctx = _Ctx([_Host("h1")])
    _wire(monkeypatch, ["r1", "r2"], ctx)
    assert [p.project for p in plan_instruction("uninstall", ctx, {})] == ["r2", "r1"]


def test_install_tools_lists_dev_tools_not_products(monkeypatch):
    tool = _Planned("helper", "r1", _plan(install=["sudo insmod helper.ko"]))
    product = _Planned("agent", "r1", _plan(install=["sh a"]))
    ctx = _Ctx([_Host("h1", products=[product], dev_tools=[tool])])
    _wire(monkeypatch, ["r1"], ctx)
    [project] = plan_instruction("install-tools", ctx, {})
    assert [e.name for e in project.hosts[0].products] == ["helper"]


def _override_gap(cls):
    return [
        (
            f"project `r1`: `{cls.__module__}.{cls.__qualname__}.install` replaces the default "
            "install; its steps are not previewed"
        )
    ]


def test_an_overriding_project_body_is_a_gap_not_a_guess(monkeypatch):
    from otto.cli.run import instruction

    with registering_project("r1"):

        @register_project_actions
        class _Mine(ProjectActions):
            @instruction(options=InstallOptions)
            async def install(self, opts: InstallOptions):  # pragma: no cover
                return Result(Status.Success)

    product = _Planned("agent", "r1", _plan(install=["sh a"]))
    ctx = _Ctx([_Host("h1", [product])])
    _wire(monkeypatch, ["r1"], ctx)
    [project] = plan_instruction("install", ctx, {})
    assert project.hosts == [HostPlan("h1", [], [])]
    assert project.gaps == _override_gap(_Mine)


def test_an_unmarked_override_is_a_gap_too(monkeypatch):
    # The subclass overrides ``install`` WITHOUT ``@instruction``: no body is attributed to
    # the project (``body_for`` finds otto's own), yet the real run dispatches to the override.
    class _Unmarked(ProjectActions):
        async def install(self, opts):  # pragma: no cover
            return Result(Status.Success)

    with registering_project("r1"):
        register_project_actions(_Unmarked, overwrite=True)
    product = _Planned("agent", "r1", _plan(install=["sh a"]))
    ctx = _Ctx([_Host("h1", [product])])
    _wire(monkeypatch, ["r1"], ctx)
    [project] = plan_instruction("install", ctx, {})
    assert project.hosts == [HostPlan("h1", [], [])]
    assert project.gaps == _override_gap(_Unmarked)


def test_an_overriding_host_class_is_a_gap_on_that_host(monkeypatch):
    product = _Planned("agent", "r1", _plan(install=["sh a"]))
    ctx = _Ctx([_Overriding("h1", [product]), _Host("h2", [product])])
    _wire(monkeypatch, ["r1"], ctx)
    [project] = plan_instruction("install", ctx, {})
    assert project.hosts[0] == HostPlan(
        "h1",
        [],
        [
            (
                f"`{_Overriding.__module__}.{_Overriding.__qualname__}.install` replaces the "
                "default; its steps are not previewed"
            )
        ],
    )
    assert [e.name for e in project.hosts[1].products] == ["agent"]


_ENSURE_RECOVER = (
    "whether the lab is already installed (--ensure skips the whole install when it is; "
    "a partial install is torn down first because --recover-partial is on)"
)
_ENSURE_OVER_PARTIAL = (
    "whether the lab is already installed (--ensure skips the whole install when it is; "
    "--no-recover-partial installs over a partial install as it stands)"
)


def test_ensure_is_a_project_gap_on_every_project(monkeypatch):
    ctx = _Ctx([_Host("h1")])
    _wire(monkeypatch, ["r1", "r2"], ctx)
    plans = plan_instruction("install", ctx, {"ensure": True})
    assert [p.gaps for p in plans] == [[_ENSURE_RECOVER], [_ENSURE_RECOVER]]
    assert [p.gaps for p in plan_instruction("install", ctx, {"ensure": False})] == [[], []]


def test_ensure_without_recover_partial_installs_over_a_partial_lab(monkeypatch):
    ctx = _Ctx([_Host("h1")])
    _wire(monkeypatch, ["r1"], ctx)
    [project] = plan_instruction("install", ctx, {"ensure": True, "recover_partial": False})
    assert project.gaps == [_ENSURE_OVER_PARTIAL]


def test_ensure_covers_a_project_whose_body_is_overridden(monkeypatch):
    class _Unmarked(ProjectActions):
        async def install(self, opts):  # pragma: no cover
            return Result(Status.Success)

    with registering_project("r1"):
        register_project_actions(_Unmarked, overwrite=True)
    ctx = _Ctx([_Host("h1")])
    _wire(monkeypatch, ["r1"], ctx)
    [project] = plan_instruction("install", ctx, {"ensure": True})
    assert project.gaps == [_ENSURE_RECOVER, *_override_gap(_Unmarked)]


def test_uninstall_log_flags_are_host_gaps(monkeypatch):
    ctx = _Ctx([_Host("h1", globs=["/var/log/*.log"])])
    _wire(monkeypatch, ["r1"], ctx)
    [project] = plan_instruction("uninstall", ctx, {"product_logs": True, "debug_logs": True})
    assert project.hosts[0].gaps == [
        (
            "product logs: each product's get_logs files and its debug_log_globs are collected "
            "before anything is removed"
        ),
        "debug logs: /var/log/*.log are swept once after every project",
    ]
    [quiet] = plan_instruction("uninstall", ctx, {"product_logs": False, "debug_logs": False})
    assert quiet.hosts[0].gaps == []


def test_an_instruction_without_a_preview_is_refused_by_name():
    with pytest.raises(ValueError, match="'status' has no install preview"):
        plan_instruction("status", _Ctx([]), {})


def test_debug_log_gap_with_no_globs_says_nothing_is_swept(monkeypatch):
    ctx = _Ctx([_Host("h1")])
    _wire(monkeypatch, ["r1"], ctx)
    [project] = plan_instruction("uninstall", ctx, {"product_logs": False, "debug_logs": True})
    assert project.hosts[0].gaps == ["debug logs: no globs declared; nothing is swept"]


def test_a_host_override_does_not_claim_the_default_log_haul(monkeypatch):
    class _OverridingUninstall(_Host):
        async def uninstall(self, *a, **k):  # pragma: no cover
            return Result(Status.Success)

    ctx = _Ctx([_OverridingUninstall("h1", globs=["/g"])])
    _wire(monkeypatch, ["r1"], ctx)
    [project] = plan_instruction("uninstall", ctx, {"product_logs": True, "debug_logs": True})
    [gap_override, gap_debug] = project.hosts[0].gaps
    assert "replaces the default" in gap_override
    assert gap_debug == "debug logs: /g are swept once after every project"


def test_the_debug_sweep_is_named_even_for_an_overridden_project(monkeypatch):
    class _Unmarked(ProjectActions):
        async def uninstall(self, opts):  # pragma: no cover
            return Result(Status.Success)

    with registering_project("r1"):
        register_project_actions(_Unmarked, overwrite=True)
    ctx = _Ctx([_Host("h1", globs=["/g"])])
    _wire(monkeypatch, ["r1"], ctx)
    [project] = plan_instruction("uninstall", ctx, {"product_logs": True, "debug_logs": True})
    assert project.hosts[0].gaps == ["debug logs: /g are swept once after every project"]


def test_install_tools_without_dev_lists_hosts_but_no_tools(monkeypatch):
    tool = _Planned("helper", "r1", _plan(install=["sudo insmod helper.ko"]))
    ctx = _Ctx([_Host("h1", dev_tools=[tool])])
    _wire(monkeypatch, ["r1"], ctx)
    [project] = plan_instruction("install-tools", ctx, {"dev": False})
    assert project.hosts == [HostPlan("h1", [], [])]


def test_install_tools_toolchain_is_a_host_gap(monkeypatch):
    tool = _Planned("helper", "r1", _plan(install=["sudo insmod helper.ko"]))
    ctx = _Ctx([_Host("h1", dev_tools=[tool])])
    _wire(monkeypatch, ["r1"], ctx)
    gap = (
        "toolchain tools: installed on this host once after every project (--toolchain); "
        "not previewed"
    )
    [project] = plan_instruction("install-tools", ctx, {"toolchain": True})
    assert project.hosts[0].gaps == [gap]
    assert [e.name for e in project.hosts[0].products] == ["helper"]
    [quiet] = plan_instruction("install-tools", ctx, {})
    assert quiet.hosts[0].gaps == []


def test_each_project_is_previewed_on_its_own_view_of_the_fleet(monkeypatch):
    ctx = _Ctx([_Host("h1"), _Host("h2")], fleets={"r2": ["h2"]})
    _wire(monkeypatch, ["r1", "r2"], ctx)
    r1, r2 = plan_instruction("install", ctx, {})
    assert [h.host_id for h in r1.hosts] == ["h1", "h2"]
    assert [h.host_id for h in r2.hosts] == ["h2"]


@pytest.mark.parametrize(
    ("instruction_name", "method"),
    [
        ("install", "stage"),
        ("install", "install"),
        ("uninstall", "uninstall"),
        ("install-tools", "install_dev_tools"),
        ("uninstall-tools", "uninstall_dev_tools"),
        ("cleanup", "uninstall"),
    ],
)
def test_every_dispatched_host_method_override_is_a_gap(monkeypatch, instruction_name, method):
    async def _replacement(self, *a, **k):  # pragma: no cover
        return Result(Status.Success)

    host_cls = type("_Replacing", (_Host,), {method: _replacement})
    ctx = _Ctx([host_cls("h1", [_Planned("agent", "r1", _plan(install=["sh a"]))])])
    _wire(monkeypatch, ["r1"], ctx)
    [project] = plan_instruction(
        instruction_name, ctx, {"product_logs": False, "debug_logs": False}
    )
    assert project.hosts == [
        HostPlan(
            "h1",
            [],
            [
                (
                    f"`{host_cls.__module__}.{host_cls.__qualname__}.{method}` replaces the "
                    "default; its steps are not previewed"
                )
            ],
        )
    ]


def test_a_host_overriding_stage_and_install_names_stage_first(monkeypatch):
    async def _replacement(self, *a, **k):  # pragma: no cover
        return Result(Status.Success)

    host_cls = type("_Both", (_Host,), {"stage": _replacement, "install": _replacement})
    ctx = _Ctx([host_cls("h1")])
    _wire(monkeypatch, ["r1"], ctx)
    [project] = plan_instruction("install", ctx, {})
    [gap] = project.hosts[0].gaps
    assert gap.startswith(f"`{host_cls.__module__}.{host_cls.__qualname__}.stage`")


def test_install_tools_walks_projects_forward(monkeypatch):
    ctx = _Ctx([_Host("h1")])
    _wire(monkeypatch, ["r1", "r2"], ctx)
    assert [p.project for p in plan_instruction("install-tools", ctx, {})] == ["r1", "r2"]


def test_a_switched_off_project_is_skipped_without_a_warning(monkeypatch, caplog):
    ctx = _Ctx([_Host("h1")], exclude_projects=["r2"])
    _wire(monkeypatch, ["r1", "r2"], ctx)
    with caplog.at_level("WARNING"):
        plans = plan_instruction("install", ctx, {})
    assert [p.project for p in plans] == ["r1"]
    assert not [r for r in caplog.records if "switched off" in r.getMessage()]


def test_no_dev_does_not_name_a_host_override_that_will_not_run(monkeypatch):
    async def _replacement(self, *a, **k):  # pragma: no cover
        return Result(Status.Success)

    host_cls = type("_ReplacingTools", (_Host,), {"install_dev_tools": _replacement})
    ctx = _Ctx([host_cls("h1")])
    _wire(monkeypatch, ["r1"], ctx)
    [project] = plan_instruction("install-tools", ctx, {"dev": False})
    assert project.hosts == [HostPlan("h1", [], [])]


def test_uninstall_tools_lists_dev_tools_removal_not_products(monkeypatch):
    tool = _Planned("helper", "r1", _plan(uninstall=["sudo rmmod helper"]))
    theirs = _Planned("their-tool", "r2", _plan(uninstall=["sudo rmmod theirs"]))
    product = _Planned("agent", "r1", _plan(uninstall=["sh u"]))
    ctx = _Ctx([_Host("h1", products=[product], dev_tools=[tool, theirs])])
    _wire(monkeypatch, ["r1"], ctx)
    [project] = plan_instruction("uninstall-tools", ctx, {})
    assert [e.name for e in project.hosts[0].dev_tools] == ["helper"]
    assert project.hosts[0].products == []


def test_uninstall_tools_walks_projects_in_reverse(monkeypatch):
    ctx = _Ctx([_Host("h1")])
    _wire(monkeypatch, ["r1", "r2"], ctx)
    assert [p.project for p in plan_instruction("uninstall-tools", ctx, {})] == ["r2", "r1"]


def test_uninstall_tools_without_dev_lists_hosts_but_no_tools(monkeypatch):
    tool = _Planned("helper", "r1", _plan(uninstall=["sudo rmmod helper"]))
    ctx = _Ctx([_Host("h1", dev_tools=[tool])])
    _wire(monkeypatch, ["r1"], ctx)
    [project] = plan_instruction("uninstall-tools", ctx, {"dev": False})
    assert project.hosts == [HostPlan("h1")]


def test_uninstall_tools_toolchain_names_each_rm(monkeypatch):
    from pathlib import Path

    from otto.host.toolchain import Toolchain, ToolchainTool

    host = _Host("h1")
    host.toolchain = Toolchain(
        tools=[ToolchainTool(name="gdb", source=Path("/x/arm-gdb"), dest=Path("/usr/bin"))]
    )
    ctx = _Ctx([host])
    _wire(monkeypatch, ["r1"], ctx)
    [project] = plan_instruction("uninstall-tools", ctx, {"toolchain": True})
    assert project.hosts[0].gaps == [
        "toolchain tools: removed once after every project (--toolchain): rm -f /usr/bin/gdb"
    ]
    [quiet] = plan_instruction("uninstall-tools", ctx, {})
    assert quiet.hosts[0].gaps == []


def test_uninstall_tools_toolchain_override_is_named_not_guessed(monkeypatch):
    from otto.host.toolchain import Toolchain

    async def _replacement(self, *a, **k):  # pragma: no cover
        return Result(Status.Success)

    host_cls = type("_OwnRemoval", (_Host,), {"remove_toolchain_tools": _replacement})
    host = host_cls("h1")
    host.toolchain = Toolchain()
    ctx = _Ctx([host])
    _wire(monkeypatch, ["r1"], ctx)
    [project] = plan_instruction("uninstall-tools", ctx, {"toolchain": True})
    assert project.hosts[0].gaps == [
        (
            f"toolchain tools: `{host_cls.__module__}.{host_cls.__qualname__}."
            "remove_toolchain_tools` replaces the default; its steps are not previewed"
        )
    ]


def test_the_previewable_names_are_exactly_the_ones_the_planner_walks():
    from otto.instruction_catalog import PREVIEWABLE_INSTRUCTIONS
    from otto.project.plan import _HOST_VERBS

    assert sorted(PREVIEWABLE_INSTRUCTIONS) == sorted(_HOST_VERBS)


# ── cleanup ──────────────────────────────────────────────────────────────

_LOGS_GAP = (
    "product logs: each product's get_logs files and its debug_log_globs are "
    "collected before anything is removed"
)


def _cleanup_host(host_cls=None, **kw):
    product = _Planned("agent", "r1", _plan(uninstall=["sh uninstall"]))
    tool = _Planned("kmodcov", "r1", _plan(uninstall=["sudo rmmod otto_kmodcov"]))
    theirs = _Planned("their-tool", "r2", _plan(uninstall=["never"]))
    return (host_cls or _Host)("h1", products=[product], dev_tools=[tool, theirs], **kw)


def test_cleanup_lists_each_projects_products_then_its_dev_tools(monkeypatch):
    ctx = _Ctx([_cleanup_host()])
    _wire(monkeypatch, ["r1"], ctx)
    [project] = plan_instruction("cleanup", ctx, {})
    [host] = project.hosts
    assert [e.name for e in host.products] == ["agent"]
    assert [e.name for e in host.dev_tools] == ["kmodcov"]
    assert host.gaps == [_LOGS_GAP]
    assert project.gaps == []


def test_cleanup_walks_projects_in_reverse(monkeypatch):
    ctx = _Ctx([_Host("h1")])
    _wire(monkeypatch, ["r1", "r2"], ctx)
    assert [p.project for p in plan_instruction("cleanup", ctx, {})] == ["r2", "r1"]


def test_cleanup_no_product_logs_drops_the_haul_gap(monkeypatch):
    ctx = _Ctx([_cleanup_host()])
    _wire(monkeypatch, ["r1"], ctx)
    [project] = plan_instruction("cleanup", ctx, {"product_logs": False})
    assert project.hosts[0].gaps == []


def test_an_uninstall_override_is_cleanups_product_half_only(monkeypatch):
    # cleanup's product half IS self.uninstall(...), so a project's uninstall
    # override replaces those steps; its dev-tool half is still otto's.
    class _OwnUninstall(ProjectActions):
        async def uninstall(self, opts):  # pragma: no cover
            return Result(Status.Success)

    with registering_project("r1"):
        register_project_actions(_OwnUninstall, overwrite=True)
    ctx = _Ctx([_cleanup_host()])
    _wire(monkeypatch, ["r1"], ctx)
    [project] = plan_instruction("cleanup", ctx, {})
    [host] = project.hosts
    assert host.products == []
    assert [e.name for e in host.dev_tools] == ["kmodcov"]
    assert host.gaps == []  # the override's log haul is its own
    cls = _OwnUninstall
    assert project.gaps == [
        (
            f"project `r1`: `{cls.__module__}.{cls.__qualname__}.uninstall` replaces the "
            "default uninstall, which cleanup calls; its steps are not previewed"
        )
    ]


def test_a_cleanup_override_is_both_halves(monkeypatch):
    class _OwnCleanup(ProjectActions):
        async def cleanup(self, opts):  # pragma: no cover
            return Result(Status.Success)

    with registering_project("r1"):
        register_project_actions(_OwnCleanup, overwrite=True)
    ctx = _Ctx([_cleanup_host()])
    _wire(monkeypatch, ["r1"], ctx)
    [project] = plan_instruction("cleanup", ctx, {})
    assert project.hosts == [HostPlan("h1")]
    cls = _OwnCleanup
    assert project.gaps == [
        (
            f"project `r1`: `{cls.__module__}.{cls.__qualname__}.cleanup` replaces the "
            "default cleanup; its steps are not previewed"
        )
    ]


@pytest.mark.parametrize(
    ("method", "previewed"), [("uninstall", "dev_tools"), ("uninstall_dev_tools", "products")]
)
def test_a_host_override_of_one_half_leaves_the_other_previewed(monkeypatch, method, previewed):
    async def _replacement(self, *a, **k):  # pragma: no cover
        return Result(Status.Success)

    host_cls = type("_OneHalf", (_Host,), {method: _replacement})
    ctx = _Ctx([_cleanup_host(host_cls)])
    _wire(monkeypatch, ["r1"], ctx)
    [project] = plan_instruction("cleanup", ctx, {"product_logs": False})
    [host] = project.hosts
    assert [e.name for e in getattr(host, previewed)]
    replaced = "dev_tools" if previewed == "products" else "products"
    assert getattr(host, replaced) == []
    assert host.gaps == [
        (
            f"`{host_cls.__module__}.{host_cls.__qualname__}.{method}` replaces the default; "
            "its steps are not previewed"
        )
    ]


def _tail_host(host_cls=None):
    from pathlib import Path

    from otto.host.toolchain import Toolchain, ToolchainTool

    host = (host_cls or _Host)("h1", globs=["/var/log/messages*"])
    host.toolchain = Toolchain(
        tools=[ToolchainTool(name="gdb", source=Path("/x/arm-gdb"), dest=Path("/usr/bin"))]
    )
    return host


def test_cleanup_tail_names_the_lab_wide_steps_in_order(monkeypatch):
    from otto.project.plan import TailStep, plan_lab_tail

    ctx = _Ctx([_tail_host(), _Host("h2")])
    _wire(monkeypatch, [], ctx)  # no project applies: the tail still runs
    assert plan_lab_tail("cleanup", ctx, {}) == [
        TailStep("debug logs", "fetch /var/log/messages*", "h1"),
        TailStep("toolchain", "rm -f /usr/bin/gdb", "h1"),
        TailStep(
            "impairments",
            "would attempt to clear otto-owned impairments on eligible links "
            "(live eligibility unchecked)",
        ),
        TailStep("tunnels", "would remove otto tunnels on every tunnel-capable host"),
    ]


def test_cleanup_tail_honours_each_flag(monkeypatch):
    from otto.project.plan import TailStep, plan_lab_tail

    ctx = _Ctx([_tail_host()])
    _wire(monkeypatch, [], ctx)
    flags = {"debug_logs": False, "reset_impairments": False, "remove_tunnels": False}
    assert plan_lab_tail("cleanup", ctx, flags) == [
        TailStep("toolchain", "rm -f /usr/bin/gdb", "h1")
    ]


def test_cleanup_tail_names_a_host_override_instead_of_guessing(monkeypatch):
    from otto.project.plan import TailStep, plan_lab_tail

    async def _replacement(self, *a, **k):  # pragma: no cover
        return Result(Status.Success)

    host_cls = type(
        "_OwnTail",
        (_Host,),
        {"get_debug_logs": _replacement, "remove_toolchain_tools": _replacement},
    )
    ctx = _Ctx([_tail_host(host_cls)])
    _wire(monkeypatch, [], ctx)
    flags = {"reset_impairments": False, "remove_tunnels": False}
    where = f"{host_cls.__module__}.{host_cls.__qualname__}"
    replaced = "replaces the default; not previewed"
    assert plan_lab_tail("cleanup", ctx, flags) == [
        TailStep("debug logs", f"`{where}.get_debug_logs` {replaced}", "h1"),
        TailStep("toolchain", f"`{where}.remove_toolchain_tools` {replaced}", "h1"),
    ]


def test_only_cleanup_has_a_lab_tail(monkeypatch):
    from otto.project.plan import plan_lab_tail

    ctx = _Ctx([_tail_host()])
    _wire(monkeypatch, [], ctx)
    for name in ("install", "uninstall", "install-tools", "uninstall-tools"):
        assert plan_lab_tail(name, ctx, {}) == []

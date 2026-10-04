"""plan_instruction walks like the real run and turns what it cannot know into gaps."""

from types import SimpleNamespace

import pytest

from otto.context import OttoContext
from otto.host.host import BaseHost
from otto.host.product import ProductPlan
from otto.project.actions import PROJECT_ACTIONS, ProjectActions, register_project_actions
from otto.project.options import InstallOptions
from otto.project.plan import HostPlan, ProductPlanEntry, RepoPlan, plan_instruction
from otto.registry import registering_repo
from otto.result import Result
from otto.utils import Status


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

    async def _exec_one(self, *a, **k):  # pragma: no cover — never reached by a plan
        raise AssertionError("a plan contacted the host")


class _Overriding(_Host):
    async def install(self, stage_only=False, owner=None):  # pragma: no cover
        return Result(Status.Success)


class _Ctx:
    """OttoContext double: the lab's hosts and the repo view seam the walk uses."""

    for_repo = OttoContext.for_repo

    def __init__(self, hosts, fleets=None, exclude_projects=()):
        self.hosts = list(hosts)
        self.lab = SimpleNamespace(hosts={h.id: h for h in self.hosts})
        self.scopes = {}
        self.include_projects = ()
        self.exclude_projects = tuple(exclude_projects)
        # {repo: [host ids]}: the repo view's fleet, narrower than the lab's.
        self.fleets = fleets or {}

    def all_hosts(self, _scope_owner=None, **kw):
        del kw
        if _scope_owner in self.fleets:
            return iter([h for h in self.hosts if h.id in self.fleets[_scope_owner]])
        return iter(self.hosts)


def _wire(monkeypatch, repo_names, ctx):
    ordered = [SimpleNamespace(name=n, dependencies=[]) for n in repo_names]
    monkeypatch.setattr("otto.config.bootstrapped.get_ordered_repos", lambda: ordered)
    monkeypatch.setattr("otto.config.bootstrapped.get_repos", lambda: ordered)
    monkeypatch.setattr("otto.context.get_context", lambda: ctx)
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
        RepoPlan(
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


def test_uninstall_walks_repos_in_reverse(monkeypatch):
    ctx = _Ctx([_Host("h1")])
    _wire(monkeypatch, ["r1", "r2"], ctx)
    assert [p.repo for p in plan_instruction("uninstall", ctx, {})] == ["r2", "r1"]


def test_install_tools_lists_dev_tools_not_products(monkeypatch):
    tool = _Planned("helper", "r1", _plan(install=["sudo insmod helper.ko"]))
    product = _Planned("agent", "r1", _plan(install=["sh a"]))
    ctx = _Ctx([_Host("h1", products=[product], dev_tools=[tool])])
    _wire(monkeypatch, ["r1"], ctx)
    [repo] = plan_instruction("install-tools", ctx, {})
    assert [e.name for e in repo.hosts[0].products] == ["helper"]


def _override_gap(cls):
    return [
        (
            f"repo `r1`: `{cls.__module__}.{cls.__qualname__}.install` replaces the default "
            "install; its steps are not previewed"
        )
    ]


def test_an_overriding_repo_body_is_a_gap_not_a_guess(monkeypatch):
    from otto.cli.run import instruction

    with registering_repo("r1"):

        @register_project_actions
        class _Mine(ProjectActions):
            @instruction(options=InstallOptions)
            async def install(self, opts: InstallOptions):  # pragma: no cover
                return Result(Status.Success)

    product = _Planned("agent", "r1", _plan(install=["sh a"]))
    ctx = _Ctx([_Host("h1", [product])])
    _wire(monkeypatch, ["r1"], ctx)
    [repo] = plan_instruction("install", ctx, {})
    assert repo.hosts == [HostPlan("h1", [], [])]
    assert repo.gaps == _override_gap(_Mine)


def test_an_unmarked_override_is_a_gap_too(monkeypatch):
    # The subclass overrides ``install`` WITHOUT ``@instruction``: no body is attributed to
    # the repo (``body_for`` finds otto's own), yet the real run dispatches to the override.
    class _Unmarked(ProjectActions):
        async def install(self, opts):  # pragma: no cover
            return Result(Status.Success)

    PROJECT_ACTIONS.register("r1", _Unmarked, overwrite=True, origin="test")
    product = _Planned("agent", "r1", _plan(install=["sh a"]))
    ctx = _Ctx([_Host("h1", [product])])
    _wire(monkeypatch, ["r1"], ctx)
    [repo] = plan_instruction("install", ctx, {})
    assert repo.hosts == [HostPlan("h1", [], [])]
    assert repo.gaps == _override_gap(_Unmarked)


def test_an_overriding_host_class_is_a_gap_on_that_host(monkeypatch):
    product = _Planned("agent", "r1", _plan(install=["sh a"]))
    ctx = _Ctx([_Overriding("h1", [product]), _Host("h2", [product])])
    _wire(monkeypatch, ["r1"], ctx)
    [repo] = plan_instruction("install", ctx, {})
    assert repo.hosts[0] == HostPlan(
        "h1",
        [],
        [
            (
                f"`{_Overriding.__module__}.{_Overriding.__qualname__}.install` replaces the "
                "default; its steps are not previewed"
            )
        ],
    )
    assert [e.name for e in repo.hosts[1].products] == ["agent"]


_ENSURE_RECOVER = (
    "whether the lab is already installed (--ensure skips the whole install when it is; "
    "a partial install is torn down first because --recover-partial is on)"
)
_ENSURE_OVER_PARTIAL = (
    "whether the lab is already installed (--ensure skips the whole install when it is; "
    "--no-recover-partial installs over a partial install as it stands)"
)


def test_ensure_is_a_repo_gap_on_every_repo(monkeypatch):
    ctx = _Ctx([_Host("h1")])
    _wire(monkeypatch, ["r1", "r2"], ctx)
    plans = plan_instruction("install", ctx, {"ensure": True})
    assert [p.gaps for p in plans] == [[_ENSURE_RECOVER], [_ENSURE_RECOVER]]
    assert [p.gaps for p in plan_instruction("install", ctx, {"ensure": False})] == [[], []]


def test_ensure_without_recover_partial_installs_over_a_partial_lab(monkeypatch):
    ctx = _Ctx([_Host("h1")])
    _wire(monkeypatch, ["r1"], ctx)
    [repo] = plan_instruction("install", ctx, {"ensure": True, "recover_partial": False})
    assert repo.gaps == [_ENSURE_OVER_PARTIAL]


def test_ensure_covers_a_repo_whose_body_is_overridden(monkeypatch):
    class _Unmarked(ProjectActions):
        async def install(self, opts):  # pragma: no cover
            return Result(Status.Success)

    PROJECT_ACTIONS.register("r1", _Unmarked, overwrite=True, origin="test")
    ctx = _Ctx([_Host("h1")])
    _wire(monkeypatch, ["r1"], ctx)
    [repo] = plan_instruction("install", ctx, {"ensure": True})
    assert repo.gaps == [_ENSURE_RECOVER, *_override_gap(_Unmarked)]


def test_uninstall_log_flags_are_host_gaps(monkeypatch):
    ctx = _Ctx([_Host("h1", globs=["/var/log/*.log"])])
    _wire(monkeypatch, ["r1"], ctx)
    [repo] = plan_instruction("uninstall", ctx, {"product_logs": True, "debug_logs": True})
    assert repo.hosts[0].gaps == [
        (
            "product logs: each product's get_logs files and its debug_log_globs are collected "
            "before anything is removed"
        ),
        "debug logs: /var/log/*.log are swept once after every repo",
    ]
    [quiet] = plan_instruction("uninstall", ctx, {"product_logs": False, "debug_logs": False})
    assert quiet.hosts[0].gaps == []


def test_an_instruction_without_a_preview_is_refused_by_name():
    with pytest.raises(ValueError, match="'status' has no install preview"):
        plan_instruction("status", _Ctx([]), {})


def test_debug_log_gap_with_no_globs_says_nothing_is_swept(monkeypatch):
    ctx = _Ctx([_Host("h1")])
    _wire(monkeypatch, ["r1"], ctx)
    [repo] = plan_instruction("uninstall", ctx, {"product_logs": False, "debug_logs": True})
    assert repo.hosts[0].gaps == ["debug logs: no globs declared; nothing is swept"]


def test_a_host_override_does_not_claim_the_default_log_haul(monkeypatch):
    class _OverridingUninstall(_Host):
        async def uninstall(self, *a, **k):  # pragma: no cover
            return Result(Status.Success)

    ctx = _Ctx([_OverridingUninstall("h1", globs=["/g"])])
    _wire(monkeypatch, ["r1"], ctx)
    [repo] = plan_instruction("uninstall", ctx, {"product_logs": True, "debug_logs": True})
    [gap_override, gap_debug] = repo.hosts[0].gaps
    assert "replaces the default" in gap_override
    assert gap_debug == "debug logs: /g are swept once after every repo"


def test_the_debug_sweep_is_named_even_for_an_overridden_repo(monkeypatch):
    class _Unmarked(ProjectActions):
        async def uninstall(self, opts):  # pragma: no cover
            return Result(Status.Success)

    PROJECT_ACTIONS.register("r1", _Unmarked, overwrite=True, origin="test")
    ctx = _Ctx([_Host("h1", globs=["/g"])])
    _wire(monkeypatch, ["r1"], ctx)
    [repo] = plan_instruction("uninstall", ctx, {"product_logs": True, "debug_logs": True})
    assert repo.hosts[0].gaps == ["debug logs: /g are swept once after every repo"]


def test_install_tools_without_dev_lists_hosts_but_no_tools(monkeypatch):
    tool = _Planned("helper", "r1", _plan(install=["sudo insmod helper.ko"]))
    ctx = _Ctx([_Host("h1", dev_tools=[tool])])
    _wire(monkeypatch, ["r1"], ctx)
    [repo] = plan_instruction("install-tools", ctx, {"dev": False})
    assert repo.hosts == [HostPlan("h1", [], [])]


def test_install_tools_toolchain_is_a_host_gap(monkeypatch):
    tool = _Planned("helper", "r1", _plan(install=["sudo insmod helper.ko"]))
    ctx = _Ctx([_Host("h1", dev_tools=[tool])])
    _wire(monkeypatch, ["r1"], ctx)
    gap = (
        "toolchain tools: installed on this host once after every repo (--toolchain); not previewed"
    )
    [repo] = plan_instruction("install-tools", ctx, {"toolchain": True})
    assert repo.hosts[0].gaps == [gap]
    assert [e.name for e in repo.hosts[0].products] == ["helper"]
    [quiet] = plan_instruction("install-tools", ctx, {})
    assert quiet.hosts[0].gaps == []


def test_each_repo_is_previewed_on_its_own_view_of_the_fleet(monkeypatch):
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
    ],
)
def test_every_dispatched_host_method_override_is_a_gap(monkeypatch, instruction_name, method):
    async def _replacement(self, *a, **k):  # pragma: no cover
        return Result(Status.Success)

    host_cls = type("_Replacing", (_Host,), {method: _replacement})
    ctx = _Ctx([host_cls("h1", [_Planned("agent", "r1", _plan(install=["sh a"]))])])
    _wire(monkeypatch, ["r1"], ctx)
    [repo] = plan_instruction(instruction_name, ctx, {"product_logs": False, "debug_logs": False})
    assert repo.hosts == [
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
    [repo] = plan_instruction("install", ctx, {})
    [gap] = repo.hosts[0].gaps
    assert gap.startswith(f"`{host_cls.__module__}.{host_cls.__qualname__}.stage`")


def test_install_tools_walks_repos_forward(monkeypatch):
    ctx = _Ctx([_Host("h1")])
    _wire(monkeypatch, ["r1", "r2"], ctx)
    assert [p.repo for p in plan_instruction("install-tools", ctx, {})] == ["r1", "r2"]


def test_a_switched_off_repo_is_skipped_without_a_warning(monkeypatch, caplog):
    ctx = _Ctx([_Host("h1")], exclude_projects=["r2"])
    _wire(monkeypatch, ["r1", "r2"], ctx)
    with caplog.at_level("WARNING"):
        plans = plan_instruction("install", ctx, {})
    assert [p.repo for p in plans] == ["r1"]
    assert not [r for r in caplog.records if "switched off" in r.getMessage()]


def test_no_dev_does_not_name_a_host_override_that_will_not_run(monkeypatch):
    async def _replacement(self, *a, **k):  # pragma: no cover
        return Result(Status.Success)

    host_cls = type("_ReplacingTools", (_Host,), {"install_dev_tools": _replacement})
    ctx = _Ctx([host_cls("h1")])
    _wire(monkeypatch, ["r1"], ctx)
    [repo] = plan_instruction("install-tools", ctx, {"dev": False})
    assert repo.hosts == [HostPlan("h1", [], [])]


def test_the_previewable_names_are_exactly_the_ones_the_planner_walks():
    from otto.instructions import PREVIEWABLE_INSTRUCTIONS
    from otto.project.plan import _HOST_VERBS

    assert sorted(PREVIEWABLE_INSTRUCTIONS) == sorted(_HOST_VERBS)

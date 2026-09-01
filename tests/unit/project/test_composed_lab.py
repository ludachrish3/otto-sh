"""The composed lab — real context, real hosts, real stamping.

Every other file under ``tests/unit/project`` drives duck-typed doubles: fast,
and right for the questions those files ask. This one asks the two questions
those doubles cannot answer, because a double is exactly what a wrong answer
here hides behind.

1. **Does a fleet walk honour a host CLASS override?**
   :meth:`otto.context.OttoContext.do_for_all_hosts` calls the function object
   it is handed (``method(host, ...)``), so handing it an unbound
   ``BaseHost.<verb>`` runs ``BaseHost``'s body whatever host is in front of
   it — and a class registered through ``register_host_class`` that overrides
   the verb (the design's override point #1, and the one the guides name: "a
   host family whose debug logs come out of journald overrides
   ``get_debug_logs``") is silently bypassed by every ``otto run`` verb while
   ``otto host <id> <verb>`` keeps honouring it. The table below asserts the
   override runs, once per walked verb.

2. **Do bootstrap-style owner stamping and per-repo scoping compose?** Spec
   section 8 asks for the two-repo attribution scenario end to end; the halves
   are pinned apart (marker observation in ``tests/unit/bootstrap``, stamping in
   ``tests/unit/host/test_product_providers.py``, scoping in
   ``test_actions.py``). This is the one place they run together.

SO THE SEAMS HERE ARE REAL, not doubles: :class:`otto.context.OttoContext` (its
own ``do_for_all_hosts`` and ``all_hosts``), concrete :class:`BaseHost`
subclasses (their own lifecycle verbs and owner filter), and
:func:`~otto.host.product.apply_product_providers` (the ingest chokepoint's own
stamping). Only the transport, the repo list, and the products' bodies are
stand-ins — a walk that reaches a transport is a walk that already proved the
point.
"""

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from otto import project
from otto.config.lab import Lab
from otto.context import OttoContext, set_context
from otto.host import product as product_mod
from otto.host.host import BaseHost
from otto.host.lab_info import LabInfo
from otto.host.product import Product, apply_product_providers, register_product_provider
from otto.host.toolchain import Toolchain
from otto.logger.mode import LogMode
from otto.registry import registering_repo
from otto.result import CommandResult, Result
from otto.utils import Status

# ── doubles ──────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _isolate_provider_registry():
    """Restore the product-provider list around each test in this file.

    The root conftest now snapshots this registry too, but per-test at this
    file's own seam stays deliberately: these tests register providers as part
    of their arrangement, and a leak between two tests IN this file would be
    masked by a guard that only fires at session scope.
    """
    saved = list(product_mod._PRODUCT_PROVIDERS)
    try:
        yield
    finally:
        product_mod._PRODUCT_PROVIDERS[:] = saved


class _FleetHost(BaseHost):
    """A concrete ``BaseHost`` with no transport — the real verbs, faked family hooks.

    The lifecycle verbs under test (``uninstall``'s ordering and owner filter,
    the toolchain probes, ``get_debug_logs``) are ``BaseHost``'s own; only the
    family hooks every host must supply are stubbed. Events land in a shared
    list so a whole lab's walk reads as one ordered transcript.
    """

    def __init__(self, host_id, events):
        self.id = host_id
        self.name = host_id
        self.log = LogMode.NORMAL
        self.lab_info = LabInfo()
        self.products = []
        self.dev_tools = []
        self.toolchain = Toolchain()
        self.power_control = None
        self.debug_log_globs = []
        self.events = events

    async def _exec_one(self, cmd, timeout, log=LogMode.NORMAL, user=None):
        del timeout, log, user
        self.events.append((self.id, f"exec:{cmd}"))
        return CommandResult(Status.Success, value="", command=cmd, retcode=0)

    async def put(self, src_files, dest_dir, mode=None):
        self.events.append((self.id, f"put:{src_files}"))
        del dest_dir, mode
        return Result(Status.Success)

    async def get(self, src_files, dest_dir):
        self.events.append((self.id, f"get:{src_files}"))
        del dest_dir
        return Result(Status.Success)

    @asynccontextmanager
    async def as_user(self, user="root", password=None):  # ty: ignore[invalid-overload]
        del password
        self.events.append((self.id, f"as_user:{user}"))
        yield self

    async def close(self):
        return None


class _JournaldHost(_FleetHost):
    """The guide's own example: a host family whose debug logs come out of journald.

    ``docs/guide/cli/run/defaults.md`` names this override by name as the sanctioned
    way to teach otto a host family's log story, so it is the honest subject for
    "does the lab-level sweep run it".
    """

    async def get_debug_logs(self, dest=None):
        del dest
        self.events.append((self.id, "journald"))
        return Result(Status.Success)


class _RecordingProduct(Product):
    """A real :class:`~otto.host.product.Product` whose lifecycle only records.

    Deliberately does NOT set ``owner``: the stamping seam is the subject, so
    the owner has to arrive from the provider's registering-repo marker.
    """

    def __init__(self, name, events):
        self.name = name
        self.events = events

    async def _note(self, verb, host):
        self.events.append((host.id, f"{verb}:{self.name}"))
        return Result(Status.Success)

    async def stage(self, host):
        return await self._note("stage", host)

    async def install(self, host):
        return await self._note("install", host)

    async def uninstall(self, host):
        return await self._note("uninstall", host)

    async def is_installed(self, host):
        self.events.append((host.id, f"is_installed:{self.name}"))
        return False


def _wire_lab(monkeypatch, tmp_path, repo_names, hosts, *, declarations=None):
    """Install a REAL context over *hosts* and point the orchestrator at *repo_names*.

    ``repo_names`` is bootstrap's resolved order (dependencies first), which is
    what ``get_ordered_repos`` hands back and what the teardown walks reversed.
    The context is set rather than patched in, so the walks run through the real
    ``all_hosts``/``do_for_all_hosts`` pair; the root conftest's
    ``_reset_otto_context`` restores the ContextVar afterwards. ``output_dir``
    keeps every ``log_dest`` under ``tmp_path``.

    The repo stubs carry ``project_scope=None`` unless *declarations* names one
    — no ``[project]`` table is what puts a walk on the whole-lab fallback
    (scoping spec §6), which is the shape most of this file tests: which host
    CLASS a walk dispatches through, with the fleet unnarrowed. ``sut_dir`` is
    read by the scope resolver only to name a file in an error message.

    ``get_repos`` is patched alongside, with the LAST name of the walk order
    first: bootstrap's own order heads with the DRIVING repo, which is where a
    dependent sits, while the walk order heads with a dependency. D3 reads the
    first of the former, so a lab wired with one list for both cannot tell a
    gate pointed at the wrong repo from a right one.

    Args:
        monkeypatch: The test's patcher.
        tmp_path: Where the context writes, and what the repo stubs claim as
            their ``sut_dir``.
        repo_names: Walk order, dependencies first.
        hosts: The lab's hosts.
        declarations: ``{repo name: ProjectScopeConfig}`` for the repos that
            declare a ``[project]`` scope; every other repo is undeclared.
    """
    lab = Lab(name="composed")
    for host in hosts:
        lab.add_host(host)
    set_context(OttoContext(lab=lab, output_dir=tmp_path))
    ordered = [
        SimpleNamespace(
            name=name,
            project_scope=(declarations or {}).get(name),
            sut_dir=tmp_path / name,
            # The real :class:`~otto.config.repo.Repo` always carries this (a
            # field with ``default_factory=list``, filled by bootstrap's
            # resolution pass), and the orchestrator's dependency pass reads it
            # off every repo it keeps. Empty here because nothing in this module
            # declares a dependency IN THE [dependencies] sense -- the walk
            # order it asserts on is handed over ready-made.
            dependencies=[],
        )
        for name in repo_names
    ]
    monkeypatch.setattr("otto.config.get_ordered_repos", lambda: ordered)
    driving = repo_names[-1] if repo_names else None
    monkeypatch.setattr(
        "otto.config.get_repos",
        lambda: (
            [repo for repo in ordered if repo.name == driving]
            + [repo for repo in ordered if repo.name != driving]
        ),
    )


def _overriding_host_class(verb, returns):
    """A ``_FleetHost`` subclass overriding exactly *verb* — the ``register_host_class`` shape."""

    async def _override(self, *args, **kwargs):
        del args, kwargs
        self.events.append((self.id, f"override:{verb}"))
        return returns

    return type(f"_Overrides_{verb}", (_FleetHost,), {verb: _override})


def _ok(outcome):
    """Whether a lab-level call succeeded — a ``Result`` for the verbs, a bool for ``is_clean``."""
    return outcome if isinstance(outcome, bool) else outcome.is_ok


# ── host-class overrides are honoured by every fleet walk ────────────────

# Every verb otto.project walks the fleet with, the value a host class's
# override returns, and the lab-level call that must reach it. One row per
# ``do_for_all_hosts`` call site in the package: if a site is added without a
# row, nothing proves that walk honours an override either.
_WALKED_VERBS = [
    ("install", Result(Status.Success), project.install),
    (
        "uninstall",
        Result(Status.Success),
        lambda: project.uninstall(get_product_logs=False, get_debug_logs=False),
    ),
    ("get_product_logs", Result(Status.Success), lambda: project.get_logs(debug=False)),
    ("get_debug_logs", Result(Status.Success), lambda: project.get_logs(product=False)),
    (
        "install_toolchain_tools",
        Result(Status.Success),
        lambda: project.install_tools(dev=False, toolchain=True),
    ),
    (
        "remove_toolchain_tools",
        Result(Status.Success),
        lambda: project.cleanup(get_product_logs=False, get_debug_logs=False),
    ),
    ("toolchain_tools_absent", True, project.is_clean),
]


@pytest.mark.parametrize(
    ("verb", "returns", "walk"), _WALKED_VERBS, ids=[row[0] for row in _WALKED_VERBS]
)
@pytest.mark.asyncio
async def test_every_fleet_walk_runs_the_host_classs_override(
    monkeypatch, tmp_path, verb, returns, walk
):
    # Kills: handing ``do_for_all_hosts`` an unbound ``BaseHost.<verb>``. That
    # call site runs BaseHost's body against a host whose class overrides the
    # verb — reporting success while doing the wrong thing, and disagreeing
    # with the ``otto host <id> <verb>`` surface that does honour the override.
    events = []
    host = _overriding_host_class(verb, returns)("h1", events)
    _wire_lab(monkeypatch, tmp_path, ["app"], [host])

    outcome = await walk()

    assert _ok(outcome), outcome
    assert ("h1", f"override:{verb}") in events, f"the walk bypassed {verb}'s override"


# ── the composed two-repo lab (spec section 8) ───────────────────────────


@pytest.mark.asyncio
async def test_two_repos_stamped_at_ingest_tear_down_in_reverse_dependency_order(
    monkeypatch, tmp_path
):
    # The whole chain in one test: providers registered under each repo's
    # bootstrap marker, products stamped with that repo at ingest, a shared
    # fleet, and the lab-level teardown reading those stamps back. Kills: a
    # stamp taken at ingest instead of registration (both providers would come
    # out unowned, and every repo's walk would touch nothing), an owner filter
    # dropped anywhere on the path (each repo would tear down the other's
    # product), a forward teardown order, and a per-repo debug sweep.
    events = []
    with registering_repo("base"):
        register_product_provider(lambda host: [_RecordingProduct(f"base-lib@{host.id}", events)])
    with registering_repo("app"):
        register_product_provider(lambda host: [_RecordingProduct(f"app-svc@{host.id}", events)])

    hosts = [_JournaldHost("h1", events), _FleetHost("h2", events)]
    for host in hosts:
        apply_product_providers(host)
    # "base" first: bootstrap resolved app's declared dependency on it.
    _wire_lab(monkeypatch, tmp_path, ["base", "app"], hosts)

    assert {p.name: p.owner for p in hosts[0].products} == {
        "base-lib@h1": "base",
        "app-svc@h1": "app",
    }

    result = await project.uninstall(get_product_logs=False)

    assert result.is_ok, result.msg
    for host in hosts:
        # Each repo's pass touched only its OWN product, and the dependent came
        # down before the dependency it was built on.
        assert [e for h, e in events if h == host.id and e.startswith("uninstall")] == [
            f"uninstall:app-svc@{host.id}",
            f"uninstall:base-lib@{host.id}",
        ]
    # ONE debug sweep, after every repo has torn down — and on h1 it is the host
    # class's journald override that ran, not BaseHost's glob fetch.
    assert events[-1] == ("h1", "journald")
    assert [e for e in events if e == ("h1", "journald")] == [("h1", "journald")]


# ── the dependency whose declaration admits no host (D3's skip, for real) ────

# THE THIRD QUESTION A DOUBLE CANNOT ANSWER. A dependency that declared
# ``[project]`` and whose host_patterns match nothing in the labs that DO apply
# has an empty fleet of its own — and the refusal that makes that loud lives in
# the repo-scoped VIEW (``ctx.for_repo(name).all_hosts()`` →
# ``require_nonempty_fleet``), which the duck-typed contexts in
# ``test_orchestrator.py`` do not have. Under a double the skip merely looks
# tidier; here, admitting such a repo into the walk RAISES out of the
# dependency's own hop and takes the driving project's verb down with it.


def _declared(*patterns):
    """A real compiled ``[project]`` declaration over the ``composed`` lab."""
    import re

    from otto.config.scope import ProjectScopeConfig

    return ProjectScopeConfig([re.compile("composed")], [re.compile(p) for p in patterns])


def _starved_lab(monkeypatch, tmp_path, events):
    """A two-repo lab whose dependency ``base`` applies to the lab and targets no host in it."""
    hosts = [_JournaldHost("h1", events)]
    with registering_repo("base"):
        register_product_provider(lambda host: [_RecordingProduct(f"base-lib@{host.id}", events)])
    with registering_repo("app"):
        register_product_provider(lambda host: [_RecordingProduct(f"app-svc@{host.id}", events)])
    for host in hosts:
        apply_product_providers(host)
    _wire_lab(
        monkeypatch,
        tmp_path,
        ["base", "app"],
        hosts,
        declarations={"base": _declared("nothing-.*"), "app": _declared(".*")},
    )
    return hosts


@pytest.mark.asyncio
async def test_the_starved_dependencys_own_view_really_does_refuse(monkeypatch, tmp_path):
    """The hostile condition, INJECTED and proven live before anything is asserted about it.

    Without this, the two tests below would pass just as well over a lab where
    nothing was ever wrong: a walk that contacts one host is what a healthy
    dependency-free run looks like too. This is the raise the orchestrator must
    now be keeping out of its walks.
    """
    from otto.bootstrap import ProjectScopeError
    from otto.context import get_context

    _starved_lab(monkeypatch, tmp_path, [])

    with pytest.raises(ProjectScopeError) as excinfo:
        list(get_context().for_repo("base").all_hosts())

    assert "nothing-.*" in " ".join(str(excinfo.value).split())


@pytest.mark.asyncio
async def test_status_reports_a_starved_dependency_rather_than_raising_out_of_it(
    monkeypatch, tmp_path
):
    """``status()`` is a READING verb and must survive a dependency it cannot walk.

    The counted-repo probe (``owns_products``) is itself a fleet walk through
    the repo-scoped view, so an unskipped ``base`` raises here before any state
    is folded — the whole report dies over a repo that has nothing to do with
    this run.
    """
    events = []
    _starved_lab(monkeypatch, tmp_path, events)

    report = await project.status()

    assert "base" not in report.repos
    assert report.scoping["base"].usable is False
    assert report.scoping["base"].applicable is True  # its LABS were never the problem
    assert report.scoping["base"].universe == ()


@pytest.mark.asyncio
async def test_a_starved_dependency_strands_no_host_global_step_of_a_teardown(
    monkeypatch, tmp_path
):
    """Best-effort teardown keeps its every-step-runs contract past the skip.

    Contacts by ``==``: ``base``'s product is never touched, ``app``'s comes
    down exactly once, and the ONE debug sweep — the host-global step the
    mid-walk raise used to strand, along with the toolchain removal and the
    tunnel reap — still runs last.
    """
    events = []
    _starved_lab(monkeypatch, tmp_path, events)

    result = await project.uninstall(get_product_logs=False)

    assert result.is_ok, result.msg
    assert [e for _, e in events if e.startswith("uninstall")] == ["uninstall:app-svc@h1"]
    assert events[-1] == ("h1", "journald")

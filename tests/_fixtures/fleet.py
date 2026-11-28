"""Real projects, real hosts, a real lab, and the one lazy seam that scopes them.

Lifted out of ``tests/unit/fleet/test_fleet_scoping.py`` when a second module
(the reservation gate, which now computes its requirement over
``OttoContext.admissible_ids()``) needed the same construction. Real ``Project``
objects parsed from a real ``settings.toml`` and real hosts built by the
factory — the scoping chain reads what settings parsing and lab loading
produced, so a stub of either would pin the stub's shape instead of the chain's.
"""

import dataclasses
import textwrap
from collections.abc import Mapping, Sequence

from otto.config.project import Project
from otto.context import OttoContext, set_context
from otto.host.element import Element
from otto.labs.loader import Lab
from tests._fixtures.bootstrap_seam import fake_bootstrap_result
from tests._fixtures.project_scaffold import make_project


def _toml_list(patterns):
    # TOML literal strings (single quotes) keep backslashes verbatim.
    return "[" + ", ".join(f"'{p}'" for p in patterns) + "]"


def _project(tmp_path, name, *, labs=None, hosts=None):
    """A real ``Project`` whose ``[project]`` block declares *labs* / *hosts*.

    Both ``None`` writes no ``[project]`` table at all — the undeclared project the
    whole-lab fallback is built on, which is a different thing from a project that
    declared an empty list.
    """
    body = ""
    if labs is not None or hosts is not None:
        lines = ["[project]"]
        if labs is not None:
            lines.append(f"lab_patterns = {_toml_list(labs)}")
        if hosts is not None:
            lines.append(f"host_patterns = {_toml_list(hosts)}")
        body = "\n".join(lines)
    return Project(root=make_project(tmp_path / name, name=name, extra=textwrap.dedent(body)))


def _host(element, lab_name, octet, *, subnet="10.0.0", hop=None):
    from otto.host.factory import create_host_from_dict

    data = {
        "os_type": "unix",
        "ip": f"{subnet}.{octet}",
        "creds": [{"login": "admin", "password": "admin"}],
    }
    if hop is not None:
        data["hop"] = hop
    return create_host_from_dict(data, lab_name=lab_name, element=Element(name=element))


def _lab(*pairs, component_names=None, subnet="10.0.0", hops: "Mapping[str, str] | None" = None):
    """A ``Lab`` holding ``(element, source_lab)`` hosts, stamped by the factory.

    *hops* maps an element to the id its host hops through, written into the
    host entry the factory builds, as a lab file would.
    """
    names = list(component_names or sorted({lab_name for _, lab_name in pairs}))
    lab = Lab(name="+".join(names), component_names=names)
    for octet, (element, lab_name) in enumerate(pairs, start=1):
        hop = (hops or {}).get(element)
        lab.add_host(_host(element, lab_name, octet, subnet=subnet, hop=hop))
    return lab


def add_builtin_local(lab, *, resources=frozenset()):
    """Inject the built-in ``LocalHost``, the way ``load_lab`` does.

    *resources* is the hostile condition, not a realistic one: the built-in host
    carries none in production, so a guard that merely observed "``local``
    required nothing" would pass against a gate that never excluded it. Handing
    it a resource is what makes the exclusion falsifiable.
    """
    from otto.host.builtin_hosts import make_builtin_local_host

    local_host = make_builtin_local_host()
    local_host.source_lab = lab.component_names[0]
    local_host.resources = frozenset(resources)
    lab.add_host(local_host)
    return lab


def install_scoped_context(
    lab, projects, *, exclude_projects=(), include_projects=(), scoped=True, ordered=None
):
    """Build and install an ``OttoContext`` whose scopes resolve over *projects*.

    *exclude_projects* and *include_projects* are the run's ``-E`` and ``-I``
    lists, as the CLI would set them. *ordered* is the result's dependency
    order (*projects* when ``None``: nothing reordered or skipped). *scoped*
    is the command's verdict: ``False`` builds the context of a command that
    opted out of scoping, which resolves any lab host while its walks and
    hosts in play keep the scope.
    The context is built through ``new_run_context``, the seam every run's
    context comes from.

    The context is handed one bootstrap result holding *projects* (``bootstrap=``,
    :func:`~tests._fixtures.bootstrap_seam.fake_bootstrap_result`), which is
    what lets a unit test declare a fleet of interest without standing up a
    bootstrap. An empty *projects* list is the whole-lab fallback — no
    declaration, no narrowing.

    Installation is not undone here: the root conftest's autouse
    ``_reset_otto_context`` snapshot-restores the three variables ``set_context``
    installs (the context, its policy and its resolver) after every test,
    and a ``reset_context(binding)`` of our own cannot work anyway — async callers
    run their body in a COPY of the context, so the binding is from a different
    Context object and ``ContextVar.reset`` raises.
    """
    from otto.config.scope import ScopeSelection
    from otto.run_context import new_run_context

    selection = ScopeSelection(
        labs=tuple(lab.component_names),
        include=tuple(include_projects),
        exclude=tuple(exclude_projects),
    )
    ctx = new_run_context(
        lab=lab,
        selection=selection,
        policy=None,
        bootstrap=fake_bootstrap_result(projects, ordered=ordered),
        scoped=scoped,
    )
    set_context(ctx)
    return ctx


@dataclasses.dataclass
class DoubleLab:
    """A lab of scripted host and link doubles, named as a context reads a ``Lab``.

    The one test-double lab of the link and tunnel suites: a library call
    reads its ``hosts`` (and ``links`` / ``static_links()``), and a context
    installed over it (:func:`install_scoped_double`) reads its ``name`` and
    ``component_names`` to judge the run's scope, as it reads a real lab's.
    """

    hosts: dict
    links: list = dataclasses.field(default_factory=list)
    name: str = "bed"
    component_names: list = dataclasses.field(default_factory=lambda: ["bed"])

    def static_links(self) -> list:
        """Return the declared links; a double declares no hop edges."""
        return list(self.links)


def install_scoped_double(tmp_path, lab, *, targets):
    """Install a scoped context over the test double *lab*, one project targeting *targets*.

    Each host is stamped with the lab's first component as its source lab, as
    ``load_lab`` stamps a real host. The one project, ``proj``, declares that
    lab and the ids in *targets*, so every other host is in the lab and
    outside the run's scope. Returns the context; its ``lab`` is the scoped
    view and its ``unscoped.lab`` is *lab*.
    """
    lab_name = lab.component_names[0]
    for host in lab.hosts.values():
        host.source_lab = lab_name
    project = _project(tmp_path, "proj", labs=[lab_name], hosts=list(targets))
    return install_scoped_context(lab, [project])


_WORKSPACE_HOSTS = {
    "alpha": (("a-dut", "alpha"), ("a-jump", "alpha")),
    "beta": (("b-dut", "beta"), ("b-jump", "beta")),
}
_WORKSPACE_HOPS = {"a-dut": "a-jump", "a-jump": "b-jump"}


@dataclasses.dataclass(frozen=True)
class ScopeWorkspace:
    """One installed scoped context, its lab, and the two declaring projects."""

    ctx: OttoContext
    lab: Lab
    inscope: Project
    """Declares ``lab_patterns=["alpha"]``, ``host_patterns=["a-dut.*"]``."""
    outscope: Project
    """Declares ``lab_patterns=["beta"]``, ``host_patterns=["b-dut.*"]``."""


def scope_workspace(
    tmp_path,
    *,
    labs: Sequence[str] = ("alpha",),
    exclude_projects: Sequence[str] = (),
    include_projects: Sequence[str] = (),
    mixed: bool = False,
    local_host: bool = True,
    extra_hosts: Sequence[object] = (),
) -> ScopeWorkspace:
    """A two-lab, two-project scoped workspace, installed as the active context.

    The lab holds the hosts of each loaded component in *labs*, in that order
    (``a-dut``, ``a-jump`` for ``alpha``; ``b-dut``, ``b-jump`` for ``beta``,
    on 192.0.2.x), so ``labs=("beta", "alpha")`` is the ``-l beta+alpha``
    order. ``a-dut`` hops through ``a-jump``, which hops through ``b-jump``: a
    two-hop chain whose hops no project targets (``b-jump`` is in the lab
    only when ``beta`` is loaded).

    *local_host* injects the built-in runner after them, stamped with the
    first component as ``load_lab`` does; *extra_hosts* are added last, as
    given (a lab entry reusing the id ``local`` wants ``local_host=False``).
    The projects are ``inscope`` and ``outscope`` (see :class:`ScopeWorkspace`);
    *mixed* adds ``plain``, which declares nothing. *exclude_projects* and
    *include_projects* are the run's ``-E`` and ``-I`` lists.

    The verdicts resolve lazily, so a host added to ``lab`` before the first
    read of them is resolved with the rest.
    """
    pairs = [pair for name in labs for pair in _WORKSPACE_HOSTS[name]]
    lab = _lab(*pairs, component_names=list(labs), subnet="192.0.2", hops=_WORKSPACE_HOPS)
    if local_host:
        add_builtin_local(lab)
    for host in extra_hosts:
        lab.add_host(host)
    inscope = _project(tmp_path, "inscope", labs=["alpha"], hosts=["a-dut.*"])
    outscope = _project(tmp_path, "outscope", labs=["beta"], hosts=["b-dut.*"])
    projects = [inscope, outscope]
    if mixed:
        projects.append(_project(tmp_path, "plain"))
    ctx = install_scoped_context(
        lab, projects, exclude_projects=exclude_projects, include_projects=include_projects
    )
    return ScopeWorkspace(ctx=ctx, lab=lab, inscope=inscope, outscope=outscope)


def dispatch_root(monkeypatch, argv, *, lab, projects, env=None, through_entry=False):
    """Run ``otto --lab <lab> <argv>`` through the root app, over *projects* and *lab*.

    The composition root hands back *projects* and the lab build hands back
    *lab*, so the run's scope verdicts judge real parsed declarations over
    real hosts. *env* is passed to the runner (``{"COLUMNS": "400"}`` keeps a
    long message on one line). *through_entry* runs the console-script
    ``entry()`` instead of the bare app, so its error boundary (the frame that
    prints an escaped ``OttoError``) is in the run.
    """
    import typer
    from typer.testing import CliRunner

    from otto.cli.main import app
    from otto.session.lab import BuiltLab
    from tests._fixtures.bootstrap_seam import install_composed, patch_bootstrap

    install_composed(monkeypatch, patch_bootstrap(monkeypatch, projects))
    monkeypatch.setattr(
        "otto.session.lab.build_lab_and_view",
        lambda _projects, **_kw: BuiltLab(lab=lab, view=None),  # ty: ignore[invalid-argument-type]
    )
    full = ["--lab", lab.name, *argv]
    if not through_entry:
        return CliRunner().invoke(app, full, env=env)
    wrapper = typer.Typer()

    @wrapper.command()
    def otto_entry() -> None:
        from otto.cli.main import entry

        entry()

    monkeypatch.setattr("sys.argv", ["otto", *full])
    return CliRunner().invoke(wrapper, [], env=env)

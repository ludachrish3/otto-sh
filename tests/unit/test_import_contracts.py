"""Import contracts: what an entry point must never load, and who loaded it if it did.

Every command pays, on each run, for every module its import graph reaches,
and on an NFS venv each module file is a round trip. The per-verb import cost
was cut edge by edge (#455); this table keeps the cuts cut. Each row maps an
ENTRY (an import statement, run as the first thing a fresh interpreter does)
to the modules it must NEVER load, directly or transitively.

When a row fails, the message prints, for each forbidden module found, the
chain of frames that first imported it, outermost first: the last otto frame
in that chain is the edge to cut. The code-shape rule sanctions the fix: a
function-scope import in the one function that uses the dependency, a
``TYPE_CHECKING`` import with quoted annotations for a type, or a lazy-table
entry in a package ``__init__``.

The DIRECT form of the same bans (a heavy import at the top of a CLI module,
an eager import in a lazy package ``__init__``) is ast-grep's job
(``.ast-grep/rules/cli-command-no-module-scope-heavy-import.yml`` and
``lazy-package-init-stays-lazy.yml``, run by ``make lint-arch``), which names
the offending line without running anything. This table catches what a
per-file rule cannot see: a harmless-looking import that itself reaches a
heavy module. The file-operation ceilings in ``tests/unit/import_budget/``
remain the cost backstop; these rows name the edge.

To add a contract, add a row. A row belongs here when it is a pure
"importing X never loads Y" check; a guard that also asserts behaviour (a call
still works, an access DOES load something) stays with the code it tests.
"""

import ast
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import render_module_graph as rmg
from tests._fixtures.paths import PROJECT_ROOT

# Nothing a verb's `--help` needs, and so nothing its command module may load
# at import: each was cut from a command module's import graph with a measured
# saving, and each belongs to a command body (running, reading the lab,
# reporting, validating options, drawing a table).
_NO_COMMAND_MODULE_LOADS = [
    "asyncio",
    "asyncssh",
    "pydantic",
    "rich.table",
    "otto.config.fleet",
    "otto.config.lab",
    "otto.config.repo",
    "otto.context",
    "otto.coverage.merge",
    "otto.coverage.reporter",
    "otto.coverage.store",
    "otto.coverage.tree",
    "otto.docker.build",
    "otto.docker.compose",
    "otto.host.host",
    "otto.host.remote_host",
    "otto.host.session",
    "otto.host.unix_host",
    "otto.models.monitor",
]

IMPORT_CONTRACTS: dict[str, list[str]] = {
    # --- Each gated verb's command module (its `--help` imports it). ---
    # The root command tree: no help reads a repo or validates options.
    "import otto.cli.main": _NO_COMMAND_MODULE_LOADS,
    "import otto.cli.run": _NO_COMMAND_MODULE_LOADS,
    # The verb menu comes from the resolved host's class, not this module.
    "import otto.cli.host": _NO_COMMAND_MODULE_LOADS,
    "import otto.cli.reservation": _NO_COMMAND_MODULE_LOADS,
    "import otto.cli.docker": _NO_COMMAND_MODULE_LOADS,
    "import otto.cli.schema": _NO_COMMAND_MODULE_LOADS,
    "import otto.cli.monitor": _NO_COMMAND_MODULE_LOADS,
    "import otto.cli.test": _NO_COMMAND_MODULE_LOADS,
    # Only building or fetching a report can meet a coverage error, and the
    # missing-tool error lives with the host errors.
    "import otto.cli.cov": [
        *_NO_COMMAND_MODULE_LOADS,
        "otto.coverage.errors",
        "otto.coverage.renderer.spa_renderer",
        "otto.host.errors",
        "otto.logger",
    ],
    # --- Tracked verbs whose module-top edges were cut. ---
    "import otto.cli.cache": ["rich.table"],
    "import otto.cli.inventory": ["rich.table"],
    "import otto.cli.link": ["otto.config.fleet", "rich.table"],
    "import otto.cli.tunnel": ["otto.config.fleet", "rich.table"],
    # --- Shared modules every command imports. ---
    # The command tree imports otto.config for every `--help`; none reads a repo.
    "import otto.config": ["otto.config.repo", "otto.config.version"],
    # Command modules take constants from otto.utils; only wait_for_async
    # needs asyncio, and it already runs on a loop.
    "import otto.utils": ["asyncio"],
    # Every command reads settings; only one that loads a lab needs host code.
    "import otto.models.settings": [
        "otto.host.specs",
        "otto.models.inventory",
        "otto.host.unix_host",
    ],
    # --- The run path: what every lab-loading command imports. ---
    # One host name imports its module, not the host package's other hosts,
    # nor the SSH stack a local host never uses.
    "from otto.host import LocalHost": [
        "asyncssh",
        "otto.host.docker_host",
        "otto.host.embedded_host",
        "otto.host.transfer.nc",
    ],
    # Host-class discovery (the dynamic `otto host` CLI) opens no connection.
    "from otto.host.os_profile import HOST_CLASSES, get_host_class": [
        "aioftp",
        "asyncssh",
        "otto.host.console",
        "telnetlib3",
    ],
    # A host with no session-setup hook never loads the hook machinery: the
    # session and the terminal bridge import it below their no-hook returns.
    "import otto.host.session": ["otto.host.session_setup"],
    "import otto.host.interact": ["otto.host.session_setup"],
    # Classifying a survey login failure loads asyncssh only for an SSH error.
    "from otto.host.survey.login import classify_login_error": ["asyncssh"],
    # The monitor's lower files are topology modules beneath the monitor service:
    # importing all eleven loads none of the service, and not the lab-aware runner.
    (
        "import otto.monitor.broadcast, otto.monitor.errors, otto.monitor.event_ops, "
        "otto.monitor.events, otto.monitor.formats, otto.monitor.log_sourced, "
        "otto.monitor.parsers, otto.monitor.rates, otto.monitor.snmp, otto.monitor.store, "
        "otto.monitor.tls"
    ): [
        "otto.monitor.archive_edit",
        "otto.monitor.collector",
        "otto.monitor.db",
        "otto.monitor.export",
        "otto.monitor.factory",
        "otto.monitor.live",
        "otto.monitor.review",
        "otto.monitor.server",
        "otto.monitor.session",
    ],
    # Host-spec validation reads IMPAIRERS; the built-in resolves by reference.
    "from otto.link import IMPAIRERS": [
        "otto.link.manage",
        "otto.link.model",
        "otto.link.netem",
        "otto.link.placement",
    ],
    # Building a report imports the renderer only once it renders.
    "import otto.coverage.reporter": ["otto.coverage.renderer.spa_renderer"],
    # A plain `otto test` decides coverage through instrumentation alone; the
    # reporter, merger, store and fetcher belong to a run that reports.
    "from otto.coverage.instrumentation import decide_coverage, detect_for_lab": [
        "otto.coverage.collect",
        "otto.coverage.fetcher",
        "otto.coverage.merge",
        "otto.coverage.reporter",
        "otto.coverage.store",
        "otto.coverage.tree",
    ],
    # Every lab-loading command places the declared container hosts.
    "from otto.docker.compose import register_declared_container_hosts": [
        "otto.docker.build",
        "otto.docker.staging",
    ],
    # Every lab-loading command builds the inventory the lab names.
    "from otto.inventory import build_inventory": [
        "otto.inventory.netbox",
        "otto.inventory.snapshot",
    ],
    # Every lab-loading command builds the reservation gate, even under -R.
    "from otto.reservations import build_reservation_gate": [
        "otto.reservations.json_backend",
        "otto.reservations.null_backend",
    ],
    # --- Library modules that must never load the CLI. ---
    # otto.cli projects these libraries, not the other way around: importing
    # any of them must never pull typer, or any otto.cli submodule, in.
    # Forbidding the exact name "otto.cli" is enough -- importing a submodule
    # always loads the parent package first, so it lands in sys.modules too.
    "import otto.instructions": ["typer", "otto.cli"],
    # The instruction catalog sits in the foundation layer and every layer
    # reads it. Of otto's own modules it loads only what tach declares it
    # depends on (IMPORT_ONLY_DECLARED below); this row bans the rest.
    "import otto.instruction_catalog": ["typer"],
    "import otto.project.commands": ["typer", "otto.cli"],
    # otto.project.orchestrator and otto.project.actions legitimately load
    # typer: their first-party Options classes (otto.project.options) build
    # typer.Option(...) metadata at class-body time by design -- otto.params
    # is the layer that carries typer, not the CLI. What they must never
    # load is otto.cli itself.
    "import otto.project.orchestrator": ["otto.cli"],
    "import otto.project.actions": ["otto.cli"],
    # typer is otto.params's only heavy import, and it's function-local (see
    # _default_hidden) -- importing the module alone must never load it.
    "import otto.params": ["typer"],
    # --- Library facades. ---
    # The composition root's accessors cost nothing until called: importing
    # otto.bootstrap loads neither the settings parsers nor the host classes. It
    # does load otto.config.scope, the foundation module that owns the error
    # types it re-exports, and otto.declared, whose initiator slot it fills.
    "import otto.bootstrap": [
        "otto.config.repo",
        "otto.config.version",
        "otto.host",
        "otto.models",
    ],
    # The lab facade is lazy: a bare import loads no implementation, and its
    # lightest name, the error class, loads only the scope module.
    "import otto.lab": ["otto.config", "otto.host"],
    "from otto.lab import EmptySelectionError": ["otto.config.fleet", "otto.host"],
}

# The positive control, for a row whose forbidden list alone could pass by
# loading nothing at all: what the entry must load. `from otto.host import
# LocalHost` must reach the module that defines LocalHost, and nothing else of
# the host package's hosts (its row above).
IMPORT_MUST_LOAD: dict[str, list[str]] = {
    "from otto.host import LocalHost": ["otto.host.local_host"],
    "from otto.lab import fleet_of_interest": ["otto.config.fleet"],
    "import otto.instruction_catalog": ["otto.errors", "otto.registry"],
}

# The allowlist form, for a module every layer reads: importing it loads, of
# otto's own modules, only itself, what tach.toml declares it depends on
# (transitively), and the packages above those. Anything else fails, named
# with the chain that loaded it, so a new import cannot slip past a list of
# what was forbidden when the row was written (spec 2026-10-11
# placement-and-phases §4.3).
#
# tach's depends_on says what a module MAY IMPORT, anywhere in it; this row is
# about what it LOADS AT IMPORT. The two differ for a dependency the module
# imports only inside a function. So each row lists its module's lazy direct
# dependencies (declared in tach.toml, imported only function-locally): those
# must not load at import, and every other direct dependency must. A newly
# declared dependency that does not load therefore fails until its row names
# it lazy, and a lazy one imported at module scope fails as well. A lazy
# dependency of a dependency is that dependency's business, not this row's.
#
# A carved module's package ancestors run on the way in, and an ancestor
# __init__ may import a sibling eagerly when that sibling is stdlib-only
# (.ast-grep/rules/lazy-package-init-stays-lazy.yml; spec 2026-10-11
# placement-and-phases §4.3): otto/config/__init__.py imports otto.config.env.
# Those modules, and only those, are allowed too (ancestor_eager_imports).
IMPORT_ONLY_DECLARED: dict[str, list[str]] = {
    "otto.instruction_catalog": [],
    # The impairment vocabulary, the impairer registry and the built-in netem
    # impairer sit in the foundation layer, below the host specs that read the
    # registry. Each loads its declared dependencies at import, and nothing else.
    "otto.link.impairer": [],
    "otto.link.netem": [],
    "otto.link.params": [],
    # The leaf otto.host and otto.models share: OttoModel, CredSpec, Cred.
    "otto.models.base": [],
    # The scope predicate and the composition root's error types, below every
    # layer; it normalizes repo names inside its functions.
    "otto.config.scope": ["otto.models.dependencies"],
    # The declared-entry core and the composed-repos catalog. It never loads
    # otto.bootstrap: the root fills its initiator slot from above.
    "otto.declared": ["otto.config.scope", "otto.invocation"],
    "otto.models.dependencies": [],
}

# Runs in the child. It records, for each watched module, the frames that
# imported it, and reports which watched modules the entry loaded.
#
# It must not change what the entry imports, so it imports nothing before the
# entry runs (json and linecache load only afterwards, to render the report),
# and its finder never finds anything: returning None hands every lookup to
# the real finders unchanged. The finder runs for a module only while it is
# absent from sys.modules, so the frames stored LAST for a name are those of
# the import that loaded it (an earlier `importlib.util.find_spec` probe that
# imported nothing is overwritten).
_CHILD = r"""
import sys

ENTRY = sys.argv[1]
WATCHED = frozenset(sys.argv[2:])
PRELOADED = sorted(m for m in WATCHED if m in sys.modules)
FRAMES = {}


class FirstImportRecorder:
    @staticmethod
    def find_spec(name, path=None, target=None):
        if name in WATCHED and name not in sys.modules:
            frames = []
            frame = sys._getframe(1)
            while frame is not None:
                frames.append((frame.f_code.co_filename, frame.f_lineno))
                frame = frame.f_back
            FRAMES[name] = frames[::-1]
        return None


sys.meta_path.insert(0, FirstImportRecorder)
try:
    exec(compile(ENTRY, "<entry>", "exec"), {"__name__": "__entry__"})
finally:
    sys.meta_path.remove(FirstImportRecorder)
LOADED = sorted(m for m in WATCHED if m in sys.modules and m not in PRELOADED)

import json
import linecache

ENTRY_LINES = ENTRY.splitlines()


def render(filename, lineno):
    if filename == "<entry>":
        return f"<entry>:{lineno}  {ENTRY_LINES[lineno - 1].strip()}"
    return f"{filename}:{lineno}  {linecache.getline(filename, lineno).strip()}"


def chain(frames):
    return [
        render(f, n)
        for f, n in frames
        if not f.startswith("<frozen") and f != "<string>"
    ]


print(json.dumps({
    "preloaded": PRELOADED,
    "loaded": {m: chain(FRAMES.get(m, [])) for m in LOADED},
}))
"""


def _shorten(line: str) -> str:
    """Show a frame's path relative to the repo, or from site-packages on."""
    root = f"{PROJECT_ROOT}/"
    if line.startswith(root):
        return line[len(root) :]
    marker = "site-packages/"
    return line[line.index(marker) + len(marker) :] if marker in line else line


def _explain(entry: str, report: dict) -> str:
    lines = [f"`{entry}` loaded modules its import contract forbids:"]
    for module, frames in report["loaded"].items():
        lines.append(f"  {module}, first imported by (outermost first):")
        shown = frames or ["(no import frames recorded)"]
        lines.extend(f"    {_shorten(frame)}" for frame in shown)
    lines.extend(
        f"  {module} was already loaded when the entry ran: interpreter startup (a .pth "
        "file or sitecustomize) imported it, so this row cannot see who else would"
        for module in report["preloaded"]
    )
    lines.append(
        "Cut the edge at the last otto frame: import the module inside the function that "
        "uses it, or under TYPE_CHECKING with quoted annotations for a type-only name. "
        "Rows live in tests/unit/test_import_contracts.py."
    )
    return "\n".join(lines)


def run_contract(entry: str, forbidden: list[str], *, pythonpath: Path | None = None) -> dict:
    """Run *entry* first in a fresh interpreter; report which *forbidden* modules it loaded."""
    env = None
    if pythonpath is not None:
        env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(pythonpath), *sys.path])}
    proc = subprocess.run(
        [sys.executable, "-c", _CHILD, entry, *forbidden],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        env=env,
    )
    assert proc.returncode == 0, f"`{entry}` failed:\n{proc.stdout}\n{proc.stderr}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize(("entry", "forbidden"), list(IMPORT_CONTRACTS.items()))
def test_an_entry_never_loads_what_its_contract_forbids(entry, forbidden):
    report = run_contract(entry, forbidden)
    assert report == {"preloaded": [], "loaded": {}}, _explain(entry, report)


@pytest.mark.parametrize(("entry", "required"), list(IMPORT_MUST_LOAD.items()))
def test_an_entry_loads_what_it_exists_to_load(entry, required):
    report = run_contract(entry, required)
    assert report["preloaded"] == [], report
    assert sorted(report["loaded"]) == sorted(required), report


# Resolving a command for its help, the walk the import-budget harness's verb
# surfaces make (scripts/import_budget.py's CLI child), never loads the
# composition root: the root group, the command stubs and the completers read
# the completion-cache snapshot from otto.registry, which every resolution
# loads anyway. "help" lists the root's commands. The fast-path test resolves
# `run` with a snapshot installed, the completion fast path's branch of the walk.
HELP_RESOLUTIONS = [
    "help",
    "run",
    "host",
    "reservation",
    "docker",
    "schema",
    "monitor",
    "test",
    "cov",
]


def resolution_entry(verb: str, *, cached: bool = False) -> str:
    """The entry that resolves *verb* for ``--help`` (``help``: the root's command list)."""
    argv = ["otto", "--help"] if verb == "help" else ["otto", verb, "--help"]
    lines = ["import sys", "import typer", f"sys.argv = {argv!r}", "import otto"]
    if cached:
        lines += [
            "from otto.registry import set_completion_names",
            'set_completion_names({"commands": [], "instructions": []})',
        ]
    lines += [
        "cmd = typer.main.get_command(otto.app)",
        'ctx = cmd.make_context("otto", sys.argv[1:], resilient_parsing=True)',
        "cmd.list_commands(ctx)" if verb == "help" else f"cmd.get_command(ctx, {verb!r})",
    ]
    return "\n".join(lines) + "\n"


@pytest.mark.parametrize("verb", HELP_RESOLUTIONS)
def test_resolving_a_command_for_help_never_loads_the_composition_root(verb):
    entry = resolution_entry(verb)
    report = run_contract(entry, ["otto.bootstrap"])
    assert report == {"preloaded": [], "loaded": {}}, _explain(entry, report)


def test_the_completion_fast_path_resolves_without_the_composition_root():
    entry = resolution_entry("run", cached=True)
    report = run_contract(entry, ["otto.bootstrap", "otto.cli.run"])
    assert report["preloaded"] == [], report
    assert sorted(report["loaded"]) == ["otto.cli.run"], _explain(entry, report)


def declared_closure(module: str, graph: rmg.ModuleGraph, lazy: "list[str]") -> "list[str]":
    """*module* and every tach module it declares a dependency on, transitively.

    *module*'s own edges to the modules in *lazy* are left out: those load
    only when a function runs, never at import.
    """
    seen = {module}
    pending = [module]
    while pending:
        current = pending.pop()
        for dep in graph.depends_on.get(current, []):
            if dep not in seen and not (current == module and dep in lazy):
                seen.add(dep)
                pending.append(dep)
    return sorted(seen)


def undeclared_loads(
    module: str,
    loaded: "list[str]",
    graph: rmg.ModuleGraph,
    lazy: "list[str]",
    ancestor_loads: "list[str] | None" = None,
) -> "list[str]":
    """The otto modules in *loaded* that importing *module* may not load.

    One is allowed when the tach module that owns it is *module* or one of its
    declared dependencies loaded at import (not in *lazy*), when it is a
    package above an allowed one (a submodule's import loads its parents first),
    or when it is in *ancestor_loads*, what those parents import eagerly.
    """
    allowed = declared_closure(module, graph, lazy)
    excused = set(ancestor_loads or [])
    return [
        name
        for name in loaded
        if name not in excused
        and rmg.declared_owner(name, graph.modules) not in allowed
        and not any(a.startswith(name + ".") for a in allowed)
    ]


def ancestor_eager_imports(module: str, src: "Path | None" = None) -> "list[str]":
    """The modules the package ``__init__`` files above *module* import at module scope.

    Only a top-level relative import counts: one under ``if TYPE_CHECKING:`` or
    inside a function runs later, if ever.
    """
    root = PROJECT_ROOT / "src" if src is None else src
    parts = module.split(".")
    found: "list[str]" = []
    for depth in range(1, len(parts)):
        package = ".".join(parts[:depth])
        init = root.joinpath(*parts[:depth], "__init__.py")
        if not init.is_file():
            continue
        for node in ast.parse(init.read_text(encoding="utf-8")).body:
            if isinstance(node, ast.ImportFrom) and node.level == 1:
                if node.module:
                    found.append(f"{package}.{node.module}")
                else:
                    found.extend(f"{package}.{alias.name}" for alias in node.names)
    return sorted(set(found))


def unloaded_eager_dependencies(
    module: str, loaded: "list[str]", graph: rmg.ModuleGraph, lazy: "list[str]"
) -> "list[str]":
    """*module*'s declared direct dependencies, not named lazy, that its import did not load."""
    owners = {rmg.declared_owner(name, graph.modules) for name in loaded}
    return [
        dep for dep in graph.depends_on.get(module, []) if dep not in lazy and dep not in owners
    ]


def otto_modules() -> "list[str]":
    """Every module a file under ``src/otto`` defines."""
    src = PROJECT_ROOT / "src"
    return sorted(
        rmg.module_of(path.relative_to(src).as_posix())
        for path in src.joinpath("otto").rglob("*.py")
    )


@pytest.mark.parametrize(("module", "lazy"), list(IMPORT_ONLY_DECLARED.items()))
def test_an_entry_loads_only_its_declared_dependencies(module, lazy):
    graph = rmg.load_graph()
    assert module in graph.depends_on, f"{module} is not a tach module"
    assert set(lazy) <= set(graph.depends_on[module]), f"{module}: {lazy} are not all declared"
    entry = f"import {module}"
    report = run_contract(entry, otto_modules())
    assert report["preloaded"] == [], report
    assert module in report["loaded"], report
    extra = undeclared_loads(
        module, sorted(report["loaded"]), graph, lazy, ancestor_eager_imports(module)
    )
    assert not extra, _explain(
        entry, {"preloaded": [], "loaded": {name: report["loaded"][name] for name in extra}}
    )
    unloaded = unloaded_eager_dependencies(module, sorted(report["loaded"]), graph, lazy)
    assert not unloaded, (
        f"`{entry}` does not load {unloaded}, which tach.toml declares as its dependencies: "
        "if the module imports them only inside a function, name them lazy in its "
        "IMPORT_ONLY_DECLARED row, so a module-scope import of them fails"
    )


def test_the_allowlist_admits_declared_dependencies_and_their_packages_only():
    graph = rmg.ModuleGraph(
        {"otto.low": ["otto.base"], "otto.base": [], "otto.pkg.mid": ["otto.low"], "otto.up": []}
    )
    loaded = ["otto", "otto.base", "otto.base.sub", "otto.low", "otto.pkg", "otto.up"]
    assert undeclared_loads("otto.low", loaded, graph, []) == ["otto.pkg", "otto.up"]
    assert undeclared_loads("otto.pkg.mid", loaded, graph, []) == ["otto.up"]


def test_the_allowlist_admits_what_a_lazy_ancestor_imports_eagerly():
    graph = rmg.ModuleGraph({"otto.pkg.leaf": []})
    loaded = ["otto", "otto.other", "otto.pkg", "otto.pkg.env", "otto.pkg.leaf"]
    assert undeclared_loads("otto.pkg.leaf", loaded, graph, []) == ["otto.other", "otto.pkg.env"]
    assert undeclared_loads("otto.pkg.leaf", loaded, graph, [], ["otto.pkg.env"]) == ["otto.other"]


def test_ancestor_eager_imports_reads_top_level_relative_imports_only(tmp_path):
    pkg = tmp_path / "otto" / "pkg"
    pkg.mkdir(parents=True)
    (tmp_path / "otto" / "__init__.py").write_text("")
    (pkg / "__init__.py").write_text(
        "from typing import TYPE_CHECKING\n"
        "from .env import X\n"
        "from . import consts\n"
        "if TYPE_CHECKING:\n"
        "    from .heavy import Y\n"
        "def f():\n"
        "    from .lazy import Z\n"
    )
    assert ancestor_eager_imports("otto.pkg.leaf", tmp_path) == ["otto.pkg.consts", "otto.pkg.env"]


def test_the_config_package_imports_only_its_env_module_eagerly():
    """Pins spec 2026-10-11 placement-and-phases §4.3: importing a module carved
    out of otto.config loads otto.config.env and nothing else of the package."""
    assert ancestor_eager_imports("otto.config.scope") == ["otto.config.env"]


def test_the_allowlist_holds_a_lazy_dependency_to_function_scope():
    """tach's "may import" is not "loads at import": a lazy dependency must stay unloaded."""
    graph = rmg.ModuleGraph(
        {"otto.low": ["otto.base", "otto.lazy"], "otto.base": [], "otto.lazy": []}
    )
    eager = ["otto", "otto.base", "otto.low"]
    assert undeclared_loads("otto.low", [*eager, "otto.lazy"], graph, ["otto.lazy"]) == [
        "otto.lazy"
    ]
    assert unloaded_eager_dependencies("otto.low", eager, graph, []) == ["otto.lazy"]
    assert unloaded_eager_dependencies("otto.low", eager, graph, ["otto.lazy"]) == []


def test_a_broken_contract_names_the_chain_that_loaded_the_module(tmp_path):
    """The failure message is the point of the table: it must name the edge.

    A three-module chain (top imports mid at module scope, mid imports heavy
    at module scope) breaks a row forbidding heavy to ``import top``; the
    report must carry every hop, outermost first, and leave out a watched
    module the entry never reached.
    """
    (tmp_path / "contract_top.py").write_text("import contract_mid\n")
    (tmp_path / "contract_mid.py").write_text("X = 1\nimport contract_heavy\n")
    (tmp_path / "contract_heavy.py").write_text("")
    (tmp_path / "contract_unused.py").write_text("")

    report = run_contract(
        "import contract_top", ["contract_heavy", "contract_unused"], pythonpath=tmp_path
    )

    assert report["preloaded"] == []
    assert report["loaded"] == {
        "contract_heavy": [
            "<entry>:1  import contract_top",
            f"{tmp_path}/contract_top.py:1  import contract_mid",
            f"{tmp_path}/contract_mid.py:2  import contract_heavy",
        ]
    }
    message = _explain("import contract_top", report)
    assert "contract_heavy, first imported by (outermost first):" in message
    assert f"{tmp_path}/contract_mid.py:2  import contract_heavy" in message


def test_the_recorder_changes_nothing_the_entry_imports():
    """Watching a module must not change whether, or what, the entry loads.

    The same entry is run with and without a watch list; the set of modules it
    leaves loaded must be identical, so the recorder can neither hide an edge
    nor invent one.
    """
    probe = (
        "import otto.cli.test\n"
        "import json, sys\n"
        "print(json.dumps(sorted(sys.modules)), file=sys.stderr)"
    )

    def modules(forbidden: list[str]) -> set[str]:
        proc = subprocess.run(
            [sys.executable, "-c", _CHILD, probe, *forbidden],
            capture_output=True,
            text=True,
            timeout=120,
            check=True,
        )
        return set(json.loads(proc.stderr.strip().splitlines()[-1]))

    watched = modules(["otto.suite.run", "otto.suite", "rich.table", "asyncio"])
    unwatched = modules([])
    assert watched == unwatched, sorted(watched ^ unwatched)

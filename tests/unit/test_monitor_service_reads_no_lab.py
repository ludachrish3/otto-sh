"""Only otto/monitor/live.py reads the run's lab, repos and tunnels.

``otto.monitor.live`` was a tach module of its own, so tach kept
``otto.config``, ``otto.context`` and ``otto.tunnel`` out of the rest of the
monitor: the collector takes its hosts and its tunnel discovery as arguments,
and only the lab-aware runner reads them from the run. It is part of the
``otto.monitor`` module now. The facade exports its names, so the two cannot be
separate modules without a loop through the facade's lazy binding (spec
2026-10-11 placement-and-phases §3.11), and tach cannot tell one file of a
module from another. This test keeps that boundary for every file under
``otto/monitor``, at any depth, except ``otto/monitor/live.py`` itself, and
keeps every other file from importing the runner. The lab is banned by
package as well as by today's modules: ``otto.lab`` is where spec 2026-10-11
placement-and-phases moves the fleet (``otto.lab.fleet`` in P9a), and a ban on
the old homes alone would pass a file that followed it there. The lab model and
its loader live in ``otto.labs.loader`` (§3.7), banned by module: the rest of
``otto.labs`` is the lab sources, which read no run.

A name imported through a package facade (``from otto.monitor import
run_live``) counts as an import of that package here; which module defines it
is ``tests/unit/test_internal_imports_skip_facades.py``'s rule.

It counts every import tach 0.35.1 counts: every import statement, at any depth
(inside a function, a class, a ``try`` or any ``if``), in every spelling
(``import a.b``, ``import a.b as c``, ``from a import b``, ``from a import b
as c``, relative to the file's own package or absolute, several names in one
statement). A name imported from a package counts as its submodule too, as tach
resolves it (``from otto import tunnel`` is ``otto.tunnel``).

It also reads every literal string the import system resolves, with the string
audit's own reader (``strings_in`` in ``tests/unit/test_dynamic_import_audit.py``):
``importlib.import_module("...")``, ``Ref("pkg.module:attr")``, a lazy table's
entry, a ``register_cli_command`` loader. The audit judges a string only when
its target lies in another tach module, and ``live.py`` shares ``otto.monitor``
with the rest of the monitor, so it is this test that refuses a literal naming
the runner or the lab. The facade's lazy table is the one place that may name
the runner: that binding is how ``otto.monitor.run_live`` reaches it. A target
computed at run time cannot be read; the audit pins every such site.

It skips only the body of an ``if`` (not an ``elif``) whose test is exactly
``TYPE_CHECKING`` or ``typing.TYPE_CHECKING``; that body never runs. tach skips
that too, and also skips more, so this test is stricter in exactly these cases,
each pinned by ``test_stricter_than_tach``:

* the ``else`` and ``elif`` branches of such an ``if``, which run;
* an ``if`` whose test is ``TYPE_CHECKING`` read through any other name
  (``t.TYPE_CHECKING`` after ``import typing as t``, ``os.TYPE_CHECKING``):
  tach skips any ``<anything>.TYPE_CHECKING``, this test only ``typing``'s;
* an import by literal string: tach reads none (``include_string_imports``
  stays off), this test reads the ones above.

tach counts an ``elif TYPE_CHECKING:`` branch, a renamed flag (``if TC:``) and
any compound test (``not TYPE_CHECKING``, ``TYPE_CHECKING and x``), and so does
this test (``test_counted_as_tach_counts``). ``test_skipped_as_tach_skips``
pins the shapes both skip. ``scripts/monitor_boundary_tach_canary.py``, a
``make lint-arch`` and ``nox -s lint`` leg, runs every fixture of those three
tests through the installed tach and fails if tach no longer counts and skips
what they say it does, because the ``tach`` floor floats.
"""

import ast
from pathlib import Path

import pytest

from tests._fixtures.paths import PROJECT_ROOT
from tests.unit.test_dynamic_import_audit import strings_in

pytestmark = pytest.mark.interpreter_agnostic

SRC = PROJECT_ROOT / "src"
MONITOR = SRC / "otto" / "monitor"
RUNNER = MONITOR / "live.py"
FACADE = MONITOR / "__init__.py"
RUNNER_MODULE = "otto.monitor.live"
LAB = ["otto.config", "otto.context", "otto.lab", "otto.labs.loader", "otto.tunnel"]
BANNED = [*LAB, RUNNER_MODULE]
SKIPPED_TESTS = ["TYPE_CHECKING", "typing.TYPE_CHECKING"]


def package_of(path: Path) -> str:
    """Return the package a relative import in the file at *path* resolves against.

    That is its directory, for a package's ``__init__.py`` and a module alike.
    """
    return ".".join(path.relative_to(SRC).parent.parts)


def _resolve(package: str, node: ast.ImportFrom) -> str:
    if not node.level:
        return node.module or ""
    base = package.split(".")
    base = base[: len(base) - node.level + 1]
    return ".".join([*base, *([node.module] if node.module else [])])


class _Imports(ast.NodeVisitor):
    def __init__(self, package: str, lines: "list[str]") -> None:
        self.package = package
        self.lines = lines
        self.found: list[list] = []

    def _is_elif(self, node: ast.If) -> bool:
        return self.lines[node.lineno - 1][node.col_offset :].startswith("elif")

    def visit_If(self, node: ast.If) -> None:
        self.visit(node.test)
        if self._is_elif(node) or ast.unparse(node.test) not in SKIPPED_TESTS:
            for statement in node.body:
                self.visit(statement)
        for statement in node.orelse:
            self.visit(statement)

    def visit_Import(self, node: ast.Import) -> None:
        self.found += [[node.lineno, alias.name] for alias in node.names]

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = _resolve(self.package, node)
        self.found.append([node.lineno, module])
        self.found += [[node.lineno, f"{module}.{alias.name}"] for alias in node.names]


def imports(text: str, package: str = "otto.monitor") -> "list[list]":
    """Each ``[line, module]`` an import statement in *text* names, as tach resolves it."""
    finder = _Imports(package, text.split("\n"))
    finder.visit(ast.parse(text))
    return finder.found


def _banned(module: str) -> bool:
    return any(module == b or module.startswith(f"{b}.") for b in BANNED)


def strings(text: str, package: str = "otto.monitor", *, facade: bool = False) -> "list[list]":
    """Each ``[line, module]`` a literal import string in *text* names.

    In the facade (*facade*), its lazy table's entries for the runner are left
    out: they are the public path to it.
    """
    path = f"{package.replace('.', '/')}/{'__init__' if facade else 'probe'}.py"
    return [
        [site.line, site.target_module]
        for site in strings_in(text, path).literals
        if not (facade and site.kind == "lazy-table" and site.target_module == RUNNER_MODULE)
    ]


def reads_the_lab(text: str, package: str = "otto.monitor", *, facade: bool = False) -> "list[str]":
    """Each import in *text* of a banned module, as ``line: module``.

    An import by string reads ``line: module (by string)``.
    """
    return sorted(
        {f"{line}: {module}" for line, module in imports(text, package) if _banned(module)}
        | {
            f"{line}: {module} (by string)"
            for line, module in strings(text, package, facade=facade)
            if _banned(module)
        }
    )


def test_only_the_runner_reads_the_lab() -> None:
    found = [
        f"{path.relative_to(SRC).as_posix()}:{hit}"
        for path in sorted(MONITOR.rglob("*.py"))
        if path != RUNNER
        for hit in reads_the_lab(
            path.read_text(encoding="utf-8"), package_of(path), facade=path == FACADE
        )
    ]
    assert not found, (
        "these monitor files import the run's lab, repos or tunnels, or the lab-aware runner; "
        "only otto/monitor/live.py may. Hand the value in from live.py instead:\n  "
        + "\n  ".join(found)
    )


def test_the_scan_sees_the_runner_read_the_lab() -> None:
    """The runner's reads of the lab are seen as banned, wherever the lab lives.

    Keyed by the names the runner reads, not by their packages: when a name
    moves home, ``LAB`` must name the new one or this fails.
    """
    seen = {
        module.rpartition(".")[2]
        for _, module in imports(RUNNER.read_text(encoding="utf-8"), package_of(RUNNER))
        if _banned(module) and not module.startswith(RUNNER_MODULE)
    }
    reads = {"all_hosts", "current_repos", "get_lab", "get_context", "discover_tunnel_records"}
    assert reads <= seen, sorted(reads - seen)


def test_the_scan_sees_the_facade_bind_the_runner_and_nothing_else() -> None:
    text = FACADE.read_text(encoding="utf-8")
    assert RUNNER_MODULE in {module for _, module in strings(text)}
    assert RUNNER_MODULE not in {module for _, module in strings(text, facade=True)}
    assert reads_the_lab("_LAZY_ATTRS = {'get_lab': 'otto.lab.fleet'}\n", facade=True) == [
        "1: otto.lab.fleet (by string)"
    ]


def test_a_nested_file_resolves_against_its_own_package() -> None:
    nested = MONITOR / "helpers" / "reader.py"
    assert package_of(nested) == "otto.monitor.helpers"
    assert package_of(MONITOR / "helpers" / "__init__.py") == "otto.monitor.helpers"
    assert package_of(RUNNER) == "otto.monitor"
    assert reads_the_lab("from ...tunnel import records\n", package_of(nested)) == [
        "1: otto.tunnel",
        "1: otto.tunnel.records",
    ]
    assert reads_the_lab("from ..tunnel import records\n", package_of(nested)) == []


@pytest.mark.parametrize(
    "text",
    [
        "from ..tunnel.records import discover_tunnel_records\n",
        "def f():\n    from ..context import get_context\n",
        "class C:\n    import otto.config\n",
        "if True:\n    from ..tunnel.records import discover_tunnel_records\n",
        "try:\n    import otto.config\nexcept ImportError:\n    pass\n",
        "import otto.tunnel as tunnels\n",
        "import os, otto.context\n",
        "from .. import tunnel as tunnels\n",
        "from .. import errors, config\n",
        "from otto import tunnel\n",
        "from otto.config.fleet import get_lab\n",
        "from otto.monitor import live\n",
        "from . import live\n",
        "from .live import run_live\n",
        "import otto.monitor.live\n",
        "if False:\n    pass\nelif TYPE_CHECKING:\n    import otto.tunnel\n",
        "if True:\n    pass\nelif TYPE_CHECKING:\n    import otto.tunnel\n",
        "from typing import TYPE_CHECKING as TC\nif TC:\n    import otto.tunnel\n",
        "if not TYPE_CHECKING:\n    import otto.tunnel\n",
        "if TYPE_CHECKING and True:\n    import otto.tunnel\n",
        "if TYPE_CHECKING == True:\n    import otto.tunnel\n",
        "from otto.lab.fleet import get_lab\n",
        "from ..lab import fleet\n",
        "import otto.lab\n",
        "from otto.labs.loader import load_lab\n",
        "from ..labs.loader import Lab\n",
        "from ..labs import loader\n",
        "import otto.labs.loader\n",
    ],
)
def test_counted_as_tach_counts(text: str) -> None:
    assert reads_the_lab(text), text


@pytest.mark.parametrize(
    "text",
    [
        "if TYPE_CHECKING:\n    pass\nelse:\n    import otto.tunnel\n",
        "if TYPE_CHECKING:\n    pass\nelif True:\n    import otto.tunnel\n",
        "import typing as t\nif t.TYPE_CHECKING:\n    import otto.tunnel\n",
        "import os\nif os.TYPE_CHECKING:\n    import otto.tunnel\n",
        "import importlib\nimportlib.import_module('otto.tunnel')\n",
        (
            "import importlib\n"
            "def hosts():\n"
            "    return importlib.import_module('otto.monitor.live').select_monitor_hosts(None)\n"
        ),
        "RUN = Ref('otto.monitor.live:run_live')\n",
        "LAB = Ref(target='otto.lab.fleet:get_lab')\n",
        "_LAZY_ATTRS = {'get_lab': 'otto.config.fleet'}\n",
        "_LAZY_ATTRS = {'load_lab': 'otto.labs.loader'}\n",
    ],
)
def test_stricter_than_tach(text: str) -> None:
    assert reads_the_lab(text), text


@pytest.mark.parametrize(
    "text",
    [
        "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from ..config.repo import Repo\n",
        "import typing\nif typing.TYPE_CHECKING:\n    import otto.tunnel\n",
        "if (TYPE_CHECKING):\n    import otto.tunnel\n",
        "def f():\n    if TYPE_CHECKING:\n        import otto.tunnel\n",
        "class C:\n    if TYPE_CHECKING:\n        import otto.tunnel\n",
        "try:\n    if TYPE_CHECKING:\n        import otto.tunnel\nexcept ImportError:\n    pass\n",
        "if TYPE_CHECKING:\n    if True:\n        import otto.tunnel\n",
        "if TYPE_CHECKING:\n    if True:\n        pass\n    else:\n        import otto.tunnel\n",
        "if False:\n    pass\nelse:\n    if TYPE_CHECKING:\n        import otto.tunnel\n",
        "if TYPE_CHECKING:\n    pass\nelse:\n    if TYPE_CHECKING:\n        import otto.tunnel\n",
        "from ..models.monitor import TunnelRecord\n",
        "from . import collector, livestream\n",
        "from otto.contexts import thing\n",
        "from otto.labs import thing\n",
        "from ..labs.errors import LabNotFoundError\n",
        "import importlib\nimportlib.import_module('otto.monitor.collector')\n",
        "RUN = Ref('otto.monitor.session:MonitorSession')\n",
    ],
)
def test_skipped_as_tach_skips(text: str) -> None:
    assert reads_the_lab(text) == [], text

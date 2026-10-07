"""No otto code or test names a moved name at its old home, in any spelling.

Two sets of names moved (spec
``docs/superpowers/specs/2026-10-06-repo-and-scope-inputs-design.md`` §2 and
the 2026-10-04 manifest spec's D1). The five repo accessors (``get_repos``,
``get_ordered_repos``, ``get_env``, ``is_bootstrapped``,
``get_completion_names``) left the ``otto.config`` package and the deleted
``otto.config.bootstrapped`` for ``otto.bootstrap``. The lab and fleet names
(``load_lab``, ``get_lab``, ``get_host``, ``all_hosts``, ``do_for_all_hosts``,
``run_on_all_hosts``) left the ``otto.config`` package: users import them
from ``otto.lab``, and otto's own code from the defining module
(``otto.config.fleet``, ``otto.config.lab``).

An old spelling fails loudly at most call sites, but not everywhere. Six
readers import an accessor inside a broad ``except``, where a stale import
degrades silently instead of raising
(``tests/unit/bootstrap/test_accessor_readers_unpatched.py`` runs them), and
the bed-lane trees (``tests/repo*/``, ``tests/e2e/``, ``tests/integration/``)
are never imported by a unit lane. So this scan reads every ``.py`` file under
``src/otto`` and ``tests/`` without importing any, and refuses:

- ``from <old home> import <moved name>``, absolute or relative, including
  ``from . import get_repos`` inside ``otto.config`` itself;
- any import of ``otto.config.bootstrapped``;
- ``<old home>.<moved name>`` through a name bound to the old home
  (``import otto.config``, ``import otto.config as cm``, ``from otto import config``);
- a lazy-table entry ``("<old home>", "<moved name>")``;
- a string that names a moved name at its old home: a patch target
  (``"otto.config.get_repos"``) or generated code (``"from otto.config import get_repos"``);
- in otto's own code (not the shipped examples, which are taught code), any import of the
  ``otto.lab`` facade or a lazy-table entry naming it.
"""

import ast
import re
from pathlib import Path

from tests._fixtures.paths import PROJECT_ROOT, TESTS_ROOT

SRC_ROOT = PROJECT_ROOT / "src" / "otto"
THIS_FILE = Path(__file__).resolve()

ACCESSORS = frozenset(
    {
        "get_repos",
        "get_ordered_repos",
        "get_env",
        "is_bootstrapped",
        "get_completion_names",
    }
)
FLEET = frozenset(
    {
        "load_lab",
        "get_lab",
        "get_host",
        "all_hosts",
        "do_for_all_hosts",
        "run_on_all_hosts",
    }
)
DELETED = "otto.config.bootstrapped"
# old home -> the moved names it no longer has
OLD_HOMES: dict[str, frozenset[str]] = {
    "otto.config": ACCESSORS | FLEET,
    DELETED: ACCESSORS,
}
FACADE = "otto.lab"
_MOVED = "|".join(sorted(OLD_HOMES["otto.config"]))
_OLD_IN_TEXT = re.compile(
    rf"otto\.config\.bootstrapped\b"
    rf"|\botto\.config\.({_MOVED})\b"
    rf"|\bfrom\s+(otto\.|\.+)config(\.bootstrapped)?\s+import\s+[^\n]*\b({_MOVED})\b"
)
_DYNAMIC_IMPORT = re.compile(r"(?:importlib\.)?import_module\(['\"]([\w.]+)['\"]\)")


def _module_of(path: Path) -> str:
    """``otto.a.b`` for ``src/otto/a/b.py``; ``otto.a.__init__`` for a package init."""
    return ".".join(path.relative_to(SRC_ROOT.parent).with_suffix("").parts)


class _OldSpellings(ast.NodeVisitor):
    """Collect ``(line, what)`` for every old spelling in one file."""

    def __init__(self, module: "str | None") -> None:
        # None outside src/otto: a relative import there names a tests package.
        self.module = module
        # otto's own code imports from the defining module; the shipped
        # examples are taught code and import from the public path.
        self.facade_banned = module is not None and not module.startswith("otto.examples.")
        self.aliases: dict[str, str] = {}
        self.found: list[tuple[int, str]] = []

    def _source(self, node: ast.ImportFrom) -> "str | None":
        if node.level == 0:
            return node.module or ""
        if self.module is None:
            return None
        # A package init is "<pkg>.__init__", so one level up is the package itself.
        base = ".".join(self.module.split(".")[: -node.level])
        return f"{base}.{node.module}" if node.module else base

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            if alias.name == DELETED or alias.name.startswith(DELETED + "."):
                self.found.append((node.lineno, f"import {alias.name}"))
            elif alias.asname and alias.name in OLD_HOMES:
                self.aliases[alias.asname] = alias.name
            if self.facade_banned and alias.name == FACADE:
                self.found.append((node.lineno, f"import {FACADE} (a facade)"))
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        source = self._source(node)
        if source is not None:
            names = [alias.name for alias in node.names]
            moved = sorted(set(names) & OLD_HOMES.get(source, frozenset()))
            if source == DELETED or (
                f"{source}.bootstrapped" == DELETED and "bootstrapped" in names
            ):
                self.found.append((node.lineno, f"from {source} import {', '.join(names)}"))
            elif moved:
                self.found.append((node.lineno, f"from {source} import {', '.join(moved)}"))
            for alias in node.names:
                if f"{source}.{alias.name}" in OLD_HOMES:
                    self.aliases[alias.asname or alias.name] = f"{source}.{alias.name}"
            if self.facade_banned and FACADE in (
                source,
                *(f"{source}.{n}" for n in names),
            ):
                self.found.append((node.lineno, f"from {source} import ... (a facade)"))
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        base = ast.unparse(node.value)
        home = self.aliases.get(base, base)
        if node.attr in OLD_HOMES.get(home, frozenset()):
            self.found.append((node.lineno, f"{base}.{node.attr}"))
        self.generic_visit(node)

    def visit_Tuple(self, node: ast.Tuple) -> None:
        pair = [elt.value for elt in node.elts if isinstance(elt, ast.Constant)]
        if len(node.elts) == 2 and len(pair) == 2 and all(isinstance(v, str) for v in pair):
            home, name = pair
            if name in OLD_HOMES.get(home, frozenset()):
                self.found.append((node.lineno, f"lazy entry ({home!r}, {name!r})"))
            if self.facade_banned and home == FACADE:
                self.found.append((node.lineno, f"lazy entry ({home!r}, {name!r}) (a facade)"))
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        """``getattr``/``setattr``/``delattr``/``patch.object`` on an old home, by name."""
        func = node.func
        callee = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if callee in ("getattr", "setattr", "delattr", "object") and len(node.args) >= 2:
            target, name = ast.unparse(node.args[0]), node.args[1]
            dynamic = _DYNAMIC_IMPORT.fullmatch(target)
            home = dynamic.group(1) if dynamic else self.aliases.get(target, target)
            if isinstance(name, ast.Constant) and name.value in OLD_HOMES.get(home, frozenset()):
                self.found.append((node.lineno, f"{callee}({target}, {name.value!r})"))
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, str) and (match := _OLD_IN_TEXT.search(node.value)):
            self.found.append((node.lineno, f"string naming {match.group(0)!r}"))


def old_spellings(source: str, module: "str | None") -> list[tuple[int, str]]:
    """``(line, what)`` for each line of *source* that spells a moved name at its old home.

    *module* is the dotted name of a ``src/otto`` file, or ``None`` for any
    other file. One entry per line, the first reason found on it.
    """
    visitor = _OldSpellings(module)
    visitor.visit(ast.parse(source))
    first: dict[int, str] = {}
    for line, what in visitor.found:
        first.setdefault(line, what)
    return sorted(first.items())


def _corpus() -> "list[tuple[Path, str | None]]":
    files: list[tuple[Path, str | None]] = [
        (path, _module_of(path)) for path in sorted(SRC_ROOT.rglob("*.py"))
    ]
    files += [(path, None) for path in sorted(TESTS_ROOT.rglob("*.py")) if path != THIS_FILE]
    return files


def test_the_corpus_is_the_whole_tree():
    """A corpus that came back short would let the check below pass vacuously."""
    corpus = {path.relative_to(PROJECT_ROOT).as_posix() for path, _ in _corpus()}
    assert "src/otto/bootstrap.py" in corpus
    assert "tests/repo3/tests/test_embedded_coverage.py" in corpus  # a bed-lane tree
    assert "tests/integration/test_docker_use_case.py" in corpus
    assert len(corpus) > 1000


def test_no_otto_code_or_test_names_a_moved_name_at_its_old_home():
    offenders = []
    for path, module in _corpus():
        try:
            found = old_spellings(path.read_text(encoding="utf-8"), module)
        except (SyntaxError, UnicodeDecodeError):
            continue  # a deliberately broken fixture file (tests/repo_broken)
        rel = path.relative_to(PROJECT_ROOT).as_posix()
        offenders += [f"{rel}:{line} {what}" for line, what in found]
    assert not offenders, (
        f"{len(offenders)} old spelling(s) of a moved name; import each from its new home "
        "(otto.bootstrap for the repo accessors; otto.config.fleet / otto.config.lab inside otto, "
        "otto.lab in tests and docs, for the lab and fleet names):\n  " + "\n  ".join(offenders)
    )


class TestTheScanSeesEverySpelling:
    """A scan that matched nothing would pass vacuously, so each spelling is pinned."""

    @staticmethod
    def lines(source: str, module: "str | None" = "otto.cli.main") -> list[int]:
        return [line for line, _ in old_spellings(source, module)]

    def test_absolute_and_relative_imports_from_either_old_home(self):
        source = (
            "from otto.config import get_repos\n"
            "from ..config import get_env as env\n"
            "from ..config.bootstrapped import is_bootstrapped\n"
            "from otto.config.bootstrapped import get_completion_names\n"
            "from ..config import Repo\n"
            "from ..bootstrap import get_repos\n"
        )
        assert self.lines(source) == [1, 2, 3, 4]

    def test_a_package_relative_import_inside_otto_config(self):
        """The swallowed case: ``otto.config.scope`` spelled the package as ``.``."""
        assert old_spellings("from . import get_repos\n", "otto.config.scope") == [
            (1, "from otto.config import get_repos")
        ]

    def test_the_deleted_module_in_every_import_form(self):
        source = (
            "import otto.config.bootstrapped\n"
            "import otto.config.bootstrapped as b\n"
            "from otto.config import bootstrapped\n"
            "from .config import bootstrapped\n"
        )
        assert self.lines(source, "otto.context") == [1, 2, 3, 4]

    def test_attribute_spellings_through_a_bound_old_home(self):
        source = (
            "import otto.config\n"
            "import otto.config as cm\n"
            "from otto import config\n"
            "otto.config.get_repos()\n"
            "cm.get_ordered_repos()\n"
            "config.is_bootstrapped()\n"
            "otto.config.Repo\n"
            "thing.get_repos()\n"
        )
        assert self.lines(source, None) == [4, 5, 6]

    def test_strings_patch_targets_and_generated_code(self):
        source = (
            'monkeypatch.setattr("otto.config.bootstrapped.get_repos", f)\n'
            'monkeypatch.setattr("otto.config.get_env", f)\n'
            'CODE = "from otto.config import get_repos\\n"\n'
            'FINE = "otto.config.repo.Repo"\n'
            'ALSO_FINE = "otto.bootstrap.get_repos"\n'
        )
        assert self.lines(source, None) == [1, 2, 3]

    def test_a_lazy_table_entry_naming_an_old_home(self):
        source = '_LAZY_EXPORTS = {"get_repos": ("otto.config", "get_repos")}\n'
        assert self.lines(source, "otto.__init__") == [1]

    def test_a_relative_import_outside_src_names_no_otto_module(self):
        assert old_spellings("from .config import get_repos\n", None) == []

    def test_dynamic_and_patch_object_spellings(self):
        source = (
            "import importlib\n"
            "import otto.config as cm\n"
            'getattr(importlib.import_module("otto.config"), "get_repos")\n'
            'monkeypatch.setattr(cm, "get_env", f)\n'
            'patch.object(cm, "is_bootstrapped")\n'
            'getattr(importlib.import_module("otto.config"), "Repo")\n'
            'hasattr(cm, "get_repos")\n'
        )
        assert self.lines(source, None) == [3, 4, 5]

    def test_the_fleet_names_at_the_otto_config_package(self):
        source = (
            "from otto.config import get_lab\n"
            "from ..config import Repo, all_hosts\n"
            'monkeypatch.setattr("otto.config.get_host", f)\n'
            '_LAZY_EXPORTS = {"load_lab": ("otto.config", "load_lab")}\n'
            "from ..config.fleet import get_lab\n"
            "from ..config.lab import load_lab\n"
        )
        assert self.lines(source) == [1, 2, 3, 4]

    def test_otto_code_never_imports_the_otto_lab_facade(self):
        source = (
            "import otto.lab\n"
            "from otto.lab import get_lab\n"
            "from ..lab import Lab\n"
            "from otto import lab\n"
            '_LAZY_EXPORTS = {"get_lab": ("otto.lab", "get_lab")}\n'
        )
        assert self.lines(source) == [1, 2, 3, 4, 5]
        assert self.lines(source, "otto.examples.reservations_cli") == []  # taught code
        assert self.lines(source, None) == []  # tests and docs may import the facade

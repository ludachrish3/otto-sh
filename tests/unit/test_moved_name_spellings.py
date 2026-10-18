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
(``otto.config.fleet``, ``otto.labs.loader``).

Two more sets moved with spec
``docs/superpowers/specs/2026-10-11-placement-and-phases-design.md`` §3.6. The
runtime settings types and the settings file's location (``SETTINGS_FILENAME``,
``TOML_SETTINGS_PATH``, ``ARCHIVE_SUFFIXES``, ``DockerImage``,
``DockerCompose``, ``DockerUseCase``, ``DockerSettings``,
``MonitorSettings``) left ``otto.config.repo`` for ``otto.config.scope``;
users keep the public path ``otto.config.<name>`` for the four the package
exports. The path-list splitter (``split_path_list``, ``_PATH_LIST_SEP``) left
``otto.config.env`` for ``otto.utils``.

One module moved whole with that spec's §3.7: the lab model and its loader
(``Lab``, ``LAB_SEPARATOR``, ``split_lab_names``, ``_describe``, ``load_lab``,
``_json_source``) left ``otto.config.lab`` for ``otto.labs.loader``. Like the
deleted ``otto.config.bootstrapped``, the old module is gone, so any import of
it, and any string naming it, is an old spelling.

The active run and two of the context's helpers moved with that spec's §3.8. The
active-run slot (``_active``) left ``otto.context`` for ``otto.invocation`` (as
``_ACTIVE``); ``_apply_option_overrides`` left ``otto.config.fleet`` for
``otto.context``, and ``_flag_holding_out`` left it for ``otto.host.builtin_hosts``,
which also owns ``is_builtin_host``: the fleet's wrapper of it is gone.

The run context moved with that spec's §3.12: ``otto.context`` is the public
facade, and every name it does not export (``LIBRARY_LAB_NAME``, ``T``,
``prepared_policy``, ``fill_teardown_deadline``, ``_apply_option_overrides``
and the module's other private helpers) lives in ``otto.run_context`` only.

The composed state moved with that spec's §3.9. The root's result caches
(``_result``, ``_in_progress``) left ``otto.bootstrap`` for the catalog's record
in ``otto.declared``, whose own private readers (``_published``,
``_composed_repos``) became ``published_result`` and ``compose``; the run's
repos (``current_repos``, ``current_ordered_repos``) left ``otto.config.fleet``
for ``otto.declared``, and the rest of the fleet left it for ``otto.fleet``: the
old module is gone. Discovery left the root for ``otto.config.repo``: its cache
(``_discovered``, now that module's ``_discovery`` record), ``discover``,
``DiscoveryResult`` and ``discovered_teardown_deadline``. The root still re-exports
``get_env``, its public name, the same object, so that one is not an old spelling there.

The project loader moved whole with the project vocabulary (spec
``docs/superpowers/specs/2026-10-17-649-project-vocabulary-design.md``):
``otto.config.repo`` is now ``otto.config.project``, so the old module is gone.

An old spelling fails loudly at most call sites, but not everywhere. Six
readers import an accessor inside a broad ``except``, where a stale import
degrades silently instead of raising
(``tests/unit/bootstrap/test_accessor_readers_unpatched.py`` runs them), and
the bed-lane trees (``tests/repo*/``, ``tests/e2e/``, ``tests/integration/``)
are never imported by a unit lane. So this scan reads every ``.py`` file under
``src/otto`` and ``tests/`` without importing any, and refuses:

- ``from <old home> import <moved name>``, absolute or relative, including
  ``from . import get_repos`` inside ``otto.config`` itself;
- any import of a module that is gone (``otto.config.bootstrapped``, ``otto.config.lab``);
- ``<old home>.<moved name>`` through a name bound to the old home
  (``import otto.config``, ``import otto.config as cm``, ``from otto import config``);
- a lazy-table entry ``("<old home>", "<moved name>")``;
- a string that names a moved name at its old home: a patch target
  (``"otto.config.get_repos"``) or generated code (``"from otto.config import get_repos"``),
  or that names a module that is gone;
- in otto's own code (not the shipped examples, which are taught code), any import of the
  ``otto.lab`` facade or a lazy-table entry naming it. This is the broader form, for that one
  facade, of ``tests/unit/test_internal_imports_skip_facades.py``'s rule, whose
  ``scripts/facade_sweep.py --apply`` re-points a ``from otto.lab import``.
"""

import ast
import importlib.util
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
SETTINGS = frozenset(
    {
        "SETTINGS_FILENAME",
        "TOML_SETTINGS_PATH",
        "ARCHIVE_SUFFIXES",
        "DockerImage",
        "DockerCompose",
        "DockerUseCase",
        "DockerSettings",
        "MonitorSettings",
    }
)
SPLITTER = frozenset({"split_path_list", "_PATH_LIST_SEP"})
LAB = frozenset(
    {
        "Lab",
        "LAB_SEPARATOR",
        "split_lab_names",
        "_describe",
        "load_lab",
        "_json_source",
    }
)
ACTIVE_RUN = frozenset({"_active"})
FLEET_HELPERS = frozenset({"_apply_option_overrides", "_flag_holding_out", "is_builtin_host"})
COMPOSED_STATE = frozenset({"_result", "_in_progress"})
DISCOVERY_STATE = frozenset(
    {"_discovered", "discover", "DiscoveryResult", "discovered_teardown_deadline"}
)
CATALOG_READERS = frozenset({"_published", "_composed_repos"})
RUN_REPOS = frozenset({"current_repos", "current_ordered_repos"})
CONTEXT_INTERNALS = frozenset(
    {
        "LIBRARY_LAB_NAME",
        "T",
        "_ContextResolver",
        "_OWNER_MISMATCH",
        "_apply_option_overrides",
        "_flags_hiding_every_match",
        "fill_teardown_deadline",
        "prepared_policy",
    }
)
# Modules that are gone -> the moved names they held. Any import of one, and any string
# naming one, is an old spelling.
GONE: dict[str, frozenset[str]] = {
    "otto.config.bootstrapped": ACCESSORS,
    "otto.config.lab": LAB,
    "otto.config.fleet": FLEET | FLEET_HELPERS | RUN_REPOS,
    "otto.config.repo": SETTINGS,
}
# old home -> the moved names it no longer has
OLD_HOMES: dict[str, frozenset[str]] = {
    "otto.config": ACCESSORS | FLEET,
    **GONE,
    "otto.config.env": SPLITTER,
    "otto.context": ACTIVE_RUN | CONTEXT_INTERNALS,
    "otto.bootstrap": COMPOSED_STATE | DISCOVERY_STATE,
    "otto.declared": CATALOG_READERS,
}
FACADE = "otto.lab"


def _old_in_text(homes: "dict[str, frozenset[str]]") -> "re.Pattern[str]":
    """A string naming a moved name at an old home, or a module that is gone."""
    spellings = [rf"\b{re.escape(module)}\b" for module in GONE]
    for module in GONE:  # a generated import of a gone module from its parent package
        parent, _, leaf = module.removeprefix("otto.").rpartition(".")
        package = re.escape(parent)
        spellings.append(rf"\bfrom\s+(otto\.|\.+){package}\s+import\s+[^\n]*\b{leaf}\b")
    for home, names in homes.items():
        moved = "|".join(sorted(names))
        package = re.escape(home.removeprefix("otto."))
        spellings.append(rf"\b{re.escape(home)}\.({moved})\b")
        spellings.append(rf"\bfrom\s+(otto\.|\.+){package}\s+import\s+[^\n]*\b({moved})\b")
    return re.compile("|".join(spellings))


_OLD_IN_TEXT = _old_in_text(OLD_HOMES)
_DYNAMIC_IMPORT = re.compile(r"(?:importlib\.)?import_module\(['\"]([\w.]+)['\"]\)")


def _gone(module: str) -> bool:
    """Whether *module* is a gone module or one of its submodules."""
    return any(module == gone or module.startswith(gone + ".") for gone in GONE)


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
            if _gone(alias.name):
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
            if _gone(source) or any(f"{source}.{name}" in GONE for name in names):
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
        "(otto.declared for the repo accessors, where a consumer or a patch reaches them; "
        "otto.bootstrap is only their public re-export; otto.fleet / otto.labs.loader inside otto, "
        "otto.lab in tests and docs, for the lab and fleet names; otto.config.scope for the "
        "settings types and the settings file's location; otto.utils for the path-list "
        "splitter; otto.invocation for the active-run slot; otto.run_context for the fleet's "
        "override helper; otto.host.builtin_hosts for the membership rule and "
        "is_builtin_host; otto.declared for the composed state and the run's repos; "
        "otto.config.project for discovery and the project loader). "
        "A line marked (a facade) "
        "is re-pointed by scripts/facade_sweep.py --apply when it is a from-import "
        "(tests/unit/test_internal_imports_skip_facades.py):\n  " + "\n  ".join(offenders)
    )


def test_a_gone_module_does_not_import():
    """A module whose names all moved is deleted, not left behind as a second home."""
    assert {"otto.config.lab", "otto.config.fleet"} <= GONE.keys()  # not an empty table
    still = [module for module in GONE if importlib.util.find_spec(module) is not None]
    assert still == [], f"these modules should be gone, but still import: {still}"


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
            'FINE = "otto.config.project.Project"\n'
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
            "from ..fleet import get_lab\n"
        )
        assert self.lines(source) == [1, 2, 3, 4, 5, 6]

    def test_the_settings_types_and_the_splitter_at_their_old_homes(self):
        source = (
            "from otto.config.repo import DockerImage, Repo\n"
            "from .repo import TOML_SETTINGS_PATH\n"
            "from .env import split_path_list\n"
            'monkeypatch.setattr("otto.config.env._PATH_LIST_SEP", f)\n'
            '_LAZY_EXPORTS = {"DockerImage": ("otto.config.repo", "DockerImage")}\n'
            "import otto.config.repo as r\n"
            "r.MonitorSettings\n"
            'CODE = "from otto.config.repo import ARCHIVE_SUFFIXES\\n"\n'
            "from otto.config.project import Project, selectable_names\n"
            "from otto.config.scope import TOML_SETTINGS_PATH, DockerImage\n"
            "from otto.utils import ottopath_entries\n"
            'FINE = "otto.config.project.Project"\n'
            "r.Project\n"
        )
        assert self.lines(source, "otto.config.collected_tests") == [1, 2, 3, 4, 5, 6, 8]

    def test_the_composed_state_and_the_run_s_repos_at_their_old_homes(self):
        """The root's caches and the run's repos live in ``otto.declared`` now."""
        source = (
            "from otto import bootstrap as bs\n"
            "bs._result\n"
            'monkeypatch.setattr(bs, "_in_progress", None)\n'
            "from otto.config.fleet import current_repos\n"
            'monkeypatch.setattr("otto.config.fleet.current_ordered_repos", f)\n'
            "from otto import declared\n"
            "declared._published()\n"
            "declared._composed_repos()\n"
            "bs.get_repos()\n"
            "declared._composition.result\n"
            "from otto.declared import current_repos, published_result\n"
        )
        assert self.lines(source, None) == [2, 3, 4, 5, 7, 8]

    def test_the_lab_module_that_moved_whole_in_every_spelling(self):
        """``otto.config.lab`` is gone: its names live in ``otto.labs.loader``."""
        source = (
            "from otto.config.lab import Lab\n"
            "from ..config.lab import load_lab, split_lab_names\n"
            "import otto.config.lab\n"
            "import otto.config.lab as lab\n"
            "from otto.config import lab\n"
            'monkeypatch.setattr("otto.config.lab.load_lab", f)\n'
            'CODE = "import otto.config.lab\\n"\n'
            'CODE = "from otto.config import lab\\n"\n'
            "CODE = 'from ..config import Lab, lab'\n"
            "from otto.labs.loader import Lab, load_lab\n"
            "from ..labs.loader import LAB_SEPARATOR\n"
            'FINE = "otto.config.labels"\n'
            'FINE = "from otto.config import labels"\n'
            "otto.labs.loader.load_lab\n"
        )
        assert self.lines(source) == [1, 2, 3, 4, 5, 6, 7, 8, 9]

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

    def test_the_active_run_slot_and_the_fleet_helpers_at_their_old_homes(self):
        """The slot left ``otto.context``; the fleet's helpers left ``otto.config.fleet``."""
        source = (
            "from otto.context import _active\n"
            "from ..config.fleet import _apply_option_overrides, get_host\n"
            "from .fleet import _flag_holding_out\n"
            'monkeypatch.setattr("otto.config.fleet._flag_holding_out", f)\n'
            "from otto import context\n"
            "context._active.get()\n"
            "from otto.config.fleet import is_builtin_host\n"
            'CODE = "from otto.config.fleet import _apply_option_overrides\\n"\n'
            'monkeypatch.setattr("otto.context._active", f)\n'
            "from otto.run_context import _apply_option_overrides, get_context\n"
            "from otto.host.builtin_hosts import is_builtin_host\n"
            "from otto.invocation import _ACTIVE, require_run\n"
            'FINE = "otto.fleet.get_hosts_in_play"\n'
            "context.get_context()\n"
        )
        assert self.lines(source, "otto.config.scope") == [1, 2, 3, 4, 6, 7, 8, 9]

    def test_discovery_at_the_root_it_left(self):
        """Discovery lives in ``otto.config.project``; the root re-exports its public get_env."""
        source = (
            "from otto import bootstrap as bs\n"
            "bs._discovered\n"
            "from otto.bootstrap import DiscoveryResult\n"
            'monkeypatch.setattr("otto.bootstrap.discovered_teardown_deadline", f)\n'
            "from ..bootstrap import discovered_teardown_deadline\n"
            'monkeypatch.setattr(bs, "_discovered", None)\n'
            "bs.discover()\n"
            "bs.get_env()\n"
            "from otto.config.project import DiscoveryResult, discover\n"
            'FINE = "otto.config.project.discover"\n'
        )
        assert self.lines(source) == [2, 3, 4, 5, 6, 7]

    def test_the_run_context_s_internals_at_the_facade(self):
        """``otto.context`` is a facade: its private names live in ``otto.run_context``."""
        source = (
            "from otto.context import LIBRARY_LAB_NAME, OttoContext\n"
            "from ..context import prepared_policy\n"
            'monkeypatch.setattr("otto.context._apply_option_overrides", f)\n'
            "from otto import context\n"
            "context.fill_teardown_deadline(policy)\n"
            "from otto.context import OttoContext, get_context\n"
            "from otto.run_context import LIBRARY_LAB_NAME, prepared_policy\n"
            'FINE = "otto.run_context.fill_teardown_deadline"\n'
            "context.get_context()\n"
        )
        assert self.lines(source) == [1, 2, 3, 5]

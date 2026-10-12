"""Find, and re-point, otto-internal imports that go through a package facade.

Run from the repo root::

    uv run python scripts/facade_sweep.py           # list every site, change nothing
    uv run python scripts/facade_sweep.py --apply   # re-point them; print each changed file
    uv run ruff check --select I001 --fix src/otto  # then sort the rewritten imports
    uv run ruff format src/otto                     # and format the rewritten files

A package facade serves its public names from a lazy table (``_LAZY_ATTRS`` or
``_LAZY_EXPORTS`` in its ``__init__``). ``from otto.inventory import
build_inventory`` is the user's spelling; otto's own code imports the name from
the module that defines it (``otto.inventory.config``), because tach maps an
import to the module it names and would otherwise judge the wrong edge.
``tests/unit/test_internal_imports_skip_facades.py`` fails on any site this
script lists and names these commands as the fix.

A site is a ``from <facade> import <name>`` under ``src/otto`` (absolute or
relative, at any scope, ``TYPE_CHECKING`` included, outside ``otto/examples``)
whose name the facade's table binds to an attribute of a module. The binding is
followed through further facades (``otto``'s ``app`` -> ``otto.cli`` ->
``otto.cli.main``). So is ``<ns>.<name>``, the same name read as an attribute
of a facade package bound by any import (``import otto.inventory as
inventory`` then ``inventory.build_inventory``, ``import otto.inventory``
then ``otto.inventory.build_inventory``, ``from otto import inventory``);
``--apply`` rewrites only ``from`` statements, so such a read is re-pointed by
hand. Bindings are read file-wide, not per scope: a local that shadows a
facade alias (a parameter named ``inventory``) reads as the facade, so rename
the local. Not sites:

* a table entry that names a submodule (``otto.logger``'s ``management``);
* a facade's own declaration mirror: an import directly inside a top-level
  ``if TYPE_CHECKING:`` block of the facade's ``__init__`` that imports exactly
  the module and attribute its own table names for that local name
  (``tests/unit/test_lazy_packages.py`` requires the mirror to match the table).

``--apply`` splits each statement into one ``from`` statement per defining
module, keeping the relative or absolute spelling, the ``as`` aliases, the
indentation and a trailing comment, and prints each changed file relative to
the root, one per line. It leaves import order and layout to the two ruff
commands above, which run under the repository's own ruff configuration; on
a tree that already passes the lint gate they change only the rewritten
files. The script reads source with ``ast`` and imports no otto module. It is
deterministic, and on a clean tree ``--apply`` changes and prints nothing. If
any statement cannot be rewritten, ``--apply`` names every such statement and
writes no file.
"""

import argparse
import ast
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

LAZY_TABLES = ["_LAZY_ATTRS", "_LAZY_EXPORTS"]

FIX_COMMANDS = [
    "uv run python scripts/facade_sweep.py --apply",
    "uv run ruff check --select I001 --fix src/otto",
    "uv run ruff format src/otto",
]
"""The fix ``tests/unit/test_internal_imports_skip_facades.py`` names, in order."""

EXEMPT = "otto/examples/"
"""Taught code: it imports the way a user does."""

Facades = dict[str, dict[str, list[str] | None]]
"""Each facade package -> {name: [module, attribute]}, ``None`` for a submodule entry."""


@dataclass
class Site:
    """One name imported through a facade, and where it is defined."""

    path: str
    """The file, relative to ``src`` (``otto/link/check.py``)."""
    line: int
    facade: str
    name: str
    asname: str | None
    module: str
    """The module that defines the name."""
    attribute: str
    """Its name there (a ``_LAZY_EXPORTS`` pair may rename it)."""
    scope: str
    """The enclosing function or class path, ``<module>`` at module level."""
    type_checking: bool
    read_as: str | None = None
    """For an attribute read, the expression read (``inventory.build_inventory``)."""

    def describe(self) -> str:
        """Return the one-line report ``--list`` and the rule test print."""
        guard = " TYPE_CHECKING" if self.type_checking else ""
        spelled = (
            f"{self.read_as} reads {self.facade}'s {self.name} (re-point it by hand)"
            if self.read_as
            else f"from {self.facade} import {self.name}"
        )
        return (
            f"{self.path}:{self.line} [{self.scope}{guard}] {spelled} -> "
            f"import {self.attribute} from {self.module}"
        )


@dataclass
class Statement:
    """One ``from`` statement that imports at least one name through a facade."""

    path: str
    node: ast.ImportFrom
    sites: list[Site]


def module_name(path: str) -> str:
    """Return ``otto.a.b`` for ``otto/a/b.py`` and ``otto.a`` for ``otto/a/__init__.py``."""
    parts = path.removesuffix(".py").split("/")
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


def package_of(path: str) -> str:
    """Return the package a relative import in *path* is resolved against."""
    name = module_name(path)
    return name if path.endswith("__init__.py") else name.rpartition(".")[0]


def _table_entries(node: ast.stmt) -> "list[tuple[str, ast.Dict]]":
    if isinstance(node, ast.Assign):
        targets, value = node.targets, node.value
    elif isinstance(node, ast.AnnAssign):
        targets, value = [node.target], node.value
    else:
        return []
    if not isinstance(value, ast.Dict):
        return []
    return [
        (target.id, value)
        for target in targets
        if isinstance(target, ast.Name) and target.id in LAZY_TABLES
    ]


def lazy_bindings(src_root: Path) -> Facades:
    """Read every facade's literal lazy tables under *src_root*/otto."""
    facades: Facades = {}
    for init in sorted((src_root / "otto").rglob("__init__.py")):
        table: dict[str, list[str] | None] = {}
        where = init.relative_to(src_root).as_posix()
        for node in ast.parse(init.read_text(encoding="utf-8")).body:
            for kind, value in _table_entries(node):
                try:
                    entries = ast.literal_eval(value)
                except ValueError:
                    raise SystemExit(f"{where}: lazy table is not a literal dict") from None
                for name, entry in entries.items():
                    if isinstance(entry, tuple):
                        table[name] = list(entry)
                    elif kind == "_LAZY_ATTRS":
                        table[name] = [entry, name]
                    else:
                        table[name] = None
        if table:
            facades[module_name(where)] = table
    return facades


def owner(facades: Facades, facade: str, name: str) -> list[str] | None:
    """Return ``[module, attribute]`` defining *facade*'s *name*, followed through facades."""
    binding = facades[facade].get(name)
    seen: list[list[str]] = []
    while binding is not None and binding[1] in facades.get(binding[0], {}):
        if binding in seen:
            return binding
        seen.append(binding)
        following = facades[binding[0]][binding[1]]
        if following is None:
            return binding
        binding = following
    return binding


def _is_type_checking(test: ast.expr) -> bool:
    return ast.unparse(test) in ("TYPE_CHECKING", "typing.TYPE_CHECKING")


class _Finder(ast.NodeVisitor):
    def __init__(self, path: str, facades: Facades) -> None:
        self.path = path
        self.facades = facades
        self.package = package_of(path)
        self.own = facades.get(module_name(path)) if path.endswith("__init__.py") else None
        self.scope: list[str] = []
        self.type_checking = 0
        self.mirror: list[ast.ImportFrom] = []
        self.namespaces: dict[str, str] = {}
        self.found: list[Statement] = []
        self.reads: list[Site] = []

    def run(self, tree: ast.Module) -> None:
        if self.own is not None:
            for node in tree.body:
                if isinstance(node, ast.If) and _is_type_checking(node.test):
                    self.mirror += [s for s in node.body if isinstance(s, ast.ImportFrom)]
        for node in ast.walk(tree):
            self._bind(node)
        self.visit(tree)

    def _bind(self, node: ast.AST) -> None:
        """Record each local name an import binds to a module, file-wide."""
        if isinstance(node, ast.Import):
            for alias in node.names:
                local = alias.asname or alias.name.partition(".")[0]
                self.namespaces[local] = alias.name if alias.asname else local
        elif isinstance(node, ast.ImportFrom):
            imported = self._imported(node)
            for alias in node.names:
                self.namespaces[alias.asname or alias.name] = f"{imported}.{alias.name}"

    def _namespace(self, node: ast.expr) -> str | None:
        """Return the module *node* names through an imported name, or ``None``."""
        if isinstance(node, ast.Name):
            return self.namespaces.get(node.id)
        if isinstance(node, ast.Attribute) and (outer := self._namespace(node.value)):
            return f"{outer}.{node.attr}"
        return None

    def _scoped(self, node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> None:
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._scoped(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._scoped(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._scoped(node)

    def visit_If(self, node: ast.If) -> None:
        guarded = _is_type_checking(node.test)
        self.type_checking += guarded
        for statement in node.body:
            self.visit(statement)
        self.type_checking -= guarded
        for statement in node.orelse:
            self.visit(statement)

    def _imported(self, node: ast.ImportFrom) -> str:
        if not node.level:
            return node.module or ""
        base = self.package.split(".")
        base = base[: len(base) - node.level + 1]
        return ".".join([*base, *([node.module] if node.module else [])])

    def visit_Attribute(self, node: ast.Attribute) -> None:
        facade = self._namespace(node.value)
        binding = owner(self.facades, facade, node.attr) if facade in self.facades else None
        if facade is not None and binding is not None:
            self.reads.append(
                Site(
                    path=self.path,
                    line=node.lineno,
                    facade=facade,
                    name=node.attr,
                    asname=None,
                    module=binding[0],
                    attribute=binding[1],
                    scope=".".join(self.scope) or "<module>",
                    type_checking=self.type_checking > 0,
                    read_as=ast.unparse(node),
                )
            )
        self.generic_visit(node)

    def _is_mirror(self, node: ast.ImportFrom, imported: str, alias: ast.alias) -> bool:
        if self.own is None or node not in self.mirror:
            return False
        return self.own.get(alias.asname or alias.name) == [imported, alias.name]

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        imported = self._imported(node)
        if imported not in self.facades:
            return
        sites = []
        for alias in node.names:
            binding = owner(self.facades, imported, alias.name)
            if binding is None or self._is_mirror(node, imported, alias):
                continue
            sites.append(
                Site(
                    path=self.path,
                    line=node.lineno,
                    facade=imported,
                    name=alias.name,
                    asname=alias.asname,
                    module=binding[0],
                    attribute=binding[1],
                    scope=".".join(self.scope) or "<module>",
                    type_checking=self.type_checking > 0,
                )
            )
        if sites:
            self.found.append(Statement(self.path, node, sites))


def _finders(src_root: Path) -> list[_Finder]:
    facades = lazy_bindings(src_root)
    finders = []
    for file in sorted((src_root / "otto").rglob("*.py")):
        path = file.relative_to(src_root).as_posix()
        if path.startswith(EXEMPT):
            continue
        finder = _Finder(path, facades)
        finder.run(ast.parse(file.read_text(encoding="utf-8"), str(file)))
        finders.append(finder)
    return finders


def find_statements(src_root: Path) -> list[Statement]:
    """Return every statement under *src_root* that imports a name through a facade."""
    return [statement for finder in _finders(src_root) for statement in finder.found]


def find_sites(src_root: Path) -> list[Site]:
    """Return every otto-internal use of a facade's lazy-table binding under *src_root*.

    Each file's sites are in line order: its ``from`` imports and its reads of
    a facade package's attribute.
    """
    return [
        site
        for finder in _finders(src_root)
        for site in sorted(
            [*(s for statement in finder.found for s in statement.sites), *finder.reads],
            key=lambda s: s.line,
        )
    ]


def _spelling(path: str, module: str, level: int) -> str:
    """Return *module* as ``from <this> import`` writes it in *path*, relative if *level*."""
    if not level:
        return module
    package = package_of(path).split(".")
    target = module.split(".")
    common = 0
    while common < min(len(package), len(target)) and package[common] == target[common]:
        common += 1
    return "." * (len(package) - common + 1) + ".".join(target[common:])


def _alias(name: str, asname: str | None) -> str:
    return name if asname in (None, name) else f"{name} as {asname}"


def rewrite_statement(path: str, node: ast.ImportFrom, sites: list[Site], indent: str) -> str:
    """Return the statements that replace *node*: one per defining module, unbound names kept."""
    bound = {site.name for site in sites}
    groups: dict[str, list[str]] = {}
    for site in sites:
        groups.setdefault(site.module, []).append(_alias(site.attribute, site.asname or site.name))
    kept = [_alias(a.name, a.asname) for a in node.names if a.name not in bound]
    lines = []
    if kept:
        lines.append(f"{indent}from {'.' * node.level}{node.module or ''} import {', '.join(kept)}")
    lines += [
        f"{indent}from {_spelling(path, module, node.level)} import {', '.join(groups[module])}"
        for module in sorted(groups)
    ]
    return "\n".join(lines)


def _cut(line: str, offset: int) -> list[str]:
    """Split *line* at *offset*, which counts UTF-8 bytes, as AST columns do."""
    raw = line.encode("utf-8")
    return [raw[:offset].decode("utf-8"), raw[offset:].decode("utf-8")]


def _indent(lines: list[str], node: ast.ImportFrom) -> str:
    """Return what precedes the statement on its first line."""
    return _cut(lines[node.lineno - 1], node.col_offset)[0]


def _trailer(lines: list[str], node: ast.ImportFrom) -> str:
    """Return what follows the statement on its last line, stripped."""
    return _cut(lines[(node.end_lineno or node.lineno) - 1], node.end_col_offset or 0)[1].strip()


def _problem(lines: list[str], path: str, node: ast.ImportFrom) -> str | None:
    first, last = node.lineno - 1, (node.end_lineno or node.lineno) - 1
    trailer = _trailer(lines, node)
    if _indent(lines, node).strip() or (trailer and not trailer.startswith("#")):
        return f"{path}:{node.lineno} shares its line with another statement"
    own = [*lines[first:last], _cut(lines[last], node.end_col_offset or 0)[0]]
    if "#" in "\n".join(own):
        return f"{path}:{node.lineno} carries a comment inside the statement"
    return None


def problems(text: str, statements: list[Statement]) -> list[str]:
    """Return why each statement in *text* cannot be rewritten, one line per statement."""
    lines = text.split("\n")
    return [p for st in statements if (p := _problem(lines, st.path, st.node))]


def rewrite_source(text: str, statements: list[Statement]) -> str:
    """Return *text* with each of its statements re-pointed; refuse a shape it cannot carry."""
    refused = problems(text, statements)
    if refused:
        raise SystemExit("\n".join(refused))
    lines = text.split("\n")
    for statement in sorted(statements, key=lambda st: -st.node.lineno):
        node = statement.node
        first, last = node.lineno - 1, (node.end_lineno or node.lineno) - 1
        trailer = _trailer(lines, node)
        indent = _indent(lines, node)
        new = rewrite_statement(statement.path, node, statement.sites, indent).split("\n")
        if trailer:
            new[0] = f"{new[0]}  {trailer}"
        lines[first : last + 1] = new
    return "\n".join(lines)


def apply(root: Path) -> list[Path]:
    """Re-point every site under *root*/src and return the changed files, unformatted.

    Every file is read and checked before any is written: if one statement
    anywhere cannot be rewritten, nothing changes and every refusal is reported.
    """
    src_root = root / "src"
    by_file: dict[str, list[Statement]] = {}
    for statement in find_statements(src_root):
        by_file.setdefault(statement.path, []).append(statement)
    texts = {path: (src_root / path).read_text(encoding="utf-8") for path in sorted(by_file)}
    refused = [line for path, text in texts.items() for line in problems(text, by_file[path])]
    if refused:
        raise SystemExit("\n".join(refused))
    changed = []
    for path, text in texts.items():
        file = src_root / path
        file.write_text(rewrite_source(text, by_file[path]), encoding="utf-8")
        changed.append(file)
    return changed


def main(argv: list[str] | None = None) -> int:
    """List the sites, or with ``--apply`` re-point them and print the changed files."""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--apply", action="store_true", help="re-point every site and print the changed files"
    )
    parser.add_argument(
        "--root", type=Path, default=ROOT, help="the repository root to read (default: this one)"
    )
    args = parser.parse_args(argv)
    if args.apply:
        for file in apply(args.root):
            print(file.relative_to(args.root))
        return 0
    sites = find_sites(args.root / "src")
    for site in sites:
        print(site.describe())
    files = len({site.path for site in sites})
    print(f"# {len(sites)} sites in {files} files", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())

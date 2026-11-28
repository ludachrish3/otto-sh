"""The import walk shared by the static import guards.

``tests/unit/test_monitor_service_reads_no_lab.py``, ``test_scope_guard.py``,
``test_moved_name_spellings.py`` and ``test_no_import_time_lazy_exports.py``
read ``src/otto`` imports the way tach resolves them; this is their one
resolver (a file's module, relative imports, ``TYPE_CHECKING`` bodies), so
they cannot drift.
"""

import ast
from pathlib import Path

from tests._fixtures.paths import CHECKOUT_ROOT

SRC = CHECKOUT_ROOT / "src"
SKIPPED_TESTS = ["TYPE_CHECKING", "typing.TYPE_CHECKING"]


def module_of(path: Path) -> str:
    """Return the dotted module of the file at *path* under ``src``.

    ``otto.a.b`` for ``src/otto/a/b.py``, and ``otto.a.__init__`` for a
    package's ``__init__.py``, so a relative import resolves against
    :func:`package_of` either way.
    """
    return ".".join(path.relative_to(SRC).with_suffix("").parts)


def package_of(path: Path) -> str:
    """Return the package a relative import in the file at *path* resolves against.

    That is its directory, for a package's ``__init__.py`` and a module alike.
    """
    return ".".join(path.relative_to(SRC).parent.parts)


def resolve_import(package: str, node: ast.ImportFrom) -> str:
    """Return the absolute dotted module a ``from`` import in *package* names."""
    if not node.level:
        return node.module or ""
    base = package.split(".")
    base = base[: len(base) - node.level + 1]
    return ".".join([*base, *([node.module] if node.module else [])])


class ImportWalk(ast.NodeVisitor):
    """Collect every import statement in a source, as tach counts them.

    Reads any depth and every spelling; skips the body of an ``if`` whose test
    is exactly ``TYPE_CHECKING`` or ``typing.TYPE_CHECKING`` (not an ``elif``).
    """

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
        module = resolve_import(self.package, node)
        self.found.append([node.lineno, module])
        self.found += [[node.lineno, f"{module}.{alias.name}"] for alias in node.names]

"""The import strings of ``src/otto``: the dynamic-import audit's one reader.

``tests/unit/test_dynamic_import_audit.py`` audits every literal and computed
import target in ``src/otto``; ``tests/unit/test_monitor_service_reads_no_lab.py``
reads the monitor's files through :func:`strings_in` too, so both guards see
the same strings.
"""

import ast
import dataclasses
from pathlib import Path

from tests._fixtures.paths import CHECKOUT_ROOT

SRC_ROOT = CHECKOUT_ROOT / "src"

LAZY_TABLES = ["_LAZY_ATTRS", "_LAZY_EXPORTS"]


@dataclasses.dataclass(frozen=True)
class StringSite:
    """A literal import target in ``src/otto``."""

    path: str
    """The file, under ``src/`` (``otto/host/__init__.py``)."""
    line: int
    target: str
    """``pkg.module:attr``, or ``pkg.module`` for a module."""
    kind: str
    """``lazy-table``, ``ref``, ``ref-table``, ``import_module`` or ``cli-loader``."""

    @property
    def key(self) -> "tuple[str, str]":
        """The site as ``STRING_EDGES`` and ``GRANDFATHERED`` spell it."""
        return (self.path, self.target)

    @property
    def target_module(self) -> str:
        """The module the target names."""
        return self.target.partition(":")[0]


@dataclasses.dataclass(frozen=True)
class ComputedSite:
    """A call that imports a target computed at run time."""

    path: str
    line: int
    scope: str
    """The enclosing ``Class.function`` qualname, or ``<module>``."""
    expanded: bool
    """Whether every target came from a literal table the audit could read."""


@dataclasses.dataclass
class Extraction:
    """What the parse of ``src/otto`` found."""

    literals: "list[StringSite]"
    computed: "list[ComputedSite]"
    lazy_table_files: "list[str]"
    refused: "list[str]" = dataclasses.field(default_factory=list)
    """``Ref`` and ``import_module`` calls in a form the audit cannot read."""


def name_of_call(node: ast.Call) -> "str | None":
    """The called name: ``f`` of ``f(...)`` and ``attr`` of ``x.attr(...)``."""
    func = node.func
    return func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)


def _call_kind(node: ast.Call) -> "str | None":
    kinds = {"Ref": "ref", "import_module": "import_module", "register_cli_command": "cli-loader"}
    return kinds.get(name_of_call(node) or "")


# The parameter that names the target: Ref's dataclass field, import_module's first
# argument, register_cli_command's loader (its second, after the command's name).
_TARGET_KEYWORD = {"ref": "target", "import_module": "name", "cli-loader": "loader"}


def _target_argument(node: ast.Call, kind: str) -> "ast.expr | None":
    """The call's target, given positionally or by keyword; ``None`` for any other form.

    A starred or ``**`` argument, a second argument (``import_module``'s
    ``package``, a relative import), or both spellings at once cannot be read,
    so the audit refuses the call rather than guess. ``register_cli_command``
    takes the command's name and options beside its loader, so only a starred
    or ``**`` argument hides its loader.
    """
    keyword = _TARGET_KEYWORD[kind]
    if any(isinstance(arg, ast.Starred) for arg in node.args):
        return None
    if kind == "cli-loader":
        if any(item.arg is None for item in node.keywords):
            return None
        named = [item.value for item in node.keywords if item.arg == keyword]
        given = [*node.args[1:2], *named]
    elif any(item.arg != keyword for item in node.keywords):
        return None
    else:
        given = [*node.args, *(item.value for item in node.keywords)]
    return given[0] if len(given) == 1 else None


def _builds_a_string(node: ast.AST) -> bool:
    """Whether *node* builds a string: an f-string, ``+`` or ``%``, or ``.format``."""
    return isinstance(node, (ast.JoinedStr, ast.BinOp)) or (
        isinstance(node, ast.Call) and name_of_call(node) == "format"
    )


def _literal(node: ast.AST) -> "str | None":
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _evaluate(expr: ast.AST, env: "dict[str, str]") -> "str | None":
    """Evaluate a name, a literal or an f-string of names against one table row."""
    if (value := _literal(expr)) is not None:
        return value
    if isinstance(expr, ast.Name):
        return env.get(expr.id)
    if isinstance(expr, ast.JoinedStr):
        pieces = []
        for part in expr.values:
            if isinstance(part, ast.FormattedValue):
                if part.conversion != -1 or part.format_spec is not None:
                    return None
                if not isinstance(part.value, ast.Name) or part.value.id not in env:
                    return None
                pieces.append(env[part.value.id])
            elif (text := _literal(part)) is not None:
                pieces.append(text)
        return "".join(pieces)
    return None


def _rebinds(loop: ast.For, names: "list[str | None]") -> bool:
    """Whether the loop's body assigns to one of its own loop variables."""
    return any(
        isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store) and node.id in names
        for statement in loop.body
        for node in ast.walk(statement)
    )


def _table_rows(loop: ast.For) -> "list[dict[str, str]] | None":
    """Each row of ``for a, b in [(...), ...]`` as {name: literal}, or ``None`` if no table.

    Conservative: a body that rebinds a loop variable makes the rows untrustworthy,
    so the loop is no table.
    """
    if not isinstance(loop.iter, (ast.List, ast.Tuple)):
        return None
    if isinstance(loop.target, ast.Name):
        names: list[str | None] = [loop.target.id]
    elif isinstance(loop.target, ast.Tuple):
        names = [e.id if isinstance(e, ast.Name) else None for e in loop.target.elts]
    else:
        return None
    if _rebinds(loop, names):
        return None
    rows = []
    for element in loop.iter.elts:
        values = element.elts if isinstance(element, ast.Tuple) else [element]
        if len(values) != len(names):
            return None
        row = {}
        for name, value in zip(names, values, strict=True):
            if name is not None and (text := _literal(value)) is not None:
                row[name] = text
        rows.append(row)
    return rows


class _Collector(ast.NodeVisitor):
    def __init__(self, path: str, found: Extraction) -> None:
        self.path = path
        self.found = found
        self.scope: list[str] = []
        self.loops: list[ast.For] = []

    def _enter(self, node: "ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef") -> None:
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._enter(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._enter(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._enter(node)

    def visit_For(self, node: ast.For) -> None:
        self.loops.append(node)
        self.generic_visit(node)
        self.loops.pop()

    def visit_Assign(self, node: ast.Assign) -> None:
        for target in node.targets:
            self._lazy_table(target, node.value)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self._lazy_table(node.target, node.value)
        self.generic_visit(node)

    def _lazy_table(self, target: ast.AST, value: "ast.AST | None") -> None:
        if self.scope or not isinstance(target, ast.Name) or target.id not in LAZY_TABLES:
            return
        if not isinstance(value, ast.Dict):
            return
        self.found.lazy_table_files.append(self.path)
        for key, entry in zip(value.keys, value.values, strict=True):
            name = _literal(key) if key is not None else None
            if isinstance(entry, ast.Tuple) and len(entry.elts) == 2:
                module, attr = _literal(entry.elts[0]), _literal(entry.elts[1])
                target_text = f"{module}:{attr}"
            elif target.id == "_LAZY_ATTRS":
                target_text = f"{_literal(entry)}:{name}"
            else:
                target_text = str(_literal(entry))
            self.found.literals.append(
                StringSite(self.path, entry.lineno, target_text, "lazy-table")
            )

    def visit_Call(self, node: ast.Call) -> None:
        kind = _call_kind(node)
        argument = _target_argument(node, kind) if kind is not None else None
        if kind is not None and argument is None:
            self.found.refused.append(
                f"{self.path}:{node.lineno} {kind} call in a form the audit cannot read: "
                f"{ast.unparse(node)}"
            )
        elif kind is not None and argument is not None:
            if (text := _literal(argument)) is not None:
                self.found.literals.append(StringSite(self.path, node.lineno, text, kind))
            elif kind == "cli-loader" and not _builds_a_string(argument):
                # A function or a Typer app: an object to register, not a string to import.
                pass
            else:
                rows = _table_rows(self.loops[-1]) if self.loops else None
                values = [_evaluate(argument, row) for row in rows] if rows else []
                expanded = bool(values) and all(v is not None for v in values)
                if expanded:
                    self.found.literals.extend(
                        StringSite(self.path, node.lineno, str(v), f"{kind}-table") for v in values
                    )
                scope = ".".join(self.scope) or "<module>"
                self.found.computed.append(ComputedSite(self.path, node.lineno, scope, expanded))
        self.generic_visit(node)


def strings_in(text: str, path: str, found: "Extraction | None" = None) -> Extraction:
    """Parse one file's *text* for its import strings, adding them to *found*.

    *path* is the file under ``src/`` (``otto/monitor/collector.py``); each
    site found carries it. ``tests/unit/test_monitor_service_reads_no_lab.py``
    reads the monitor's files through this too, so both guards see the same strings.
    """
    found = found if found is not None else Extraction([], [], [])
    _Collector(path, found).visit(ast.parse(text, path))
    return found


def extract(src_root: Path = SRC_ROOT) -> Extraction:
    """Parse every ``.py`` file under *src_root*/otto for its import strings."""
    found = Extraction([], [], [])
    for file in sorted((src_root / "otto").rglob("*.py")):
        strings_in(file.read_text(encoding="utf-8"), file.relative_to(src_root).as_posix(), found)
    return found

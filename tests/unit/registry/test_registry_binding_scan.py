"""An AST scan for registry bindings that ast-grep's patterns cannot resolve.

ast-grep matches spellings: ``class K(Registry)`` and ``class K(registry.Registry)``
are visible to ``no-registry-subclass``, but an alias (``from otto.registry
import Registry as R``), a qualified import (``import otto.registry as reg``) or
a relative one (``from .. import registry``) is not. This scan resolves every
import of the engine module, so a subclass of an engine type is found however
it is spelled.

The same file scans the signatures of every registration and construction
surface beside an engine table: no ``Any``, no ``Callable[..., Any]``, no bare
``Registry``/``BackendRegistry`` and no bare ``Ref``.
"""

import ast

from tests._fixtures.paths import PROJECT_ROOT

ENGINE = {"Registry", "BackendRegistry", "Subscription", "RegistryView"}
ENGINE_MODULE = "otto.registry"
SRC = PROJECT_ROOT / "src" / "otto"


def _absolute(node: ast.ImportFrom, module: str, is_package: bool) -> str:
    """The absolute module an ImportFrom names, resolving ``from .. import x`` (module is None)."""
    if node.level == 0:
        return node.module or ""
    parts = module.split(".")
    base = parts if is_package else parts[:-1]
    base = base[: len(base) - (node.level - 1)]
    return ".".join([*base, node.module] if node.module else base)


def _engine_aliases(tree: ast.Module, module: str, is_package: bool) -> "list[set[str]]":
    """``[names, modules]``: local names bound to an engine type, and to the engine module."""
    names: set[str] = set()
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            source = _absolute(node, module, is_package)
            for alias in node.names:
                if source == ENGINE_MODULE and alias.name in ENGINE:
                    names.add(alias.asname or alias.name)
                elif f"{source}.{alias.name}" == ENGINE_MODULE:
                    # from otto import registry / from .. import registry
                    modules.add(alias.asname or alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == ENGINE_MODULE:
                    # bound as "otto.registry" when not aliased
                    modules.add(alias.asname or alias.name)
    return [names, modules]


def _is_engine_ref(expr: ast.expr, names: set[str], modules: set[str]) -> bool:
    if isinstance(expr, ast.Subscript):
        expr = expr.value
    if isinstance(expr, ast.Name):
        return expr.id in names
    return (
        isinstance(expr, ast.Attribute)
        and expr.attr in ENGINE
        and ast.unparse(expr.value) in modules
    )


def scan(source: str, module: str = "otto.host.sample", is_package: bool = False) -> "list[str]":
    """Every class in *source* that subclasses an engine type, however the type is imported."""
    tree = ast.parse(source)
    names, modules = _engine_aliases(tree, module, is_package)
    return [
        f"subclass {node.name}"
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef)
        and any(_is_engine_ref(b, names, modules) for b in node.bases)
    ]


def _src_modules() -> "list[tuple[str, bool, str]]":
    """``(module, is_package, source)`` for every module under ``src/otto``."""
    found = []
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC.parent).with_suffix("")
        is_package = path.name == "__init__.py"
        module = ".".join(rel.parts[:-1] if is_package else rel.parts)
        found.append((module, is_package, path.read_text()))
    return found


def test_the_scan_sees_aliases_and_qualified_imports():
    assert scan("from otto.registry import Registry as R\nclass X(R[int]): ...") == ["subclass X"]
    assert scan("import otto.registry as reg\nclass Y(reg.BackendRegistry): ...") == ["subclass Y"]
    assert scan("import otto.registry\nclass W(otto.registry.Registry): ...") == ["subclass W"]
    assert scan("from .. import registry\nclass Z(registry.Subscription): ...") == ["subclass Z"]
    assert scan("from ..registry import RegistryView\nclass V(RegistryView): ...") == ["subclass V"]
    # constrained to the ENGINE module: a same-named class elsewhere is not the engine
    assert scan("from otto.cli import registry\nclass N(registry.Registry): ...") == []
    assert (
        scan("from .registry import Registry\nclass M(Registry): ...", module="otto.labs.sources")
        == []
    )
    assert scan("class Ok(dict): ...") == []


def test_no_module_subclasses_an_engine_type():
    """Red: add ``class KindRegistry(Registry)`` to a module that imports the engine."""
    offenders = {}
    for module, is_package, source in _src_modules():
        if module == ENGINE_MODULE:
            continue
        if hits := scan(source, module, is_package):
            offenders[module] = hits
    assert offenders == {}


# ── signatures of the registration and construction surface ─────────────────

_BARE_TABLES = {"Registry", "BackendRegistry"}


def _annotation(node: "ast.expr | None") -> "ast.expr | None":
    """The annotation as an expression, parsing a string (forward-reference) annotation."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return ast.parse(node.value, mode="eval").body
    return node


def _last_name(expr: ast.expr) -> "str | None":
    """``Any`` for ``Any`` and ``typing.Any`` alike; ``None`` for anything else."""
    if isinstance(expr, ast.Name):
        return expr.id
    if isinstance(expr, ast.Attribute):
        return expr.attr
    return None


def _union_members(expr: ast.expr) -> "list[ast.expr]":
    """The members of ``A | B``, ``Optional[A]`` and ``Union[A, B]``; ``[expr]`` otherwise."""
    if isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.BitOr):
        return [*_union_members(expr.left), *_union_members(expr.right)]
    if isinstance(expr, ast.Subscript) and _last_name(expr.value) in {"Optional", "Union"}:
        inner = expr.slice
        items = inner.elts if isinstance(inner, ast.Tuple) else [inner]
        return [member for item in items for member in _union_members(item)]
    return [expr]


def _is_none(expr: ast.expr) -> bool:
    return isinstance(expr, ast.Constant) and expr.value is None


def _forbidden(expr: ast.expr) -> "str | None":
    """Name the forbidden shape *expr* is, or ``None`` when it is allowed."""
    members = _union_members(expr)
    for member in members:
        name = _last_name(member)
        if name == "Any":
            return "Any"
        if name in _BARE_TABLES:
            return f"a bare {name}"
        if (
            isinstance(member, ast.Subscript)
            and _last_name(member.value) == "Callable"
            and isinstance(member.slice, ast.Tuple)
            and len(member.slice.elts) == 2
            and isinstance(member.slice.elts[0], ast.Constant)
            and member.slice.elts[0].value is Ellipsis
            and _last_name(member.slice.elts[1]) == "Any"
        ):
            return "Callable[..., Any]"
    typed = [m for m in members if not _is_none(m) and _last_name(m) != "Ref"]
    if any(_last_name(m) == "Ref" for m in members) and not typed:
        return "a bare Ref"
    return None


def _marked(fn: "ast.FunctionDef | ast.AsyncFunctionDef") -> bool:
    return any(_last_name(d) == "registration_boundary" for d in fn.decorator_list)


def _is_surface(fn: "ast.FunctionDef | ast.AsyncFunctionDef") -> bool:
    """A public ``register_*``/``build_*`` function, or a public wrapper decorator.

    A wrapper decorator is marked as a registration boundary itself, or is a
    decorator factory whose inner decorator is marked.
    """
    if fn.name.startswith("_"):
        return False
    if fn.name.startswith(("register_", "build_")) or _marked(fn):
        return True
    return any(
        isinstance(inner, (ast.FunctionDef, ast.AsyncFunctionDef)) and _marked(inner)
        for inner in ast.walk(fn)
        if inner is not fn
    )


def _builds_a_table(tree: ast.Module) -> bool:
    """Whether *tree* assigns a module-level engine construction."""
    for stmt in tree.body:
        value = stmt.value if isinstance(stmt, (ast.Assign, ast.AnnAssign)) else None
        if isinstance(value, ast.Call):
            func = value.func.value if isinstance(value.func, ast.Subscript) else value.func
            if _last_name(func) in ENGINE:
                return True
    return False


def scan_signatures(source: str) -> "list[str]":
    """Every forbidden annotation on the registration surface of a table-building *source*."""
    tree = ast.parse(source)
    if not _builds_a_table(tree):
        return []
    hits = []
    for fn in tree.body:
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) or not _is_surface(fn):
            continue
        args = fn.args
        params = [*args.posonlyargs, *args.args, *args.kwonlyargs]
        params += [a for a in (args.vararg, args.kwarg) if a is not None]
        annotated = [(p.arg, p.annotation) for p in params]
        annotated.append(("return", fn.returns))
        for where, node in annotated:
            expr = _annotation(node)
            if expr is not None and (shape := _forbidden(expr)) is not None:
                hits.append(f"{fn.name} {where}: {shape}")
    return hits


_TABLE = (
    "from otto.registry import Registry\n"
    "T: 'Registry[int]' = Registry('t', entry=E, register_hint='x')\n"
)


def test_the_signature_scan_flags_each_forbidden_shape():
    flagged = {
        "def register_x(obj: Any) -> None: ...": ["register_x obj: Any"],
        "def register_x(obj: 'typing.Any') -> None: ...": ["register_x obj: Any"],
        "def build_x(name: str) -> Any: ...": ["build_x return: Any"],
        "def register_x(fn: Callable[..., Any]) -> None: ...": [
            "register_x fn: Callable[..., Any]"
        ],
        "def register_x(table: Registry) -> None: ...": ["register_x table: a bare Registry"],
        "def register_x(table: BackendRegistry) -> None: ...": [
            "register_x table: a bare BackendRegistry"
        ],
        "def register_x(cls: Ref) -> None: ...": ["register_x cls: a bare Ref"],
        "def register_x(cls: 'Ref | None') -> None: ...": ["register_x cls: a bare Ref"],
        "def register_x(cls: Optional[Ref]) -> None: ...": ["register_x cls: a bare Ref"],
        "def register_x(cls: 'Any | None') -> None: ...": ["register_x cls: Any"],
        "def register_x(**kw: Any) -> None: ...": ["register_x kw: Any"],
        "@registration_boundary\ndef widget(fn: Any) -> Any: ...": [
            "widget fn: Any",
            "widget return: Any",
        ],
        "def widget(name: str) -> Callable[..., Any]:\n"
        "    @registration_boundary\n    def deco(fn): ...\n    return deco\n": [
            "widget return: Callable[..., Any]"
        ],
    }
    for snippet, expected in flagged.items():
        assert scan_signatures(_TABLE + snippet) == expected, snippet


def test_the_signature_scan_allows_typed_shapes():
    allowed = [
        "def register_x(cls: 'type[Frame] | Ref', *, overwrite: bool = False) -> None: ...",
        "def register_x(cls: Union[type[Frame], Ref]) -> None: ...",
        "def register_x(table: 'Registry[ClassEntry[Frame]]') -> None: ...",
        "def build_x(name: str) -> Frame: ...",
        "def register_x(fn: Callable[[Host], list[str]]) -> None: ...",
        "def register_x(raw: 'Mapping[str, object]') -> None: ...",
        "def _register_private(obj: Any) -> None: ...",
        "def describe(obj: Any) -> str: ...",
    ]
    for snippet in allowed:
        assert scan_signatures(_TABLE + snippet) == [], snippet


def test_the_signature_scan_reads_only_modules_that_build_a_table():
    assert scan_signatures("def register_x(obj: Any) -> None: ...") == []
    subscripted = (
        "from otto.registry import Registry\nT = Registry[int]('t', entry=E, register_hint='x')\n"
    )
    assert scan_signatures(subscripted + "def register_x(obj: Any) -> None: ...") == [
        "register_x obj: Any"
    ]


# fmt: off
PREDATES_THE_RULE: dict[str, list[str]] = {
    "otto.cli.registry": [
        "register_cli_command loader: Any",
        "cli_command return: Callable[..., Any]",
    ],
    "otto.instructions": [
        "instruction args: Any",
    ],
    "otto.params": [
        "build_options return: Any",
        "options dataclass_kwargs: Any",
        "options return: Any",
    ],
}
# fmt: on
"""Surfaces whose ``Any`` annotations predate the rule. SHRINK-ONLY.

The rule is that no NEW registration or construction surface uses these
shapes. Typing one of these properly deletes its line here (the comparison
is exact, so a stale line fails too); nothing is ever added."""


def test_no_registration_surface_uses_any_or_a_bare_table_or_ref():
    """Red: annotate one ``register_*`` wrapper's parameter as ``Any``."""
    offenders = {}
    for module, _, source in _src_modules():
        if module == ENGINE_MODULE:
            continue
        if hits := scan_signatures(source):
            offenders[module] = hits
    assert offenders == PREDATES_THE_RULE

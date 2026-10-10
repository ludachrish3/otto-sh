"""After the registry move, no module under otto/host imports otto.context at runtime.

Spec 2 §3.5: a TYPE_CHECKING-only import is allowed; any runtime one is not.
"""

import ast

from tests._fixtures.paths import PROJECT_ROOT

HOST = PROJECT_ROOT / "src" / "otto" / "host"


def _runtime_imports_of_context(path) -> list[str]:
    package = ["otto", *path.relative_to(PROJECT_ROOT / "src" / "otto").parent.parts]
    return _context_imports(path.read_text(), package, path.name)


def _context_imports(source: str, package: list[str], name: str) -> list[str]:
    """The runtime imports of otto.context in *source*, a module of *package* named *name*."""
    tree = ast.parse(source)
    # Only the guarded body is exempt: an ``else:`` branch runs at runtime.
    guarded = {
        id(n)
        for top in tree.body
        if isinstance(top, ast.If)
        and ast.unparse(top.test) in {"TYPE_CHECKING", "typing.TYPE_CHECKING"}
        for stmt in top.body
        for n in ast.walk(stmt)
    }
    found = []
    for node in ast.walk(tree):
        if id(node) in guarded:
            continue
        if isinstance(node, ast.Import):
            found += [
                a.name
                for a in node.names
                if a.name == "otto.context" or a.name.startswith("otto.context.")
            ]
        elif isinstance(node, ast.ImportFrom):
            base = package[: len(package) - node.level + 1] if node.level else []
            target = ".".join([*base, *(node.module.split(".") if node.module else [])])
            if target == "otto.context" or target.startswith("otto.context."):
                found.append(f"{target} ({name}:{node.lineno})")
            elif target == "otto" and any(a.name == "context" for a in node.names):
                found.append(f"otto.context ({name}:{node.lineno})")
    return found


_GUARDED_WITH_AN_ELSE = """\
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..context import OttoContext
else:
    from ..context import try_get_context
"""


def test_only_the_type_checking_body_is_exempt():
    # The else branch runs at runtime, so its import counts; the body's does not.
    assert _context_imports(_GUARDED_WITH_AN_ELSE, ["otto", "host"], "probe.py") == [
        "otto.context (probe.py:6)"
    ]


def test_no_host_module_imports_the_context_at_runtime():
    offenders = [
        hit for path in sorted(HOST.rglob("*.py")) for hit in _runtime_imports_of_context(path)
    ]
    assert offenders == []

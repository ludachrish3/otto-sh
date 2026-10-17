"""After the registry move, no module under otto/host imports the run context at runtime.

Neither ``otto.context``, the facade, nor ``otto.run_context``, its home.

Spec 2 §3.5: a TYPE_CHECKING-only import is allowed; any runtime one is not.
"""

import ast

from tests._fixtures.paths import PROJECT_ROOT

HOST = PROJECT_ROOT / "src" / "otto" / "host"
_CONTEXT = ("otto.context", "otto.run_context")
"""The run context: its public facade and its home."""


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
                if a.name in _CONTEXT or a.name.startswith("otto.context.")
            ]
        elif isinstance(node, ast.ImportFrom):
            base = package[: len(package) - node.level + 1] if node.level else []
            target = ".".join([*base, *(node.module.split(".") if node.module else [])])
            if target in _CONTEXT or target.startswith("otto.context."):
                found.append(f"{target} ({name}:{node.lineno})")
            elif target == "otto":
                found += [
                    f"otto.{a.name} ({name}:{node.lineno})"
                    for a in node.names
                    if f"otto.{a.name}" in _CONTEXT
                ]
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


def test_the_run_context_s_home_counts_in_every_spelling():
    source = (
        "from ..run_context import get_context\n"
        "import otto.run_context\n"
        "from .. import run_context\n"
        "from .. import context\n"
    )
    assert _context_imports(source, ["otto", "host"], "probe.py") == [
        "otto.run_context (probe.py:1)",
        "otto.run_context",
        "otto.run_context (probe.py:3)",
        "otto.context (probe.py:4)",
    ]

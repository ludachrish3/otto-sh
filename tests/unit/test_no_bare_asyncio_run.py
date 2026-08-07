"""Every command-path ``asyncio.run`` must go through ``otto.lifecycle.run_command``.

A bare ``asyncio.run`` bypasses the host-scope sweep and the two-stage
interrupt policy — the exact bug class chaos plan 1 closed (sync command
paths never swept their hosts). AST-based: docstring example snippets (e.g.
``otto/monitor/__init__.py``) are string constants, not Call nodes.
"""

import ast
from pathlib import Path

from tests._fixtures.paths import PROJECT_ROOT

SRC = PROJECT_ROOT / "src" / "otto"

# The one module allowed to call asyncio.run: the lifecycle entry itself.
ALLOWED = {SRC / "lifecycle.py"}


def _bare_asyncio_run_lines(path: Path) -> list[int]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "run"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "asyncio"
    ]


def test_no_bare_asyncio_run_outside_lifecycle():
    offenders = {
        str(path.relative_to(SRC)): lines
        for path in sorted(SRC.rglob("*.py"))
        if path not in ALLOWED and (lines := _bare_asyncio_run_lines(path))
    }
    assert offenders == {}, (
        f"bare asyncio.run() outside otto.lifecycle: {offenders} — route "
        "command bodies through otto.lifecycle.run_command instead"
    )

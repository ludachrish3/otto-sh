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

# The two modules allowed to call asyncio.run.
#
# `lifecycle.py` is the entry this rule exists to funnel command bodies through.
#
# `testing/conformance_host.py` is not a command path at all: it is a helper a
# backend author calls from their own SYNCHRONOUS pytest test, and its probes
# await one host verb under a dry run. There is no host scope to sweep -- the
# caller built the instance and closes it -- and installing the two-stage
# interrupt policy from inside an assertion helper would take pytest's own
# signal handling away for the duration. Routing it through `run_command` would
# buy neither and cost both. The exemption is per FILE, so the other
# `otto.testing` modules stay covered.
ALLOWED = {SRC / "lifecycle.py", SRC / "testing" / "conformance_host.py"}


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

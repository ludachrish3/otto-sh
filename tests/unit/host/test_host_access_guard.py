"""Async host code reaches connections only through the accessors that claim the loop.

A host's connection belongs to the event loop that opened it, and the claim is
what records that owner (and fails fast on a foreign one). An ``async def`` in a
host-family module that reads a raw manager attribute skips the claim, so this
guard walks those modules' ASTs and names every such read.

Every module in the package is walked except the ones named in
``_NOT_HOST_MODULES``: ``session.py`` holds a ``SessionManager`` whose own
``self._connections`` is the manager it drives, not a host's, and it has no
host accessor to route through. Excluding by name, rather than listing the
host modules, keeps a new host family guarded from its first line.
"""

import ast
from pathlib import Path

import otto.host

_RAW = {"_session_mgr", "_connections", "_file_transfer"}
_EXEMPT_METHODS = {
    "close",
    "_close",
    "rebuild_connections",
    "_live_session_mgr",
    "_live_connections",
    "_live_file_transfer",
    "__post_init__",
}
_NOT_HOST_MODULES = {"session.py"}


def _violations(path: Path) -> list[str]:
    tree = ast.parse(path.read_text())
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef) or node.name in _EXEMPT_METHODS:
            continue
        found.extend(
            f"{path.name}:{sub.lineno} {node.name} reads self.{sub.attr}"
            for sub in ast.walk(node)
            if isinstance(sub, ast.Attribute)
            and sub.attr in _RAW
            and isinstance(sub.value, ast.Name)
            and sub.value.id == "self"
        )
    return found


def test_no_async_host_method_reads_a_raw_manager():
    host_dir = Path(otto.host.__file__).parent
    paths = [p for p in sorted(host_dir.glob("*.py")) if p.name not in _NOT_HOST_MODULES]
    found = [v for p in paths for v in _violations(p)]
    assert found == [], (
        "use _live_session_mgr() / _live_connections() / _live_file_transfer():\n"
        + "\n".join(found)
    )


def test_every_excluded_module_exists():
    host_dir = Path(otto.host.__file__).parent
    assert sorted(n for n in _NOT_HOST_MODULES if not (host_dir / n).is_file()) == []


def test_the_guard_catches_a_raw_read(tmp_path):
    bad = tmp_path / "bad.py"
    bad.write_text("class H:\n    async def run(self):\n        return self._session_mgr\n")
    assert _violations(bad) == ["bad.py:3 run reads self._session_mgr"]

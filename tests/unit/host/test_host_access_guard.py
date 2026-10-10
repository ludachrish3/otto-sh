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
_EXEMPT_METHODS = {"_close"}
"""Async methods that may read a raw manager: a family's ``_close`` tears its own managers down."""
_NOT_HOST_MODULES = {"session.py"}


def _host_paths() -> list[Path]:
    host_dir = Path(otto.host.__file__).parent
    return [p for p in sorted(host_dir.glob("*.py")) if p.name not in _NOT_HOST_MODULES]


def _raw_reads(path: Path) -> "list[tuple[str, str]]":
    """Each async method's raw manager reads, as ``(method name, "file:line ... reads self.x")``."""
    tree = ast.parse(path.read_text())
    return [
        (node.name, f"{path.name}:{sub.lineno} {node.name} reads self.{sub.attr}")
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef)
        for sub in ast.walk(node)
        if isinstance(sub, ast.Attribute)
        and sub.attr in _RAW
        and isinstance(sub.value, ast.Name)
        and sub.value.id == "self"
    ]


def _violations(path: Path) -> list[str]:
    return [line for name, line in _raw_reads(path) if name not in _EXEMPT_METHODS]


def test_no_async_host_method_reads_a_raw_manager():
    found = [v for p in _host_paths() for v in _violations(p)]
    assert found == [], (
        "use _live_session_mgr() / _live_connections() / _live_file_transfer():\n"
        + "\n".join(found)
    )


def test_every_exempt_name_is_one_the_guard_would_otherwise_trip():
    """An exemption that nothing needs hides nothing today and silently waives the next method."""
    tripping = {name for p in _host_paths() for name, _ in _raw_reads(p)}
    assert _EXEMPT_METHODS - tripping == set(), (
        "inert exemptions: no async method of that name reads a raw manager"
    )


def test_every_excluded_module_exists():
    host_dir = Path(otto.host.__file__).parent
    assert sorted(n for n in _NOT_HOST_MODULES if not (host_dir / n).is_file()) == []


def test_the_guard_catches_a_raw_read(tmp_path):
    bad = tmp_path / "bad.py"
    bad.write_text("class H:\n    async def run(self):\n        return self._session_mgr\n")
    assert _violations(bad) == ["bad.py:3 run reads self._session_mgr"]

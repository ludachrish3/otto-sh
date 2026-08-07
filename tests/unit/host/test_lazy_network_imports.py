"""Heavy network libraries are runtime dependencies, not import dependencies.

``asyncssh`` / ``aioftp`` / ``telnetlib3`` are only needed once otto actually
opens a connection or transfers a file. They must NOT be imported merely by
importing the host package and its concrete host classes — that path is on the
critical path of *every* invocation (tab-completion, ``--help``, ``schema``,
and the front of every real command) because ``host.factory`` imports
``host.os_profile``, which registers the built-in host classes at import.

The dynamic ``otto host`` CLI discovers exposed verbs purely by class
introspection (``__cli_exposed__`` markers), so that discovery — and the
built-in host-class registry it reads — must remain fully intact without any
network library loaded.

These run in a clean subprocess because ``sys.modules`` is process-global: a
library imported by any earlier test in the parent process would mask the
regression.
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from tests.e2e._otto_subprocess import PROJECT_ROOT, coverage_subprocess_env

# Importing the host package + a concrete host class is exactly what the
# dynamic-CLI / registration path does. We then introspect the unix host's
# exposed verbs (the real discovery mechanism) and report which heavy network
# libraries leaked into sys.modules.
_PROBE = """
import sys, json, inspect
import otto.host
from otto.host.os_profile import get_host_class, HOST_CLASSES

assert set(HOST_CLASSES.names()) >= {"unix", "embedded", "zephyr"}, sorted(HOST_CLASSES.names())

cls = get_host_class("unix")
exposed = [
    name
    for name, fn in inspect.getmembers(cls, inspect.iscoroutinefunction)
    if getattr(fn, "__cli_exposed__", False)
]
assert exposed, "no @cli_exposed methods discoverable on unix host"

print(json.dumps({m: m in sys.modules for m in ("asyncssh", "aioftp", "telnetlib3")}))
"""


def _loaded_network_libs() -> dict[str, bool]:
    # A plain ``python`` child, not otto: the coverage dance alone — no otto
    # keys, and deliberately no ``PYTEST_ADDOPTS`` (the probe runs no pytest).
    result = subprocess.run(
        [sys.executable, "-c", _PROBE],
        env=coverage_subprocess_env(),
        capture_output=True,
        text=True,
        cwd=str(PROJECT_ROOT),
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, (
        f"probe failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )
    return json.loads(result.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize("lib", ["telnetlib3", "asyncssh", "aioftp"])
def test_network_lib_not_imported_by_host_class_discovery(lib: str) -> None:
    loaded = _loaded_network_libs()
    assert loaded[lib] is False, (
        f"{lib} was imported just by importing host classes; it is a runtime "
        f"dependency and must be deferred to point-of-use. Loaded map: {loaded}"
    )

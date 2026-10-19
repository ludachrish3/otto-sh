"""Run a snippet in a fresh interpreter over a scratch project and count what it composed.

A composition is counted by its dependency pass, which every composition runs exactly
once: the child wraps ``otto.config.dependencies.resolve_dependencies`` the moment that
module is loaded (an import hook, so the wrapping itself loads nothing) and logs each
call. An init module's run would not do: Python caches the imported module, so a second
composition in the same process runs no init module and leaves no trace there. The hook
logs ``armed`` when it wraps the module, so a case that counts a pass can assert the hook
was there to count it, and a case that counts none can arm it afterwards as its control.

A fresh interpreter, because in-process a root conftest fixture or an earlier test may
already have loaded the composition root.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

from tests._fixtures.project_scaffold import make_project

INIT = "compose_marker"
"""The scratch project's one init module; a composition imports it."""

_COUNT = """
import importlib.abc, importlib.machinery, sys

class _CountPasses(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path, target=None):
        if name != "otto.config.dependencies":
            return None
        spec = importlib.machinery.PathFinder.find_spec(name, path)
        run = spec.loader.exec_module

        def exec_module(module):
            run(module)
            real = module.resolve_dependencies

            def counted(projects, refused=None):
                with open({log!r}, "a") as f:
                    f.write("pass\\n")
                return real(projects, refused=refused)

            module.resolve_dependencies = counted
            with open({log!r}, "a") as f:
                f.write("armed\\n")

        spec.loader.exec_module = exec_module
        return spec

sys.meta_path.insert(0, _CountPasses())
"""


def scratch_project(tmp_path: Path, *, extra: str = "") -> Path:
    """The scratch project ``project``, with one init module, so a composition imports user code.

    *extra* is more settings TOML, appended after the init module's lines.
    """
    return make_project(
        tmp_path / "project",
        name="project",
        extra=f'libs = ["lib"]\ninit = ["{INIT}"]\n' + extra,
        files={f"lib/{INIT}.py": "MARKED = True\n"},
    )


def run_fresh(tmp_path: Path, source: str, *, extra: str = "") -> "list[object]":
    """Run *source* in a new interpreter over the scratch project, compositions counted.

    *extra* is passed to :func:`scratch_project`. Returns the source's last JSON line.
    """
    project = scratch_project(tmp_path, extra=extra)
    source = _COUNT.format(log=str(tmp_path / "passes.log")) + source
    env = {
        **os.environ,
        "OTTOPATH": str(project),
        "OTTO_HOME": str(tmp_path / "home"),
    }
    env.pop("PYTEST_ADDOPTS", None)
    out = subprocess.run(
        [sys.executable, "-c", source],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
        env=env,
        cwd=tmp_path,
    )
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


def hook_log(tmp_path: Path) -> "list[str]":
    """The child's hook log: ``armed`` once the hook wraps the module, ``pass`` per pass."""
    log = tmp_path / "passes.log"
    return log.read_text().split() if log.exists() else []


def compositions(tmp_path: Path) -> int:
    """How many dependency passes, so compositions, the child ran."""
    return hook_log(tmp_path).count("pass")


def armed(tmp_path: Path) -> bool:
    """Whether the child's hook wrapped the dependency pass, so could count it."""
    return "armed" in hook_log(tmp_path)

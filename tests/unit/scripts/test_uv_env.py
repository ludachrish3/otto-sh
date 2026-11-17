"""scripts/uv_env.py: the one home of the uv env, importable where otto is not installed.

The api_dump_invariance lanes run scripts/api_regen.py with otto's runtime
dependencies and no otto, so nothing api_regen imports may need otto. The
import budget's harness, which does, loads uv_env by path instead of the
other way round.
"""

import ast
import subprocess
import sys
import sysconfig
from pathlib import Path

from tests._fixtures.paths import CHECKOUT_ROOT

SCRIPTS = CHECKOUT_ROOT / "scripts"


def _imported_modules(path: Path) -> "set[str]":
    """Every top-level module *path* imports, at any depth of its code."""
    names = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            names |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module)
    return names


def test_uv_env_imports_only_the_standard_library():
    imported = {name.split(".")[0] for name in _imported_modules(SCRIPTS / "uv_env.py")}
    assert imported <= set(sys.stdlib_module_names), imported - set(sys.stdlib_module_names)


def test_api_regen_never_imports_the_import_budget_harness():
    imported = _imported_modules(SCRIPTS / "api_regen.py")
    assert "scripts.import_budget" not in imported
    assert "scripts.uv_env" in imported


def test_api_regen_imports_where_otto_is_not_installed():
    """api_regen imports with no otto importable, as the api_dump_invariance lanes run it.

    The base interpreter runs isolated (``-I``) and without site (``-S``), with
    the dev venv's site-packages put on ``sys.path`` directly, so no ``.pth``
    file (the editable otto's among them) is processed and no system
    site-packages can supply an otto. That proves only "api_regen imports with
    no otto": the dev venv's packages are all on the path, so it is not the
    lanes' runtime-dependencies-only shape.
    """
    site_packages = sysconfig.get_paths()["purelib"]
    code = (
        "import sys\n"
        f"sys.path[:0] = [{str(CHECKOUT_ROOT)!r}, {site_packages!r}]\n"
        "try:\n"
        "    import otto\n"
        "except ModuleNotFoundError:\n"
        "    pass\n"
        "else:\n"
        "    raise SystemExit('otto is importable here; the test proves nothing')\n"
        "import scripts.api_regen\n"
        "assert 'otto' not in sys.modules, 'api_regen imported otto'\n"
        "print('ok')\n"
    )
    # The interpreter the venv was made from (-S keeps even its site from running).
    base = getattr(sys, "_base_executable", None) or str(
        Path(sys.base_prefix) / "bin" / f"python{sys.version_info[0]}.{sys.version_info[1]}"
    )
    done = subprocess.run(
        [base, "-I", "-S", "-c", code], capture_output=True, text=True, check=False, cwd="/"
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "ok"

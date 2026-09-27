"""The CLI turns pydantic's plugin scan off before the first model build; library use does not.

pydantic's plugin loader opens every installed distribution's
``entry_points.txt`` the first time any model is built: one file operation
per installed package, a network round trip each when the venv is on NFS.
otto ships and uses no pydantic plugin, so the console entry sets
``PYDANTIC_DISABLE_PLUGINS=__all__`` unless the user already chose a value.

Each case runs a fresh interpreter: the variable is process state, and this
test process has long since built every model it will.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

VAR = "PYDANTIC_DISABLE_PLUGINS"

# Records the variable's value each time pydantic asks for its plugins, which
# it does on every model build (`create_schema_validator` looks the loader up
# at call time, so patching the loader module is enough).
_SPY = """
import json, os, sys
import pydantic.plugin._loader as _loader
_seen = []
_real = _loader.get_plugins
def _spy():
    _seen.append(os.environ.get({var!r}))
    return _real()
_loader.get_plugins = _spy
sys.argv = ["otto", "--help"]
from otto._shim import main
try:
    main()
except SystemExit:
    pass
with open(os.environ["SPY_RESULT"], "w") as fh:
    json.dump(_seen, fh)
"""


def _clean_env(tmp_path: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("OTTO_") and k != VAR}
    env["OTTO_HOME"] = str(tmp_path / "home")
    env["SPY_RESULT"] = str(tmp_path / "seen.json")
    return env


def _values_seen_by_pydantic(tmp_path: Path, env: dict[str, str]) -> list[str | None]:
    subprocess.run(
        [sys.executable, "-c", _SPY.format(var=VAR)],
        env=env,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    )
    seen = json.loads((tmp_path / "seen.json").read_text())
    # A CLI run that built no model would pass the assertions below vacuously.
    assert seen, "the CLI built no pydantic model; the spy saw nothing"
    return seen


def test_the_cli_disables_the_scan_before_the_first_model_build(tmp_path):
    seen = _values_seen_by_pydantic(tmp_path, _clean_env(tmp_path))
    assert set(seen) == {"__all__"}, seen


@pytest.mark.parametrize("value", ["", "some_plugin"], ids=["empty-opts-back-in", "named"])
def test_a_value_the_user_set_is_kept(tmp_path, value):
    env = _clean_env(tmp_path)
    env[VAR] = value
    seen = _values_seen_by_pydantic(tmp_path, env)
    assert set(seen) == {value}, seen


def test_library_use_leaves_the_environment_alone():
    code = f"import os, otto, otto._shim; print(repr(os.environ.get({VAR!r})))"
    env = {k: v for k, v in os.environ.items() if k != VAR}
    out = subprocess.run(
        [sys.executable, "-c", code], env=env, capture_output=True, text=True, check=True
    )
    assert out.stdout.strip() == "None", out.stdout

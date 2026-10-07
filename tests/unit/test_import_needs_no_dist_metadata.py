"""Importing otto, and binding every lazily exported name, reads no distribution metadata.

The breaking-marks checker regenerates the API dump at a historical commit in
an environment built from the lock file alone, with the project itself never
installed (``scripts/api_regen.py``, ``UvDepsEnv``), so there is no
``otto-sh`` dist-info for ``importlib.metadata`` to find. The dump spec (§5.2)
therefore requires otto to read its version lazily, never at import: a
namespace whose import needs the metadata is a refusal there. This runs a
fresh interpreter in which every metadata lookup for ``otto-sh`` fails, then
imports every namespace ``api/public.toml`` declares and every lazy package
(not every lazy package is declared), and binds every name in each ``__all__``.
"""

import subprocess
import sys

from scripts.api_manifest import load_manifest
from tests._fixtures._lazy_exports import lazy_package_names
from tests._fixtures.paths import PROJECT_ROOT

_CHILD = """\
import importlib
import importlib.metadata as md
import sys

def _missing(real):
    def lookup(name, *args, **kwargs):
        if name.replace("_", "-").lower() == "otto-sh":
            raise md.PackageNotFoundError(name)
        return real(name, *args, **kwargs)
    return lookup

for attr in ("version", "distribution", "metadata"):
    setattr(md, attr, _missing(getattr(md, attr)))

for package in sys.argv[1:]:
    module = importlib.import_module(package)
    for name in module.__all__:
        getattr(module, name)
"""


def test_no_public_or_lazy_name_reads_otto_distribution_metadata_at_import():
    declared = set(load_manifest(PROJECT_ROOT / "api" / "public.toml"))
    lazy = set(lazy_package_names())
    packages = sorted(declared | lazy)
    assert "otto" in packages
    proc = subprocess.run(
        [sys.executable, "-B", "-c", _CHILD, *packages],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr

"""The live declaration agrees with the runtime (spec 1 §6, "Agreement tests").

Every namespace ``api/public.toml`` declares imports in a fresh interpreter, has a
literal ``__all__`` of unique names, and binds each of them at runtime (a name
bound only under ``TYPE_CHECKING`` fails); a name reachable from two namespaces is
one object at both. ``scripts/api_agreement.py`` is the machinery, and its fixture
tests (``tests/unit/scripts/test_api_agreement.py``) prove each check can fail.
This module runs the checks over the live declaration on every interpreter of the
matrix, because a binding can depend on the Python version.
"""

from scripts.api_agreement import agreement_failures, namespace_reports
from scripts.api_manifest import load_manifest
from tests._fixtures.paths import PROJECT_ROOT

MANIFEST = PROJECT_ROOT / "api" / "public.toml"


def test_every_declared_namespace_agrees_with_its_runtime_bindings():
    names = sorted(load_manifest(MANIFEST))
    assert agreement_failures(names, namespace_reports(names, PROJECT_ROOT)) == []

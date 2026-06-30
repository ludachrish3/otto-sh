"""End-to-end tier conftest — auto-stamp the ``e2e`` marker from the path.

Mirrors ``tests/integration/conftest.py``: the ``tests/e2e/`` directory is the
single source of truth for the e2e tier (level axis). Resource markers
(``integration``/``embedded``) stay explicit on the e2e tests that need a
testbed, so this hook is additive — it only tags the tier.
"""

from pathlib import Path

_E2E_ROOT = Path(__file__).parent


def pytest_collection_modifyitems(config, items):
    """Auto-apply the ``e2e`` marker to every test under this tree.

    Idempotent and additive — explicit ``integration``/``embedded`` resource
    markers on individual e2e tests are left untouched.
    """
    for item in items:
        if _E2E_ROOT in item.path.parents:
            item.add_marker("e2e")

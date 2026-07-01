"""End-to-end tier conftest — two path-keyed responsibilities.

Both key off membership in the ``tests/e2e/`` tree:

1. **Auto-stamp the ``e2e`` level marker** on every test here (mirrors
   ``tests/integration/conftest.py``; additive and idempotent — explicit
   resource markers are left untouched). ``e2e`` is a *level* marker,
   orthogonal to the resource axis.
2. **Enforce the resource-marker rule:** every e2e test must declare exactly
   one *primary* bed marker from ``{hostless, integration, embedded}``, with
   ``hops`` permitted only as an additive refinement of ``integration``. All
   other axes (``e2e`` level, ``xdist_group``, ``stability``, ``timeout``,
   ``retry``) are ignored. This keeps the tier deliberately sorted — nothing
   slips into the no-testbed gate untagged.

Runs ``tryfirst`` so the guard sees every collected item before ``-m``
deselection, and the ``e2e`` stamp lands before any marker-based filtering.
"""

from pathlib import Path

import pytest

_E2E_ROOT = Path(__file__).parent
_PRIMARY = {"hostless", "integration", "embedded"}


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(config, items):  # type: ignore[no-untyped-def]
    offenders: list[str] = []
    for item in items:
        if _E2E_ROOT not in item.path.parents:
            continue
        # (1) Auto-stamp the e2e level marker (additive, idempotent).
        item.add_marker("e2e")
        # (2) Enforce exactly one primary resource marker on real collected items.
        # Minimal/synthetic items (e.g. the unit test that exercises only the
        # stamp) may not expose the marker API — stamp them and move on.
        if not hasattr(item, "iter_markers"):
            continue
        names = {m.name for m in item.iter_markers()}
        primary = names & _PRIMARY
        if len(primary) != 1:
            offenders.append(
                f"{item.nodeid}: resource markers={sorted(primary)} "
                f"(need exactly one of {sorted(_PRIMARY)})"
            )
        if "hops" in names and "integration" not in names:
            offenders.append(f"{item.nodeid}: 'hops' requires 'integration'")
    if offenders:
        raise pytest.UsageError(
            "tests/e2e resource-marker rule violated:\n  " + "\n  ".join(offenders)
        )

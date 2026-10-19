"""Centralized test path anchors and sys.path setup.

``TESTS_ROOT`` / ``CHECKOUT_ROOT`` are THE path anchors for test code — G9 bans
``Path(__file__).parents[N]`` arithmetic everywhere else under ``tests/``,
because depth arithmetic breaks silently when a file moves; this module is the
one sanctioned place that derives them (from its own known location).
``CHECKOUT_ROOT`` is otto's own source checkout (the parent of ``tests/``), not
a project root: the fixture projects that tests run against live under it.
``ensure_custom_hosts_on_path`` makes the checkout's shared ``custom_hosts``
package importable (the third-party-style frame package the fixture projects
depend on). ``default_project_root`` names the ``project1`` fixture project;
the integration tree's per-test fixture points ``OTTOPATH`` at it — at RUNTIME,
never at conftest import time (G11: module-scope env writes are banned; an
earlier import-time helper here was the live offender, re-injecting the
variable after the root conftest's hermeticity strip).
"""

import sys
from pathlib import Path

# tests/  (this file lives at tests/_fixtures/paths.py)
TESTS_ROOT = Path(__file__).resolve().parents[1]
CHECKOUT_ROOT = TESTS_ROOT.parent
_TESTS_ROOT = TESTS_ROOT
_CUSTOM_HOSTS = _TESTS_ROOT / "custom_hosts"
_PROJECT1 = _TESTS_ROOT / "project1"
WHEELS_DIR = TESTS_ROOT / "_fixtures" / "wheels"
"""Committed fixture wheels (see that directory's README).

Holds ``beetroot``, the requirement that CANNOT be satisfied from an index --
so a test that installs it without pointing here has reached the network.
"""


def ensure_custom_hosts_on_path() -> None:
    """Prepend the shared ``custom_hosts`` dir to ``sys.path`` (idempotent)."""
    p = str(_CUSTOM_HOSTS)
    if p not in sys.path:
        sys.path.insert(0, p)


def default_project_root() -> str:
    """Path to the ``project1`` fixture project used by the cov/integration suites."""
    return str(_PROJECT1)

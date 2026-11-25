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
GOLDENS = TESTS_ROOT / "_fixtures" / "goldens"
"""Frozen JSON documents a template must render: ``<name>_lab.json`` per lab file
a Jsonnet entry replaced, and ``pages/<template>.json`` per docs page template."""
TECH1_GOLDEN = GOLDENS / "tech1_lab.json"
"""The bed cell's frozen JSON lab file: ``lab_data/tech1``'s hand-written lab, before its entry.

``tech1/lab.jsonnet`` renders exactly this document
(``tests/unit/labs/test_testbed_templates.py`` holds the two equal, section
for section), so an edit to the bed (a new VM, a renamed interface) edits the
entry AND this file, in one commit. It is two things at once: the template's
oracle and the legacy JSON fixture that ``test_unchanged_loading`` loads.
Updating it moves ``test_unchanged_loading``'s captured outputs too, so
re-capture them in the same commit. The import budget's project does not read
it: see :data:`BUDGET_PROJECT_LAB`.
"""
BUDGET_PROJECT_LAB = TESTS_ROOT / "_fixtures" / "budget_project_lab.json"
"""The JSON lab the import budget's realistic project reads in place.

(:func:`tests._fixtures.generated_project.generate_project`.) It is its own
file, not the bed's golden, because the release delta generates that project
with the tip's harness under the PREVIOUS release's otto
(``scripts/import_budget_delta.py``), and the bed's lab gains fields that
release cannot read.

The rule for this file: it holds only fields the previous release also reads.
It is not rendered from the bed's Jsonnet and no test ties it to the golden.
A deliberate breaking change to lab fields edits it in the same pull request,
so both sides still load it; a field or host both sides cannot read is removed.

It has one lab, ``unix``, and every element is a member. The previous release
validates the members of the selected lab strictly and skips a record it
cannot read anywhere else, so with every record in the lab the delta's canary
selects, a record the base rejects fails the canary instead of being skipped
(``tests/unit/import_budget/test_budget_project_lab.py``).
"""
GETTING_STARTED_GOLDEN = GOLDENS / "getting_started_lab.json"
"""``docs/examples/getting-started``'s hand-written lab file, before its Jsonnet entry.

The example's ``lab_data/lab.jsonnet`` renders exactly this document
(``tests/unit/docs/test_getting_started_templates.py``), so an edit to the
example's lab edits the entry AND this file, in one commit.
"""
GETTING_STARTED_INVENTORY_GOLDEN = GOLDENS / "getting_started_inventory_lab.json"
"""``docs/examples/getting-started-inventory``'s hand-written lab file, before its entry.

``test_unchanged_loading`` loads it as the twin's legacy JSON, and the twin's
template builds the labs recorded for it
(``tests/unit/docs/test_getting_started_templates.py``).
"""
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

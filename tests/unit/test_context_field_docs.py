"""The run context's fields keep their documentation where the API reference reads it.

Sphinx's autodoc documents a class's fields from the source of the module that the
class's ``__module__`` names: ``get_class_members`` analyzes that module for each
field's attribute docstring. So the API page shows ``OttoContext``'s field docs only
while ``__module__`` names the module whose source defines the class.

``get_class_members`` (``sphinx.ext.autodoc.importer``) is Sphinx's internal API, not a
public one, and ``sphinx>=8.0`` has no upper bound: this pin calls it because it is
exactly what autodoc runs. If a Sphinx release moves or reshapes it, this file fails
for a reason that is Sphinx's, not otto's; re-point it at what autodoc then calls.
"""

import sys
from pathlib import Path

from sphinx.ext.autodoc.importer import get_class_members
from sphinx.util.inspect import safe_getattr

from otto.context import OttoContext

# Each documented field, with the start of its docstring.
FIELDS = {
    "policy": "The run's policy, read live by every host",
    "cov_decision": "Whether coverage is on for this run, once decided",
    "include_projects": "Project names forced ACTIVE this invocation",
    "exclude_projects": "Project names forced INACTIVE this invocation",
    "verb": "The verb this invocation dispatched",
}


def test_the_context_class_names_the_module_its_code_is_in() -> None:
    defined_in = Path(OttoContext.__init__.__code__.co_filename).resolve()
    named = sys.modules[OttoContext.__module__].__file__
    assert named is not None
    assert Path(named).resolve() == defined_in


def test_autodoc_reads_every_documented_field_of_the_context() -> None:
    members = get_class_members(OttoContext, ["OttoContext"], safe_getattr)
    found = {name: (members[name].docstring or "")[: len(start)] for name, start in FIELDS.items()}
    assert found == FIELDS

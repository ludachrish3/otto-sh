"""Every lab-data field is documented on its own reference page (spec §13, §15).

The reference cannot fall behind the model: a field added to a spec without a
row on the page fails here. Two pages are covered — the lab file's fields on
``lab-config.md``, and the inventory record's on ``inventory.md`` — so each
case carries the page it is about rather than reading one module-level file.

A case may also carry the SECTION its rows must live in. ``inventory.md`` needs
it: that page carries a second table — the NetBox mapping — whose left column
is also ``| `<field>` ``, and it names ten of the fifteen record fields. Searched
over the whole page, deleting a row from the Record-fields table would leave the
mapping row satisfying the guard, and the reference could lose two thirds of its
rows while staying green. Scoping the search to the section makes the check
about the table it is supposed to be about.

Known limitation, still true for the unscoped cases — this proves PRESENCE, not
PLACEMENT. ``lab-config.md`` is searched whole, so a row in the wrong table
satisfies it: ``name`` has rows in the per-host, element AND link tables, and a
field that MOVES between levels looks documented while its old row still
stands. That is why this guard stayed green on ``name`` / ``labs`` /
``resources`` across the v1-to-v2 break even though their rows described the
shape v2 forbids. Green here means no field is missing; it never means the rows
are right.
"""

from pathlib import Path

import pytest

from otto.host.os_profile import registered_host_specs
from otto.models.inventory import InventoryRecord
from otto.models.lab import HOISTED_HOST_KEYS, ElementSpec, LabEntrySpec
from tests._fixtures.paths import PROJECT_ROOT

_CONFIG = PROJECT_ROOT / "docs" / "guide" / "configuration"
_PAGE = _CONFIG / "lab-config.md"
_INVENTORY_PAGE = _CONFIG / "inventory.md"
_RECORD_FIELDS = "## Record fields"
"""The inventory page's reference table, verbatim as its heading reads."""


def _section(text: str, heading: "str | None", page: Path) -> str:
    """Return *heading*'s section — up to the next ``## `` — or the whole page.

    Fails loudly on a heading that is not there: a renamed section must not
    quietly widen the search back to the whole page, which is the failure this
    scoping exists to prevent.
    """
    if heading is None:
        return text
    assert heading in text, f"{page.name}: no {heading!r} heading to scope the search to"
    rest = text.split(heading, 1)[1]
    return rest.split("\n## ", 1)[0]


@pytest.mark.parametrize(
    ("page", "heading", "owner", "fields"),
    [
        (_PAGE, None, "labs entry", sorted(LabEntrySpec.model_fields)),
        (_PAGE, None, "element", sorted(ElementSpec.model_fields)),
        # The inventory record's own reference table (spec §4): the record is
        # the join's whole vocabulary, so a field with no row is a field a
        # reader can only discover from a validation error.
        (
            _INVENTORY_PAGE,
            _RECORD_FIELDS,
            "inventory record",
            sorted(InventoryRecord.model_fields),
        ),
        # HOISTED_HOST_KEYS are excluded for host specs: `element` and
        # `element_id` survive on HostSpec because the flat host-dict API
        # composes the host id from them (spec §14), but in a v2 FILE they are
        # the element's `name` / `id` and are errors inside a host entry. They
        # are documented under "Elements"; a per-host row for them would teach
        # exactly the shape v2 forbids. (`labs` / `resources`, the other two
        # hoisted keys, left HostSpec outright and never reach this set.)
        *[
            (_PAGE, None, stem, sorted(set(spec.model_fields) - HOISTED_HOST_KEYS))
            for stem, spec in registered_host_specs(builtins_only=True).items()
        ],
    ],
)
def test_every_field_has_a_row(
    page: Path, heading: "str | None", owner: str, fields: list[str]
) -> None:
    text = _section(page.read_text(), heading, page)
    missing = [f for f in fields if f"| `{f}`" not in text]
    where = f"{page.name}" if heading is None else f"{page.name} under {heading!r}"
    assert not missing, f"{owner}: undocumented in {where}: {missing}"

"""Every lab-data field is documented in lab-config.md (spec §13).

The reference cannot fall behind the model: a field added to a spec without a
row on the page fails here.

Known limitation — this proves PRESENCE, not PLACEMENT. The search is over the
whole page, so a row in the wrong table still satisfies it: ``name`` has rows
in the per-host, element AND link tables, and a field that MOVES between
levels looks documented while its old row still stands. That is why this guard
stayed green on ``name`` / ``labs`` / ``resources`` across the v1-to-v2 break
even though their rows described the shape v2 forbids. Green here means no
field is missing; it never means the rows are right.
"""

import pytest

from otto.host.os_profile import registered_host_specs
from otto.models.lab import HOISTED_HOST_KEYS, ElementSpec, LabEntrySpec
from tests._fixtures.paths import PROJECT_ROOT

_PAGE = PROJECT_ROOT / "docs" / "guide" / "configuration" / "lab-config.md"


def _documented() -> str:
    return _PAGE.read_text()


@pytest.mark.parametrize(
    ("owner", "fields"),
    [
        ("labs entry", sorted(LabEntrySpec.model_fields)),
        ("element", sorted(ElementSpec.model_fields)),
        # HOISTED_HOST_KEYS are excluded for host specs: `element` and
        # `element_id` survive on HostSpec because the flat host-dict API
        # composes the host id from them (spec §14), but in a v2 FILE they are
        # the element's `name` / `id` and are errors inside a host entry. They
        # are documented under "Elements"; a per-host row for them would teach
        # exactly the shape v2 forbids. (`labs` / `resources`, the other two
        # hoisted keys, left HostSpec outright and never reach this set.)
        *[
            (stem, sorted(set(spec.model_fields) - HOISTED_HOST_KEYS))
            for stem, spec in registered_host_specs(builtins_only=True).items()
        ],
    ],
)
def test_every_field_has_a_row(owner: str, fields: list[str]) -> None:
    text = _documented()
    missing = [f for f in fields if f"| `{f}`" not in text]
    assert not missing, f"{owner}: undocumented in lab-config.md: {missing}"

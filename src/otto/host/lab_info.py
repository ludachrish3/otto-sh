"""``LabInfo`` — the resolved lab a host was loaded from, carried on the host.

Stamped by :func:`otto.config.lab.load_lab`'s attribution sweep (beside the
older ``source_lab`` string it complements), so a host handed to code in
isolation can answer "which lab, with what resources and metadata" without a
trip back to the ``OttoContext``. One structured attribute rather than three
parallel ones: a future lab-level field reaches hosts here without growing
the host contract. For ``a+b`` loads it names the COMPONENT lab, matching
``source_lab``.
"""

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class LabInfo:
    """The lab a host belongs to: its name, declared resources, and metadata.

    Hashing raises ``TypeError`` (``metadata`` is a dict); key collections by
    ``name``.
    """

    name: str = ""
    """Component lab name; ``""`` for a host no loader attributed."""

    resources: frozenset[str] = frozenset()
    """The lab's declared reservation identifiers (spec §8.1)."""

    metadata: dict[str, Any] = field(default_factory=dict)
    """The lab's opaque ``metadata`` table — a per-host copy."""

    def __post_init__(self) -> None:
        # ``frozen=True`` blocks rebinding the attribute, not mutation of the
        # dict behind it. One lab's ``metadata`` table is stamped onto every
        # host of that lab, so without this copy each host would alias the
        # lab's dict and every sibling's, making the per-host-copy promise
        # above false and one host's write visible to all.
        object.__setattr__(self, "metadata", dict(self.metadata))

"""Capability resolution for menu-style host fields (term / transfer).

A *capability* is a host field where the lab declares a closed menu of valid
options (e.g. ``valid_transfers = ["scp", "nc"]``) and the active selection is
resolved from that menu. One ``CapabilityResolver`` handles one field; it is
stateless apart from the field name it carries for error messages.
"""
from __future__ import annotations

import re
from collections.abc import Sequence


class CapabilityResolver:
    """Resolve / validate the active selection for one menu-style capability."""

    def __init__(self, field: str) -> None:
        self.field = field  # e.g. "term" / "transfer" — used in error messages

    def validate_choice(self, menu: Sequence[str], choice: str) -> str:
        """Return *choice* if it is in *menu*, else raise a fail-loud ValueError."""
        if choice not in menu:
            raise ValueError(
                f"{self.field} {choice!r} is not in this host's "
                f"{self.field} menu {list(menu)}"
            )
        return choice

    def resolve_active(
        self,
        menu: Sequence[str],
        *,
        pin: str | None = None,
        preference: Sequence[str] | None = None,
    ) -> str:
        """Resolve the active selection from *menu*.

        Precedence: an explicit *pin* (validated against the menu) wins; else the
        first *preference* entry that is in the menu; else the menu's first entry.
        *menu* is assumed non-empty (the spec validator guarantees this).
        """
        if pin is not None:
            return self.validate_choice(menu, pin)
        if preference:
            for choice in preference:
                if choice in menu:
                    return choice
        return menu[0]


TERM_RESOLVER = CapabilityResolver("term")
TRANSFER_RESOLVER = CapabilityResolver("transfer")


def select_preferences(
    table: dict[str, dict[str, list[str]]], host_id: str
) -> dict[str, list[str]]:
    """Reduce a nested ``{selector: {capability: [...]}}`` preference table to a
    flat ``{capability: [...]}`` for one host, by the definition-order cascade:
    walk selectors in insertion (file) order and, for each whose regex
    ``re.fullmatch``es *host_id*, overlay its capabilities (later matches win
    per-capability). A selector matching nothing is skipped; lists are copied so
    the caller never aliases the table.
    """
    effective: dict[str, list[str]] = {}
    for selector, caps in table.items():
        if re.fullmatch(selector, host_id):
            for cap, order in caps.items():
                effective[cap] = list(order)
    return effective

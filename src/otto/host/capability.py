"""Capability resolution for menu-style host fields (term / transfer).

A *capability* is a host field where the lab declares a closed menu of valid
options (e.g. ``valid_transfers = ["scp", "nc"]``) and the active selection is
resolved from that menu. One ``CapabilityResolver`` handles one field; it is
stateless apart from the field name it carries for error messages.
"""
from __future__ import annotations

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

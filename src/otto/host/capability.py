"""Capability resolution for menu-style host fields (term / transfer).

A *capability* is a host field where the lab declares a closed menu of valid
options (e.g. ``valid_transfers = ["scp", "nc"]``) and the active selection is
resolved from that menu. One ``CapabilityResolver`` handles one field; it is
stateless apart from the field name it carries for error messages.
"""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from typing_extensions import final

from .inventory_ref import recording_fields


class _DefaultSelection(str):
    """A class default for a menu field: a string nobody chose.

    It compares, hashes, prints and serialises exactly as the plain string
    does; only :func:`is_default_selection` tells it apart. A plain ``str``
    is always a pin. A string derived from a marked value (``.lower()``,
    slicing, formatting) is a plain ``str``, so :func:`user_pin` must be given
    the raw field value.
    """

    __slots__ = ()


def default_selection(*, value: str) -> str:
    """Mark *value* as a class default, so it is not read as a user's pin."""
    return _DefaultSelection(value)


def is_default_selection(value: object) -> bool:
    """Whether *value* is a marked default (nobody chose it)."""
    return isinstance(value, _DefaultSelection)


def user_pin(value: str | None) -> str | None:
    """Return the user's pin in *value*: ``None`` for ``None`` and a marked default.

    The one place that decides what counts as a pin; any other string is one.
    """
    return None if value is None or is_default_selection(value) else value


class CapabilityResolver:
    """Resolve / validate the active selection for one menu-style capability."""

    def __init__(self, field: str) -> None:
        self.field = field  # e.g. "term" / "transfer" — used in error messages

    def validate_choice(self, menu: Sequence[str], choice: str) -> str:
        """Return *choice* if it is in *menu*, else raise a fail-loud ValueError."""
        if choice not in menu:
            raise ValueError(
                f"{self.field} {choice!r} is not in this host's {self.field} menu {list(menu)}"
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

        Precedence: the first *preference* entry that is in the menu wins; else an
        explicit *pin*; else the menu's first entry. A set *pin* is always
        validated against the menu (fail-loud on malformed lab data) even when a
        preference overrides it. A marked *pin* (:func:`user_pin`) is no pin.
        *menu* is assumed non-empty (the spec validator guarantees this).

        The menu's first entry is returned marked (:func:`default_selection`):
        nobody chose it. A preference's or a pin's win is the plain string.

        This is the one decision of which input wins, so a recorded build
        (``otto lab explain``) attributes a preference's win here
        (:func:`otto.host.provenance.preference_won`); a pin's win keeps the
        layer the pin's own merge recorded.
        """
        pin = user_pin(pin)
        validated_pin = self.validate_choice(menu, pin) if pin is not None else None
        if preference:
            for choice in preference:
                if choice in menu:
                    if recording_fields():
                        _preference_won(field=self.field)
                    return choice
        if validated_pin is not None:
            return validated_pin
        return default_selection(value=menu[0])


TERM_RESOLVER = CapabilityResolver("term")
TRANSFER_RESOLVER = CapabilityResolver("transfer")
IMPAIRER_RESOLVER = CapabilityResolver("impairer")


_CONSOLE_NC_WHY = (
    "nc needs a second command channel beside its remote listener, and a "
    "single-client console has only one"
)


def console_transfer_menu(host: str, menu: Sequence[str], pin: str | None) -> list[str]:
    """Return the transfer menu a console-term host resolves from: *menu* without nc.

    A console serves one client; nc's remote ``nc -l`` holds that one session
    while its control commands must run beside it. An explicit nc *pin*, or a
    menu with nothing but nc, is therefore a config error (``ValueError``)
    naming *host*. The one home for this rule: the lab model resolves a
    console host's transfer from it, and so does a ``--term console``
    override (``otto.run_context._apply_option_overrides``). A marked *pin*
    (:func:`user_pin`) is no pin.
    """
    pin = user_pin(pin)
    if pin == "nc":
        raise ValueError(
            f"{host}: transfer 'nc' cannot run on a console term: {_CONSOLE_NC_WHY}; "
            f"pin shell, scp, sftp or ftp"
        )
    usable = [t for t in menu if t != "nc"]
    if menu and not usable:
        raise ValueError(
            f"{host}: valid_transfers {list(menu)} leaves a console term no transfer: "
            f"{_CONSOLE_NC_WHY}; add shell, scp, sftp or ftp"
        )
    return usable


@final
@dataclass(frozen=True, kw_only=True)
class MenuCapability:
    """One menu-style host field: its pin, its list and the list's default.

    Attributes:
        resolver: The resolver for the field.
        pin_field: The pin attribute (``term``, ``transfer``, ``impairer``);
            also the field's ``[host_preferences]`` key.
        menu_field: The list attribute (``valid_terms`` and so on).
        default_menu: The class default list; never mutated.
        witnessed: Whether the no-preference witness search varies the field.
    """

    resolver: CapabilityResolver
    pin_field: str
    menu_field: str
    default_menu: list[str]
    witnessed: bool = True

    @property
    def default_pin(self) -> str:
        """The class default pin: the default list's head, marked as nobody's choice."""
        return default_selection(value=self.default_menu[0])

    def fresh_menu(self) -> list[str]:
        """Return a new copy of the default list on each call, for a ``default_factory``."""
        return list(self.default_menu)


MENU_CAPABILITIES: dict[str, list[MenuCapability]] = {
    "unix": [
        MenuCapability(
            resolver=TERM_RESOLVER,
            pin_field="term",
            menu_field="valid_terms",
            default_menu=["ssh", "telnet"],
        ),
        MenuCapability(
            resolver=TRANSFER_RESOLVER,
            pin_field="transfer",
            menu_field="valid_transfers",
            default_menu=["scp", "sftp", "ftp", "nc"],
        ),
        MenuCapability(
            resolver=IMPAIRER_RESOLVER,
            pin_field="impairer",
            menu_field="valid_impairers",
            default_menu=["netem"],
            witnessed=False,
        ),
    ],
    "embedded": [
        MenuCapability(
            resolver=TERM_RESOLVER,
            pin_field="term",
            menu_field="valid_terms",
            default_menu=["telnet"],
        ),
        MenuCapability(
            resolver=TRANSFER_RESOLVER,
            pin_field="transfer",
            menu_field="valid_transfers",
            default_menu=["console"],
        ),
    ],
}
"""Each host family's menu capabilities, in resolution order (term first)."""


def menu_capabilities(family: str | None) -> list[MenuCapability]:
    """Return *family*'s menu capabilities; ``[]`` for no family or an unknown one."""
    return list(MENU_CAPABILITIES.get(family, [])) if family is not None else []


def menu_capability(*, family: str, name: str) -> MenuCapability:
    """Return *family*'s capability whose ``pin_field`` is *name*.

    Raises:
        KeyError: Naming both *family* and *name* when there is none.
    """
    for cap in menu_capabilities(family):
        if cap.pin_field == name:
            return cap
    raise KeyError(f"no menu capability {name!r} for host family {family!r}")


def resolve_selections(
    capabilities: Sequence[MenuCapability],
    *,
    label: str,
    menus: Mapping[str, Sequence[str]],
    pins: Mapping[str, str | None],
    preferences: Mapping[str, Sequence[str]],
) -> dict[str, str]:
    """Resolve every capability's active selection, in *capabilities*' order.

    The one selection function: *menus*, *pins*, *preferences* and the result
    are keyed by ``pin_field``. A ``transfer`` list is narrowed by
    :func:`console_transfer_menu` when the resolved ``term`` is ``console``.
    The given lists are never changed.

    Raises:
        ValueError: A list is empty, a pin is not in its list, or the console
            rule refuses the transfer (each naming *label* or the field).
    """
    result: dict[str, str] = {}
    for cap in capabilities:
        menu = list(menus[cap.pin_field])
        if not menu:
            raise ValueError(f"{cap.menu_field} must be a non-empty list")
        pin = pins.get(cap.pin_field)
        if cap.pin_field == "transfer" and result.get("term") == "console":
            menu = console_transfer_menu(label, menu, pin)
        result[cap.pin_field] = cap.resolver.resolve_active(
            menu, pin=pin, preference=preferences.get(cap.pin_field)
        )
    return result


def settle_selections(host: object, *, family: str) -> None:
    """Set *host*'s pin attributes from its lists and pins (a constructor's entry).

    Reads each of *family*'s capability attributes from *host*, resolves them
    with :func:`resolve_selections` (``label`` is ``host.name``, no
    preferences) and sets each pin attribute. A list attribute is never set.
    """
    caps = menu_capabilities(family)
    selected = resolve_selections(
        caps,
        label=getattr(host, "name"),  # noqa: B009 — *host* is typed object: any host class
        menus={c.pin_field: getattr(host, c.menu_field) for c in caps},
        pins={c.pin_field: getattr(host, c.pin_field) for c in caps},
        preferences={},
    )
    for field, value in selected.items():
        setattr(host, field, value)


def select_preferences(
    table: dict[str, dict[str, list[str] | dict[str, Any]]], host_id: str
) -> dict[str, list[str]]:
    """Reduce a preferences table to the flat capability **selections** for one host.

    Only list-valued entries (selections) are considered; dict-valued entries
    (option tables) are ignored here. Definition-order cascade: later matching
    selectors replace a capability's list. Lists are copied.
    """
    return _selection_cascade(table, host_id, selectors=None)


def preference_selectors(
    table: dict[str, dict[str, list[str] | dict[str, Any]]], host_id: str
) -> dict[str, str]:
    """Return which selector supplied each of :func:`select_preferences`' lists.

    The same cascade, keyed the same way (``{"term": "dut-.*"}``). For a
    recorded build only: it names a ``[host_preferences]`` winner.
    """
    selectors: dict[str, str] = {}
    _selection_cascade(table, host_id, selectors=selectors)
    return selectors


def _selection_cascade(
    table: dict[str, dict[str, list[str] | dict[str, Any]]],
    host_id: str,
    *,
    selectors: "dict[str, str] | None",
) -> dict[str, list[str]]:
    """Return *host_id*'s capability lists, filling *selectors* (when given) with each selector."""
    effective: dict[str, list[str]] = {}
    for selector, entries in table.items():
        if re.fullmatch(selector, host_id):
            for key, val in entries.items():
                if isinstance(val, list):
                    effective[key] = list(val)
                    if selectors is not None:
                        selectors[key] = selector
    return effective


def select_option_defaults(
    table: dict[str, dict[str, Any]], host_id: str
) -> dict[str, dict[str, Any]]:
    """Reduce a preferences table to the per-host option-value **defaults**.

    Only dict-valued entries (option tables) are considered; list-valued entries (selections) are
    ignored here. Definition-order cascade: later matching selectors merge
    **per key** into each option table. Tables are copied.
    """
    return _option_cascade(table, host_id, selectors=None)


def option_default_selectors(
    table: dict[str, dict[str, Any]], host_id: str
) -> dict[str, dict[str, str]]:
    """Return which selector supplied each of :func:`select_option_defaults`' values.

    The same cascade, keyed the same way (``{"ssh_options": {"port": ".*"}}``),
    each value the selector whose table the default came from. For a
    recorded build only: it is what names a ``[host_preferences]`` winner.
    """
    selectors: dict[str, dict[str, str]] = {}
    _option_cascade(table, host_id, selectors=selectors)
    return selectors


def _option_cascade(
    table: dict[str, dict[str, Any]],
    host_id: str,
    *,
    selectors: "dict[str, dict[str, str]] | None",
) -> dict[str, dict[str, Any]]:
    """Return *host_id*'s option defaults, filling *selectors* (when given) with their selectors."""
    effective: dict[str, dict[str, Any]] = {}
    for selector, entries in table.items():
        if re.fullmatch(selector, host_id):
            for key, val in entries.items():
                if isinstance(val, dict):
                    effective.setdefault(key, {}).update(val)
                    if selectors is not None:
                        selectors.setdefault(key, {}).update(dict.fromkeys(val, selector))
    return effective


def _preference_won(*, field: str) -> None:
    """Attribute capability *field* to its ``[host_preferences]`` list (recorded builds only)."""
    from . import provenance

    provenance.preference_won(field=field)


def authenticating_protocols() -> list[str]:
    """Sorted names of every registered term/transfer backend that logs in with a cred.

    The ONE place otto computes the vocabulary a cred's ``protocols`` scope may
    use (spec 2026-09-13 cred-scope §2.1): the host-spec validator, its error
    message and the docs all read this. Function-local imports keep this
    module off the registries' import graph, as the spec validators do.
    """
    from .connections import TERM_BACKENDS  # off the startup graph on purpose
    from .transfer.registry import TRANSFER_BACKENDS

    terms = [name for name, e in TERM_BACKENDS.raw_items() if e.metadata.authenticates]
    transfers = [name for name, e in TRANSFER_BACKENDS.raw_items() if e.metadata.authenticates]
    return sorted({*terms, *transfers})

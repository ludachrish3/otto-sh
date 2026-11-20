"""The process inventory view: every named inventory, and the one selection decision (#1176).

A process has one catalog of inventories, keyed by name. ``default`` is
today's inventory, the one a bare ``"inventory": "<key>"`` reference resolves
through; every other name comes from an ``[[inventories]]`` entry and is
reached only by a qualified ``{"name": "<inventory>", "key": "<key>"}``
reference. :class:`InventoryView` holds that catalog and decides, in
:meth:`InventoryView.selected_name`, which inventory a reference consults.

A project that declares ``[[inventories]]`` without binding ``default`` itself
refuses bare references (lab templating D1): :func:`refuses_bare` decides it,
and :class:`RefusedDefault` stands in for ``default`` there, raising
:class:`~otto.inventory.errors.BareReferenceRefused` on first use.

The builders live in :mod:`otto.inventory.config`
(:func:`~otto.inventory.config.build_inventory_view`,
:func:`~otto.inventory.config.deferred_inventory_view`). This module builds
nothing and imports no settings model at module scope: it sits on the
bootstrap path.
"""

import dataclasses
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn

from typing_extensions import final

from ..host.inventory_ref import DEFAULT_INVENTORY
from .errors import BareReferenceRefused, InventoryError

if TYPE_CHECKING:
    from ..creds.config import CredsIdentity
    from ..host.inventory_ref import InventoryReference
    from ..registry import FrozenMap
    from .config import BindingIdentity
    from .protocol import Inventory


def no_default_message(*, key: str) -> str:
    """Return the error text for a reference through ``default`` when none is configured.

    One message for every resolver: the bare-reference join
    (:func:`~otto.inventory.resolve_host_entry`) and the view's
    :meth:`InventoryView.selected_name` both raise it.
    """
    return (
        f"references inventory key {key!r} but no inventory is configured; declare "
        "[inventory] in ~/.otto/settings.toml or in the project's .otto/settings.toml"
    )


def needs_view_message(*, name: str, key: str) -> str:
    """Return the error text for a qualified reference to *name* that only ``default`` can reach.

    A legacy ``inventory=`` argument stands for ``default`` alone
    (:meth:`InventoryView.of_default`), so a reference naming any other
    inventory needs the whole named-inventory view.
    """
    return (
        f"references inventory {name!r} (key {key!r}), which needs a named-inventory view; "
        f"this join resolves through the {DEFAULT_INVENTORY!r} inventory only"
    )


def bare_refusal_message(*, origin: str, key: str, declared: list[str]) -> str:
    """Return the D1 diagnostic for bare reference *key* in the project whose settings are *origin*.

    It quotes the qualified form as valid JSON, ``{"name": "<inventory>",
    "key": <key>}``, so the fix can be pasted into a lab file, and lists the
    *declared* inventory names it could name.
    """
    qualified = json.dumps({"name": "<inventory>", "key": key})
    names = ", ".join(repr(n) for n in declared) or "none"
    return (
        f"bare inventory reference {key!r} is refused: {origin} declares [[inventories]] "
        f"without binding {DEFAULT_INVENTORY!r}, so a bare key names no inventory; write "
        f"the qualified form, {qualified} (declared inventories: {names}), or bind "
        f"{DEFAULT_INVENTORY!r} in that file with [inventory] or an [[inventories]] entry "
        f"named {DEFAULT_INVENTORY!r}"
    )


def refuses_bare(*, inventory: "Mapping[str, Any] | None", inventories: "list[Any]") -> bool:
    """Return whether one project's settings refuse bare inventory references (D1).

    They do when the file declares ``[[inventories]]`` (*inventories*, raw
    tables or parsed entries) and does not bind ``default`` itself, through
    ``[inventory]`` (*inventory*, a raw table: ``{}`` or ``None`` when absent)
    or an entry named ``default``. The decision is
    :func:`~otto.models.settings.binds_default`'s.
    """
    from ..models.settings import binds_default, bound_table  # deferred: bootstrap path

    return bool(inventories) and not binds_default(
        inventory=bound_table(inventory), inventories=inventories
    )


@final
@dataclass(frozen=True, kw_only=True, eq=False)
class RefusedDefault:
    """``default`` as a project that refuses bare references sees it (D1).

    It satisfies :class:`~otto.inventory.protocol.Inventory`, so it can stand
    wherever the bare-reference inventory is passed. Its :meth:`lookup` never
    answers: every bare reference is refused with one diagnostic, before any
    supplies check, because :attr:`supplies` is only ``{"ip"}``.
    :attr:`default` reads the process ``default`` (or ``None``) from
    :attr:`inventories` only when used, which a qualified ``{"name":
    "default", ...}`` reference may still do. It compares by identity, as
    :class:`InventoryView` does: value equality would build a deferred
    catalog.
    """

    origin: str
    """The refusing project's settings file, for the diagnostic."""
    declared: list[str]
    """The inventory names the diagnostic lists."""
    inventories: "Mapping[str, Inventory]" = field(default_factory=dict)
    """Every inventory by name (:attr:`InventoryView.inventories`); :attr:`default` reads it."""
    on_refusal: "Callable[[str, str], None] | None" = None
    """Called ``(what, message)`` before each refusal is raised: the one drop recorder."""

    @property
    def default(self) -> "Inventory | None":
        """Return the process ``default`` inventory, for a qualified reference to it.

        Read on each use, so building it (and any error doing so) happens only
        where ``default`` is actually used; ``None`` when none is configured.
        """
        return self.inventories.get(DEFAULT_INVENTORY)

    @property
    def label(self) -> str:
        """Return ``refused:<origin>``."""
        return f"refused:{self.origin}"

    @property
    def supplies(self) -> frozenset[str]:
        """Return ``{"ip"}``, the least an inventory may supply."""
        return frozenset({"ip"})

    def refuse(self, *, key: str) -> NoReturn:
        """Refuse bare reference *key*: the one owner of the D1 diagnostic.

        Calls :attr:`on_refusal` with ``inventory reference '<key>'`` and the
        message when set, so the refusal is recorded exactly once, then
        raises.

        Raises:
            otto.inventory.errors.BareReferenceRefused: Always.
        """
        message = bare_refusal_message(origin=self.origin, key=key, declared=self.declared)
        if self.on_refusal is not None:
            self.on_refusal(f"inventory reference {key!r}", message)
        raise BareReferenceRefused(message)

    def lookup(self, key: str) -> NoReturn:
        """Refuse *key* (see :meth:`refuse`)."""
        self.refuse(key=key)

    def list_keys(self) -> list[str]:
        """Return ``[]``: no bare key is reachable here."""
        return []

    def fingerprint(self) -> None:
        """Return ``None``: a refusal has no records to be fresh."""
        return


@final
@dataclass(frozen=True, kw_only=True)
class BindingKey:
    """What one name of a view is bound to, for in-process memo keys.

    Both the configuration (:attr:`binding`, :attr:`creds`) and what it
    constructed (:attr:`label`, :attr:`creds_label`): two bindings with one
    label but different settings differ, and so do two selections of one
    setting whose symlinked file the factory resolved differently.
    """

    binding: "BindingIdentity"
    """The inventory's configuration, :meth:`~otto.inventory.CompiledInventory.binding_identity`."""
    creds: "CredsIdentity | None"
    """The credentials store's configuration, or ``None`` when the name has none."""
    label: str
    """The constructed inventory's own ``label``."""
    creds_label: "str | None"
    """The constructed credentials store's own ``label``, or ``None``."""


def inventory_label(*, inventory: object) -> str:
    """Return *inventory*'s ``label``, or its class name when it has none: the one label reader.

    Read through ``getattr``, not the protocol attribute: completion and
    ``otto cache info`` run it on the TAB path, and a third-party inventory
    without a ``label`` must not crash the shell.
    """
    return getattr(inventory, "label", type(inventory).__name__)


def binding_key(
    *, binding: "BindingIdentity", creds: "CredsIdentity | None", inventory: object
) -> BindingKey:
    """Return the :class:`BindingKey` of *inventory*, built from *binding* and *creds*.

    The inventory's label is :func:`inventory_label`'s. The credentials label
    is :func:`~otto.inventory.creds.creds_store_label`'s, because a
    :class:`~otto.inventory.CredsOverlay` reports only the inner inventory's.
    """
    from .creds import creds_store_label  # deferred: bootstrap path

    return BindingKey(
        binding=binding,
        creds=creds,
        label=inventory_label(inventory=inventory),
        creds_label=creds_store_label(inventory=inventory),
    )


@final
@dataclass(frozen=True, kw_only=True)
class ViewIdentity:
    """A hashable identity of a whole view: every name's binding, who refuses bare keys, and scope.

    Two views with one identity resolve every reference alike, so a memo
    keyed on it never serves one view's answer to another.
    """

    bindings: "FrozenMap[str, BindingKey]"
    """Each name's :class:`BindingKey`."""
    refusing_roots: frozenset[Path]
    """The project roots that refuse bare references."""
    default_only: bool = False
    """:attr:`InventoryView.default_only`: an empty legacy view refuses a qualified name
    with another message than an empty catalog does."""


def _stat_paths_of(inventory: object) -> "list[Path] | None":
    """Return *inventory*'s ``stat_paths()``, or ``None`` when it names none."""
    stat = getattr(inventory, "stat_paths", None)
    return stat() if callable(stat) else None


@final
@dataclass(frozen=True, kw_only=True, eq=False)
class InventoryView:
    """The process's inventories by name, and the one decision of which a reference consults.

    :attr:`inventories` maps each name to its inventory; ``default`` is the
    bare-reference inventory. A view is immutable: :meth:`for_project` and
    :meth:`with_refusal_hook` return a copy for one project, or with a drop
    recorder. Views compare by identity: what two views bind is compared
    through :meth:`memo_identity`, because value equality on a deferred view
    would build every name.
    """

    inventories: "Mapping[str, Inventory]"
    """Every configured inventory by name; ``default`` when one is configured."""
    bare_refused_by: Mapping[Path, str] = field(default_factory=dict)
    """Project root -> its settings file, for each project that refuses bare references."""
    project_root: "Path | None" = None
    """The project whose references this view resolves, or ``None`` for none in particular."""
    on_refusal: "Callable[[str, str], None] | None" = None
    """Called ``(what, message)`` on each bare-reference refusal; see :class:`RefusedDefault`."""
    keys: Mapping[str, BindingKey] = field(default_factory=dict)
    """Each name's :class:`BindingKey`, filled by the builders."""
    default_only: bool = False
    """Whether only ``default`` is reachable: the view a legacy ``inventory=`` argument stands for.

    A qualified reference to any other name raises :func:`needs_view_message`,
    whatever :attr:`inventories` holds (see :meth:`of_default`)."""

    @classmethod
    def of_default(cls, *, inventory: "Inventory | None") -> "InventoryView":
        """Return the view a legacy ``inventory=`` argument stands for: ``default`` alone.

        *inventory* is ``default``, or ``None`` for none. The view is
        :attr:`default_only`: a qualified reference to any other inventory
        raises :func:`needs_view_message`.
        :func:`~otto.inventory.resolve_host_entry` and the built-in json
        source's legacy methods all resolve through it, so a legacy argument
        means one thing on every route.

        A :class:`RefusedDefault` (what :attr:`bare_inventory` hands a legacy
        backend in a project that refuses bare references) becomes the
        refusing view it came from: a bare reference is refused before any
        supplies check or lookup, and a qualified ``{"name": "default", ...}``
        one still reaches :attr:`RefusedDefault.default`. The diagnostic lists
        the names of :attr:`RefusedDefault.inventories`, which is what
        :attr:`bare_inventory` puts in :attr:`RefusedDefault.declared`.
        """
        if isinstance(inventory, RefusedDefault):
            # The origin keys the refusal; it stands in for the project root.
            root = Path(inventory.origin)
            return cls(
                inventories=inventory.inventories,
                bare_refused_by={root: inventory.origin},
                project_root=root,
                on_refusal=inventory.on_refusal,
                default_only=True,
            )
        return cls(
            inventories={} if inventory is None else {DEFAULT_INVENTORY: inventory},
            default_only=True,
        )

    def for_project(self, *, root: Path) -> "InventoryView":
        """Return this view as the project at *root* sees it."""
        return dataclasses.replace(self, project_root=root)

    def with_refusal_hook(self, *, hook: "Callable[[str, str], None] | None") -> "InventoryView":
        """Return this view with *hook* recording each bare-reference refusal."""
        return dataclasses.replace(self, on_refusal=hook)

    def names(self) -> list[str]:
        """Return every configured inventory name, sorted."""
        return sorted(self.inventories)

    def _refusing_origin(self) -> "str | None":
        """Return the settings file of this view's project when it refuses bare keys."""
        if self.project_root is None:
            return None
        return self.bare_refused_by.get(self.project_root)

    def _refused(self, *, origin: str) -> RefusedDefault:
        """Return the :class:`RefusedDefault` for the refusing project at *origin*."""
        return RefusedDefault(
            origin=origin,
            declared=self.names(),
            inventories=self.inventories,
            on_refusal=self.on_refusal,
        )

    @property
    def bare_inventory(self) -> "Inventory | None":
        """Return what a bare reference resolves through.

        A :class:`RefusedDefault` when this view's project refuses bare
        references, decided first and without touching ``default`` (D1 wins:
        a ``default`` that cannot be built does not pre-empt the refusal);
        otherwise the process ``default``, or ``None`` when none is
        configured.

        Raises:
            otto.inventory.errors.InventoryError: Outside a refusing project,
                ``default`` cannot be built (a deferred view's unreadable user
                settings file, say).
        """
        origin = self._refusing_origin()
        if origin is not None:
            return self._refused(origin=origin)
        return self.inventories.get(DEFAULT_INVENTORY)

    def selected_name(self, *, reference: "InventoryReference") -> str:
        """Return the name of the inventory *reference* consults: the one selection decision.

        In order: in a :attr:`default_only` view, a qualified reference to
        any other name raises :func:`needs_view_message`; a bare reference in
        a project that refuses them is refused
        (:meth:`RefusedDefault.refuse`); a reference through ``default``,
        bare or qualified, with no ``default`` configured raises
        :func:`no_default_message`; any other undeclared name raises,
        listing the declared ones. It builds and looks up nothing.

        Raises:
            otto.inventory.errors.BareReferenceRefused: The bare reference is refused.
            otto.inventory.errors.InventoryError: The name is not configured.
        """
        if self.default_only and reference.name != DEFAULT_INVENTORY:
            raise InventoryError(needs_view_message(name=reference.name, key=reference.key))
        origin = self._refusing_origin()
        if not reference.qualified and origin is not None:
            self._refused(origin=origin).refuse(key=reference.key)
        if reference.name in self.inventories:
            return reference.name
        if reference.name == DEFAULT_INVENTORY:
            raise InventoryError(no_default_message(key=reference.key))
        declared = ", ".join(repr(n) for n in self.names()) or "none"
        raise InventoryError(
            f"references inventory {reference.name!r} (key {reference.key!r}), which is not "
            f"configured; declared inventories: {declared}"
        )

    def select_name(self, *, name: str) -> "Inventory":
        """Return the inventory named *name*.

        Raises:
            otto.inventory.errors.InventoryError: No inventory of that name is configured.
        """
        if name not in self.inventories:
            declared = ", ".join(repr(n) for n in self.names()) or "none"
            raise InventoryError(
                f"no inventory named {name!r} is configured; declared inventories: {declared}"
            )
        return self.inventories[name]

    def select(self, *, reference: "InventoryReference") -> "Inventory":
        """Return the inventory *reference* consults: :meth:`selected_name`, :meth:`select_name`."""
        return self.select_name(name=self.selected_name(reference=reference))

    def fingerprint(self) -> "str | None":
        """Return a value that changes whenever any inventory's records may have.

        ``"none"`` for an empty view; ``default``'s own fingerprint for a view
        holding only ``default``; otherwise the JSON of the sorted ``[name,
        fingerprint]`` pairs, so no character inside a fingerprint can make two
        views collide. ``None`` when any inventory's fingerprint is ``None``.
        """
        names = self.names()
        if not names:
            return "none"
        if names == [DEFAULT_INVENTORY]:
            return self.inventories[DEFAULT_INVENTORY].fingerprint()
        pairs: list[list[str]] = []
        for name in names:
            fingerprint = self.inventories[name].fingerprint()
            if fingerprint is None:
                return None
            pairs.append([name, fingerprint])
        return json.dumps(sorted(pairs))

    def stat_paths(self) -> "list[Path] | None":
        """Return the files whose stat decides every inventory's freshness, or ``None``.

        A view holding only ``default`` passes its ``stat_paths()`` through;
        otherwise the sorted union, only when every inventory names its files
        (read through ``getattr``, as :meth:`~otto.inventory.CredsOverlay.stat_paths`
        reads its inner's), else ``None``.
        """
        names = self.names()
        if names == [DEFAULT_INVENTORY]:
            return _stat_paths_of(self.inventories[DEFAULT_INVENTORY])
        union: set[Path] = set()
        for name in names:
            paths = _stat_paths_of(self.inventories[name])
            if paths is None:
                return None
            union.update(paths)
        return sorted(union)

    def memo_identity(self) -> ViewIdentity:
        """Return a hashable identity of this view, for in-process memo keys.

        Every name needs its :class:`BindingKey`, which the builders in
        :mod:`otto.inventory.config` fill; a view built by hand (as
        :meth:`of_default` builds one) has none, and two such views would
        otherwise share one identity whatever they hold.

        Raises:
            ValueError: A name of this view has no binding key.
        """
        from ..registry import FrozenMap  # deferred: bootstrap path

        missing = sorted(set(self.names()) - set(self.keys))
        if missing:
            raise ValueError(
                f"this inventory view has no binding key for {', '.join(map(repr, missing))}; "
                "only a view the inventory builders made has a memo identity"
            )
        return ViewIdentity(
            bindings=FrozenMap(mapping={name: self.keys[name] for name in self.names()}),
            refusing_roots=frozenset(self.bare_refused_by),
            default_only=self.default_only,
        )

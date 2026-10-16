"""
Declared-entry core: settings-declared strategies bound to code-registered kinds.

The seam-neutral half of the declared products/tools design
(spec 2026-09-01): a ``[[products]]`` / ``[[dev_tools]]`` table in
``.otto/settings.toml`` selects hosts with a typed **match table** and binds
behavior by naming a **kind** registered in code. This module owns the entry
schema, the one matcher, and the per-seam registry type; each seam
(:mod:`otto.host.product`, :mod:`otto.host.dev_tool`) contributes only a
registry instance, a ``register_<seam>_kind`` wrapper, and an apply loop.
Future declarative seams adopt the identical convention by doing the same.

This module also holds the **composed state** (spec 2026-10-11
placement-and-phases §3.5, §3.9): what the composition root,
:mod:`otto.bootstrap`, composed, a :class:`BootstrapResult`, published where
the layers below the root can read it. The root publishes its in-progress
result before its repos' init modules run and its complete result when it
finishes, and withdraws both when it fails or is invalidated. When it is
imported, it installs the **initiator**, the one way a reader down here
starts composition. :func:`compose` is the one read that starts it: the
complete result, else the in-progress one, else the initiator's. The repo
accessors (:func:`get_repos`, :func:`get_ordered_repos`, and
:func:`current_repos` and :func:`current_ordered_repos`, which read the
active run first) and :func:`scope_for_repo` read through it.
:func:`is_bootstrapped` and :func:`declared_for_host` read the published
results only and never start composition. This module never imports
:mod:`otto.bootstrap`: the root writes here, and nothing here reaches up to
it. The root re-exports four of them (:class:`BootstrapResult`,
:func:`get_repos`, :func:`get_ordered_repos` and :func:`is_bootstrapped`),
each the same object.

Deliberately import-light: :mod:`otto.registry` at module scope, and
``packaging``, ``otto.invocation`` and :mod:`otto.config.scope` only
inside the functions that use them, because the host seam modules import
this one and construct their registries at import.
"""

import logging
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Generic, Literal, NamedTuple, TypeVar, cast

from .registry import Ref, Registry, resolved

if TYPE_CHECKING:
    from .config.repo import Repo
    from .config.scope import BootstrapError, BootstrapWarning, ProjectScopeConfig
    from .host.host import Host
    from .models.settings import OttoEnvSettings

logger = logging.getLogger(__name__)

T = TypeVar("T")
"""Type variable for instances built by a :class:`KindBuilder`."""

MatchLeaf = bool | int | float | str
"""One TOML scalar in a match table; strings carry the regex/specifier split."""

MatchValue = MatchLeaf | list[MatchLeaf]
"""One match-table value — a single leaf, or a list meaning any-of."""

MATCH_KEYS = frozenset(
    {"id", "element.name", "element.id", "os_type", "os_name", "os_version", "ip", "source_lab"}
)
"""Host attribute paths a match table may name directly (spec 2026-09-05
§8.1): a key IS the path on the host — ``element.id`` reads
``host.element.id``. Everything else is reached as a dotted ``metadata.`` /
``element.metadata.`` path; an unlisted key is a settings error, never a
silent no-match."""

_SPECIFIER_PREFIXES = (">=", "<=", "==", "~=", "!=", ">", "<")
_METADATA_ROOTS = ("metadata", "element.metadata")

_RETIRED_KEYS = {"element": "element.name", "element_id": "element.id"}
"""Pre-2026-09-05 spellings, refused with the path that replaced them. The
retired ``element_metadata.<key>`` root is handled in :func:`_reject_key`."""


@dataclass(frozen=True)
class DeclaredEntry:
    """One parsed ``[[products]]``/``[[dev_tools]]`` table, in runtime form."""

    name: str
    """Logical identity — the ``Product.name``/``DevTool.name`` role."""

    kind: str | None
    """Key into the seam's kind registry; ``None`` when :attr:`cls` names the class."""

    seam: str
    """The TOML array this entry came from (``"products"``/``"dev_tools"``) —
    error wording only, so a factory's complaint names the table to fix."""

    owner: str | None
    """Declaring repo's name; stamped onto built instances whose own owner is None."""

    base_dir: Path
    """The declaring repo's root — what kind factories anchor local paths against
    (:func:`otto.utils.anchor_path`); remote paths stay in the host's domain."""

    match: dict[str, MatchValue] = field(default_factory=dict)
    """Typed match table; empty admits every host the repo targets."""

    params: dict[str, Any] = field(default_factory=dict)
    """Every non-reserved TOML key, verbatim — the kind's to validate."""

    cls: str | None = None
    """``class = "package.module:ClassName"`` — a
    :class:`~otto.host.declared_product.DeclaredProduct` subclass the registry's class factory
    builds; exactly one of :attr:`kind`/:attr:`cls` is set."""

    variant: str | None = None
    """``variant = "debug" | "field"``: the run variant this entry is for; ``None`` is any run."""

    @property
    def kind_label(self) -> str:
        """Return the kind name, or the ``class =`` path, as the entry's printed kind."""
        return self.kind if self.kind is not None else (self.cls or "")


def _is_metadata_path(key: str) -> bool:
    """Report whether *key* is a dotted ``metadata.``/``element.metadata.`` path."""
    return any(key.startswith(root + ".") and len(key) > len(root) + 1 for root in _METADATA_ROOTS)


def _reject_key(key: str) -> None:
    """Raise the settings error for an unrecognized match key.

    A retired spelling (``element``, ``element_id``, ``element_metadata.<k>``)
    names its replacement; anything else lists :data:`MATCH_KEYS` and the
    dotted escape hatch.
    """
    root, _, rest = key.partition(".")
    replacement: str | None = None
    if key in _RETIRED_KEYS:
        replacement = _RETIRED_KEYS[key]
    elif root == "element_metadata" and rest:
        replacement = f"element.metadata.{rest}"
    if replacement is not None:
        raise ValueError(
            f"match key {key!r} is now spelled {replacement!r} — a key is the host attribute path"
        )
    raise ValueError(
        f"unknown match key {key!r}; valid keys: {sorted(MATCH_KEYS)}, "
        f"or a dotted 'metadata.<key>' / 'element.metadata.<key>' path"
    )


def validate_match_table(match: dict[str, MatchValue]) -> None:
    """Reject a malformed match table (unknown key, bad regex, bad specifier).

    The parse-time half of matching: everything about a match table is static,
    so it is judged once at settings parse — a typo'd key or an uncompilable
    pattern is a settings error at bootstrap, never a fleet walk that silently
    matches nothing later (the :class:`~otto.config.scope.ProjectScopeConfig`
    rule). :func:`host_matches` may then assume a valid table.

    Raises:
        ValueError: Naming the offending key or pattern; for an unknown key
            the message lists :data:`MATCH_KEYS` and the dotted
            ``metadata.``/``element.metadata.`` escape hatch; a retired
            spelling names its replacement instead.
    """
    for key, value in match.items():
        if key not in MATCH_KEYS and not _is_metadata_path(key):
            _reject_key(key)
        leaves = value if isinstance(value, list) else [value]
        for leaf in leaves:
            if not isinstance(leaf, str):
                continue
            if leaf.startswith(_SPECIFIER_PREFIXES):
                from packaging.specifiers import InvalidSpecifier, SpecifierSet

                try:
                    SpecifierSet(leaf)
                except InvalidSpecifier:
                    raise ValueError(
                        f"match key {key!r}: {leaf!r} is not a valid version specifier"
                    ) from None
            else:
                try:
                    re.compile(leaf)
                except re.error as e:
                    raise ValueError(
                        f"match key {key!r}: {leaf!r} is not a valid regex: {e}"
                    ) from None


_warned_versions: set[tuple[str, str, str]] = set()
"""(host id, key, pattern) triples already warned about — see :func:`host_matches`.
Grows monotonically per process; this is accepted (triples are tiny)."""


def _resolve_key(host: Any, key: str) -> Any:
    """Walk *key* as a path from *host*: attributes, then dict keys, None when absent.

    A key IS the host attribute path (spec 2026-09-05 §8.1), so
    ``element.metadata.rev`` and ``element.name`` share this one walk. An
    unvalidated table (library use skipping the settings boundary) gets the
    same loud complaint the boundary would have raised, before any attribute
    is read.

    Beneath a metadata root (``metadata.``/``element.metadata.``) every
    remaining segment is a DICT KEY ONLY — never a further attribute lookup.
    Metadata is opaque user data, not an object graph: without this, a
    numeric leaf's ``.denominator`` or a string leaf's ``.__class__`` would
    resolve through Python's own attributes instead of reporting "no value".
    """
    if key not in MATCH_KEYS and not _is_metadata_path(key):
        _reject_key(key)
    root = next((r for r in _METADATA_ROOTS if key.startswith(r + ".")), None)
    prefix = root.split(".") if root else key.split(".")
    value: Any = host
    for part in prefix:
        value = getattr(value, part, None)
        if value is None:
            return None
    if root is None:
        return value
    for part in key[len(root) + 1 :].split("."):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def _leaf_matches(leaf: MatchLeaf, value: Any, *, host: Any, key: str) -> bool:
    if isinstance(leaf, str):
        if leaf.startswith(_SPECIFIER_PREFIXES):
            # Parse the HOST value ourselves and warn on failure: packaging >=26
            # makes SpecifierSet.contains() return False on an unparseable
            # version instead of raising, so relying on it would be silent on
            # new packaging and a crash on old. One warning per (host, key,
            # pattern) — this runs per ingest, per entry.
            from packaging.specifiers import SpecifierSet
            from packaging.version import InvalidVersion, Version

            try:
                version = Version(str(value))
            except InvalidVersion:
                token = (str(getattr(host, "id", "?")), key, leaf)
                if token not in _warned_versions:
                    _warned_versions.add(token)
                    logger.warning(
                        "match key %r: host %s value %r is not a parseable "
                        "version; treating as no-match",
                        key,
                        getattr(host, "id", "?"),
                        value,
                    )
                return False
            return version in SpecifierSet(leaf)
        return re.fullmatch(leaf, str(value)) is not None
    return bool(value == leaf)


def host_matches(match: dict[str, MatchValue], host: Any) -> bool:
    """Report whether *host* satisfies every clause of *match* (AND across keys).

    Value semantics are type-driven — a plain string is ``re.fullmatch``
    (never ``search``: a scoping mistake that widens is the one no test
    notices), a specifier-prefixed string is a ``packaging`` version
    comparison, a bool/number is equality, a list is any-of. A host attribute
    that is ``None`` or absent (including a missing metadata key) never
    matches — presence varies per lab, and absence must select the fallback
    entry rather than crash the ingest.

    *host* is typed ``Any`` on purpose: the matcher reads plain attributes,
    and the seam tests drive it with the same ``SimpleNamespace`` doubles the
    provider tests use.
    """
    for key, value in match.items():
        resolved = _resolve_key(host, key)
        if resolved is None:
            return False
        leaves = value if isinstance(value, list) else [value]
        if not any(_leaf_matches(leaf, resolved, host=host, key=key) for leaf in leaves):
            return False
    return True


_RETIRED_KINDS: dict[str, str] = {
    "file": (
        "kind 'file' was renamed 'shell' (its verbs are the shell commands "
        "you write); update the entry"
    ),
    "llext": (
        "kind 'llext' is now 'embedded' (change `kind = \"llext\"` to "
        '`kind = "embedded"`); update the entry'
    ),
}
"""Kind names that no longer exist, each with the message that names its
replacement. A settings file written for a retired name fails at lab load
pointing at the fix, instead of the generic unknown-kind listing."""


KindFactory = Callable[[DeclaredEntry, "Host"], T]
"""The code a declared entry's kind binds to: ``factory(entry, host) -> instance``."""


@dataclass(frozen=True)
class KindEntry(Generic[T]):
    """The record a seam's kind registry stores: the factory, or a ``Ref`` naming it."""

    factory: "KindFactory[T] | Ref"
    """The kind's factory; a :class:`~otto.registry.Ref` is imported at its first use."""


def _check_kind_factory(name: str, factory: object) -> None:
    """Refuse a kind whose factory is not callable.

    One check, two call sites: a kind registry's ``validate`` runs it on a
    factory registered eagerly, its ``check_resolved`` on the object a ``Ref``
    names, once imported.
    """
    if not callable(factory):
        raise TypeError(f"kind {name!r}: factory {factory!r} is not callable")


def _validate_kind_entry(name: str, entry: "KindEntry[Any]", _proposed: object) -> None:
    """Check an eager factory now (a kind registry's ``validate``)."""
    if not isinstance(entry.factory, Ref):
        _check_kind_factory(name, entry.factory)


def _check_resolved_kind_entry(name: str, entry: "KindEntry[Any]") -> None:
    """Check the factory a ``Ref`` named (a kind registry's ``check_resolved``)."""
    _check_kind_factory(name, resolved(entry.factory))


class KindBuilder(Generic[T]):
    """Builds ONE declarative seam's entries from that seam's kind registry.

    It reads the plain :class:`~otto.registry.Registry` of
    :class:`KindEntry` records it is given, and holds the two hooks that
    build a ``class =`` entry. Each seam owns its registry and its builder —
    a kind can never attach to the other seam's lifecycle, the same two-list
    reasoning as the provider registries.
    """

    def __init__(
        self,
        kinds: "Registry[KindEntry[T]]",
        *,
        class_factory: Ref | None = None,
        class_resolver: Ref | None = None,
    ) -> None:
        """Build entries from *kinds*; *class_factory* and *class_resolver* go together.

        Each is a :class:`~otto.registry.Ref`, so the kind module is not
        imported until a ``class =`` entry needs it.

        Raises:
            ValueError: If only one of *class_factory* and *class_resolver*
                is given.
        """
        if (class_factory is None) != (class_resolver is None):
            raise ValueError(
                f"{kinds.kind} builder: `class_factory` and `class_resolver` go together — "
                "a seam that builds `class =` entries needs both"
            )
        self._kinds = kinds
        self._class_refs: dict[str, Ref | None] = {
            "factory": class_factory,
            "resolver": class_resolver,
        }
        self._class_resolved: dict[str, Callable[..., Any]] = {}

    @property
    def kinds(self) -> "Registry[KindEntry[T]]":
        """The kind registry this builder reads."""
        return self._kinds

    def _class_hook(self, which: Literal["factory", "resolver"]) -> Callable[..., Any]:
        """Import one of the two ``class =`` hooks on first use (one cache for both)."""
        if which not in self._class_resolved:
            ref = self._class_refs[which]
            if ref is None:
                raise ValueError(f"{self._kinds.kind} registry builds no `class =` entries")
            self._class_resolved[which] = cast("Callable[..., Any]", ref.resolve())
        return self._class_resolved[which]

    def class_factory(self) -> "KindFactory[T]":
        """Return the factory that builds a ``class =`` entry, imported on first use."""
        return self._class_hook("factory")

    def class_resolver(self) -> Callable[[DeclaredEntry], object]:
        """Return the check that imports and validates a ``class =`` entry's class.

        :meth:`build` runs it before any match, so a bad class path fails
        every ingest.
        """
        return self._class_hook("resolver")

    def build(self, entries: Iterable[DeclaredEntry], host: Any) -> list[T]:
        """Build the instances *entries* declare for *host*.

        Declaration order, first match per :attr:`DeclaredEntry.name` wins —
        literally the provider loops' dedup semantics, so "specific entries
        first, generic fallback last" needs no new mental model. Every
        entry's kind is resolved BEFORE its match is consulted: an unknown
        kind must fail every ingest loudly, not only on the hosts the entry
        happens to match. A name in ``_RETIRED_KINDS`` is refused first,
        with a message naming its replacement. Built instances whose
        ``owner`` is None are stamped with the entry's declaring repo (the
        provider loops' carve-out: a factory may hand its instance to another
        repo's ownership). Every built instance is also stamped with the
        entry's ``kind_label`` (the class path for a ``class =`` entry),
        ``origin = "declared"`` and the entry itself (``source_entry``) —
        recorded here, where the entry is in hand, so ``otto --list-products``
        reads facts instead of re-deriving them.

        An entry's ``variant``, when set, must equal the run's
        (:func:`otto.context.variant`) or the entry is passed over as a
        non-match — so a variant entry is a more specific entry and goes
        before its fallback. A ``class =`` entry is built by
        :meth:`class_factory`; its class is resolved by :meth:`class_resolver`
        right where a kind is looked up, for the same reason a kind is —
        a bad class path fails every ingest, whatever the match.
        """
        from .invocation import current_policy  # function-scope: the leaf is read when ingest runs

        run = current_policy().variant
        out: list[T] = []
        taken: set[str] = set()
        for entry in entries:
            if entry.kind is not None and entry.kind in _RETIRED_KINDS:
                raise ValueError(f"[[{entry.seam}]] {entry.name!r}: {_RETIRED_KINDS[entry.kind]}")
            if entry.kind is not None:
                factory = resolved(self._kinds.get(entry.kind).factory)
            elif self._class_refs["factory"] is None:
                raise ValueError(
                    f"[[{entry.seam}]] {entry.name!r}: {self._kinds.kind} registry builds no "
                    "`class =` entries"
                )
            else:
                factory = self.class_factory()
                self.class_resolver()(entry)
            if entry.name in taken:
                continue
            if entry.variant is not None and entry.variant != run:
                continue
            if not host_matches(entry.match, host):
                continue
            obj = factory(entry, host)
            if getattr(obj, "owner", None) is None:
                # Product/DevTool contract: built instances carry a mutable owner attribute.
                obj.owner = entry.owner  # ty: ignore[unresolved-attribute]
            # Same contract: Product/DevTool carry mutable kind/origin/source_entry attributes.
            obj.kind = entry.kind_label  # ty: ignore[unresolved-attribute]
            obj.origin = "declared"  # ty: ignore[unresolved-attribute]
            obj.source_entry = entry  # ty: ignore[unresolved-attribute]
            out.append(obj)
            taken.add(entry.name)
        return out


# ── The composed state, its catalog and the bootstrap initiator ─────────────
#
# Two slots of spec 2026-10-11 placement-and-phases §2.1, filled from above by
# otto.bootstrap, their one installer (tests/unit/test_slot_installers.py):
# the catalog through publish_composed and clear_composed, the initiator
# through install_initiator. Both live on one record, so no function here
# rebinds a module global.


@dataclass(frozen=True)
class BootstrapResult:
    """Everything bootstrap produced: environment, repos, contained errors."""

    env: "OttoEnvSettings"
    repos: "list[Repo]"
    errors: "list[BootstrapError]" = field(default_factory=list)
    warnings: "list[BootstrapWarning]" = field(default_factory=list)
    ordered_repos: "list[Repo]" = field(default_factory=list)
    """Repos in the dependency pass's topological order (skipped repos excluded)."""


@dataclass
class _Composition:
    """What the composition root published, and the function that starts it."""

    result: "BootstrapResult | None" = None
    """The complete result of the last composition that finished, or ``None``."""

    in_progress: "BootstrapResult | None" = None
    """The result a running composition published before its repos' init modules ran.

    Its ``repos`` and ``ordered_repos`` are final; its ``errors`` grow until
    the composition ends, when it is withdrawn. It exists so a re-entrant read
    has something true to answer with, and the reentrance is real: the import
    phase runs repo ``init`` modules, user code, and anything there that
    reaches :func:`get_repos` (directly, or by way of a stamped host whose
    product providers consult :func:`scope_for_repo`) lands here while no
    complete result is published. Without it, that read composed a second,
    nested root.
    """

    initiator: "Callable[[], BootstrapResult] | None" = None
    """The function that starts composition, installed when :mod:`otto.bootstrap` is imported."""


_composition: _Composition = _Composition()
"""The catalog and the initiator slot: written only through the functions below."""


def publish_composed(result: "BootstrapResult", *, complete: bool) -> None:
    """Publish *result*: the complete composition, or (``complete=False``) the one still running.

    Privileged: only :mod:`otto.bootstrap` calls it. Publishing the complete
    result withdraws the in-progress one.
    """
    if complete:
        _composition.result = result
        _composition.in_progress = None
    else:
        _composition.in_progress = result


def clear_composed() -> None:
    """Withdraw every published result; the initiator stays.

    Privileged, like :func:`publish_composed`. The root calls it when a
    composition fails and from :func:`otto.bootstrap.invalidate`.
    """
    _composition.result = None
    _composition.in_progress = None


def install_initiator(initiator: "Callable[[], BootstrapResult]") -> None:
    """Install *initiator*, the function a reader below the root calls to start composition.

    Privileged: only :mod:`otto.bootstrap` calls it, as the last statement of
    its module, so importing the root installs it and composes nothing.
    :func:`compose` is the one reader that calls it.
    """
    _composition.initiator = initiator


_NO_COMPOSITION_ROOT = (
    "Nothing composed otto's repos, and no composition root is loaded to compose them: "
    "call otto.bootstrap.bootstrap(), or open a run with otto.open_context(...)."
)
""":func:`compose`'s refusal when :mod:`otto.bootstrap` was never imported."""


def published_result() -> "BootstrapResult | None":
    """Return the complete published result, else the in-progress one, else ``None``.

    Never starts composition. A running composition's result has final
    ``repos`` and ``ordered_repos``, and its ``errors`` list is the one the
    composition keeps appending to, so a reader inside the window sees a
    later init's contained failure.
    """
    if _composition.result is not None:
        return _composition.result
    return _composition.in_progress


def compose() -> "BootstrapResult":
    """Return the composition: the published result, else the one the initiator composes.

    The one read below the root that starts composition. It answers with the
    complete result when one is published, else with the in-progress one (a
    repo init module that asks during composition gets the result being
    built, never a second, nested composition), else with what the
    initiator returns: :func:`otto.bootstrap.bootstrap`, looked up when
    called, so a patched one runs. A composition that raises publishes
    nothing, so the next call composes again and fails the same way.

    Raises:
        RuntimeError: no initiator is installed: :mod:`otto.bootstrap` was
            never imported in this process.
    """
    result = published_result()
    if result is not None:
        return result
    initiator = _composition.initiator
    if initiator is None:
        raise RuntimeError(_NO_COMPOSITION_ROOT)
    return initiator()


def get_repos() -> "list[Repo]":
    """Return the configured repos, as discovered (composes lazily, through :func:`compose`).

    Every repo whose ``settings.toml`` parsed, in ``OTTO_SUT_DIRS`` order,
    including one the dependency pass skipped; a repo whose settings would not
    parse is absent, and its error is in :attr:`BootstrapResult.errors`.
    """
    return compose().repos


def get_ordered_repos() -> "list[Repo]":
    """Return configured repos in dependency-topological order (composes lazily).

    Dependencies first, dependents after: the walk order the ``otto.project``
    orchestrator installs in (and reverses to uninstall). Skipped repos
    (unsatisfied required deps) are absent, exactly as they are absent from
    phase-2 registration.
    """
    return compose().ordered_repos


def is_bootstrapped() -> bool:
    """Report whether composition has started, a published result: never starts it.

    True once a complete result is published (a finished, successful
    composition) or an in-progress one is (the repos' init modules are
    running); false before the first composition, after one that failed, and
    after :func:`otto.bootstrap.invalidate`. Mid-composition counts as
    bootstrapped ON PURPOSE: an init module that builds a host, directly or by
    way of a stamped host whose product or dev-tool providers apply, runs
    INSIDE that window, and :func:`get_repos` already answers correctly and
    for free there (:func:`compose` answers with the in-progress result,
    whose ``repos`` and ``ordered_repos`` are final). Treating that window as
    "not bootstrapped" would make a host built mid-composition silently drop
    its declared entries while its providers still applied. The
    non-forcing probe: a caller that must not TRIGGER discovery or repo init
    imports as a side effect of merely asking reads this instead of calling
    :func:`otto.bootstrap.bootstrap` or :func:`get_repos`.
    """
    return published_result() is not None


def current_repos() -> "list[Repo]":
    """Return the run's repos where no context object is in hand.

    The active run's ``repos`` when one is installed (the slot in
    ``otto.invocation``), else the composition's (:func:`compose`). Code
    that holds a context reads ``ctx.repos``. This is for the conveniences
    that work with or without one (``prepare_run``, coverage's entry points,
    docker's repo reads). A fresh list on every call.
    """
    from .invocation import installed_run  # function-scope: import-light

    run = installed_run()
    if run is not None:
        return run.repos
    return list(compose().repos)


def current_ordered_repos() -> "list[Repo]":
    """Return :func:`current_repos` in dependency order, dependency-skipped repos absent."""
    from .invocation import installed_run  # function-scope: import-light

    run = installed_run()
    if run is not None:
        return run.ordered_repos
    return list(compose().ordered_repos)


class _LoadedRepos(NamedTuple):
    """The repos whose declarations exist, as :func:`_loaded_repos` reads them."""

    repos: list[Any]
    """Every discovered repo, in discovery order."""

    surviving: set[str | None]
    """The names of the repos the dependency pass kept."""


def _loaded_repos() -> "_LoadedRepos | None":
    """Return the published repos and the survivors' names, or ``None`` when nothing is published.

    The one place that decides which repos' declarations exist. It reads the
    catalog only: the complete result, else the one a running composition
    published, whose ``repos`` and ``ordered_repos`` are already final, so a
    host an init module builds mid-composition gets its entries. It never
    starts composition and never calls the initiator. Discovery order is kept
    for the repos; the survivors are the ones the dependency pass did not
    skip.
    """
    result = published_result()
    if result is None:
        return None
    return _LoadedRepos(
        repos=list(result.repos or ()),
        surviving={getattr(r, "name", None) for r in result.ordered_repos or ()},
    )


def surviving_repos() -> list[Any]:
    """Return the discovered repos whose declarations apply, in discovery order.

    The set :func:`declared_for_host` collects from: every discovered repo except
    one the dependency pass skipped. Empty when nothing is composed.
    """
    loaded = _loaded_repos()
    if loaded is None:
        return []
    return [r for r in loaded.repos if getattr(r, "name", None) in loaded.surviving]


def declared_for_host(host: Any, seam_attr: str) -> list[DeclaredEntry]:
    """Collect every loaded repo's *seam_attr* entries admitted for *host*.

    The declarative twin of the provider loops' §5 gate: a repo's entries are
    skipped when its ``[project]`` declaration does not target
    ``(host.source_lab, host.id)``, with the same unstamped-host carve-out
    (``source_lab == ""`` is not judged — hosts built outside the loader
    predate scoping). Unlike :func:`scope_for_repo`'s admit-on-failure, a
    process that has composed nothing yields ``[]``: the entries LIVE in the
    composed repos, so "nothing composed" means there is nothing to collect —
    the empty answer is the true one, not a refusal to answer.

    This must never itself be the reason a process composes: composition is
    lazy so a bare-library caller (``create_host_from_dict`` in an ``otto
    init``/unit-test process, say) never pays discovery's cost or runs repo
    init imports unless something else needed them. So collection reads the
    published results only (the complete one, else the in-progress one) and
    never calls the initiator.

    Dependency-skipped repos contribute nothing: a repo the dependency pass
    dropped had its ``[[products]]``/``[[dev_tools]]`` parsed in phase 1, but
    its init modules never ran — applying its declared half while its provider
    half stayed silent would make the two seams disagree about whether the
    repo is present at all (Chris, 2026-09-02). Iteration still walks the full
    discovered list in DISCOVERY order and filters against the survivors, so
    cross-repo first-match precedence stays the documented ``sut_dirs`` order
    rather than the dependency pass's topological reshuffle.

    ``getattr`` with a default because bare-library ``Repo`` stand-ins predate
    these fields; *seam_attr* is ``"declared_products"``/``"declared_dev_tools"``.
    """
    loaded = _loaded_repos()
    if loaded is None:
        logger.debug("declared %s: nothing composed yet — no entries", seam_attr)
        return []
    from .config.scope import repo_targets  # function-scope: same import-light seam

    out: list[DeclaredEntry] = []
    for repo in loaded.repos:
        entries = getattr(repo, seam_attr, None)
        if not entries:
            continue
        if getattr(repo, "name", None) not in loaded.surviving:
            logger.debug(
                "declared %s: repo %r was dependency-skipped — entries do not apply",
                seam_attr,
                getattr(repo, "name", "?"),
            )
            continue
        if host.source_lab and not repo_targets(
            getattr(repo, "project_scope", None), host.source_lab, host.id
        ):
            logger.debug(
                "declared %s: repo %r does not target host %s of lab %r — skipped",
                seam_attr,
                getattr(repo, "name", "?"),
                host.id,
                host.source_lab,
            )
            continue
        out.extend(entries)
    return out


def scope_for_repo(owner: "str | None") -> "ProjectScopeConfig | None":
    """Look up the compiled ``[project]`` declaration of the repo named *owner*.

    The NAME-to-declaration hop the ingest gate needs. A provider registration
    carries the registering repo's name
    (:func:`otto.registry.get_registering_repo`), while
    :func:`~otto.config.scope.repo_targets` judges a compiled
    :class:`~otto.config.scope.ProjectScopeConfig`. The repos that hold both
    come from :func:`compose`: the published result (inside a running
    composition, the in-progress one), else the initiator's answer, which
    composes exactly as :func:`get_repos` does. With no initiator installed,
    :mod:`otto.bootstrap` was never imported, and the gate admits rather than
    start composition from below (spec 2026-10-11 placement-and-phases,
    decision D2).

    EVERY "cannot answer" answers ``None``, which
    :func:`~otto.config.scope.repo_targets` reads as *admits everything*, and
    that direction is deliberate. This lookup is consulted from the host
    factory, which runs in bare library use, in ``otto init`` scaffolding and
    in several hundred unit tests — none of which have a composition to ask.
    Raising or refusing there would turn "otto could not find its config"
    into "your host has no products", which is the same silent-wrong-answer
    this scoping exists to prevent, only pointed the other way. A gate is a
    NARROWING; when it cannot compute one, it narrows nothing.

    An owner no repo claims is admitted for the same reason, and is NOT the
    unknown-owner refusal :func:`~otto.config.scope.scoped_ids` raises: there
    a caller asked to be bounded BY a repo and getting the whole lab instead
    would be a widening, while here a provider arrived carrying a name this
    process cannot resolve (a repo bootstrap skipped, a marker from another
    run) and refusing it would take a working lab offline.

    Args:
        owner: ``Repo.name`` of the repo being asked about, or ``None`` for a
            registration made outside any repo's init import.

    Returns:
        That repo's compiled declaration, or ``None`` when the owner is
        ``None``, names no known repo, names a repo with no ``[project]``
        table, no initiator is installed, or composition fails.
    """
    if owner is None:
        return None
    try:
        repos = compose().repos
    except Exception:  # noqa: BLE001 — see above: ANY failure to reach the repos admits, deliberately
        return None
    for repo in repos or ():
        if repo.name == owner:
            return repo.project_scope
    return None

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

Deliberately import-light (stdlib + :mod:`otto.registry`; ``packaging``
lazily): the seam modules construct their registries at import, and this
module must never drag in ``otto.config`` — its package init boots the app.
The one function that needs config (:func:`declared_for_host`) imports it
function-scope, the same seam :func:`otto.config.scope.scope_for_repo` uses.
"""

import logging
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Generic, TypeVar

from .registry import Registry

logger = logging.getLogger(__name__)

T = TypeVar("T")
"""Type variable for instances built by a :class:`KindRegistry`."""

MatchLeaf = bool | int | float | str
"""One TOML scalar in a match table; strings carry the regex/specifier split."""

MatchValue = MatchLeaf | list[MatchLeaf]
"""One match-table value — a single leaf, or a list meaning any-of."""

MATCH_KEYS = frozenset(
    {"id", "element", "element_id", "os_type", "os_name", "os_version", "ip", "source_lab"}
)
"""Host attributes a match table may name directly — the same tooling-agnostic
surface the provider docstrings promise (see
:func:`otto.host.product.register_product_provider`), plus the OS identity
pair. Everything else is reached as a dotted ``metadata.``/``element_metadata.``
path; an unlisted bare key is a settings error, never a silent no-match."""

_SPECIFIER_PREFIXES = (">=", "<=", "==", "~=", "!=", ">", "<")
_METADATA_ROOTS = ("metadata", "element_metadata")


@dataclass(frozen=True)
class DeclaredEntry:
    """One parsed ``[[products]]``/``[[dev_tools]]`` table, in runtime form."""

    name: str
    """Logical identity — the ``Product.name``/``DevTool.name`` role."""

    kind: str
    """Key into the seam's :class:`KindRegistry`."""

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
            ``metadata.``/``element_metadata.`` escape hatch.
    """
    for key, value in match.items():
        root, _, rest = key.partition(".")
        if key not in MATCH_KEYS and not (root in _METADATA_ROOTS and rest):
            raise ValueError(
                f"unknown match key {key!r}; valid keys: {sorted(MATCH_KEYS)}, "
                f"or a dotted 'metadata.<key>' / 'element_metadata.<key>' path"
            )
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
    """Return the host value *key* names, or None when the host lacks it."""
    if key in MATCH_KEYS:
        return getattr(host, key, None)
    root, _, rest = key.partition(".")
    if root in _METADATA_ROOTS and rest:
        value: Any = getattr(host, root, None)
        for part in rest.split("."):
            if not isinstance(value, dict) or part not in value:
                return None
            value = value[part]
        return value
    # host_matches on an unvalidated table (library use skipping the settings
    # boundary) gets the same loud complaint the boundary would have raised.
    validate_match_table({key: ""})
    return None  # pragma: no cover — validate_match_table always raises above


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


class KindRegistry(Registry[Callable[[DeclaredEntry, Any], T]], Generic[T]):
    """Named kind factories for ONE declarative seam.

    A :class:`~otto.registry.Registry` subclass (not composition) on purpose:
    the test suite's registry isolation discovers global registries by
    scanning for ``Registry`` instances, and a wrapped inner registry would
    be invisible to it. Each seam owns its instance — a kind can never attach
    to the other seam's lifecycle, the same two-list reasoning as the
    provider registries.
    """

    def build(self, entries: Iterable[DeclaredEntry], host: Any) -> list[T]:
        """Build the instances *entries* declare for *host*.

        Declaration order, first match per :attr:`DeclaredEntry.name` wins —
        literally the provider loops' dedup semantics, so "specific entries
        first, generic fallback last" needs no new mental model. Every
        entry's kind is resolved BEFORE its match is consulted: an unknown
        kind must fail every ingest loudly, not only on the hosts the entry
        happens to match. Built instances whose ``owner`` is None are stamped
        with the entry's declaring repo (the provider loops' carve-out: a
        factory may hand its instance to another repo's ownership).
        """
        out: list[T] = []
        taken: set[str] = set()
        for entry in entries:
            factory = self.get(entry.kind)
            if entry.name in taken or not host_matches(entry.match, host):
                continue
            obj = factory(entry, host)
            if getattr(obj, "owner", None) is None:
                # Product/DevTool contract: built instances carry a mutable owner attribute.
                obj.owner = entry.owner  # ty: ignore[unresolved-attribute]
            out.append(obj)
            taken.add(entry.name)
        return out


def declared_for_host(host: Any, seam_attr: str) -> list[DeclaredEntry]:
    """Collect every loaded repo's *seam_attr* entries admitted for *host*.

    The declarative twin of the provider loops' §5 gate: a repo's entries are
    skipped when its ``[project]`` declaration does not target
    ``(host.source_lab, host.id)``, with the same unstamped-host carve-out
    (``source_lab == ""`` is not judged — hosts built outside the loader
    predate scoping). Unlike :func:`otto.config.scope.scope_for_repo`'s
    admit-on-failure, an unreachable config yields ``[]``: the entries LIVE in
    that config, so "cannot reach it" means there is nothing to collect — the
    empty answer is the true one, not a refusal to answer.

    This must never itself be the reason a process bootstraps: bootstrap is
    lazy so a bare-library caller (``create_host_from_dict`` in an ``otto
    init``/unit-test process, say) never pays discovery's cost or runs repo
    init imports unless something else needed them. So collection PROBES
    ``otto.config.is_bootstrapped()`` first and returns ``[]`` without calling
    ``get_repos()`` at all when bootstrap has not already happened — a process
    that has not composed the root has no entries loaded yet, which is the
    same true empty answer as any other unreachable-config case.

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
    try:
        # function-scope: config's init boots the app
        from .config import get_ordered_repos, get_repos, is_bootstrapped

        if not is_bootstrapped():
            return []
        repos = get_repos()
        surviving = {getattr(r, "name", None) for r in get_ordered_repos()}
    except Exception as exc:  # noqa: BLE001 — see docstring: no config means no entries
        logger.debug("declared %s: config unreachable (%s) — no entries", seam_attr, exc)
        return []
    from .config.scope import repo_targets  # function-scope: same import-light seam

    out: list[DeclaredEntry] = []
    for repo in repos or ():
        entries = getattr(repo, seam_attr, None)
        if not entries:
            continue
        if getattr(repo, "name", None) not in surviving:
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

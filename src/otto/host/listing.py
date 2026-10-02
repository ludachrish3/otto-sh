"""What the repos declare and what each host gets: the data behind ``otto --list-products``.

Two views, both pure reads that never contact a host (no exec, no
``login_home``, no probe):

* **declared** — :func:`declared_rows` and :func:`provider_notes` describe the
  ``[[products]]`` / ``[[dev_tools]]`` entries the bootstrapped repos hold and the
  code providers they registered. Nothing about a host is known yet, so what a
  provider supplies cannot be listed.
* **lab** — :func:`lab_rows` describes what ingest attached to each host of a
  resolved lab, plus what was left out and why.

The lab view reads what the ingest chokepoint
(:func:`otto.host.factory.apply_providers`) recorded — each instance's ``kind``,
``origin`` and ``source_entry``, each host's ``shadowed_*`` list — rather than
re-deriving it. The one thing not recorded is why a declared entry built nowhere,
so :func:`lab_rows` asks the ingest's own questions again per host
(:func:`otto.declared.declared_for_host` for the scope gate,
:func:`otto.declared.host_matches` for the selector) and compares the answers
with the recorded ``source_entry`` stamps. No selection loop is re-implemented.

Cells are plain strings (and a list of host ids); the CLI owns presentation.
``seam`` is ``"products"`` or ``"dev_tools"`` throughout.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..declared import DeclaredEntry, MatchValue, declared_for_host, host_matches, surviving_repos
from .dev_tool import registered_dev_tool_providers
from .product import LOGIN_HOME, registered_product_providers

_SEAMS = {
    "products": {
        "entries": "declared_products",
        "attached": "products",
        "shadowed": "shadowed_products",
        "noun": "product",
    },
    "dev_tools": {
        "entries": "declared_dev_tools",
        "attached": "dev_tools",
        "shadowed": "shadowed_dev_tools",
        "noun": "dev tool",
    },
}

ANY_HOST = "any host"
"""The ``match`` cell of an entry whose selector is empty."""

LOGIN_HOME_CELL = "login home"
"""The ``stage dir`` cell when the directory is the login user's home, which only a
connected host can name."""


@dataclass
class DeclaredRow:
    """One declared entry, as the no-lab table shows it."""

    name: str
    kind: str
    repo: str
    match: str
    """The host selector rendered compactly; :data:`ANY_HOST` when empty."""

    artifact: str
    """The entry's artifact path (or image) as declared; empty when the kind has none."""


@dataclass
class LabRow:
    """One distinct product (or tool) on the lab's hosts, as the lab table shows it."""

    name: str
    kind: str
    """The declared kind, or ``code (<ClassName>)`` for a provider-built instance."""

    repo: str
    hosts: list[str]
    """Ids of the hosts carrying it, in lab order."""

    artifact: str
    stage_dir: str
    """Declared dir, else the host's ``default_dest_dir``, else :data:`LOGIN_HOME_CELL`;
    empty for a kind that stages nothing."""


@dataclass
class UnusedEntry:
    """A declared entry or provider product that landed on no host, and why."""

    name: str
    kind: str
    repo: str
    reason: str


@dataclass
class LabListing:
    """The lab view: the rows, then what was left out."""

    rows: list[LabRow] = field(default_factory=list[LabRow])
    unused: list[UnusedEntry] = field(default_factory=list[UnusedEntry])


def active_repos() -> list[Any]:
    """Return the bootstrapped repos whose declarations apply, in discovery order.

    Delegates to :func:`otto.declared.surviving_repos`, the same set
    :func:`otto.declared.declared_for_host` collects from.
    """
    return surviving_repos()


def _entries(repo: Any, seam: str) -> list[DeclaredEntry]:
    return list(getattr(repo, _SEAMS[seam]["entries"], None) or [])


def _leaf(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _match_cell(match: dict[str, MatchValue]) -> str:
    if not match:
        return ANY_HOST
    parts = []
    for key, value in match.items():
        shown = (
            "[" + ", ".join(_leaf(v) for v in value) + "]"
            if isinstance(value, list)
            else _leaf(value)
        )
        parts.append(f"{key}={shown}")
    return ", ".join(parts)


def _declared_artifact(entry: DeclaredEntry) -> str:
    for key in ("artifact", "image"):
        value = entry.params.get(key)
        if isinstance(value, str):
            return value
    return ""


def declared_rows(repos: list[Any], seam: str) -> list[DeclaredRow]:
    """One row per *seam* entry across *repos*: repo order, then declaration order."""
    return [
        DeclaredRow(
            name=entry.name,
            kind=entry.kind,
            repo=repo.name,
            match=_match_cell(entry.match),
            artifact=_declared_artifact(entry),
        )
        for repo in repos
        for entry in _entries(repo, seam)
    ]


def provider_notes(repos: list[Any], seam: str) -> list[str]:
    """One line per provider that a repo in *repos* registered for *seam*.

    A provider's output depends on the host it is asked about, so the declared
    view cannot list it; the line says so and points at ``--lab``.
    """
    noun = _SEAMS[seam]["noun"]
    providers = (
        registered_product_providers() if seam == "products" else registered_dev_tool_providers()
    )
    notes = []
    for repo in repos:
        for provider, owner in providers:
            if owner != repo.name:
                continue
            module = getattr(provider, "__module__", "<unknown>")
            qualname = getattr(provider, "__qualname__", repr(provider))
            where = f"{module}.{qualname}"
            notes.append(
                f"{repo.name} also registers a {noun} provider ({where}); "
                "what it supplies depends on the host, so it is listed only with --lab."
            )
    return notes


def _kind_cell(item: Any) -> str:
    if getattr(item, "origin", "provider") == "declared":
        return str(getattr(item, "kind", "code"))
    return f"code ({type(item).__name__})"


def _artifact_cell(item: Any, repo_roots: dict[str, Path]) -> str:
    image = getattr(item, "image", None)
    if isinstance(image, str) and image:
        return image
    artifact = getattr(item, "artifact", None)
    if artifact is None or str(artifact) in ("", "."):
        return ""
    path = Path(artifact)
    root = repo_roots.get(getattr(item, "owner", None) or "")
    if root is not None and path.is_absolute():
        try:
            return path.relative_to(root).as_posix()
        except ValueError:
            pass
    return str(path)


def _stage_cell(item: Any, host: Any) -> str:
    if not getattr(item, "stages_artifact", False) or not hasattr(item, "stage_key"):
        return ""
    key = item.stage_key(host)
    return LOGIN_HOME_CELL if key == LOGIN_HOME else key


def _unused_declared(repos: list[Any], seam: str, hosts: list[Any]) -> list[UnusedEntry]:
    """List the declared entries that built on no host, each with the furthest it got.

    Per host: the entries :func:`~otto.declared.declared_for_host` admits passed
    the scope gate, those of them :func:`~otto.declared.host_matches` accepts
    matched, and the ones that built are read off the recorded ``source_entry``
    stamps. Entries are tracked by identity (``DeclaredEntry`` holds dicts, so it
    does not hash).
    """
    meta = _SEAMS[seam]
    scoped: set[int] = set()
    matched: set[int] = set()
    built: set[int] = set()
    for host in hosts:
        for entry in declared_for_host(host, meta["entries"]):
            scoped.add(id(entry))
            if host_matches(entry.match, host):
                matched.add(id(entry))
        built.update(
            id(source)
            for item in getattr(host, meta["attached"], [])
            if (source := getattr(item, "source_entry", None)) is not None
        )
    out = []
    for repo in repos:
        for entry in _entries(repo, seam):
            key = id(entry)
            if key in built:
                continue
            if key not in scoped and hosts and getattr(repo, "project_scope", None) is not None:
                reason = f"outside {repo.name}'s [project] scope"
            elif key not in matched:
                reason = "no host matches"
            else:
                reason = f"shadowed by an earlier entry named {entry.name!r}"
            out.append(UnusedEntry(entry.name, entry.kind, repo.name, reason))
    return out


def _unused_providers(seam: str, hosts: list[Any]) -> list[UnusedEntry]:
    """List provider instances dropped for a taken name that no host ended up carrying."""
    meta = _SEAMS[seam]
    carried = {
        (getattr(item, "owner", None), item.name)
        for host in hosts
        for item in getattr(host, meta["attached"], [])
        if getattr(item, "origin", "provider") == "provider"
    }
    out: list[UnusedEntry] = []
    seen: set[tuple[str, str]] = set()
    for host in hosts:
        for _host_id, item in getattr(host, meta["shadowed"], []):
            if (getattr(item, "owner", None), item.name) in carried:
                continue
            holder = next(
                (i for i in getattr(host, meta["attached"], []) if i.name == item.name), None
            )
            if holder is not None and getattr(holder, "origin", "provider") == "declared":
                reason = f"shadowed by the declared entry named {item.name!r}"
            else:
                reason = f"shadowed by an earlier entry named {item.name!r}"
            # One line per provider product, however many hosts dropped it: the
            # first reason met (lab order) stands.
            key = (getattr(item, "owner", None) or "", item.name)
            if key not in seen:
                seen.add(key)
                out.append(UnusedEntry(item.name, _kind_cell(item), key[0], reason))
    return out


def lab_rows(lab: Any, repos: list[Any], seam: str) -> LabListing:
    """Describe what ingest attached to *lab*'s hosts for *seam*, and what it left out.

    One row per distinct (repo, name, kind, artifact, stage dir), rows in order
    of first appearance walking the lab's hosts, each carrying the ids of the
    hosts that have it. ``unused`` lists declared entries that built on no host
    (with the reason), then provider instances dropped for a taken name that no
    host carried. Reads recorded state only: nothing here asks a host anything.
    """
    hosts = list(lab.hosts.values())
    roots = {r.name: Path(r.sut_dir) for r in repos if getattr(r, "sut_dir", None) is not None}
    by_key: dict[tuple[str, str, str, str, str], LabRow] = {}
    for host in hosts:
        for item in getattr(host, _SEAMS[seam]["attached"], []):
            repo = getattr(item, "owner", None) or ""
            kind = _kind_cell(item)
            artifact = _artifact_cell(item, roots)
            stage = _stage_cell(item, host)
            key = (repo, item.name, kind, artifact, stage)
            row = by_key.get(key)
            if row is None:
                by_key[key] = LabRow(item.name, kind, repo, [host.id], artifact, stage)
            elif host.id not in row.hosts:
                row.hosts.append(host.id)
    return LabListing(
        rows=list(by_key.values()),
        unused=_unused_declared(repos, seam, hosts) + _unused_providers(seam, hosts),
    )

"""Single source of truth for test lab-data paths and host builders.

Centralizes the lab JSON location so test modules never hand-roll
``Path(__file__).parents[N] / "lab_data" / ...`` arithmetic (which breaks
whenever a file moves to a different depth). Import from here (or via the
re-exports in :mod:`tests.conftest`).
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from otto.host.login_proxy import Cred
from otto.host.unix_host import UnixHost
from otto.labs.sources import CompiledLabSource
from otto.models.lab import HOISTED_HOST_KEYS

_LAB_DATA_DIR = Path(__file__).resolve().parent / "lab_data"


def lab_data_dir() -> Path:
    """Directory holding the per-tech lab-data trees (``tech1``/``tech2``)."""
    return _LAB_DATA_DIR


def lab_data_path(tech: str = "tech1") -> Path:
    """Path to a tech's ``lab.json`` (default the primary ``tech1`` lab)."""
    return _LAB_DATA_DIR / tech / "lab.json"


def host_data(ne: str, tech: str = "tech1") -> dict[str, Any]:
    """Return the flat host dict for element ``ne`` (see :func:`flat_hosts`).

    Factory-ready: ``element``/``element_id`` are stamped from the element, and
    there is no ``labs``/``resources`` (``HostSpec`` forbids both since Task 2).
    Callers that need membership want :func:`flat_hosts` with ``with_labs=True``.
    """
    for host in flat_hosts(tech):
        if host["element"] == ne:
            return host
    raise KeyError(f"NE {ne!r} not found in {lab_data_path(tech)}")


def lab_json_v2(
    hosts: Iterable[dict[str, Any]],
    links: Iterable[dict[str, Any]] = (),
    *,
    declare_labs: bool = True,
) -> dict[str, Any]:
    """Wrap flat v1-style host dicts into a v2 lab.json document.

    Hoists ``labs`` onto the element and ``resources`` into the ``labs`` table
    (union per lab), grouping hosts that share ``(element, element_id)``. A host
    with no ``labs`` gets an inert ``__unreachable__`` pattern (v2 forbids empty
    membership) so it still parses but joins nothing.

    With ``declare_labs=False`` the document carries NO ``labs`` table (elements
    and links only) — what a MEMBER file of a multi-file source looks like, since
    within one source a lab declared in two files is an error (spec §2.4).

    Raises ``ValueError`` when two hosts of one ``(element, element_id)`` group
    carry different ``labs``: v2 assigns membership per ELEMENT (spec §7), so
    such a v1 fixture has no v2 spelling. Taking the first host's list would
    silently drop the others from their labs, and every downstream "host X is
    not in lab Y" assertion would then pass for the wrong reason.
    """
    labs: dict[str, dict[str, Any]] = {}
    elements: dict[tuple[str, Any], dict[str, Any]] = {}
    for h in hosts:
        key = (h["element"], h.get("element_id"))
        host_labs = list(h.get("labs", []))
        el = elements.setdefault(key, {"name": h["element"], "labs": host_labs, "hosts": []})
        if set(el["labs"]) != set(host_labs):
            raise ValueError(
                f"lab_json_v2: hosts of element {h['element']!r} (element_id "
                f"{h.get('element_id')!r}) disagree on 'labs': {el['labs']} vs {host_labs}. "
                f"v2 assigns membership per ELEMENT (spec §7) — give them distinct "
                f"'element'/'element_id' values, or the same labs."
            )
        if h.get("element_id") is not None:
            el["id"] = h["element_id"]
        # HOISTED_HOST_KEYS is the product's own list of what a v2 host entry
        # may not carry, so a writer built on it cannot drift from the validator.
        el["hosts"].append({k: v for k, v in h.items() if k not in HOISTED_HOST_KEYS})
        for lab in h.get("labs", []):
            entry = labs.setdefault(lab, {"resources": []})
            entry["resources"].extend(
                r for r in h.get("resources", []) if r not in entry["resources"]
            )
    for el in elements.values():
        if not el["labs"]:
            el["labs"] = ["__unreachable__"]
    doc: dict[str, Any] = {"elements": list(elements.values()), "links": list(links)}
    if declare_labs:
        return {"labs": labs, **doc}
    return doc


def write_lab_json(
    path: Path,
    hosts: Iterable[dict[str, Any]],
    links: Iterable[dict[str, Any]] = (),
    *,
    declare_labs: bool = True,
) -> Path:
    """Write ``lab_json_v2(hosts, links)`` to *path* (a ``lab.json``); return it.

    ``declare_labs`` is forwarded: ``False`` writes a member-only document (no
    ``labs`` table), which is what the second and later files of ONE source
    must be — see :func:`lab_json_v2` for both keywords' rules.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(lab_json_v2(hosts, links, declare_labs=declare_labs)))
    return path


def flatten_lab_doc(doc: dict[str, Any], *, with_labs: bool = False) -> list[dict[str, Any]]:
    """Every host of an already-parsed v2 document as a flat v1-style dict.

    The reader half of :func:`lab_json_v2`, for the sites that hold a document
    rather than a tech name — a file the test itself wrote, or one found by
    walking a tree. :func:`flat_hosts` is this over a fixture's ``lab.json``;
    see it for what the flat dict does and does not carry.
    """
    declared = sorted(doc.get("labs", {}))
    out: list[dict[str, Any]] = []
    for el in doc.get("elements", []):
        labs = [n for n in declared if any(re.fullmatch(p, n) for p in el["labs"])]
        for h in el["hosts"]:
            flat = {**h, "element": el["name"]}
            if el.get("id") is not None:
                flat["element_id"] = el["id"]
            if with_labs:
                flat["labs"] = labs
            out.append(flat)
    return out


def flat_hosts(tech: str = "tech1", *, with_labs: bool = False) -> list[dict[str, Any]]:
    """Every host of a tech's v2 lab.json as a flat v1-style dict.

    ``element``/``element_id`` come from the element, so the result is what
    :func:`otto.host.factory.create_host_from_dict` takes. No ``labs`` and no
    ``resources``: ``HostSpec`` forbids both since Task 2 — pass
    ``with_labs=True`` for the membership readers, which adds the list of
    DECLARED lab names the element's patterns match, so membership reads as it
    did on v1 entries.
    """
    return flatten_lab_doc(json.loads(lab_data_path(tech).read_text()), with_labs=with_labs)


def make_host(ne: str, **kwargs: Any) -> UnixHost:
    """Build a UnixHost from lab data with optional field overrides."""
    data = host_data(ne)
    return UnixHost(
        ip=data["ip"],
        element=data["element"],
        creds=[Cred(**c) for c in data["creds"]],
        board=data.get("board"),
        is_virtual=data.get("is_virtual", False),
        **kwargs,
    )


def json_lab_sources(
    sut_dir: Path, paths: list[Path], *, name: str = "fake"
) -> list[CompiledLabSource]:
    """The compiled ``[[lab.sources]]`` list a Repo STAND-IN must carry.

    Repo stand-ins (``SimpleNamespace``/``MagicMock``) that only set the old
    ``labs`` list model a ``Repo`` that no longer exists: ``build_lab_sources``
    and the completion cache read :attr:`~otto.config.repo.Repo.lab_sources`,
    so such a double contributes no host source at all and silently completes
    nothing. One home for the shape, so the doubles cannot drift from what
    :func:`~otto.labs.sources.compile_lab_sources` actually produces for a repo
    whose only host source is json files under *paths*.
    """
    return [
        CompiledLabSource(
            label=f"{name}/json#1", backend="json", repo_dir=sut_dir, paths=list(paths)
        )
    ]

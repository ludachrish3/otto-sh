"""Built-in ``"json"`` ``LabRepository`` backend: loads labs from ``lab.json`` files."""

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..host.factory import (
    create_host_from_dict,
    validate_host_dict,
)
from .errors import (
    LabNotFoundError,
    LabRepositoryError,
)

if TYPE_CHECKING:
    # Deferred: otto.config.lab imports this module (for the built-in "json"
    # backend), so a module-level import here would cycle when otto.labs is
    # the first thing imported. load_lab() below imports Lab locally at call
    # time instead, by which point both modules are fully initialized.
    from ..config.lab import Lab

logger = logging.getLogger(__name__)

LAB_FILENAME = "lab.json"

# Known top-level sections of a lab.json object. ``hosts`` is the array of host
# entries (schema unchanged from the old bare-array file); ``links`` is the
# array of declared data-plane routes. Adding a future section (e.g.
# ``elements``) is a one-line change here plus handling in ``_load_lab_file``.
_LAB_SECTIONS = frozenset({"hosts", "links"})


def parse_lab_sections(data: object, source: str) -> dict[str, list[Any]]:
    """Validate a parsed ``lab.json`` object's section shape; return its sections.

    The single source of truth for the ``lab.json`` object contract — shared by
    the runtime loader (``JsonFileLabRepository._load_lab_file``) and the
    ``otto init`` doctor (``otto.cli.init._validate_lab``) so the doctor
    cannot drift from what otto actually accepts (there is no second validator
    to drift). *data* is the already-parsed JSON value; *source* names its
    origin (a file path) for error messages.

    Top-level ``_``-prefixed keys and the ``$schema`` key (standard
    editor-wiring idiom for VS Code / jsonls) are treated as comment space;
    unknown sections fail loud. Returns a dict carrying every known section as
    a (possibly empty) list.

    Raises
    ------
    LabRepositoryError
        If *data* is not a JSON object, carries an unknown top-level section,
        or a section's value is not a JSON array.
    """
    if not isinstance(data, dict):
        raise LabRepositoryError(
            f"Lab file '{source}' must contain a JSON object with "
            f"'hosts'/'links' sections, got {type(data).__name__}"
        )
    # `$schema` is the standard editor-wiring key (VS Code / jsonls) — treated
    # as comment space alongside `_`-prefixed keys.
    unknown = {
        k for k in data if not (isinstance(k, str) and (k.startswith("_") or k == "$schema"))
    } - _LAB_SECTIONS
    if unknown:
        raise LabRepositoryError(
            f"Lab file '{source}' has unknown section(s) {sorted(unknown)}; "
            f"known sections: {sorted(_LAB_SECTIONS)}"
        )
    out: dict[str, list[Any]] = {}
    for section in _LAB_SECTIONS:
        value = data.get(section, [])
        if not isinstance(value, list):
            raise LabRepositoryError(
                f"Lab file '{source}': section '{section}' must be a JSON array, "
                f"got {type(value).__name__}"
            )
        out[section] = value
    return out


class JsonFileLabRepository:
    """Load labs from ``lab.json`` files under a fixed set of search paths.

    Each ``lab.json`` is a JSON object with array sections —
    ``{"hosts": [...], "links": [...]}``. The search paths are supplied once at
    construction — this is the built-in ``"json"`` backend, and
    :func:`otto.labs.build_lab_repository` feeds it the aggregated ``labs``
    directories. The ``hosts`` section holds all known hosts; a host's ``labs``
    field lists the labs it belongs to, mirroring a row-with-membership database
    design. Top-level ``_``-prefixed keys are comment space; unknown sections
    fail loud.
    """

    def __init__(self, search_paths: list[Path] | None = None) -> None:
        self.search_paths: list[Path] = list(search_paths or [])

    def load_lab(
        self,
        name: str,
        preferences: dict[str, dict[str, Any]] | None = None,
    ) -> "Lab":
        """Load a lab by filtering hosts from the configured lab.json files.

        Raises
        ------
        LabNotFoundError
            If no lab.json exists in any search path, or no host belongs to
            the requested lab.
        LabRepositoryError
            If a lab.json is malformed or a host's data is invalid.
        """
        from ..config.lab import Lab

        try:
            lab_files = self._find_lab_files()
        except FileNotFoundError as e:
            raise LabNotFoundError(str(e)) from None

        all_hosts_data: list[dict[str, Any]] = []
        all_links_data: list[dict[str, Any]] = []
        for lab_file in lab_files:
            sections = self._load_lab_file(lab_file)
            all_hosts_data.extend(sections["hosts"])
            all_links_data.extend(sections["links"])
        # ``all_links_data`` spans ALL lab files (like ``all_hosts_data``) so
        # declared links can resolve dangling (cross-lab) endpoints below.

        matching = [h for h in all_hosts_data if name in h.get("labs", [])]

        if not matching:
            searched = "\n  ".join(str(p) for p in self.search_paths)
            raise LabNotFoundError(
                f"Lab '{name}' not found in any search path:\n  {searched}"
            ) from None

        for idx, host_data in enumerate(matching):
            try:
                validate_host_dict(host_data)
            except ValueError as e:  # noqa: PERF203 — per-item resilience
                raise LabRepositoryError(
                    f"Invalid host data at index {idx} in lab '{name}': {e}"
                ) from e

        lab = Lab(name=name)

        for idx, host_data in enumerate(matching):
            try:
                host = create_host_from_dict(host_data, preferences=preferences)
                lab.add_host(host)
                lab.resources.update(host.resources)
            except Exception as e:  # noqa: PERF203 — per-item resilience
                logger.exception(f"Failed to create host at index {idx} in lab '{name}'")
                raise LabRepositoryError(
                    f"Failed to create host at index {idx} in lab '{name}': {e}"
                ) from e

        from ..link.derive import addressing_from_dict, resolve_declared_links

        # Guard: all_hosts_data spans ALL lab files, including entries never
        # validated (they belong to other labs) — skip shapes that can't
        # produce an id rather than crash link resolution on someone else's typo.
        # The requested lab's own hosts were already validated above, so any
        # exception here belongs to an unrelated lab's malformed record.
        addressing: dict[str, Any] = {}
        for h in all_hosts_data:
            if not (isinstance(h, dict) and isinstance(h.get("element"), str)):
                continue
            try:
                host_id, host_addressing = addressing_from_dict(h)
            except Exception:  # noqa: BLE001 — per-item resilience, see guard above
                logger.debug(f"Skipping malformed cross-lab host record: {h!r}")
                continue
            if host_id in addressing and addressing[host_id] != host_addressing:
                logger.warning(
                    "Duplicate host id %r across lab files with differing addressing; "
                    "keeping the first. Differentiate the element, element_id, or board/slot.",
                    host_id,
                )
                continue
            addressing[host_id] = host_addressing
        loaded_ids = set(lab.hosts)
        # ``all_links_data`` spans ALL lab files (like ``all_hosts_data``), so a
        # typo'd link between two hosts of an UNRELATED lab must not break this
        # lab's load: ``resolve_declared_links`` skips entries touching no loaded
        # host, symmetric with the cross-lab host-record containment above. Links
        # touching this lab still fail loud with their original file index.
        try:
            declared = resolve_declared_links(
                all_links_data, addressing, source=LAB_FILENAME, loaded_ids=loaded_ids
            )
        except ValueError as e:
            raise LabRepositoryError(str(e)) from e
        # Membership: only links with >= 1 endpoint in this lab (guaranteed by
        # the skip above, restated here so the invariant is visible at the call site).
        lab.links = [
            link for link in declared if link.a.host in loaded_ids or link.b.host in loaded_ids
        ]

        logger.debug(f"Loaded lab '{name}' with {len(lab.hosts)} hosts")
        return lab

    def list_labs(self) -> list[str]:
        """List all lab names referenced by hosts across the configured paths.

        Returns an empty list when no lab.json exists. A malformed lab.json is
        skipped rather than raised, so listing stays best-effort.
        """
        lab_names: set[str] = set()

        try:
            lab_files = self._find_lab_files()
        except FileNotFoundError:
            return []

        for lab_file in lab_files:
            try:
                hosts_data = self._load_lab_file(lab_file)["hosts"]
            except LabRepositoryError:
                continue
            for host in hosts_data:
                for lab in host.get("labs", []):
                    lab_names.add(lab)

        return sorted(lab_names)

    def _find_lab_files(self) -> list[Path]:
        """Find all lab.json files across the configured search paths.

        Raises
        ------
        FileNotFoundError
            Internal signal (translated to LabNotFoundError by ``load_lab`` and
            swallowed by ``list_labs``) when no lab.json is found.
        """
        found: list[Path] = []
        for search_path in self.search_paths:
            candidate = search_path / LAB_FILENAME
            if candidate.exists() and candidate.is_file():
                found.append(candidate)

        if not found:
            searched = "\n  ".join(str(p) for p in self.search_paths)
            raise FileNotFoundError(
                f"No {LAB_FILENAME} found in any search path:\n  {searched}"
            ) from None

        return found

    def _load_lab_file(self, lab_file: Path) -> dict[str, list[Any]]:
        """Load one ``lab.json``: an object with ``hosts`` / ``links`` array sections.

        Reads the file, then delegates the section-shape contract (object guard,
        ``_``-comment allowance — also tolerating a top-level ``$schema`` key,
        the editor-wiring idiom — unknown-section rejection, per-section array
        check) to :func:`parse_lab_sections` — the same helper the ``otto init``
        doctor uses, so the two can never diverge. Adding a future section (e.g.
        ``elements``) means extending ``_LAB_SECTIONS``.

        Raises
        ------
        LabRepositoryError
            If the file contains malformed JSON, its top-level value is not a
            JSON object, it carries an unknown section, or a section's value is
            not a JSON array.
        """
        try:
            with lab_file.open() as f:
                data = json.load(f)
        except json.JSONDecodeError as e:
            raise LabRepositoryError(f"Lab file '{lab_file}' contains malformed JSON: {e}") from e
        return parse_lab_sections(data, str(lab_file))

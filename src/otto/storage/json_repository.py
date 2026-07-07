"""Built-in ``"json"`` ``LabRepository`` backend: loads labs from ``lab.json`` files."""

import json
from pathlib import Path
from typing import Any

from ..configmodule.lab import Lab
from ..logger import get_logger
from .errors import (
    LabNotFoundError,
    LabRepositoryError,
)
from .factory import (
    create_host_from_dict,
    validate_host_dict,
)

logger = get_logger()

LAB_FILENAME = "lab.json"

# Known top-level sections of a lab.json object. ``hosts`` is the array of host
# entries (schema unchanged from the old bare-array file); ``links`` is the
# array of declared data-plane routes. Adding a future section (e.g.
# ``elements``) is a one-line change here plus handling in ``_load_lab_file``.
_LAB_SECTIONS = frozenset({"hosts", "links"})


class JsonFileLabRepository:
    """Load labs from ``lab.json`` files under a fixed set of search paths.

    Each ``lab.json`` is a JSON object with array sections —
    ``{"hosts": [...], "links": [...]}``. The search paths are supplied once at
    construction — this is the built-in ``"json"`` backend, and
    :func:`otto.storage.build_lab_repository` feeds it the aggregated ``labs``
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
    ) -> Lab:
        """Load a lab by filtering hosts from the configured lab.json files.

        Raises
        ------
        LabNotFoundError
            If no lab.json exists in any search path, or no host belongs to
            the requested lab.
        LabRepositoryError
            If a lab.json is malformed or a host's data is invalid.
        """
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
        # ``all_links_data`` is aggregated here so this loader owns reading both
        # sections; Task 5 consumes the ``links`` section (declared-link
        # derivation). It is intentionally not used yet.

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

    def _load_lab_file(self, lab_file: Path) -> dict[str, list[dict[str, Any]]]:
        """Load one ``lab.json``: an object with ``hosts`` / ``links`` array sections.

        Top-level ``_``-prefixed keys are comment space (same idiom as host
        entries). Unknown sections fail loud; adding a future section (e.g.
        ``elements``) means extending ``_LAB_SECTIONS`` and handling it here.

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

        if not isinstance(data, dict):
            raise LabRepositoryError(
                f"Lab file '{lab_file}' must contain a JSON object with "
                f"'hosts'/'links' sections, got {type(data).__name__}"
            )
        unknown = {k for k in data if not k.startswith("_")} - _LAB_SECTIONS
        if unknown:
            raise LabRepositoryError(
                f"Lab file '{lab_file}' has unknown section(s) {sorted(unknown)}; "
                f"known sections: {sorted(_LAB_SECTIONS)}"
            )
        out: dict[str, list[dict[str, Any]]] = {}
        for section in _LAB_SECTIONS:
            value = data.get(section, [])
            if not isinstance(value, list):
                raise LabRepositoryError(
                    f"Lab file '{lab_file}': section '{section}' must be a JSON array, "
                    f"got {type(value).__name__}"
                )
            out[section] = value
        return out

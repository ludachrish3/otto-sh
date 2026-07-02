"""Built-in ``"json"`` ``LabRepository`` backend: loads labs from ``hosts.json`` files."""

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

HOSTS_FILENAME = "hosts.json"


class JsonFileLabRepository:
    """Load labs from ``hosts.json`` files under a fixed set of search paths.

    The search paths are supplied once at construction — this is the built-in
    ``"json"`` backend, and :func:`otto.storage.build_lab_repository` feeds it
    the aggregated ``labs`` directories. Each ``hosts.json`` holds all known
    hosts; a host's ``labs`` field lists the labs it belongs to, mirroring a
    row-with-membership database design.
    """

    def __init__(self, search_paths: list[Path] | None = None) -> None:
        self.search_paths: list[Path] = list(search_paths or [])

    def load_lab(
        self,
        name: str,
        preferences: dict[str, dict[str, Any]] | None = None,
    ) -> Lab:
        """Load a lab by filtering hosts from the configured hosts.json files.

        Raises
        ------
        LabNotFoundError
            If no hosts.json exists in any search path, or no host belongs to
            the requested lab.
        LabRepositoryError
            If a hosts.json is malformed or a host's data is invalid.
        """
        try:
            hosts_files = self._find_hosts_files()
        except FileNotFoundError as e:
            raise LabNotFoundError(str(e)) from None

        all_hosts_data: list[dict[str, Any]] = []
        for hosts_file in hosts_files:
            all_hosts_data.extend(self._load_json_hosts(hosts_file))

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

        Returns an empty list when no hosts.json exists. A malformed hosts.json
        is skipped rather than raised, so listing stays best-effort.
        """
        lab_names: set[str] = set()

        try:
            hosts_files = self._find_hosts_files()
        except FileNotFoundError:
            return []

        for hosts_file in hosts_files:
            try:
                hosts_data = self._load_json_hosts(hosts_file)
            except LabRepositoryError:
                continue
            for host in hosts_data:
                for lab in host.get("labs", []):
                    lab_names.add(lab)

        return sorted(lab_names)

    def _find_hosts_files(self) -> list[Path]:
        """Find all hosts.json files across the configured search paths.

        Raises
        ------
        FileNotFoundError
            Internal signal (translated to LabNotFoundError by ``load_lab`` and
            swallowed by ``list_labs``) when no hosts.json is found.
        """
        found: list[Path] = []
        for search_path in self.search_paths:
            candidate = search_path / HOSTS_FILENAME
            if candidate.exists() and candidate.is_file():
                found.append(candidate)

        if not found:
            searched = "\n  ".join(str(p) for p in self.search_paths)
            raise FileNotFoundError(
                f"No {HOSTS_FILENAME} found in any search path:\n  {searched}"
            ) from None

        return found

    def _load_json_hosts(self, hosts_file: Path) -> list[dict[str, Any]]:
        """Load and parse a hosts.json file, returning the list of host dicts.

        Raises
        ------
        LabRepositoryError
            If the file contains malformed JSON or its top-level value is not a
            JSON array.
        """
        try:
            with hosts_file.open() as f:
                data = json.load(f)
        except json.JSONDecodeError as e:
            raise LabRepositoryError(
                f"Hosts file '{hosts_file}' contains malformed JSON: {e}"
            ) from e

        if not isinstance(data, list):
            raise LabRepositoryError(
                f"Hosts file '{hosts_file}' must contain a JSON array, got {type(data).__name__}"
            )

        return data

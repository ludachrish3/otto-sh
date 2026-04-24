import json
from pathlib import Path
from typing import Any

from ..configmodule.lab import Lab
from ..logger import getOttoLogger
from .factory import (
    create_host_from_dict,
    validate_host_dict,
)

logger = getOttoLogger()

HOSTS_FILENAME = "hosts.json"


class JsonFileLabRepository:
    """Repository implementation for loading labs from a hosts.json file.

    Each search-path directory may contain a hosts.json file holding all
    known hosts. Each host carries a 'labs' field listing the lab names it
    belongs to, mirroring a database row-with-membership design.
    """

    def supports_location(self,
        path: Path,
    ) -> bool:
        """Check if path is a directory that could contain a hosts.json file."""
        return path.is_dir()

    def load_lab(self,
        name: str,
        search_paths: list[Path],
    ) -> Lab:
        """
        Load a lab by filtering hosts from hosts.json files.

        Searches all search paths for hosts.json files, merges all hosts,
        then returns only those whose 'labs' field contains the requested name.

        Parameters
        ----------
        name : str
            Name of the lab to load
        search_paths : list[Path]
            Directories to search for hosts.json files

        Returns
        -------
        Lab
            Constructed Lab object with all matching hosts added

        Raises
        ------
        FileNotFoundError
            If no hosts.json found in any search path, or no hosts belong
            to the requested lab
        ValueError
            If a hosts.json file doesn't contain a JSON array or host data
            is invalid
        json.JSONDecodeError
            If a hosts.json file contains malformed JSON
        """

        hosts_files = self._find_hosts_files(search_paths)

        all_hosts_data: list[dict[str, Any]] = []
        for hosts_file in hosts_files:
            all_hosts_data.extend(self._load_json_hosts(hosts_file))

        matching = [h for h in all_hosts_data if name in h.get("labs", [])]

        if not matching:
            searched = '\n  '.join(str(p) for p in search_paths)
            raise FileNotFoundError(
                f"Lab '{name}' not found in any search path:\n  {searched}"
            ) from None

        for idx, host_data in enumerate(matching):
            try:
                validate_host_dict(host_data)
            except ValueError as e:
                raise ValueError(
                    f"Invalid host data at index {idx} in lab '{name}': {e}"
                ) from e

        lab = Lab(name=name)

        for idx, host_data in enumerate(matching):
            try:
                host = create_host_from_dict(host_data)
                lab.addHost(host)
                lab.resources.update(host.resources)
            except Exception as e:
                logger.error(
                    f"Failed to create host at index {idx} in lab '{name}': {e}"
                )
                raise ValueError(
                    f"Failed to create host at index {idx} in lab '{name}': {e}"
                ) from e

        logger.debug(f"Loaded lab '{name}' with {len(lab.hosts)} hosts")
        return lab

    def list_labs(self, search_paths: list[Path]) -> list[str]:
        """
        List all lab names referenced by hosts across all hosts.json files.

        Parameters
        ----------
        search_paths : list[Path]
            Directories to search for hosts.json files

        Returns
        -------
        list[str]
            Sorted list of unique lab names found
        """
        lab_names: set[str] = set()

        try:
            hosts_files = self._find_hosts_files(search_paths)
        except FileNotFoundError:
            return []

        for hosts_file in hosts_files:
            try:
                hosts_data = self._load_json_hosts(hosts_file)
            except (ValueError, json.JSONDecodeError):
                continue
            for host in hosts_data:
                for lab in host.get("labs", []):
                    lab_names.add(lab)

        return sorted(lab_names)

    def _find_hosts_files(self, search_paths: list[Path]) -> list[Path]:
        """
        Find all hosts.json files across the search paths.

        Parameters
        ----------
        search_paths : list[Path]
            Directories to search

        Returns
        -------
        list[Path]
            All found hosts.json paths (one per search path at most)

        Raises
        ------
        FileNotFoundError
            If no hosts.json found in any search path
        """
        found: list[Path] = []
        for search_path in search_paths:
            candidate = search_path / HOSTS_FILENAME
            if candidate.exists() and candidate.is_file():
                found.append(candidate)

        if not found:
            searched = '\n  '.join(str(p) for p in search_paths)
            raise FileNotFoundError(
                f"No {HOSTS_FILENAME} found in any search path:\n  {searched}"
            ) from None

        return found

    def _load_json_hosts(self, hosts_file: Path) -> list[dict[str, Any]]:
        """
        Load and parse a hosts.json file, returning the list of host dicts.

        Parameters
        ----------
        hosts_file : Path
            Path to the hosts.json file

        Returns
        -------
        list[dict]
            List of host dictionaries from the JSON file

        Raises
        ------
        json.JSONDecodeError
            If the file contains malformed JSON
        ValueError
            If the top-level JSON value is not an array
        """

        with hosts_file.open() as f:
            data = json.load(f)

        if not isinstance(data, list):
            raise ValueError(
                f"Hosts file '{hosts_file}' must contain a JSON array, "
                f"got {type(data).__name__}"
            )

        return data

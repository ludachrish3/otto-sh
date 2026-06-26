# Pluggable Host Source Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make otto's host-data-source seam (`LabRepository`) a registered, pluggable backend selected by bare name — exactly symmetric with the reservation backend — with `JsonFileLabRepository` reframed as the built-in `"json"` pass-through, a host-source error contract, a `[lab]` settings block, and a `build_lab_repository` factory; all backward-compatible (a repo with no `[lab]` block behaves exactly as today).

**Architecture:** Configuration moves into the backend at construction time. The generic `LabRepository` Protocol sheds the JSON-specific `search_paths`/`supports_location` and becomes two methods: `load_lab(name, preferences)` and `list_labs()`. A `[lab] backend = "<name>"` setting selects a backend from a name→class registry (`register_lab_repository`, seeded with `json`), constructed once by `build_lab_repository(settings, repo_dir, *, search_paths)`. The CLI callback aggregates `labs` search paths across repos for the default json backend and takes a non-default backend from the first repo that declares one — mirroring reservations' "first repo declares" rule.

**Tech Stack:** Python 3.10+, Pydantic v2 (`OttoModel`/`ConfigDict(extra="allow")`), Typer ≥0.26, pytest, Sphinx (nitpicky `-W`), `ty` type checker.

**Scope note:** This is **Plan A** of the four-phase "pluggable host source + backend conformance" design (`docs/superpowers/specs/2026-06-25-pluggable-host-source-and-conformance-design.md`, §3/§3.1–§3.4 — the host-source feature only). The conformance suite (§4), sample backends (§5), and the host-database guide / onboarding docs (§6) are **Plans C and D** and are out of scope here. The reservation-interface workstream (§3a/§3b/§3c/§3d) was **Plan B** — already implemented and staged. This plan touches **only** the host-source half; it does not modify any `reservations/` code.

## Global Constraints

- **STAGE-ONLY — never `git commit`.** otto-sh's `prepare-commit-msg` hook needs `/dev/tty` and mis-attributes agent commits; Chris commits at the end. Each task's final step is `git add <listed files>` (NOT commit). The controller captures per-task tree snapshots for diff isolation. Wherever a step below says "Commit", it means **stage only**.
- **Backward compatibility is a hard requirement.** A repo with **no `[lab]` block** must load labs exactly as today: the built-in `json` backend over the merged `labs` search paths from all repos. An explicit regression test guards this (Task 7).
- **Do not touch `reservations/` code.** Mirror its patterns (registry, factory, config spec, error contract) but change only `storage/`, `models/settings.py`, `configmodule/`, `cli/main.py`, and `docs/api/storage.rst`.
- **`build_lab_repository` signature deviates intentionally from the spec's indicative 2-arg form.** The spec wrote `build_lab_repository(settings, repo_dir)`; this plan uses `build_lab_repository(settings, repo_dir, *, search_paths=None)` because §3.4's multi-repo rule requires the **aggregated** `labs` paths (which are not in any single repo's `[lab]` settings) to reach the default json backend. `repo_dir` is forwarded to custom backends; `search_paths` feeds the json built-in.
- **Custom-backend construction contract:** `build_lab_repository` constructs a non-json backend as `cls(repo_dir=<repo root>, **kwargs)` where `kwargs` is the `[lab.<name>]` sub-table. A custom backend constructor therefore accepts a `repo_dir` keyword (and whatever `[lab.<name>]` keys it declares). The built-in `json` backend is constructed as `JsonFileLabRepository(search_paths=...)` and ignores `repo_dir`.
- **Error contract:** a missing lab raises `LabNotFoundError`; any other backend failure (I/O, parse, malformed host data) raises `LabRepositoryError`. `LabNotFoundError` subclasses `LabRepositoryError`. Both are re-exported from `otto.storage`.
- **Gate per task:** run the task's own tests. Per-task gate command floor: `make coverage` is NOT required per task; run the named pytest files. **Final gate (Task 8):** `make coverage` (unit coverage ≥85%), `ty` (via `make typecheck`), `make docs` (nitpicky, `-W`, 0 warnings). Live `make nox` / bed-dependent `make coverage` integration+e2e is Chris's call (lab beds are unreachable from the dev VM; those failures are environmental, not regressions).
- DRY, YAGNI, TDD, focused files. Match the surrounding code's style (the reservations subsystem is the reference for every new pattern here).

---

## File Structure

**New files**

- `src/otto/storage/errors.py` — `LabRepositoryError`, `LabNotFoundError`.
- `src/otto/storage/registry.py` — name→class registry + `register_lab_repository`, seeded with `json`.
- `tests/unit/storage/test_registry.py` — registry unit tests.
- `tests/unit/storage/test_build_lab_repository.py` — factory extension-seam tests.

**Modified files**

- `src/otto/storage/protocol.py` — reshape `LabRepository` (drop `search_paths`, drop `supports_location`).
- `src/otto/storage/json_repository.py` — construct-time `search_paths`; raise the new errors.
- `src/otto/storage/__init__.py` — re-export errors + `register_lab_repository`; add `build_lab_repository`.
- `src/otto/models/settings.py` — `LabConfigSpec`; wire `lab` into `SettingsModel`.
- `src/otto/configmodule/repo.py` — `lab_settings` property; `get_lab_panel` uses the factory.
- `src/otto/configmodule/lab.py` — `load_lab` accepts an optional `repository`; drop `_get_individual_lab`.
- `src/otto/cli/main.py` — callback builds the repository via `build_lab_repository` and passes it to `load_lab`.
- `tests/unit/storage/test_json_repository.py` — construct-time signature + new error assertions; drop `supports_location` tests.
- `tests/unit/models/test_settings.py` — `[lab]` acceptance + default.
- `tests/unit/configmodule/` — `load_lab` repository-injection + backward-compat regression.
- `docs/api/storage.rst` — document the errors module, the registry, and the factory/register functions (keeps nitpicky green for the touched code).

---

## Task 1: Host-source error contract

**Files:**
- Create: `src/otto/storage/errors.py`
- Modify: `src/otto/storage/__init__.py`
- Test: `tests/unit/storage/test_errors.py` *(new)*

**Interfaces:**
- Produces: `otto.storage.LabRepositoryError(Exception)`, `otto.storage.LabNotFoundError(LabRepositoryError)`. Mirrors `otto.reservations.ReservationBackendError`.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/storage/test_errors.py`:

```python
"""Unit tests for the host-source (LabRepository) error contract."""

from otto.storage import LabNotFoundError, LabRepositoryError


def test_lab_not_found_is_a_lab_repository_error():
    assert issubclass(LabNotFoundError, LabRepositoryError)


def test_lab_repository_error_is_an_exception():
    assert issubclass(LabRepositoryError, Exception)


def test_errors_are_raisable_with_a_message():
    err = LabNotFoundError("lab 'x' not found")
    assert "not found" in str(err)
    assert isinstance(err, LabRepositoryError)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/storage/test_errors.py -q`
Expected: FAIL with `ImportError: cannot import name 'LabNotFoundError' from 'otto.storage'`.

- [ ] **Step 3: Write the errors module**

Create `src/otto/storage/errors.py`:

```python
"""Error contract for the host-source (``LabRepository``) backend interface.

Mirrors the reservation backend's error contract
(:class:`~otto.reservations.check.ReservationBackendError`): a backend signals
trouble through these types so callers and the conformance suite can rely on a
stable surface instead of backend-specific exceptions.
"""

from __future__ import annotations


class LabRepositoryError(Exception):
    """A host-source backend failed to satisfy a query.

    Raised for I/O, network, parse, or credential failures while loading or
    listing labs — anything other than "the named lab does not exist", which
    raises the more specific :class:`LabNotFoundError`.
    """


class LabNotFoundError(LabRepositoryError):
    """``load_lab`` was asked for a lab name the backend does not know.

    A missing lab must raise this — not return ``None`` or raise a bare
    ``KeyError`` / ``FileNotFoundError`` — so callers can distinguish "unknown
    lab" from "backend is broken".
    """
```

- [ ] **Step 4: Re-export from the package**

In `src/otto/storage/__init__.py`, add these imports (keep the existing factory/json_repository/protocol re-exports; place the error imports first, alphabetically by symbol):

```python
from .errors import (
    LabNotFoundError as LabNotFoundError,
)
from .errors import (
    LabRepositoryError as LabRepositoryError,
)
```

- [ ] **Step 5: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/unit/storage/test_errors.py -q`
Expected: PASS (3 passed).

- [ ] **Step 6: Stage (do NOT commit)**

```bash
git add src/otto/storage/errors.py src/otto/storage/__init__.py tests/unit/storage/test_errors.py
```

---

## Task 2: Reshape `LabRepository` + construct-time `JsonFileLabRepository`

Reshape the Protocol to construct-time config and make the JSON backend take `search_paths` in `__init__`, drop `supports_location`, and raise the Task-1 errors. `supports_location` has **no live caller** outside its own definition and tests (verified at plan time) — delete it.

**Files:**
- Modify: `src/otto/storage/protocol.py` (full rewrite — small)
- Modify: `src/otto/storage/json_repository.py` (full rewrite)
- Test: `tests/unit/storage/test_json_repository.py` (full rewrite)

**Interfaces:**
- Consumes: `LabNotFoundError`, `LabRepositoryError` (Task 1).
- Produces:
  - `LabRepository` Protocol: `load_lab(self, name: str, preferences: dict[str, dict[str, Any]] | None = None) -> Lab`; `list_labs(self) -> list[str]`. (No `search_paths` parameter; no `supports_location`.)
  - `JsonFileLabRepository(search_paths: list[Path] | None = None)`; `load_lab(name, preferences=None) -> Lab`; `list_labs() -> list[str]`. Raises `LabNotFoundError` for a missing lab, `LabRepositoryError` for parse/host-data failures.

- [ ] **Step 1: Rewrite the test file (the failing test)**

Replace the entire contents of `tests/unit/storage/test_json_repository.py` with:

```python
import json
from pathlib import Path

import pytest

from otto.configmodule.lab import Lab
from otto.storage import LabNotFoundError, LabRepositoryError
from otto.storage.json_repository import JsonFileLabRepository


def _hosts_file(path: Path, hosts: list[dict]) -> Path:
    """Write a hosts.json file to the given directory and return its path."""
    f = path / "hosts.json"
    f.write_text(json.dumps(hosts))
    return f


class TestJsonFileLabRepository:
    """Tests for JsonFileLabRepository (construct-time search paths)."""

    def test_load_lab_simple(self, tmp_path):
        _hosts_file(tmp_path, [
            {
                "ip": "10.10.200.11",
                "element": "orange",
                "board": "seed",
                "creds": {"vagrant": "vagrant"},
                "resources": ["orange"],
                "labs": ["testlab"],
            },
        ])

        repo = JsonFileLabRepository([tmp_path])
        lab = repo.load_lab("testlab")

        assert isinstance(lab, Lab)
        assert lab.name == "testlab"
        assert len(lab.hosts) == 1
        assert "orange" in lab.resources

    def test_load_lab_multiple_hosts(self, tmp_path):
        _hosts_file(tmp_path, [
            {
                "ip": "10.10.200.11",
                "element": "orange",
                "board": "seed",
                "creds": {"vagrant": "vagrant"},
                "resources": ["orange"],
                "labs": ["multilab"],
            },
            {
                "ip": "10.10.200.12",
                "element": "tomato",
                "board": "seed",
                "creds": {"vagrant": "vagrant"},
                "resources": ["tomato"],
                "labs": ["multilab"],
            },
        ])

        repo = JsonFileLabRepository([tmp_path])
        lab = repo.load_lab("multilab")

        assert isinstance(lab, Lab)
        assert lab.name == "multilab"
        assert len(lab.hosts) == 2
        assert "orange" in lab.resources
        assert "tomato" in lab.resources

    def test_load_lab_not_found_no_hosts_file(self, tmp_path):
        """A missing hosts.json raises LabNotFoundError, not FileNotFoundError."""
        repo = JsonFileLabRepository([tmp_path])

        with pytest.raises(LabNotFoundError) as exc_info:
            repo.load_lab("nonexistent")

        assert str(tmp_path) in str(exc_info.value)

    def test_load_lab_not_found_lab_absent(self, tmp_path):
        """hosts.json exists but the lab name is not present -> LabNotFoundError."""
        _hosts_file(tmp_path, [
            {
                "ip": "10.10.200.11",
                "element": "orange",
                "creds": {"vagrant": "vagrant"},
                "resources": ["orange"],
                "labs": ["other_lab"],
            },
        ])

        repo = JsonFileLabRepository([tmp_path])

        with pytest.raises(LabNotFoundError) as exc_info:
            repo.load_lab("nonexistent")

        assert "nonexistent" in str(exc_info.value)

    def test_load_lab_only_returns_matching_hosts(self, tmp_path):
        _hosts_file(tmp_path, [
            {
                "ip": "10.10.200.11",
                "element": "orange",
                "creds": {"vagrant": "vagrant"},
                "resources": ["orange"],
                "labs": ["lab_a"],
            },
            {
                "ip": "10.10.200.12",
                "element": "tomato",
                "creds": {"vagrant": "vagrant"},
                "resources": ["tomato"],
                "labs": ["lab_b"],
            },
        ])

        repo = JsonFileLabRepository([tmp_path])
        lab = repo.load_lab("lab_a")

        assert len(lab.hosts) == 1
        assert "orange" in lab.hosts

    def test_load_lab_multiple_search_paths(self, tmp_path):
        path1 = tmp_path / "path1"
        path2 = tmp_path / "path2"
        path1.mkdir()
        path2.mkdir()

        _hosts_file(path2, [
            {
                "ip": "10.10.200.11",
                "element": "orange",
                "creds": {"vagrant": "vagrant"},
                "resources": ["orange"],
                "labs": ["testlab"],
            },
        ])

        repo = JsonFileLabRepository([path1, path2])
        lab = repo.load_lab("testlab")

        assert isinstance(lab, Lab)
        assert lab.name == "testlab"

    def test_load_lab_not_a_list(self, tmp_path):
        """A non-array JSON root raises LabRepositoryError."""
        (tmp_path / "hosts.json").write_text(json.dumps({"hosts": []}))

        repo = JsonFileLabRepository([tmp_path])

        with pytest.raises(LabRepositoryError) as exc_info:
            repo.load_lab("badlab")

        assert "array" in str(exc_info.value)

    def test_load_lab_invalid_json(self, tmp_path):
        """Malformed JSON raises LabRepositoryError."""
        (tmp_path / "hosts.json").write_text("[{invalid json")

        repo = JsonFileLabRepository([tmp_path])

        with pytest.raises(LabRepositoryError):
            repo.load_lab("badlab")

    def test_load_lab_invalid_host_data(self, tmp_path):
        """Invalid host data raises LabRepositoryError with index context."""
        _hosts_file(tmp_path, [
            {
                "element": "orange",
                "creds": {"vagrant": "vagrant"},
                "labs": ["badlab"],
            },
        ])

        repo = JsonFileLabRepository([tmp_path])

        with pytest.raises(LabRepositoryError) as exc_info:
            repo.load_lab("badlab")

        assert "index 0" in str(exc_info.value)
        assert "ip" in str(exc_info.value)

    def test_load_lab_resource_aggregation(self, tmp_path):
        _hosts_file(tmp_path, [
            {
                "ip": "10.10.200.11",
                "element": "orange",
                "creds": {"vagrant": "vagrant"},
                "resources": ["orange", "citrus"],
                "labs": ["resourcelab"],
            },
            {
                "ip": "10.10.200.12",
                "element": "tomato",
                "creds": {"vagrant": "vagrant"},
                "resources": ["tomato", "vegetable"],
                "labs": ["resourcelab"],
            },
        ])

        repo = JsonFileLabRepository([tmp_path])
        lab = repo.load_lab("resourcelab")

        assert "orange" in lab.resources
        assert "citrus" in lab.resources
        assert "tomato" in lab.resources
        assert "vegetable" in lab.resources

    def test_load_lab_host_ids_generated(self, tmp_path):
        _hosts_file(tmp_path, [
            {
                "ip": "10.10.200.11",
                "element": "orange",
                "board": "seed",
                "slot": 0,
                "creds": {"vagrant": "vagrant"},
                "resources": ["orange"],
                "labs": ["idlab"],
            },
        ])

        repo = JsonFileLabRepository([tmp_path])
        lab = repo.load_lab("idlab")

        assert "orange_seed0" in lab.hosts

    def test_list_labs(self, tmp_path):
        _hosts_file(tmp_path, [
            {
                "ip": "10.10.200.11",
                "element": "orange",
                "creds": {"vagrant": "vagrant"},
                "resources": ["orange"],
                "labs": ["alpha"],
            },
            {
                "ip": "10.10.200.12",
                "element": "tomato",
                "creds": {"vagrant": "vagrant"},
                "resources": ["tomato"],
                "labs": ["beta"],
            },
        ])

        repo = JsonFileLabRepository([tmp_path])
        assert repo.list_labs() == ["alpha", "beta"]

    def test_list_labs_multiple_search_paths(self, tmp_path):
        path1 = tmp_path / "p1"
        path2 = tmp_path / "p2"
        path1.mkdir()
        path2.mkdir()

        _hosts_file(path1, [
            {
                "ip": "10.10.200.11",
                "element": "orange",
                "creds": {"vagrant": "vagrant"},
                "resources": [],
                "labs": ["alpha"],
            },
        ])
        _hosts_file(path2, [
            {
                "ip": "10.10.200.12",
                "element": "tomato",
                "creds": {"vagrant": "vagrant"},
                "resources": [],
                "labs": ["beta"],
            },
        ])

        repo = JsonFileLabRepository([path1, path2])
        assert repo.list_labs() == ["alpha", "beta"]

    def test_list_labs_no_hosts_file(self, tmp_path):
        repo = JsonFileLabRepository([tmp_path])
        assert repo.list_labs() == []

    def test_list_labs_skips_malformed_file(self, tmp_path):
        """A malformed hosts.json is skipped by list_labs, not fatal."""
        (tmp_path / "hosts.json").write_text("[{invalid json")
        repo = JsonFileLabRepository([tmp_path])
        assert repo.list_labs() == []

    def test_default_search_paths_empty(self):
        """Constructed with no search paths -> no labs, no hosts file found."""
        repo = JsonFileLabRepository()
        assert repo.list_labs() == []


class TestLoadLabWithPreferences:
    """End-to-end tests for the unified ``preferences=`` parameter on ``load_lab``."""

    def _hosts(self, tmp_path):
        _hosts_file(tmp_path, [
            {
                "ip": "10.10.200.11",
                "element": "orange",
                "creds": {"vagrant": "vagrant"},
                "resources": [],
                "labs": ["testlab"],
                "ssh_options": {"port": 9000},
            },
        ])

    def test_defaults_apply_during_load(self, tmp_path):
        """Product preferences (option tables) merge into hosts during load_lab.
        The preference connect_timeout wins; the host-only port is preserved.
        """
        self._hosts(tmp_path)
        repo = JsonFileLabRepository([tmp_path])
        lab = repo.load_lab(
            "testlab",
            preferences={".*": {"ssh_options": {"connect_timeout": 99.0}}},
        )
        host = next(iter(lab.hosts.values()))
        assert host.ssh_options.port == 9000          # host-only key preserved
        assert host.ssh_options.connect_timeout == 99.0  # preferences wins

    def test_defaults_none_unchanged_behavior(self, tmp_path):
        """``preferences=None`` matches today's behavior."""
        self._hosts(tmp_path)
        repo = JsonFileLabRepository([tmp_path])
        lab = repo.load_lab("testlab")
        host = next(iter(lab.hosts.values()))
        assert host.ssh_options.port == 9000
        from otto.host.options import SshOptions
        assert host.ssh_options.connect_timeout == SshOptions().connect_timeout
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/storage/test_json_repository.py -q`
Expected: FAIL (the old positional-`search_paths` signature and `FileNotFoundError`/`ValueError` types do not match).

- [ ] **Step 3: Rewrite the Protocol**

Replace the entire contents of `src/otto/storage/protocol.py` with:

```python
from typing import (
    Any,
    Protocol,
    runtime_checkable,
)

from ..configmodule.lab import Lab


@runtime_checkable
class LabRepository(Protocol):
    """DB-agnostic interface for loading labs.

    A backend is configured at construction time (the built-in JSON backend
    takes its ``search_paths`` in ``__init__``), then queried through the two
    methods below. Selection and construction happen in
    :func:`otto.storage.build_lab_repository`.
    """

    def load_lab(
        self,
        name: str,
        preferences: dict[str, dict[str, Any]] | None = None,
    ) -> Lab:
        """Load a lab by name.

        Parameters
        ----------
        name : str
            Name of the lab to load.
        preferences : dict[str, dict[str, Any]] | None
            The unified ``{selector: {capability: [...] | option_table: {key: val}}}``
            product-preference table forwarded to the factory, which matches each
            host's ``id`` and applies the result. ``None`` reproduces today's
            behavior.

        Returns
        -------
        Lab
            Fully constructed lab.

        Raises
        ------
        LabNotFoundError
            If no lab named ``name`` exists.
        LabRepositoryError
            If the backend fails to satisfy the query (I/O, parse, network).
        """
        ...

    def list_labs(self) -> list[str]:
        """List all lab names this backend can provide.

        Returns
        -------
        list[str]
            Lab names (every element a ``str``).
        """
        ...
```

- [ ] **Step 4: Rewrite the JSON backend**

Replace the entire contents of `src/otto/storage/json_repository.py` with:

```python
import json
from pathlib import Path
from typing import Any

from ..configmodule.lab import Lab
from ..logger import get_otto_logger
from .errors import (
    LabNotFoundError,
    LabRepositoryError,
)
from .factory import (
    create_host_from_dict,
    validate_host_dict,
)

logger = get_otto_logger()

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
            searched = '\n  '.join(str(p) for p in self.search_paths)
            raise LabNotFoundError(
                f"Lab '{name}' not found in any search path:\n  {searched}"
            ) from None

        for idx, host_data in enumerate(matching):
            try:
                validate_host_dict(host_data)
            except ValueError as e:
                raise LabRepositoryError(
                    f"Invalid host data at index {idx} in lab '{name}': {e}"
                ) from e

        lab = Lab(name=name)

        for idx, host_data in enumerate(matching):
            try:
                host = create_host_from_dict(host_data, preferences=preferences)
                lab.add_host(host)
                lab.resources.update(host.resources)
            except Exception as e:
                logger.error(
                    f"Failed to create host at index {idx} in lab '{name}': {e}"
                )
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
            searched = '\n  '.join(str(p) for p in self.search_paths)
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
                f"Hosts file '{hosts_file}' must contain a JSON array, "
                f"got {type(data).__name__}"
            )

        return data
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `.venv/bin/python -m pytest tests/unit/storage/test_json_repository.py -q`
Expected: PASS (all tests). If a test elsewhere imports `supports_location`, that surfaces here as a collection error — it should not (verified at plan time: no live caller), but if it does, note it for the controller (it belongs to Task 7's wiring sweep).

- [ ] **Step 6: Verify the package still imports both ways**

Run: `.venv/bin/python -c "import otto.storage.json_repository; import otto.storage; from otto.storage import JsonFileLabRepository, LabRepository, LabNotFoundError; print('ok')"`
Expected: prints `ok` (the storage import cycle stays intact).

- [ ] **Step 7: Stage (do NOT commit)**

```bash
git add src/otto/storage/protocol.py src/otto/storage/json_repository.py tests/unit/storage/test_json_repository.py
```

---

## Task 3: Lab-repository name registry

Mirror `src/otto/reservations/registry.py` exactly. A name→class registry, `register_lab_repository(name, cls)`, `get_lab_repository_class(name)` (raises `LabRepositoryError` listing registered names), seeded with the built-in `json` backend at import.

**Files:**
- Create: `src/otto/storage/registry.py`
- Modify: `src/otto/storage/__init__.py`
- Test: `tests/unit/storage/test_registry.py` *(new)*

**Interfaces:**
- Consumes: `LabRepositoryError` (Task 1); `JsonFileLabRepository` (Task 2).
- Produces: `otto.storage.register_lab_repository(name: str, cls: type) -> None`; `otto.storage.registry.get_lab_repository_class(name: str) -> type` (raises `LabRepositoryError` on unknown); `otto.storage.registry._LAB_REPOSITORIES: dict[str, type]` seeded with `"json"`.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/storage/test_registry.py`:

```python
"""Unit tests for the host-source (LabRepository) backend registry."""

import pytest

from otto.storage import (
    JsonFileLabRepository,
    LabRepositoryError,
    register_lab_repository,
)
from otto.storage.registry import (
    _LAB_REPOSITORIES,
    get_lab_repository_class,
)


def test_json_builtin_registered():
    assert get_lab_repository_class("json") is JsonFileLabRepository


def test_register_and_lookup():
    class MyRepo:
        def load_lab(self, name, preferences=None):
            raise NotImplementedError

        def list_labs(self):
            return []

    register_lab_repository("mine-test", MyRepo)
    try:
        assert get_lab_repository_class("mine-test") is MyRepo
    finally:
        _LAB_REPOSITORIES.pop("mine-test", None)


def test_unknown_name_lists_registered():
    with pytest.raises(LabRepositoryError, match="Unknown lab repository backend"):
        get_lab_repository_class("does-not-exist")
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/storage/test_registry.py -q`
Expected: FAIL with `ImportError: cannot import name 'register_lab_repository'`.

- [ ] **Step 3: Write the registry module**

Create `src/otto/storage/registry.py`:

```python
"""Name → class registry for host-source (``LabRepository``) backends.

Mirrors :mod:`otto.reservations.registry` and otto's other extension
registries (``register_term_backend`` / ``register_transfer_backend`` /
``register_host_class``): a custom backend registers a bare name from an
``init`` module, and ``[lab] backend = "<name>"`` selects it. The built-in
``json`` backend is pre-registered at import so it resolves through the same
path.
"""

from __future__ import annotations

from .errors import LabRepositoryError

# Name -> LabRepository-compatible class. ``build_lab_repository`` constructs the
# resolved class (the json built-in gets search_paths=...; a custom backend gets
# repo_dir= + its ``[lab.<name>]`` kwargs).
_LAB_REPOSITORIES: dict[str, type] = {}


def register_lab_repository(name: str, cls: type) -> None:
    """Make a custom host-source backend selectable as ``backend = "<name>"``.

    Call from an ``init`` module listed in ``.otto/settings.toml``. The class
    must satisfy the :class:`~otto.storage.protocol.LabRepository` protocol.
    """
    _LAB_REPOSITORIES[name] = cls


def get_lab_repository_class(name: str) -> type:
    """Return the backend class registered under *name*.

    Raises
    ------
    LabRepositoryError
        If *name* is not registered; the message lists the registered names.
    """
    try:
        return _LAB_REPOSITORIES[name]
    except KeyError:
        known = ", ".join(sorted(_LAB_REPOSITORIES))
        raise LabRepositoryError(
            f"Unknown lab repository backend {name!r}. Registered backends: {known}. "
            f"Custom backends can be added via register_lab_repository()."
        ) from None


def _register_builtins() -> None:
    from .json_repository import JsonFileLabRepository

    _LAB_REPOSITORIES.setdefault("json", JsonFileLabRepository)


_register_builtins()
```

- [ ] **Step 4: Re-export `register_lab_repository`**

In `src/otto/storage/__init__.py`, add (after the protocol re-export):

```python
from .registry import (
    register_lab_repository as register_lab_repository,
)
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `.venv/bin/python -m pytest tests/unit/storage/test_registry.py -q`
Expected: PASS (3 passed).

- [ ] **Step 6: Stage (do NOT commit)**

```bash
git add src/otto/storage/registry.py src/otto/storage/__init__.py tests/unit/storage/test_registry.py
```

---

## Task 4: `LabConfigSpec` + `[lab]` settings wiring

Add `LabConfigSpec` to `models/settings.py` (mirror `ReservationConfigSpec`), wire a `lab` field into `SettingsModel` (so `[lab]` is accepted under `extra='forbid'`), and add a `lab_settings` property to `Repo` (mirror `reservation_settings`).

**Files:**
- Modify: `src/otto/models/settings.py`
- Modify: `src/otto/configmodule/repo.py`
- Test: `tests/unit/models/test_settings.py`

**Interfaces:**
- Produces: `otto.models.settings.LabConfigSpec` (`backend: str = "json"`, `extra="allow"`); `SettingsModel.lab: LabConfigSpec`; `Repo.lab_settings -> dict[str, Any]` (`${sut_dir}`-expanded `[lab]` sub-dict, `{}` when absent).

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/models/test_settings.py` (add `LabConfigSpec` to the existing `from otto.models.settings import ...` line at the top of the file):

```python
def test_lab_config_spec_defaults_to_json():
    cfg = LabConfigSpec.model_validate({})
    assert cfg.backend == "json"
    assert cfg.model_extra == {}


def test_lab_config_spec_keeps_backend_subtable_open():
    cfg = LabConfigSpec.model_validate(
        {"backend": "myteam", "myteam": {"url": "https://cmdb"}}
    )
    assert cfg.backend == "myteam"
    assert cfg.model_extra == {"myteam": {"url": "https://cmdb"}}


def test_settings_model_accepts_lab_block():
    m = SettingsModel.model_validate(
        {"name": "demo", "version": "1.0.0", "lab": {"backend": "json"}}
    )
    assert m.lab.backend == "json"


def test_settings_model_lab_defaults_when_absent():
    m = SettingsModel.model_validate({"name": "demo", "version": "1.0.0"})
    assert m.lab.backend == "json"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/unit/models/test_settings.py -q -k "lab"`
Expected: FAIL with `ImportError: cannot import name 'LabConfigSpec'` (and the `SettingsModel.lab` attribute errors).

- [ ] **Step 3: Add `LabConfigSpec`**

In `src/otto/models/settings.py`, add this class immediately **after** `ReservationConfigSpec` (around line 122):

```python
class LabConfigSpec(OttoModel):
    """The otto-owned ``[lab]`` envelope: which host-source ``backend`` to use.

    ``extra='allow'`` keeps the backend-specific ``[lab.<backend>]`` sub-table
    open — otto-core cannot type a third-party backend's kwargs. Defaults to the
    built-in ``"json"`` backend so repos with no ``[lab]`` block behave exactly
    as before.
    """

    model_config = ConfigDict(extra="allow")

    backend: str = "json"
```

- [ ] **Step 4: Wire `lab` into `SettingsModel`**

In `src/otto/models/settings.py`, in the `SettingsModel` "structured sub-tables" block (next to `reservations`), add the `lab` field:

```python
    docker: DockerSettingsSpec = DockerSettingsSpec()
    lab: LabConfigSpec = LabConfigSpec()
    reservations: ReservationConfigSpec = ReservationConfigSpec()
```

- [ ] **Step 5: Add `lab_settings` to `Repo`**

In `src/otto/configmodule/repo.py`, add this property immediately **after** the `reservation_settings` property (around line 492):

```python
    @property
    def lab_settings(self) -> dict[str, Any]:
        """Return the ``[lab]`` settings sub-dict with ``${sut_dir}`` expanded.

        Returns an empty dict when the section is absent, so the host-source
        factory falls back to the built-in ``json`` backend over this repo's
        ``labs`` search paths.
        """
        raw = self.settings.get('lab', {}) or {}
        return self._expand_recursive(raw)
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/unit/models/test_settings.py -q`
Expected: PASS (the new `lab` tests plus the existing settings tests; the `forbids_unknown_top_level_key` test still passes — its bad key is not `lab`).

- [ ] **Step 7: Stage (do NOT commit)**

```bash
git add src/otto/models/settings.py src/otto/configmodule/repo.py tests/unit/models/test_settings.py
```

---

## Task 5: `build_lab_repository` factory

Add `build_lab_repository(settings, repo_dir, *, search_paths=None)` to `src/otto/storage/__init__.py`, paralleling `otto.reservations.build_backend`. Default → json over `search_paths`; a registered name → `cls(repo_dir=..., **[lab.<name>] kwargs)`; unknown name → `LabRepositoryError`.

**Files:**
- Modify: `src/otto/storage/__init__.py`
- Test: `tests/unit/storage/test_build_lab_repository.py` *(new)*

**Interfaces:**
- Consumes: `LabConfigSpec` (Task 4); `get_lab_repository_class` (Task 3); `JsonFileLabRepository` (Task 2); `LabRepositoryError` (Task 1).
- Produces: `otto.storage.build_lab_repository(settings: dict[str, Any], repo_dir: Path, *, search_paths: list[Path] | None = None) -> LabRepository`.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/storage/test_build_lab_repository.py`:

```python
"""Unit tests for the host-source backend factory (build_lab_repository)."""

from pathlib import Path

import pytest

from otto.storage import (
    JsonFileLabRepository,
    LabRepositoryError,
    build_lab_repository,
    register_lab_repository,
)
from otto.storage.registry import _LAB_REPOSITORIES


class TestJsonDefault:

    def test_missing_backend_defaults_to_json(self, tmp_path):
        repo = build_lab_repository({}, tmp_path, search_paths=[tmp_path])
        assert isinstance(repo, JsonFileLabRepository)
        assert repo.search_paths == [tmp_path]

    def test_explicit_json_receives_search_paths(self, tmp_path):
        p1 = tmp_path / "a"
        p2 = tmp_path / "b"
        repo = build_lab_repository(
            {"backend": "json"}, tmp_path, search_paths=[p1, p2]
        )
        assert isinstance(repo, JsonFileLabRepository)
        assert repo.search_paths == [p1, p2]

    def test_json_without_search_paths_is_empty(self, tmp_path):
        repo = build_lab_repository({"backend": "json"}, tmp_path)
        assert isinstance(repo, JsonFileLabRepository)
        assert repo.search_paths == []


class TestCustomBackend:

    def test_registered_backend_receives_repo_dir_and_kwargs(self, tmp_path):
        class FakeRepo:
            def __init__(self, repo_dir, url=None):
                self.repo_dir = repo_dir
                self.url = url

            def load_lab(self, name, preferences=None):
                raise NotImplementedError

            def list_labs(self):
                return []

        register_lab_repository("fake-build-test", FakeRepo)
        try:
            repo = build_lab_repository(
                {"backend": "fake-build-test", "fake-build-test": {"url": "https://x"}},
                tmp_path,
                search_paths=[tmp_path],
            )
            assert isinstance(repo, FakeRepo)
            assert repo.repo_dir == tmp_path
            assert repo.url == "https://x"
        finally:
            _LAB_REPOSITORIES.pop("fake-build-test", None)

    def test_registered_backend_without_kwargs(self, tmp_path):
        class BareRepo:
            def __init__(self, repo_dir):
                self.repo_dir = repo_dir

            def load_lab(self, name, preferences=None):
                raise NotImplementedError

            def list_labs(self):
                return []

        register_lab_repository("bare-build-test", BareRepo)
        try:
            repo = build_lab_repository({"backend": "bare-build-test"}, tmp_path)
            assert isinstance(repo, BareRepo)
            assert repo.repo_dir == tmp_path
        finally:
            _LAB_REPOSITORIES.pop("bare-build-test", None)


class TestErrors:

    def test_unknown_backend_raises_lab_repository_error(self, tmp_path):
        with pytest.raises(LabRepositoryError, match="Unknown lab repository backend"):
            build_lab_repository({"backend": "does-not-exist"}, tmp_path)

    def test_malformed_envelope_raises_value_error(self, tmp_path):
        with pytest.raises(ValueError, match=r"Invalid \[lab\] settings"):
            build_lab_repository({"backend": 3}, tmp_path)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/unit/storage/test_build_lab_repository.py -q`
Expected: FAIL with `ImportError: cannot import name 'build_lab_repository'`.

- [ ] **Step 3: Add the factory**

In `src/otto/storage/__init__.py`, first ensure the module header has the needed imports (add `from __future__ import annotations`, `from pathlib import Path`, `from typing import Any` at the top if not already present), then add this function at the end of the file:

```python
def build_lab_repository(
    settings: dict[str, Any],
    repo_dir: Path,
    *,
    search_paths: list[Path] | None = None,
) -> LabRepository:
    """Construct a host-source backend from a parsed ``[lab]`` section.

    Parameters
    ----------
    settings : dict[str, Any]
        The ``[lab]`` sub-dict parsed from ``.otto/settings.toml``. ``backend``
        selects a registered name (defaults to ``"json"``); ``[lab.<name>]``
        holds the backend's keyword arguments.
    repo_dir : Path
        The SUT repo root, forwarded as ``repo_dir=`` to a custom backend's
        constructor. The built-in ``json`` backend ignores it and uses
        ``search_paths`` instead.
    search_paths : list[Path] | None
        The aggregated ``labs`` directories. Passed to the built-in ``json``
        backend (preserving today's multi-repo path merge); custom backends
        carry their own config and do not receive it.

    Returns
    -------
    LabRepository
        A ready-to-query backend instance.

    Raises
    ------
    ValueError
        If the ``[lab]`` envelope is malformed.
    LabRepositoryError
        If ``backend`` names an unknown (unregistered) backend.
    """
    from pydantic import ValidationError

    from ..models.settings import LabConfigSpec

    try:
        cfg = LabConfigSpec.model_validate(settings)
    except ValidationError as e:
        # Keep the documented exception surface (ValueError for a malformed
        # [lab] envelope) with a contextual message, not a raw pydantic dump.
        raise ValueError(f"Invalid [lab] settings: {e}") from e

    backend_name = cfg.backend

    if backend_name == "json":
        return JsonFileLabRepository(search_paths=list(search_paths or []))

    # Custom backend: resolved by registered name (register_lab_repository from
    # an init module). No dotted-path / importlib resolution.
    from .registry import get_lab_repository_class

    cls = get_lab_repository_class(backend_name)  # raises LabRepositoryError if unknown
    extra_kwargs: dict[str, Any] = settings.get(backend_name) or {}
    return cls(repo_dir=repo_dir, **extra_kwargs)  # type: ignore[no-any-return]
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/unit/storage/test_build_lab_repository.py -q`
Expected: PASS (all tests).

- [ ] **Step 5: Run the storage suite + import check**

Run: `.venv/bin/python -m pytest tests/unit/storage/ -q && .venv/bin/python -c "from otto.storage import build_lab_repository, register_lab_repository, LabRepositoryError; print('ok')"`
Expected: PASS; prints `ok`.

- [ ] **Step 6: Stage (do NOT commit)**

```bash
git add src/otto/storage/__init__.py tests/unit/storage/test_build_lab_repository.py
```

---

## Task 6: Route `configmodule.load_lab` through a repository

Make `configmodule.lab.load_lab` accept an optional pre-built `repository` and use it; when none is given, default to `JsonFileLabRepository(search_paths=...)` (backward compatible for `context.open_context` / library use). Drop the now-redundant `_get_individual_lab` helper.

**Files:**
- Modify: `src/otto/configmodule/lab.py`
- Test: `tests/unit/configmodule/test_load_lab.py` *(new)*

**Interfaces:**
- Consumes: `JsonFileLabRepository` (Task 2, already imported at `lab.py:67`); `LabRepository` (Task 2, for the type hint, under `TYPE_CHECKING`).
- Produces: `otto.configmodule.load_lab(labnames, search_paths=None, preferences=None, repository=None) -> Lab`.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/configmodule/test_load_lab.py`:

```python
"""Unit tests for configmodule.load_lab repository routing."""

import json
from pathlib import Path

from otto.configmodule.lab import Lab, load_lab
from otto.storage.json_repository import JsonFileLabRepository


def _hosts_file(path: Path, hosts: list[dict]) -> Path:
    f = path / "hosts.json"
    f.write_text(json.dumps(hosts))
    return f


def test_load_lab_default_repository_uses_search_paths(tmp_path):
    """With no repository given, load_lab builds a json backend over search_paths."""
    _hosts_file(tmp_path, [
        {
            "ip": "10.10.200.11",
            "element": "orange",
            "creds": {"vagrant": "vagrant"},
            "resources": ["orange"],
            "labs": ["testlab"],
        },
    ])
    lab = load_lab("testlab", search_paths=[tmp_path])
    assert isinstance(lab, Lab)
    assert lab.name == "testlab"
    assert len(lab.hosts) == 1


def test_load_lab_uses_injected_repository(tmp_path):
    """A passed repository is used instead of the default json backend."""
    _hosts_file(tmp_path, [
        {
            "ip": "10.10.200.11",
            "element": "orange",
            "creds": {"vagrant": "vagrant"},
            "resources": ["orange"],
            "labs": ["injected"],
        },
    ])
    repo = JsonFileLabRepository([tmp_path])
    lab = load_lab("injected", repository=repo)
    assert lab.name == "injected"
    assert len(lab.hosts) == 1


def test_load_lab_merges_multiple_names(tmp_path):
    """Comma-joined names merge into one lab (preserved behavior)."""
    _hosts_file(tmp_path, [
        {
            "ip": "10.10.200.11",
            "element": "orange",
            "creds": {"vagrant": "vagrant"},
            "resources": ["orange"],
            "labs": ["lab_a"],
        },
        {
            "ip": "10.10.200.12",
            "element": "tomato",
            "creds": {"vagrant": "vagrant"},
            "resources": ["tomato"],
            "labs": ["lab_b"],
        },
    ])
    repo = JsonFileLabRepository([tmp_path])
    lab = load_lab("lab_a,lab_b", repository=repo)
    assert len(lab.hosts) == 2
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/unit/configmodule/test_load_lab.py -q`
Expected: FAIL (`load_lab` has no `repository` parameter; the default path still calls the old positional signature).

- [ ] **Step 3: Refactor `load_lab` and drop `_get_individual_lab`**

In `src/otto/configmodule/lab.py`:

(a) Add a `TYPE_CHECKING` import for the `LabRepository` annotation. The file already has `from __future__ import annotations` and a `TYPE_CHECKING` block (importing `Host`); extend it:

```python
if TYPE_CHECKING:
    from ..host.host import Host
    from ..storage.protocol import LabRepository
```

(b) **Delete** the entire `_get_individual_lab` function (lines ~69–98).

(c) Replace the `load_lab` function body so it builds/accepts a repository and loops over it:

```python
def load_lab(
    labnames: str | list[str],
    search_paths: list[Path] | None = None,
    preferences: dict[str, dict[str, Any]] | None = None,
    repository: "LabRepository | None" = None,
) -> Lab:
    """
    Build a Lab object from one or more lab names.

    Parameters
    ----------
    labnames : str | list[str]
        Name(s) of lab data to retrieve (a comma-separated string is split).
    search_paths : list[Path] | None
        Directories searched by the default json backend. Ignored when
        ``repository`` is supplied.
    preferences : dict[str, dict[str, Any]] | None
        The unified ``{selector: {capability: [...] | option_table: {key: val}}}``
        product-preference table applied to every host in the resulting lab.
        ``None`` reproduces today's behavior.
    repository : LabRepository | None
        A pre-built host-source backend (e.g. from
        :func:`otto.storage.build_lab_repository`). When ``None``, a built-in
        json backend over ``search_paths`` is used — preserving library/script
        behavior.

    Returns
    -------
    Lab
        Fully defined lab instance.
    """

    match labnames:
        case str():
            labnameList = labnames.split(",")
        case _:
            labnameList = labnames

    if repository is None:
        repository = JsonFileLabRepository(search_paths=search_paths or [])

    labs = [repository.load_lab(name, preferences=preferences) for name in labnameList]
    lab = labs[0]
    for additionalLab in labs[1:]:
        lab += additionalLab

    return lab
```

Note: the `JsonFileLabRepository` import already exists at `lab.py:67`; keep it. The `Any`/`Path` imports already exist at the top of the file.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/unit/configmodule/test_load_lab.py -q`
Expected: PASS (3 passed).

- [ ] **Step 5: Verify nothing else imported `_get_individual_lab`**

Run: `grep -rn "_get_individual_lab" src tests`
Expected: no matches (it was private, used only inside `lab.py`).

- [ ] **Step 6: Run the configmodule + context regression**

Run: `.venv/bin/python -m pytest tests/unit/configmodule/ -q`
Expected: PASS (no regression in the lab-loading path; `context.open_context` still uses the default json branch).

- [ ] **Step 7: Stage (do NOT commit)**

```bash
git add src/otto/configmodule/lab.py tests/unit/configmodule/test_load_lab.py
```

---

## Task 7: Wire the CLI callback + repo panel through `build_lab_repository`

The top-level callback aggregates `labs` search paths across repos and selects the backend from the first repo declaring `[lab]` (mirror reservations' "first repo declares" rule), builds it via `build_lab_repository`, and passes it to `load_lab`. `get_lab_panel` uses the factory too. Add an explicit backward-compat regression and a custom-backend integration test.

**Files:**
- Modify: `src/otto/cli/main.py`
- Modify: `src/otto/configmodule/repo.py` (`get_lab_panel`)
- Test: `tests/unit/cli/test_lab_source_wiring.py` *(new)*

**Interfaces:**
- Consumes: `build_lab_repository`, `LabRepositoryError` (Task 5); `Repo.lab_settings` (Task 4); `Repo.labs`, `Repo.sut_dir` (existing); `configmodule.load_lab(..., repository=...)` (Task 6).

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/cli/test_lab_source_wiring.py`. These test the wiring units directly (the callback's repository-selection logic), without standing up a full `CliRunner` lab — keeping them fast and hermetic:

```python
"""Wiring tests: the CLI lab-source path honors [lab] and stays backward compatible."""

import json
from pathlib import Path

import pytest

from otto.configmodule.lab import load_lab
from otto.storage import (
    JsonFileLabRepository,
    LabRepositoryError,
    build_lab_repository,
    register_lab_repository,
)
from otto.storage.registry import _LAB_REPOSITORIES


def _hosts_file(path: Path, hosts: list[dict]) -> Path:
    f = path / "hosts.json"
    f.write_text(json.dumps(hosts))
    return f


def test_no_lab_block_defaults_to_json_over_merged_paths(tmp_path):
    """Backward compat: no [lab] block -> json backend over aggregated labs paths."""
    p1 = tmp_path / "r1"
    p2 = tmp_path / "r2"
    p1.mkdir()
    p2.mkdir()
    _hosts_file(p1, [
        {
            "ip": "10.10.200.11",
            "element": "orange",
            "creds": {"vagrant": "vagrant"},
            "resources": ["orange"],
            "labs": ["merged"],
        },
    ])
    _hosts_file(p2, [
        {
            "ip": "10.10.200.12",
            "element": "tomato",
            "creds": {"vagrant": "vagrant"},
            "resources": ["tomato"],
            "labs": ["merged"],
        },
    ])

    # lab_settings == {} (no [lab] block); aggregated search paths from both repos.
    repository = build_lab_repository({}, tmp_path, search_paths=[p1, p2])
    assert isinstance(repository, JsonFileLabRepository)

    lab = load_lab("merged", repository=repository)
    assert len(lab.hosts) == 2


def test_custom_backend_selected_by_name(tmp_path):
    """A [lab] backend name selects a registered custom repository."""
    sentinel_lab_name = "from-custom"

    class DictRepo:
        def __init__(self, repo_dir, names=None):
            self.repo_dir = repo_dir
            self._names = names or []

        def load_lab(self, name, preferences=None):
            from otto.configmodule.lab import Lab
            return Lab(name=name)

        def list_labs(self):
            return list(self._names)

    register_lab_repository("dict-wiring-test", DictRepo)
    try:
        repository = build_lab_repository(
            {"backend": "dict-wiring-test",
             "dict-wiring-test": {"names": [sentinel_lab_name]}},
            tmp_path,
            search_paths=[tmp_path],
        )
        assert isinstance(repository, DictRepo)
        assert repository.list_labs() == [sentinel_lab_name]
        lab = load_lab(sentinel_lab_name, repository=repository)
        assert lab.name == sentinel_lab_name
    finally:
        _LAB_REPOSITORIES.pop("dict-wiring-test", None)


def test_unknown_backend_name_raises(tmp_path):
    with pytest.raises(LabRepositoryError, match="Unknown lab repository backend"):
        build_lab_repository({"backend": "nope"}, tmp_path, search_paths=[tmp_path])
```

- [ ] **Step 2: Run the tests to verify they fail or pass**

Run: `.venv/bin/python -m pytest tests/unit/cli/test_lab_source_wiring.py -q`
Expected: these exercise Task-5/Task-6 surfaces and should already PASS. (They lock the behavior the callback wiring depends on; they fail only if Tasks 5/6 are incomplete.) If they pass, proceed — the callback edit in Step 3 is what they protect against regressing.

- [ ] **Step 3: Edit the CLI callback**

In `src/otto/cli/main.py`, replace the lab-loading block (currently lines ~290–309: the `lab_search_paths` loop, the `merged_host_preferences` loop, and the `load_lab(...)` call) with:

```python
    # Extract + aggregate lab search paths across all repos (for the default
    # json backend).
    lab_search_paths: list[Path] = []
    for repo in repos:
        lab_search_paths.extend(repo.labs)

    # Reduce repos' [host_preferences] tables in OTTO_SUT_DIRS order; later repos
    # overlay earlier ones. Selections (list) are atomic — last repo to set a
    # (selector, capability) wins it; option tables (dict) merge per key.
    merged_host_preferences: dict[str, dict[str, Any]] = {}
    for repo in repos:
        for selector, entries in repo.host_preferences.items():
            dest = merged_host_preferences.setdefault(selector, {})
            for key, val in entries.items():
                if isinstance(val, list):
                    dest[key] = list(val)
                else:
                    dest.setdefault(key, {}).update(val)

    # Select the host-source backend: the first repo that declares a [lab] block
    # wins (mirrors reservations' "first repo declares" rule). With no [lab]
    # block anywhere, lab_settings stays {} and the factory falls back to the
    # built-in json backend over the aggregated search paths.
    lab_settings: dict[str, Any] = {}
    lab_repo_dir: Path = repos[0].sut_dir if repos else Path.cwd()
    for repo in repos:
        if repo.lab_settings:
            lab_settings = repo.lab_settings
            lab_repo_dir = repo.sut_dir
            break

    from ..storage import LabRepositoryError, build_lab_repository

    try:
        lab_repository = build_lab_repository(
            lab_settings, lab_repo_dir, search_paths=lab_search_paths
        )
    except (ValueError, LabRepositoryError) as e:
        rprint(f"[bold red]Host source unavailable:[/bold red] {e}")
        raise typer.Exit(1) from e

    lab = load_lab(labs, preferences=merged_host_preferences, repository=lab_repository)
```

(Keep everything before — `repos = get_repos()` — and everything after the `load_lab(...)` call unchanged. `Any`, `Path`, `rprint`, and `typer` are already imported in `main.py`.)

- [ ] **Step 4: Edit `get_lab_panel` in `repo.py`**

In `src/otto/configmodule/repo.py`, replace the body of `get_lab_panel` that builds `lab_names` (currently lines ~208–213) with the factory path:

```python
        from ..storage import build_lab_repository

        repository = build_lab_repository(
            self.lab_settings, self.sut_dir, search_paths=self.labs
        )
        lab_names = repository.list_labs()
```

(Remove the old `from ..storage import JsonFileLabRepository` line and the `lab_search_paths` local that fed `list_labs(search_paths=...)`. Keep the rest of the panel-building code unchanged.)

- [ ] **Step 5: Run the wiring tests + the broader CLI/configmodule unit suites**

Run: `.venv/bin/python -m pytest tests/unit/cli/test_lab_source_wiring.py tests/unit/configmodule/ tests/unit/cli/ -q`
Expected: PASS. Watch specifically for any test that constructed `JsonFileLabRepository()` with the old positional `list_labs(search_paths=...)` / `load_lab(name, paths)` signature, or that relied on `supports_location` — fix any such call site to the construct-time signature as part of this task (it is wiring this task owns). Report counts.

- [ ] **Step 6: Stage (do NOT commit)**

```bash
git add src/otto/cli/main.py src/otto/configmodule/repo.py tests/unit/cli/test_lab_source_wiring.py
```

---

## Task 8: API-doc stubs + full gate

Keep nitpicky docs green for the touched modules (document the new errors module, registry, and factory/register functions) and run the full local gate.

**Files:**
- Modify: `docs/api/storage.rst`

- [ ] **Step 1: Update `docs/api/storage.rst`**

Replace the contents of `docs/api/storage.rst` with (mirrors `docs/api/reservations.rst`'s structure — documents the package functions, the protocol, the json backend, the registry, the errors, and the factory):

```rst
storage
=======

The storage package provides a DB-agnostic host-source (``LabRepository``)
backend, selected by name and constructed via :func:`otto.storage.build_lab_repository`.
The built-in ``json`` backend reads ``hosts.json`` files; custom backends
register a name via :func:`otto.storage.register_lab_repository` from an
``init`` module.

.. autofunction:: otto.storage.build_lab_repository

.. autofunction:: otto.storage.register_lab_repository

.. automodule:: otto.storage.protocol

.. automodule:: otto.storage.json_repository

.. automodule:: otto.storage.registry

.. automodule:: otto.storage.errors

.. automodule:: otto.storage.factory
```

- [ ] **Step 2: Build the docs (nitpicky, `-W`)**

Run: `make docs`
Expected: 0 warnings. If a numpydoc "Raises" cross-reference to `LabNotFoundError`/`LabRepositoryError` is reported unresolved, the fix is local to this task: ensure `.. automodule:: otto.storage.errors` is present (it is, above) — the exception classes then have xref targets. Re-run until clean.

- [ ] **Step 3: Type-check**

Run: `make typecheck`
Expected: clean (no new `ty` findings). The `# type: ignore[no-any-return]` on the custom-backend construction line in `build_lab_repository` mirrors `build_backend`'s existing suppressions.

- [ ] **Step 4: Full unit coverage gate**

Run: `make coverage`
Expected: unit coverage ≥85%; all `tests/unit` green. Integration/e2e tests that need a live lab bed will fail with connection errors (`OSError: [Errno 113]` to `10.10.200.x`) — those are **environmental**, not regressions (the dev VM cannot reach the beds). Triage with `scripts/junit_failures.py` if needed and confirm every failure is bed-connectivity, not a code regression in the touched files.

- [ ] **Step 5: Stage (do NOT commit)**

```bash
git add docs/api/storage.rst
```

- [ ] **Step 6: Hand off**

All host-source feature work staged. Report the full staged file list and gate results to the controller for the final whole-branch review. Do **not** commit — Chris commits.

---

## Self-Review (controller, before dispatching Task 1)

Pre-flight checks against the spec (§3.1–§3.4) and this plan:

1. **Spec coverage:**
   - §3.1 reshape Protocol (drop `search_paths`, drop `supports_location`) → Task 2. ✅
   - §3.2 error contract (`LabRepositoryError`/`LabNotFoundError`) → Task 1; json backend raises them → Task 2. ✅
   - §3.3 `build_lab_repository` + `LabConfigSpec` + `[lab]` block + registry name dispatch → Tasks 3, 4, 5. ✅
   - §3.4 wire `configmodule/lab.py` + multi-repo "first repo declares, json aggregates" → Tasks 6, 7. ✅
   - §3b `register_lab_repository` + json pre-registered + bare-name dispatch (no dotted path) → Tasks 3, 5. ✅
   - Backward-compat regression (no `[lab]` block) → Task 7 `test_no_lab_block_defaults_to_json_over_merged_paths`. ✅
2. **Out of scope (correctly deferred):** conformance suite (§4), samples (§5), host-database guide / onboarding (§6) — Plans C/D. Reservation code (§3a/§3b-reservations/§3c/§3d) — Plan B (already staged). ✅
3. **Type consistency:** `load_lab(name, preferences=None)` and `list_labs()` signatures are identical across the Protocol (Task 2), `JsonFileLabRepository` (Task 2), and the custom-backend fakes (Tasks 3/5/7). `build_lab_repository(settings, repo_dir, *, search_paths=None)` is used consistently in Tasks 5, 7, and `get_lab_panel`. `register_lab_repository(name, cls)` matches across Tasks 3/5/7. ✅
4. **`supports_location` removal:** verified no live caller at plan time (only its own def + the json-repository tests, which Task 2's rewrite drops). ✅
5. **Import-cycle safety:** the storage package imports cleanly from any entry order (verified at plan time); registry mirrors reservations' module-load `_register_builtins()`. ✅

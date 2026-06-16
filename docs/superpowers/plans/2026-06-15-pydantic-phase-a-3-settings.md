# Pydantic Phase A — Plan 3: Settings (`settings.toml` + `OTTO_*` env) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the hand-rolled `.otto/settings.toml` parsing/validation in `src/otto/configmodule/repo.py` (`_parse_host_defaults` / `_parse_os_profiles` / `_parse_docker_settings`), the reservation envelope + JSON-file validation, and the ad-hoc `OTTO_*` env reads with pydantic boundary models — a `SettingsModel` (`extra='forbid'`), docker/os-profile/reservation specs, and a `pydantic-settings` `OttoEnvSettings`.

**Architecture:** A new `src/otto/models/settings.py` joins the existing leaf boundary package (`models/base.py`, `options.py`, `host.py`). It validates the settings dict and builds the **unchanged** runtime objects (`DockerSettings`/`DockerImage`/`DockerCompose` frozen dataclasses, `OsProfile`, the reservation backend) via the same two-type-split pattern Plans 1–2b used (`*Spec.to_runtime()`). `Repo.parse_settings` keeps its raw `self.settings` dict but routes typed-field population through one `SettingsModel.model_validate(expanded)` call. `${sut_dir}` expansion stays a pre-pass (a whole-dict `_expand_recursive` before validation), so the model is context-free. The `OTTO_*` surface becomes one typed `OttoEnvSettings(BaseSettings)`.

**Tech Stack:** pydantic v2 (`model_validate`, `model_dump(exclude_unset=True)`, `field_validator`, `ConfigDict(extra=...)`), `pydantic-settings` 2.x (`BaseSettings`, `SettingsConfigDict`), `tomli`, the existing string registries in `otto.host.os_profile`.

---

## Context the engineer needs (read once before Task 1)

You are working on branch `phase-a-pydantic-1-foundation-options` (Plans 1, 2a, 2b are committed there — `308af53`, `b8882e4`, `0a38090`). **Stage only — do NOT `git commit`.** The repo's `prepare-commit-msg` hook needs `/dev/tty`; an agent commit mis-tags the AI-assist trailer; Chris commits manually. Each task ends with a `git add` step, not a commit.

**Do NOT run `make test` (it spins up live VM tiers — never kill those mid-run) or `make coverage` (the 90% gate is Chris's pre-merge run).** Run the *targeted* `pytest --no-cov` commands each task specifies. They are fast and hermetic.

### Five rules specific to this plan

1. **`models/` must stay a leaf — never import from `configmodule` at module top.** Importing *anything* under `otto.configmodule` (even `configmodule.version` or `configmodule.repo`) executes `configmodule/__init__.py`, which **bootstraps the whole app at import time** (`OttoEnv()`, `get_repos()`, `apply_repo_settings()`). The new `models/settings.py` therefore: (a) imports the runtime `DockerSettings`/`DockerImage`/`DockerCompose` only **lazily inside `to_runtime()`** and under `if TYPE_CHECKING:` for annotations (the same pattern `host/os_profile.py` uses for `HostSpec`); (b) validates the `version` string with a **local regex**, not by importing `configmodule.version.Version`. Runtime modules under `otto.host.*` and `otto.docker.*`-the-dataclasses are fine to import the normal way only because they don't trip the bootstrap — but `configmodule` does, so treat it as off-limits at module top.

2. **Two-type split, unchanged runtime objects (the Phase A invariant).** The frozen `DockerSettings`/`DockerImage`/`DockerCompose` dataclasses in `configmodule/repo.py` stay **byte-for-byte unchanged** (they are hashable and feed the docker context-hash; many consumers depend on them). `models/settings.py` adds `*Spec` models that *build* them via `to_runtime()`. Same for `OsProfile` and the reservation file. This is why the spec line "docker becomes pydantic models" is implemented as docker **spec** models over the unchanged dataclasses, exactly as `host/options.py` (runtime) vs `models/options.py` (spec).

3. **Expand-first, then validate once.** `${sut_dir}` substitution is orthogonal to validation. `Repo.parse_settings` runs `_expand_recursive` over the whole parsed dict into a *separate* expanded copy, then `SettingsModel.model_validate(expanded)`. **Keep `self.settings` the raw (unexpanded) `tomli.loads` result** — `repo.settings['coverage']` (read in `cli/test.py:750`) and the `reservation_settings` property both operate on the raw dict and must not change shape.

4. **`extra='forbid'` on `SettingsModel` must tolerate the real top-level keys.** A scan of every in-tree `settings.toml` fixture + every `self.settings.get(...)` read gives the complete top-level set: `name`, `version`, `lab_data_type`, `labs`, `valid_labs`, `libs`, `tests`, `init`, `host_defaults`, `os_profiles`, `docker`, `reservations`, `coverage`. **`lab_data_type` and `coverage` are present in fixtures but never consumed by `parse_settings`** — they must be declared as allowed fields (an opaque passthrough for `coverage`) or every real repo fails to validate. ⚠ Chris's *downstream* product repos may carry additional top-level keys this in-tree scan can't see; flag at hand-off that `make test`/`coverage` against the real labs is the authoritative check for an unmodeled-but-real key, since `extra='forbid'` will now reject one with a clear error instead of silently ignoring it (the intended behavior change).

5. **Omit-unset for partial tables.** `host_defaults` sub-tables stay **partial** for the downstream per-key merge: validate each `*_options` table against its `*OptionsSpec` (to catch typos + type errors) but recover only the user-set keys via `model_dump(exclude_unset=True)`. Never emit a full default-filled table — that would clobber the per-key precedence merge in `storage/factory.py`.

### Files this plan touches

| File | Change |
| --- | --- |
| `src/otto/models/settings.py` | **new** — `DockerImageSpec`/`DockerComposeSpec`/`DockerSettingsSpec`, `OsProfileSpec`, `ReservationConfigSpec`, `ReservationEntry`/`ReservationFile`, `SettingsModel`, `OttoEnvSettings`, drift-guard pairs |
| `src/otto/models/__init__.py` | export the new public models |
| `src/otto/reservations/__init__.py` | `build_backend` validates the envelope via `ReservationConfigSpec` |
| `src/otto/reservations/json_backend.py` | `_load` validates the file via `ReservationFile` |
| `src/otto/configmodule/repo.py` | `parse_settings` → expand-first + `SettingsModel.model_validate`; delete `_parse_host_defaults`/`_parse_os_profiles`/`_parse_docker_settings`; keep raw `self.settings`, `reservation_settings`, the `Docker*` dataclasses, `_expand_recursive`/`_expand_string` |
| `src/otto/configmodule/env.py` | replace `OttoEnv` dataclass with `OttoEnvSettings` re-export + keep the name constants + `validate_path`; `load_otto_env()` factory preserving `FileNotFoundError` |
| `src/otto/configmodule/__init__.py` | build env via `load_otto_env()` |
| `src/otto/cli/main.py` | `_field_default` via the env model |
| `src/otto/docker/compose.py` | `OTTO_COMPOSE_SUFFIX` via the env model |
| `src/otto/configmodule/completion_cache.py` | `OTTO_XDIR` via the env model |
| `tests/unit/models/test_settings.py` | **new** — all model tests |
| `tests/unit/configmodule/test_repo.py` | adapt error-type expectations (`ValueError` → `ValidationError`) |
| `tests/unit/configmodule/test_env.py` | adapt to `OttoEnvSettings` / `load_otto_env()` |
| `tests/unit/reservations/test_json_backend.py`, `tests/unit/cli/test_reservation.py` | adapt malformed-file/envelope error expectations |

### Dependency order

Task 1 (docker specs) and Task 2 (os-profile + reservation specs) build the pieces Task 4 (`SettingsModel`) composes. Task 3 (wire reservation models) only needs Task 2. Task 5 (wire `SettingsModel` into `Repo`) needs Task 4. Task 6 (env) is independent and can go any time. Recommended order: **1 → 2 → 3 → 4 → 5 → 6.**

---

## Task 1: Docker specs (`models/settings.py`)

**Why first:** self-contained; establishes the new file with the leaf-isolation pattern (lazy `to_runtime` import + `TYPE_CHECKING` annotations) that Tasks 2 & 4 reuse.

**Files:**
- Create: `src/otto/models/settings.py`
- Test: `tests/unit/models/test_settings.py` (new)

- [ ] **Step 1: Write the failing test**

Create `tests/unit/models/test_settings.py`:

```python
from pathlib import Path

import pytest
from pydantic import ValidationError

from otto.configmodule.repo import DockerCompose, DockerImage, DockerSettings
from otto.models.settings import (
    DockerComposeSpec,
    DockerImageSpec,
    DockerSettingsSpec,
)


def test_docker_settings_spec_defaults_to_empty_runtime():
    rt = DockerSettingsSpec().to_runtime()
    assert isinstance(rt, DockerSettings)
    assert rt.registry_url == "docker.io"
    assert rt.images == ()
    assert rt.composes == ()


def test_docker_image_spec_builds_runtime_with_sorted_tupled_build_args():
    spec = DockerSettingsSpec.model_validate({
        "registry_url": "reg.example",
        "images": [{
            "name": "api",
            "dockerfile": "/repo/docker/Dockerfile",
            "context": "/repo/docker",
            "target": "prod",
            "build_args": {"B": "2", "A": "1"},
        }],
    })
    rt = spec.to_runtime()
    assert isinstance(rt.images[0], DockerImage)
    img = rt.images[0]
    assert img.name == "api"
    assert img.dockerfile == Path("/repo/docker/Dockerfile")
    assert img.context == Path("/repo/docker")
    assert img.target == "prod"
    # build_args: frozen, sorted-by-key, all-string tuple-of-tuples (hashable)
    assert img.build_args == (("A", "1"), ("B", "2"))


def test_docker_compose_spec_builds_runtime():
    spec = DockerSettingsSpec.model_validate({
        "composes": [{
            "path": "/repo/compose.yml",
            "default_host": "pepper_seed",
            "services": ["api", "worker"],
        }],
    })
    rt = spec.to_runtime()
    assert isinstance(rt.composes[0], DockerCompose)
    assert rt.composes[0].path == Path("/repo/compose.yml")
    assert rt.composes[0].default_host == "pepper_seed"
    assert rt.composes[0].services == ("api", "worker")


def test_docker_spec_forbids_unknown_top_level_key():
    with pytest.raises(ValidationError):
        DockerSettingsSpec.model_validate({"registy_url": "x"})  # typo


def test_docker_image_spec_requires_name_dockerfile_context():
    with pytest.raises(ValidationError):
        DockerImageSpec.model_validate({"name": "api"})  # missing dockerfile/context
```

- [ ] **Step 2: Run it to confirm it fails**

Run: `pytest tests/unit/models/test_settings.py -q --no-cov`
Expected: FAIL — `ModuleNotFoundError: No module named 'otto.models.settings'`.

- [ ] **Step 3: Create `src/otto/models/settings.py` with the docker specs**

```python
"""Pydantic boundary specs for ``.otto/settings.toml`` and the ``OTTO_*`` env.

These validate the settings dict (``extra='forbid'``) and build the **unchanged**
runtime objects (``DockerSettings``/``DockerImage``/``DockerCompose`` frozen
dataclasses, ``OsProfile``, the reservation backend) via ``to_runtime()`` — the
same two-type split the option/host specs use.

Leaf isolation: this module must NOT import from ``otto.configmodule`` at module
top — doing so triggers ``configmodule/__init__``'s app bootstrap. Runtime types
from ``configmodule.repo`` are imported lazily inside ``to_runtime()`` and under
``TYPE_CHECKING`` for annotations only.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .base import OttoModel

if TYPE_CHECKING:
    from ..configmodule.repo import DockerCompose, DockerImage, DockerSettings


class DockerImageSpec(OttoModel):
    name: str
    dockerfile: Path
    context: Path
    target: str | None = None
    build_args: dict[str, str] = {}

    def to_runtime(self) -> DockerImage:
        from ..configmodule.repo import DockerImage
        return DockerImage(
            name=self.name,
            dockerfile=self.dockerfile,
            context=self.context,
            target=self.target,
            # frozen, sorted, all-string tuple-of-tuples so the runtime object
            # stays hashable and order-stable for the docker context hash.
            build_args=tuple(
                (k, str(v)) for k, v in sorted(self.build_args.items())
            ),
        )


class DockerComposeSpec(OttoModel):
    path: Path
    default_host: str | None = None
    services: tuple[str, ...] = ()

    def to_runtime(self) -> DockerCompose:
        from ..configmodule.repo import DockerCompose
        return DockerCompose(
            path=self.path,
            default_host=self.default_host,
            services=tuple(self.services),
        )


class DockerSettingsSpec(OttoModel):
    registry_url: str = "docker.io"
    images: list[DockerImageSpec] = []
    composes: list[DockerComposeSpec] = []

    def to_runtime(self) -> DockerSettings:
        from ..configmodule.repo import DockerSettings
        return DockerSettings(
            registry_url=self.registry_url,
            images=tuple(i.to_runtime() for i in self.images),
            composes=tuple(c.to_runtime() for c in self.composes),
        )
```

- [ ] **Step 4: Run the test to confirm it passes**

Run: `pytest tests/unit/models/test_settings.py -q --no-cov`
Expected: PASS (5 passed).

- [ ] **Step 5: Verify the existing docker suite is unaffected, then type/lint-check**

Run: `pytest tests/unit/docker -q --no-cov` (the runtime dataclasses are untouched — expect all pass)
Run: `ty check src/otto/models/settings.py && ruff check src/otto/models/settings.py tests/unit/models/test_settings.py`
Expected: no diagnostics, no lint errors.

- [ ] **Step 6: Stage**

```bash
git add src/otto/models/settings.py tests/unit/models/test_settings.py
```

---

## Task 2: `OsProfileSpec`, `ReservationConfigSpec`, and the reservation file models

**Why now:** the remaining leaf models `SettingsModel` (Task 4) composes; plus the reservation file model Task 3 wires into the backend. `OsProfileSpec` and `ReservationConfigSpec` are the two models that deliberately relax `extra='forbid'` to `extra='allow'` (they collect open sub-tables).

**Files:**
- Modify: `src/otto/models/settings.py` (append)
- Test: `tests/unit/models/test_settings.py` (extend)

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/models/test_settings.py`:

```python
from datetime import datetime, timezone

from otto.models.settings import (
    OsProfileSpec,
    ReservationConfigSpec,
    ReservationEntry,
    ReservationFile,
)


def test_os_profile_spec_requires_base_and_collects_defaults():
    spec = OsProfileSpec.model_validate({
        "base": "embedded",
        "os_name": "Zephyr",
        "os_version": "3.7",
        "command_frame": "zephyr",
        "max_filename_len": 32,
    })
    assert spec.base == "embedded"
    # every non-`base` key is collected as a raw defaults bundle
    assert spec.defaults == {
        "os_name": "Zephyr",
        "os_version": "3.7",
        "command_frame": "zephyr",
        "max_filename_len": 32,
    }


def test_os_profile_spec_missing_base_raises():
    with pytest.raises(ValidationError):
        OsProfileSpec.model_validate({"os_name": "Zephyr"})


def test_reservation_config_defaults_to_none_backend():
    cfg = ReservationConfigSpec()
    assert cfg.backend == "none"
    assert cfg.url is None


def test_reservation_config_keeps_open_backend_subtable():
    # extra='allow' — the backend-specific sub-table is forwarded, not rejected.
    cfg = ReservationConfigSpec.model_validate({
        "backend": "json",
        "json": {"path": "reservations.json"},
    })
    assert cfg.backend == "json"
    assert cfg.model_extra == {"json": {"path": "reservations.json"}}


def test_reservation_config_rejects_non_string_backend():
    with pytest.raises(ValidationError):
        ReservationConfigSpec.model_validate({"backend": 3})


def test_reservation_file_parses_entries_and_z_suffix():
    f = ReservationFile.model_validate({
        "version": 1,
        "reservations": [
            {"user": "alice", "resources": ["rack3-psu"], "expires": "2099-01-01T00:00:00Z"},
            {"user": "bob", "resources": ["rack4-psu"]},
        ],
    })
    assert isinstance(f.reservations[0], ReservationEntry)
    assert f.reservations[0].user == "alice"
    assert f.reservations[0].expires == datetime(2099, 1, 1, tzinfo=timezone.utc)
    assert f.reservations[1].expires is None


def test_reservation_file_naive_expires_treated_as_utc():
    f = ReservationFile.model_validate({
        "version": 1,
        "reservations": [{"user": "a", "resources": ["r"], "expires": "2099-01-01T00:00:00"}],
    })
    assert f.reservations[0].expires == datetime(2099, 1, 1, tzinfo=timezone.utc)


def test_reservation_file_rejects_bad_version():
    with pytest.raises(ValidationError):
        ReservationFile.model_validate({"version": 2, "reservations": []})


def test_reservation_file_rejects_non_string_resources():
    with pytest.raises(ValidationError):
        ReservationFile.model_validate({
            "version": 1,
            "reservations": [{"user": "a", "resources": [3]}],
        })
```

- [ ] **Step 2: Run to confirm failure**

Run: `pytest tests/unit/models/test_settings.py -q --no-cov`
Expected: FAIL — `ImportError: cannot import name 'OsProfileSpec'`.

- [ ] **Step 3: Append the models to `src/otto/models/settings.py`**

Add these imports to the top of the file (merge with the existing import block; keep them sorted — `ruff check --fix` will order them):

```python
from datetime import datetime, timezone
from typing import Literal

from pydantic import ConfigDict, field_validator
```

Then append:

```python
class OsProfileSpec(OttoModel):
    """A named ``[os_profiles.<name>]`` bundle: a required ``base`` host-class
    name plus an open bag of raw default field values merged beneath each host.

    ``extra='allow'`` collects the non-``base`` keys; the per-field typo guard
    runs later, in ``register_os_profile`` (against the base class's slots), so
    the bundle stays raw here exactly as a ``hosts.json`` entry would be.
    """

    model_config = ConfigDict(extra="allow")

    base: str

    @property
    def defaults(self) -> dict[str, Any]:
        return dict(self.model_extra or {})


class ReservationConfigSpec(OttoModel):
    """The otto-owned ``[reservations]`` envelope: ``backend`` + optional ``url``.

    ``extra='allow'`` keeps the backend-specific ``[reservations.<backend>]``
    sub-table open — otto-core cannot type a third-party backend's kwargs.
    """

    model_config = ConfigDict(extra="allow")

    backend: str = "none"
    url: str | None = None


def _iso8601_utc(value: object) -> object:
    """Mirror ``json_backend._parse_iso8601``: normalize trailing ``Z`` and
    treat a naive timestamp as UTC. Non-strings pass through for pydantic."""
    if not isinstance(value, str):
        return value
    normalized = value.replace("Z", "+00:00") if value.endswith("Z") else value
    dt = datetime.fromisoformat(normalized)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


class ReservationEntry(OttoModel):
    user: str
    resources: list[str]
    expires: datetime | None = None

    @field_validator("expires", mode="before")
    @classmethod
    def _normalize_expires(cls, v: object) -> object:
        return _iso8601_utc(v)


class ReservationFile(OttoModel):
    """The ``version: 1`` JSON reservation file the built-in JSON backend reads."""

    version: Literal[1]
    reservations: list[ReservationEntry] = []
```

- [ ] **Step 4: Run to confirm pass + lint**

Run: `pytest tests/unit/models/test_settings.py -q --no-cov`
Expected: PASS (all, incl. Task 1's).
Run: `ruff check --fix src/otto/models/settings.py && ty check src/otto/models/settings.py`
Expected: imports ordered, no diagnostics.

- [ ] **Step 5: Stage**

```bash
git add src/otto/models/settings.py tests/unit/models/test_settings.py
```

---

## Task 3: Wire the reservation models into the backend + envelope

**Files:**
- Modify: `src/otto/reservations/json_backend.py` (`_load` uses `ReservationFile`)
- Modify: `src/otto/reservations/__init__.py` (`build_backend` validates the envelope)
- Test: `tests/unit/reservations/test_json_backend.py`, `tests/unit/cli/test_reservation.py` (adapt)

- [ ] **Step 1: Read the existing tests to preserve their contract**

Read `tests/unit/reservations/test_json_backend.py` and `tests/unit/cli/test_reservation.py`. The behavior to preserve: malformed/corrupt files and bad timestamps surface as `ReservationBackendError` (the CLI's fail-closed wrapping), past-dated entries are silently ignored, and the file is a list so a user may appear more than once.

- [ ] **Step 2: Update `json_backend.py` `_load`/`_active_entries` to validate via the model**

In `src/otto/reservations/json_backend.py`, replace the hand-rolled structural checks. Import the model and the pydantic error at the top:

```python
from pydantic import ValidationError

from ..models.settings import ReservationFile
```

Rewrite `_active_entries` and `_load` so the model does the structural validation, wrapping `ValidationError` as `ReservationBackendError` (keep the JSON-decode and OSError handling as-is):

```python
    def _active_entries(self) -> list[ReservationEntry]:
        """Load the file and return entries that are not past their expiry."""
        data = self._load()
        now = datetime.now(tz=timezone.utc)
        active: list[ReservationEntry] = []
        for entry in data.reservations:
            if entry.expires is None or entry.expires > now:
                active.append(entry)
        return active

    def _load(self) -> ReservationFile:
        try:
            raw = self._path.read_text()
        except OSError as e:
            raise ReservationBackendError(
                f"Failed to read reservation file {self._path}: {e}"
            ) from e
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            raise ReservationBackendError(
                f"Malformed JSON in reservation file {self._path}: {e}"
            ) from e
        try:
            return ReservationFile.model_validate(data)
        except ValidationError as e:
            raise ReservationBackendError(
                f"Invalid reservation file {self._path}: {e}"
            ) from e
```

Update the two query methods to use attribute access (`entry.user`, `entry.resources`) instead of `entry["user"]`. Import `ReservationEntry` alongside `ReservationFile`. Delete the now-unused `_parse_iso8601` helper, the `SUPPORTED_VERSION` constant's hand-checks, and the `cast` import if it becomes unused (run `ruff check --fix`).

- [ ] **Step 3: Validate the envelope in `build_backend`**

In `src/otto/reservations/__init__.py`, at the top of `build_backend`, validate the otto-owned envelope so a malformed `backend`/`url` raises a clear error (the open backend sub-table is preserved via the raw `settings` dict, unchanged):

```python
    from ..models.settings import ReservationConfigSpec

    cfg = ReservationConfigSpec.model_validate(settings)
    backend_name = cfg.backend
    url = cfg.url
```

Replace the existing `backend_name = settings.get("backend", "none")` / `url = settings.get("url")` lines with the above. Everything below (the `none`/`json`/dotted-path branches reading `settings.get(...)` for sub-tables) stays unchanged — it still reads the raw `settings` dict for the open sub-table.

- [ ] **Step 4: Adapt the tests**

Update any assertion in `test_json_backend.py` that matched the *exact* old `ReservationBackendError` message text for a structural problem (e.g. "missing string 'user' field", "must be a list of strings", "unsupported version") to assert `pytest.raises(ReservationBackendError)` and match on a stable substring (the file path, or `"Invalid reservation file"`). The *I/O* and *JSON-decode* messages are unchanged. Add a test that a bad envelope (`{"backend": 3}`) raises from `build_backend` if `test_reservation.py` covers envelope parsing.

- [ ] **Step 5: Run + lint**

Run: `pytest tests/unit/reservations tests/unit/cli/test_reservation.py -q --no-cov`
Expected: PASS.
Run: `ruff check --fix src/otto/reservations tests/unit/reservations && ty check src/otto/reservations`
Expected: clean.

- [ ] **Step 6: Stage**

```bash
git add src/otto/reservations/json_backend.py src/otto/reservations/__init__.py tests/unit/reservations tests/unit/cli/test_reservation.py
```

---

## Task 4: `SettingsModel` (`models/settings.py`)

**Why now:** composes the Task 1–2 specs into the single boundary model for the whole settings dict.

**Files:**
- Modify: `src/otto/models/settings.py` (append `SettingsModel` + the `host_defaults` option-spec map + drift-guard pair list)
- Modify: `src/otto/models/__init__.py` (export public models)
- Test: `tests/unit/models/test_settings.py` (extend)

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/models/test_settings.py`:

```python
from otto.models.settings import SettingsModel


def _minimal() -> dict:
    return {"name": "repo1", "version": "1.0.0"}


def test_settings_requires_name_and_version():
    with pytest.raises(ValidationError) as exc:
        SettingsModel.model_validate({"name": "repo1"})  # no version
    assert "version" in str(exc.value)


def test_settings_rejects_bad_version_format():
    with pytest.raises(ValidationError):
        SettingsModel.model_validate({"name": "r", "version": "1.0"})  # not X.Y.Z


def test_settings_allows_legacy_lab_data_type_and_opaque_coverage():
    # both appear in every real fixture but are never consumed by parse_settings
    m = SettingsModel.model_validate({
        **_minimal(),
        "lab_data_type": "json",
        "coverage": {"gcda_remote_dir": "/var/cov", "embedded": {"extension": "cov"}},
    })
    assert m.lab_data_type == "json"
    assert m.coverage == {"gcda_remote_dir": "/var/cov", "embedded": {"extension": "cov"}}


def test_settings_forbids_unknown_top_level_key():
    with pytest.raises(ValidationError) as exc:
        SettingsModel.model_validate({**_minimal(), "labz": []})  # typo: labs
    assert "labz" in str(exc.value)


def test_settings_paths_coerce_to_path_lists():
    m = SettingsModel.model_validate({
        **_minimal(),
        "labs": ["/a/lab"], "libs": ["/a/lib"], "tests": ["/a/tests"],
        "init": ["mod_a"], "valid_labs": ["embedded"],
    })
    assert m.labs == [Path("/a/lab")]
    assert m.init == ["mod_a"]
    assert m.valid_labs == ["embedded"]


def test_settings_host_defaults_validated_but_kept_partial():
    m = SettingsModel.model_validate({
        **_minimal(),
        "host_defaults": {"ssh_options": {"port": 2222}},
    })
    # only the user-set key survives (partial), validated against SshOptionsSpec
    assert m.host_defaults == {"ssh_options": {"port": 2222}}


def test_settings_host_defaults_rejects_unknown_subtable():
    with pytest.raises(ValidationError):
        SettingsModel.model_validate({**_minimal(), "host_defaults": {"sssh_options": {}}})


def test_settings_host_defaults_rejects_bad_option_field():
    with pytest.raises(ValidationError):
        SettingsModel.model_validate({
            **_minimal(),
            "host_defaults": {"ssh_options": {"prot": 22}},  # typo: port
        })


def test_settings_builds_docker_and_os_profiles():
    m = SettingsModel.model_validate({
        **_minimal(),
        "os_profiles": {"zephyr-3.7": {"base": "embedded", "os_version": "3.7"}},
        "docker": {"registry_url": "reg.x"},
    })
    assert m.os_profiles["zephyr-3.7"].base == "embedded"
    assert m.os_profiles["zephyr-3.7"].defaults == {"os_version": "3.7"}
    assert m.docker.to_runtime().registry_url == "reg.x"


def test_settings_validates_every_in_tree_fixture():
    """Every real settings.toml (after ${sut_dir} expansion is a no-op for the
    top-level shape) validates — the regression guard for the extra='forbid' set.
    """
    import tomllib  # py3.11+
    from pathlib import Path as P
    for name in ("repo1", "repo2", "repo3"):
        raw = (P("tests") / name / ".otto" / "settings.toml").read_text()
        # ${sut_dir} left as-is; it doesn't affect top-level key validation
        SettingsModel.model_validate(tomllib.loads(raw))
```

> Note: `tomllib` is 3.11+. The repo's lowest supported Python is 3.10; mirror the codebase pattern `try: import tomllib except ModuleNotFoundError: import tomli as tomllib` (see `tests/unit/test_docs_deps.py`).

- [ ] **Step 2: Run to confirm failure**

Run: `pytest tests/unit/models/test_settings.py -q --no-cov`
Expected: FAIL — `ImportError: cannot import name 'SettingsModel'`.

- [ ] **Step 3: Append `SettingsModel` + supporting map to `src/otto/models/settings.py`**

Add the option-spec imports near the top (sorted by `ruff`):

```python
from .options import (
    FtpOptionsSpec,
    NcOptionsSpec,
    ScpOptionsSpec,
    SftpOptionsSpec,
    SshOptionsSpec,
    TelnetOptionsSpec,
)
```

Append:

```python
# settings.toml version floor: X.Y.Z. Mirrors configmodule.version.version_re;
# duplicated (not imported) so models/ stays free of the configmodule bootstrap.
_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+")

# The six per-protocol option tables accepted under [host_defaults], each mapped
# to the spec that validates it. Keys mirror storage.factory.OPTIONS_KEYS (a
# drift test below keeps them in lockstep).
_HOST_DEFAULT_OPTION_SPECS: dict[str, type[OttoModel]] = {
    "ssh_options": SshOptionsSpec,
    "telnet_options": TelnetOptionsSpec,
    "sftp_options": SftpOptionsSpec,
    "scp_options": ScpOptionsSpec,
    "ftp_options": FtpOptionsSpec,
    "nc_options": NcOptionsSpec,
}


class SettingsModel(OttoModel):
    """Boundary model for a repo's ``.otto/settings.toml`` (post ``${sut_dir}``
    expansion). ``extra='forbid'`` turns a typo'd top-level key into an error.
    """

    # required identity
    name: str
    version: str

    # legacy / passthrough — present in every fixture, consumed by nobody in
    # parse_settings, but must be tolerated under extra='forbid'.
    lab_data_type: str = "json"
    coverage: dict[str, Any] = {}

    # paths + module/name lists
    labs: list[Path] = []
    valid_labs: list[str] = []
    libs: list[Path] = []
    tests: list[Path] = []
    init: list[str] = []

    # structured sub-tables
    host_defaults: dict[str, dict[str, Any]] = {}
    os_profiles: dict[str, OsProfileSpec] = {}
    docker: DockerSettingsSpec = DockerSettingsSpec()
    reservations: ReservationConfigSpec = ReservationConfigSpec()

    @field_validator("version")
    @classmethod
    def _validate_version_format(cls, v: str) -> str:
        if _VERSION_RE.match(v) is None:
            raise ValueError(
                f"version {v!r} must be of the form MAJOR.MINOR.PATCH"
            )
        return v

    @field_validator("host_defaults")
    @classmethod
    def _validate_host_defaults_partial(
        cls, v: dict[str, dict[str, Any]]
    ) -> dict[str, dict[str, Any]]:
        """Validate each ``*_options`` sub-table against its spec (catching
        typos + type errors) but keep only the user-set keys, so the downstream
        per-key precedence merge in storage.factory still applies its defaults.
        """
        result: dict[str, dict[str, Any]] = {}
        for key, table in v.items():
            spec_cls = _HOST_DEFAULT_OPTION_SPECS.get(key)
            if spec_cls is None:
                known = ", ".join(sorted(_HOST_DEFAULT_OPTION_SPECS))
                raise ValueError(
                    f"unknown [host_defaults] sub-table {key!r}. Valid: {known}"
                )
            validated = spec_cls.model_validate(table)
            result[key] = validated.model_dump(exclude_unset=True)
        return result


SETTINGS_DOCKER_RUNTIME_PAIRS: list[tuple[type[OttoModel], type]] = []
"""Populated lazily by the drift test (importing the runtime classes there keeps
this module leaf-clean)."""
```

- [ ] **Step 4: Export the public models from `src/otto/models/__init__.py`**

Add to the imports and `__all__`:

```python
from .settings import (
    DockerComposeSpec,
    DockerImageSpec,
    DockerSettingsSpec,
    OsProfileSpec,
    OttoEnvSettings,   # added in Task 6 — leave this line out until then
    ReservationConfigSpec,
    ReservationEntry,
    ReservationFile,
    SettingsModel,
)
```

> Do **not** add `OttoEnvSettings` to the import/`__all__` yet — it doesn't exist until Task 6. Add the other eight names now; add `OttoEnvSettings` in Task 6.

- [ ] **Step 5: Add the `host_defaults` key-set drift guard**

Append to `tests/unit/models/test_settings.py`:

```python
def test_host_default_option_keys_match_factory_options_keys():
    from otto.models.settings import _HOST_DEFAULT_OPTION_SPECS
    from otto.storage.factory import OPTIONS_KEYS
    assert set(_HOST_DEFAULT_OPTION_SPECS) == OPTIONS_KEYS
```

- [ ] **Step 6: Run + lint**

Run: `pytest tests/unit/models/test_settings.py -q --no-cov`
Expected: PASS (all).
Run: `ruff check --fix src/otto/models/settings.py src/otto/models/__init__.py tests/unit/models/test_settings.py && ty check src/otto/models`
Expected: clean.

- [ ] **Step 7: Stage**

```bash
git add src/otto/models/settings.py src/otto/models/__init__.py tests/unit/models/test_settings.py
```

---

## Task 5: Route `Repo.parse_settings` through `SettingsModel`

**Why now:** `SettingsModel` exists; this makes the runtime *use* it and deletes the three hand-rolled `_parse_*` methods.

**Files:**
- Modify: `src/otto/configmodule/repo.py`
- Test: `tests/unit/configmodule/test_repo.py` (adapt)

- [ ] **Step 1: Read `tests/unit/configmodule/test_repo.py`**

Note which behaviors are asserted (field population, the `[host_defaults]`/`[os_profiles]`/`[docker]` parsing, and any test that asserts the *exact* `ValueError`/`KeyError` message a `_parse_*` method raised). Those error-type/text assertions will change to `pydantic.ValidationError` (a subclass of `ValueError`).

- [ ] **Step 2: Rewrite `parse_settings`**

In `src/otto/configmodule/repo.py`, replace the body of `parse_settings` (keep `self.settings` the raw dict; expand into a separate copy; validate once; populate). Import `SettingsModel` **lazily inside the method** (leaf-bootstrap safety is moot here since repo is already loaded, but the lazy import also avoids a module-top cycle with `models.settings`):

```python
    def parse_settings(self) -> None:
        """Parse + validate the repo's ``.otto/settings.toml`` via SettingsModel."""
        from ..models.settings import SettingsModel

        settingsText = self.read_settings()
        self.settings = tomli.loads(settingsText)  # raw — coverage/reservation read it

        expanded = self._expand_recursive(self.settings)
        model = SettingsModel.model_validate(expanded)

        self.name = model.name
        self.version = Version(model.version)
        self.labs = list(model.labs)
        self.valid_labs = list(model.valid_labs)
        self.libs = list(model.libs)
        self.tests = list(model.tests)
        self.init = list(model.init)
        self.host_defaults = {k: dict(v) for k, v in model.host_defaults.items()}
        self.os_profiles = self._register_os_profiles(model.os_profiles)
        self.docker_settings = model.docker.to_runtime()
```

- [ ] **Step 3: Replace `_parse_os_profiles` with a registration loop; delete `_parse_host_defaults` and `_parse_docker_settings`**

Delete `_parse_host_defaults` and `_parse_docker_settings` entirely (their validation now lives in `SettingsModel`/`DockerSettingsSpec`). Replace `_parse_os_profiles` with a thin registration helper that consumes the validated specs (the per-field slot typo guard still runs inside `register_os_profile`):

```python
    def _register_os_profiles(
        self, profiles: dict[str, 'OsProfileSpec'],
    ) -> dict[str, 'OsProfile']:
        """Register each validated os-profile into the global registry and
        return the built profiles, keyed by name. Runs at settings-parse time,
        before init modules import, so a code registration can override a data
        table of the same name (last writer wins)."""
        from ..host.os_profile import build_os_profile, register_os_profile

        result: dict[str, OsProfile] = {}
        for name, prof in profiles.items():
            try:
                register_os_profile(name, prof.base, prof.defaults)
            except ValueError as e:
                raise ValueError(
                    f"{TOML_SETTINGS_PATH}: [os_profiles.{name}]: {e}"
                ) from e
            result[name] = build_os_profile(name)
        return result
```

Add `OsProfileSpec` to the `TYPE_CHECKING` block at the top of `repo.py`:

```python
if TYPE_CHECKING:
    ...
    from ..host.os_profile import OsProfile
    from ..models.settings import OsProfileSpec
```

Delete the now-unused `from ..storage.factory import OPTIONS_KEYS` import that lived inside the deleted `_parse_host_defaults` (it moved into `models/settings.py`). Keep `_expand_recursive`, `_expand_string`, `reservation_settings`, the `Docker*` dataclasses, and everything else.

- [ ] **Step 4: Adapt `test_repo.py`**

For each test asserting a `_parse_*` `ValueError`/`KeyError` *message*: keep `pytest.raises(ValueError)` where possible (`ValidationError` subclasses `ValueError`) and match on a stable substring, or switch to `pytest.raises(ValidationError)`. A missing-`name`/`version` test that expected `KeyError` becomes `pytest.raises(ValidationError)`. Field-population tests should pass unchanged.

- [ ] **Step 5: Run the config + storage + docker + os-profile suites**

Run: `pytest tests/unit/configmodule tests/unit/storage tests/unit/host/test_os_profile.py tests/unit/docker -q --no-cov`
Expected: PASS. (Watch for any test that fed a now-rejected typo'd key and was silently tolerated before — that is the intended `extra='forbid'` behavior change; migrate the assertion, as Plan 2b did for `test_options.py`.)

- [ ] **Step 6: Lint + type-check**

Run: `ruff check --fix src/otto/configmodule/repo.py tests/unit/configmodule/test_repo.py && ty check src/otto/configmodule/repo.py`
Expected: clean.

- [ ] **Step 7: Stage**

```bash
git add src/otto/configmodule/repo.py tests/unit/configmodule/test_repo.py
```

---

## Task 6: `OttoEnvSettings` (pydantic-settings) + rewire the `OTTO_*` reads

> **DESIGN CALLOUT (decide at plan review).** Chris chose **full rewire** for the env surface. This task rewires every **direct / ad-hoc** `OTTO_*` read (`OttoEnv()` `sut_dirs`, `cli/main.py` `_field_default`, `docker/compose.py` `OTTO_COMPOSE_SUFFIX`, `completion_cache.py` `OTTO_XDIR`) through one typed `OttoEnvSettings`. It **keeps Typer's `envvar=`** on the six CLI options in `cli/main.py` (`--lab`, `--xdir`, `--field/--debug`, `--log-days`, `--log-level`, `--rich-log-file`): Typer is the *structured, parse-time* reader there (correct flag > env > default precedence, `--help` documentation), not an ad-hoc read, and routing those defaults through `get_env()` at import time risks precedence/`--help`/import-order regressions (notably the `--field/--debug` toggle, which is driven by *two* vars: `OTTO_FIELD_PRODUCTS` for the option + `OTTO_FIELD_DEFAULT` for its default). The env var **names** stay single-sourced in `env.py` and are shared by both Typer and the model's aliases. **If you want the CLI options driven from the model too, say so at review and this task expands to do that.**

**Files:**
- Modify: `src/otto/configmodule/env.py`
- Modify: `src/otto/configmodule/__init__.py`, `src/otto/cli/main.py`, `src/otto/docker/compose.py`, `src/otto/configmodule/completion_cache.py`
- Modify: `src/otto/models/settings.py` (`OttoEnvSettings`), `src/otto/models/__init__.py`
- Test: `tests/unit/configmodule/test_env.py` (adapt)

- [ ] **Step 1: Write the failing env-model tests**

Append to `tests/unit/models/test_settings.py`:

```python
def test_otto_env_settings_defaults(monkeypatch):
    for var in ("OTTO_SUT_DIRS", "OTTO_LAB", "OTTO_XDIR", "OTTO_COMPOSE_SUFFIX",
                "OTTO_FIELD_DEFAULT", "OTTO_LOG_DAYS"):
        monkeypatch.delenv(var, raising=False)
    from otto.models.settings import OttoEnvSettings
    env = OttoEnvSettings()
    assert env.sut_dirs == []
    assert env.lab is None
    assert env.compose_suffix is None
    assert env.field_default is None
    assert env.log_days == 30


def test_otto_env_settings_reads_prefixed_vars(monkeypatch, tmp_path):
    monkeypatch.setenv("OTTO_SUT_DIRS", str(tmp_path))
    monkeypatch.setenv("OTTO_COMPOSE_SUFFIX", "ci")
    from otto.models.settings import OttoEnvSettings
    env = OttoEnvSettings()
    assert env.sut_dirs == [tmp_path]
    assert env.compose_suffix == "ci"


def test_otto_env_settings_splits_sut_dirs_on_comma_and_pathsep(monkeypatch, tmp_path):
    import os
    a = tmp_path / "a"; b = tmp_path / "b"; a.mkdir(); b.mkdir()
    monkeypatch.setenv("OTTO_SUT_DIRS", f"{a}{os.pathsep}{b}")
    from otto.models.settings import OttoEnvSettings
    assert OttoEnvSettings().sut_dirs == [a, b]
```

And update `tests/unit/configmodule/test_env.py`: keep the env-var-name constant tests (external interface — `LAB_ENV_VAR == 'OTTO_LAB'`, etc.). Replace `OttoEnv()` construction with `load_otto_env()`; the existence tests (`test_env_sutdirs_set_to_one_path_that_does_not_exist`, `..._multiple_paths_one_does_not_exist`) keep `pytest.raises(FileNotFoundError)` against `load_otto_env()`.

- [ ] **Step 2: Run to confirm failure**

Run: `pytest tests/unit/models/test_settings.py -k otto_env -q --no-cov`
Expected: FAIL — `ImportError: cannot import name 'OttoEnvSettings'`.

- [ ] **Step 3: Add `OttoEnvSettings` to `models/settings.py`**

At the top, add the pydantic-settings imports and the path-split regex (mirror `env.py`):

```python
import os
from pydantic_settings import BaseSettings, SettingsConfigDict
```

Append:

```python
# Split OTTO_SUT_DIRS on comma OR the OS path separator (':' on Linux), matching
# the historical configmodule.env behavior.
_PATH_LIST_SEP = re.compile(rf"[,{re.escape(os.pathsep)}]")


class OttoEnvSettings(BaseSettings):
    """Typed view of the ``OTTO_*`` environment surface. Single source of truth
    for the variables otto reads programmatically (the CLI-option vars are read
    by Typer's ``envvar=`` at parse time; their names are shared via env.py).

    Existence-checking of ``sut_dirs`` is done by ``configmodule.env.load_otto_env``
    so a missing dir raises ``FileNotFoundError`` (not a wrapped ValidationError).
    """

    model_config = SettingsConfigDict(env_prefix="OTTO_", extra="ignore")

    sut_dirs: list[Path] = []
    lab: str | None = None
    xdir: Path | None = None
    log_days: int = 30
    log_level: str = "INFO"
    log_rich: bool = False
    field_default: str | None = None
    field_products: str | None = None
    compose_suffix: str | None = None

    @field_validator("sut_dirs", mode="before")
    @classmethod
    def _split_path_list(cls, v: object) -> object:
        if isinstance(v, str):
            return [p for p in _PATH_LIST_SEP.split(v) if p]
        return v
```

Add `OttoEnvSettings` to the `models/__init__.py` import/`__all__` (the line deferred in Task 4).

- [ ] **Step 4: Replace the `OttoEnv` dataclass in `env.py`**

In `src/otto/configmodule/env.py`, keep the env-var-name constants (external interface) and `validate_path`. Delete the `OttoEnv` dataclass + its `get_env_*` classmethods. Re-export the model and add a `load_otto_env()` factory that preserves the `FileNotFoundError` contract:

```python
from ..models.settings import OttoEnvSettings as OttoEnvSettings


def load_otto_env() -> OttoEnvSettings:
    """Construct the env settings and validate that every sut_dir exists,
    raising FileNotFoundError (the historical OttoEnv() contract)."""
    env = OttoEnvSettings()
    for path in env.sut_dirs:
        validate_path(path, must_exist=True)
    return env
```

> `validate_path` is currently a `classmethod` of `OttoEnv`. Move it to a module-level function in `env.py` (same body) so `load_otto_env` and tests can call it.

- [ ] **Step 5: Rewire the four consumers**

- `src/otto/configmodule/__init__.py:46`: `_env = OttoEnv()` → `_env = load_otto_env()`. Update the import from `.env` to bring in `load_otto_env` (and re-export `OttoEnvSettings` in place of `OttoEnv` if anything imported the old name — grep first). `get_env()` now returns `OttoEnvSettings`.
- `src/otto/cli/main.py:43`: `_field_default = OttoEnv.get_env_var(FIELD_DEFAULT_ENV_VAR) is not None` → `from ..configmodule import get_env` then `_field_default = get_env().field_default is not None`. Remove the now-unused `OttoEnv` import; keep the env-var-name constant imports (still used by the Typer `envvar=`).
- `src/otto/docker/compose.py:72`: `os.environ.get("OTTO_COMPOSE_SUFFIX")` → `get_env().compose_suffix`. Import `get_env` from `..configmodule`.
- `src/otto/configmodule/completion_cache.py:122`: `os.environ.get(XDIR_ENV_VAR)` → `get_env().xdir` (note: `xdir` is `Path | None`; adapt the downstream `str` usage — wrap with `str(...)` or keep `os.environ.get` here if it sits on the completion fast path where importing `get_env` would be circular. Check the import graph; if `completion_cache` is imported by `configmodule/__init__` *before* `_env` is built, read the var directly here and leave a comment. Prefer the model, fall back to a direct read only if the import order forbids it.)

- [ ] **Step 6: Run env + the rewired consumers + a broad smoke**

Run: `pytest tests/unit/configmodule tests/unit/models/test_settings.py tests/unit/docker -q --no-cov`
Expected: PASS.
Run: `python -c "import otto.cli.main; import otto.configmodule; import otto.docker.compose; print('import ok')"` (with `OTTO_SUT_DIRS` unset) — confirms no import-order/circular regression.

- [ ] **Step 7: Lint + type-check**

Run: `ruff check --fix src/otto/models/settings.py src/otto/configmodule src/otto/cli/main.py src/otto/docker/compose.py tests/unit/configmodule/test_env.py && ty check src/otto/models src/otto/configmodule`
Expected: clean.

- [ ] **Step 8: Stage**

```bash
git add src/otto/models/settings.py src/otto/models/__init__.py src/otto/configmodule/env.py src/otto/configmodule/__init__.py src/otto/cli/main.py src/otto/docker/compose.py src/otto/configmodule/completion_cache.py tests/unit/configmodule/test_env.py tests/unit/models/test_settings.py
```

---

## Final verification (after all tasks — the subagent-driven flow's holistic review)

- [ ] **Targeted hermetic surface green:**

```bash
pytest tests/unit/models tests/unit/configmodule tests/unit/storage tests/unit/reservations \
       tests/unit/docker tests/unit/host/test_os_profile.py tests/unit/cli/test_reservation.py \
       -q --no-cov
```

- [ ] **Whole-repo import smoke (no bootstrap regression):** `python -c "import otto"` and `python -c "import otto.models.settings"` (the latter must NOT trigger the configmodule bootstrap — if it prints repo-scanning side effects, the leaf-isolation rule was violated; find the offending module-top `configmodule` import).

- [ ] **Static gates:** `ty check src/otto` (0 diagnostics) and `ruff check src/otto tests` (clean; note the one pre-existing `E501` at `remote_host.py:334` predates Phase A).

- [ ] **Drift guards present:** `test_host_default_option_keys_match_factory_options_keys` (host_defaults keys == `OPTIONS_KEYS`) and the docker/reservation round-trip tests.

- [ ] **Hand-off to Chris (stage-only):** summarize the staged files + the **intended behavior changes** to flag for `make test` / `make coverage` / `make nox`:
  1. `SettingsModel` `extra='forbid'` now **rejects an unmodeled top-level `settings.toml` key** (was silently ignored). The in-tree scan covers `name/version/lab_data_type/labs/valid_labs/libs/tests/init/host_defaults/os_profiles/docker/reservations/coverage`; a real downstream repo with another key will surface here — that's the point, but it needs a real-lab run to confirm none are missing.
  2. `host_defaults` / `os_profiles` / `docker` / reservation typos now raise `pydantic.ValidationError` (subclass of `ValueError`) instead of the old bespoke `ValueError`/`KeyError` messages.
  3. The env surface is now one `OttoEnvSettings`; Typer `envvar=` on the six CLI options is **retained** (see the Task 6 callout) — confirm that's the desired boundary.
- [ ] **Then:** `superpowers:finishing-a-development-branch` is **not** run by the agent — Chris commits the staged work and runs the pre-merge gate (`make test`, `make coverage` ≥ 90%, `make nox`). No `make docs` needed unless a docstring/RST referenced here changed (this plan touches none).

---

## Self-review (done while authoring — recorded for the executor)

- **Spec coverage (section 4):** `SettingsModel` ✓ (replaces `_parse_host_defaults`/`_parse_os_profiles`/`_parse_docker_settings`); `${sut_dir}` pre-pass ✓ (Rule 3); `host_defaults` partial via `exclude_unset` ✓ (Task 4); docker spec models ✓ (Task 1); `os_profiles` → `OsProfileSpec` ✓ (Task 2/5); reservation envelope (`ReservationConfigSpec`, open sub-table) ✓ + JSON-backend-file model (`ReservationFile`) ✓ (Tasks 2/3); `OTTO_*` → `pydantic-settings` ✓ (Task 6). Monitor records (§5), JSON Schema export (§6), and the spike report (§7) are **out of scope** for Plan 3 — they are Plans 4 and 5.
- **Leaf isolation:** every `configmodule` import in `models/settings.py` is lazy-inside-`to_runtime()` or `TYPE_CHECKING`; the `version` check uses a local regex; verified by the import-smoke step.
- **`extra='forbid'` real-key set:** derived from all in-tree fixtures + every `settings.get`/`settings[` read; `lab_data_type` + `coverage` explicitly allowed.
- **Type consistency:** `to_runtime()` on every `*Spec`; `DockerSettingsSpec.to_runtime()` returns the unchanged frozen `DockerSettings`; `OsProfileSpec.defaults` is the collected `model_extra`; `OttoEnvSettings` field names map to `OTTO_*` via `env_prefix`.

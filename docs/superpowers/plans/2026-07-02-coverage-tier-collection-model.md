# Coverage Tier & Collection Model Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the approved spec `docs/superpowers/specs/2026-07-02-coverage-tier-collection-model-design.md`: declarative coverage tiers in settings.toml, a per-board `capture.json` artifact, a single `otto cov get` retrieval command, a hunk remap engine for dirty-tree captures, blob-anchored manual-capture validity at report time, and tier-colored rendering with a legend.

**Architecture:** A new `otto/coverage/capture/` package holds git plumbing, the hunk remap engine, the `CaptureFile` model, the manual store dir, and per-board capture production. Report-time additions live in `otto/coverage/validity.py` (manual anchor chain), `otto/coverage/exclusions.py` (marker scan), and `otto/coverage/colors.py` (palette + validation). The existing `CoverageStore`/`LcovMerger`/`HtmlRenderer` grow additively. `SettingsModel.coverage` becomes typed.

**Tech Stack:** Python 3.10+, pydantic v2, Typer 0.26, Jinja2 renderer, git via `subprocess`, lcov/gcov toolchains (existing machinery). No new dependencies.

## Global Constraints

- **NEVER** add `from __future__ import annotations` — repo-wide ban (breaks the Sphinx `-W` nitpicky gate). Use real 3.10+ annotations with module-top imports.
- ruff runs with `select = ALL` minus a curated deny-list. After every implementation step run `ruff check <changed files>` and `ruff format <changed files>`, then **re-run `ruff check`** (format is not lint-neutral).
- `ty` runs only at `nox -s typecheck`; budget a typecheck round after src edits (final task).
- The import-budget guard (`tests/unit/import_budget/`, golden snapshots) fails if `import otto` or bare `otto --help` grows eager imports. All new heavy imports (pydantic capture model, git subprocess helpers) must be imported **lazily inside command function bodies** in `src/otto/cli/cov.py` and `src/otto/cli/test.py` — never at module top of CLI modules beyond what exists.
- Tests: unit tests live under `tests/unit/cov/` (hostless by construction — no lab, no network). Use `tmp_path` for scratch git repos; NEVER create files inside the checkout (`feedback_no_destructive_tests_in_dev_repo`). No `-n 8` xdist storms; run scoped `pytest` invocations.
- After changing `src/otto/models/settings.py`, run `make schema` and commit the regenerated schema artifacts with the same task.
- Commit per task in the worktree with conventional-commit messages; `git add` **explicit paths only** (never `git add -u`). Do not push; do not merge to main.
- The repo's `prepare-commit-msg` hook cannot prompt without a TTY, so EVERY commit message must end with these two trailer lines (blank line before them): `Assisted-by: Claude Fable 5` and `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.
- The full gates (`make coverage`, `make nox`, live e2e in `tests/e2e/cov/`) touch the lab bed — **do not run them**; they are deferred pending user go-ahead (another agent owns the lab). Per-task verification = scoped pytest + ruff. Final task runs `nox -s typecheck`, `nox -s lint`, `make docs`, and the hostless test tree only.
- Spec is authoritative for semantics. `TIER_SYSTEM = "system"` remains the default e2e tier name. Capture JSON schema version is `1`. `max_age` format is `"<N>d"` (days only).

---

### Task 1: Color palette + typed coverage settings

**Files:**
- Create: `src/otto/coverage/colors.py`
- Create: `tests/unit/cov/test_colors.py`
- Modify: `src/otto/models/settings.py` (replace `coverage: dict[str, Any]` field, add spec classes above `SettingsModel`)
- Test: `tests/unit/models/test_settings_coverage.py` (create)

**Interfaces:**
- Produces: `otto.coverage.colors.validate_color(value: str) -> str` (returns the value, raises `ValueError` on bad color), `DEFAULT_TIER_COLORS: dict[str, str]` (`{"e2e": "green", "unit": "yellow", "manual": "orange"}`), `STATE_COLORS: dict[str, str]` (`{"uncovered": "#f4a9a8", "excluded": "grey", "stale": "violet", "aging": "tan"}`), `CSS_COLOR_NAMES: frozenset[str]`.
- Produces: `otto.models.settings.CoverageTierSpec` (fields: `kind: Literal["e2e","unit","manual"]`, `precedence: int`, `color: str | None = None`, `harvest_dirs: list[Path] = []`, `max_age: str | None = None`), `CoverageExclusionsSpec` (`markers: list[str] = []`), `CoverageSettingsSpec` (`hosts: str | None`, `gcda_remote_dir: str = ""`, `embedded: dict[str, Any] = {}`, `tiers: dict[str, CoverageTierSpec] = {}`, `exclusions: CoverageExclusionsSpec = CoverageExclusionsSpec()`), and `SettingsModel.coverage: CoverageSettingsSpec`.

- [ ] **Step 1: Write failing tests for colors**

```python
# tests/unit/cov/test_colors.py
"""Color validation for coverage tier config."""

import pytest

from otto.coverage.colors import (
    CSS_COLOR_NAMES,
    DEFAULT_TIER_COLORS,
    STATE_COLORS,
    validate_color,
)


def test_hex_colors_accepted() -> None:
    assert validate_color("#22c55e") == "#22c55e"
    assert validate_color("#ABC123") == "#ABC123"


def test_named_colors_accepted() -> None:
    assert validate_color("green") == "green"
    assert validate_color("tan") == "tan"
    assert validate_color("Violet") == "Violet"  # case-insensitive lookup


@pytest.mark.parametrize("bad", ["#22c55", "#GGGGGG", "notacolor", "", "22c55e"])
def test_bad_colors_rejected(bad: str) -> None:
    with pytest.raises(ValueError, match="color"):
        validate_color(bad)


def test_default_palette_is_valid() -> None:
    for value in (*DEFAULT_TIER_COLORS.values(), *STATE_COLORS.values()):
        assert validate_color(value) == value


def test_css_names_include_basics() -> None:
    assert {"green", "yellow", "orange", "grey", "violet", "tan"} <= CSS_COLOR_NAMES
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/cov/test_colors.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'otto.coverage.colors'`

- [ ] **Step 3: Implement `src/otto/coverage/colors.py`**

```python
"""Tier/state color palette and validation for coverage rendering.

Colors come from ``[coverage.tiers.<name>] color`` in settings.toml and
may be a CSS named color or a ``#RRGGBB`` hex code.  Validation happens
at settings load; the renderer consumes values verbatim.
"""

import re

_HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")

# The CSS Color Module Level 4 extended named colors (lowercase).
CSS_COLOR_NAMES: frozenset[str] = frozenset({
    "aliceblue", "antiquewhite", "aqua", "aquamarine", "azure", "beige",
    "bisque", "black", "blanchedalmond", "blue", "blueviolet", "brown",
    "burlywood", "cadetblue", "chartreuse", "chocolate", "coral",
    "cornflowerblue", "cornsilk", "crimson", "cyan", "darkblue", "darkcyan",
    "darkgoldenrod", "darkgray", "darkgreen", "darkgrey", "darkkhaki",
    "darkmagenta", "darkolivegreen", "darkorange", "darkorchid", "darkred",
    "darksalmon", "darkseagreen", "darkslateblue", "darkslategray",
    "darkslategrey", "darkturquoise", "darkviolet", "deeppink", "deepskyblue",
    "dimgray", "dimgrey", "dodgerblue", "firebrick", "floralwhite",
    "forestgreen", "fuchsia", "gainsboro", "ghostwhite", "gold", "goldenrod",
    "gray", "green", "greenyellow", "grey", "honeydew", "hotpink", "indianred",
    "indigo", "ivory", "khaki", "lavender", "lavenderblush", "lawngreen",
    "lemonchiffon", "lightblue", "lightcoral", "lightcyan",
    "lightgoldenrodyellow", "lightgray", "lightgreen", "lightgrey",
    "lightpink", "lightsalmon", "lightseagreen", "lightskyblue",
    "lightslategray", "lightslategrey", "lightsteelblue", "lightyellow",
    "lime", "limegreen", "linen", "magenta", "maroon", "mediumaquamarine",
    "mediumblue", "mediumorchid", "mediumpurple", "mediumseagreen",
    "mediumslateblue", "mediumspringgreen", "mediumturquoise",
    "mediumvioletred", "midnightblue", "mintcream", "mistyrose", "moccasin",
    "navajowhite", "navy", "oldlace", "olive", "olivedrab", "orange",
    "orangered", "orchid", "palegoldenrod", "palegreen", "paleturquoise",
    "palevioletred", "papayawhip", "peachpuff", "peru", "pink", "plum",
    "powderblue", "purple", "rebeccapurple", "red", "rosybrown", "royalblue",
    "saddlebrown", "salmon", "sandybrown", "seagreen", "seashell", "sienna",
    "silver", "skyblue", "slateblue", "slategray", "slategrey", "snow",
    "springgreen", "steelblue", "tan", "teal", "thistle", "tomato",
    "turquoise", "violet", "wheat", "white", "whitesmoke", "yellow",
    "yellowgreen",
})

# Per-kind defaults when a tier declares no explicit color (spec §9).
DEFAULT_TIER_COLORS: dict[str, str] = {
    "e2e": "green",
    "unit": "yellow",
    "manual": "orange",
}

# Non-tier line states (spec §9). "uncovered" is a light red.
STATE_COLORS: dict[str, str] = {
    "uncovered": "#f4a9a8",
    "excluded": "grey",
    "stale": "violet",
    "aging": "tan",
}


def validate_color(value: str) -> str:
    """Return *value* if it is a valid CSS named color or #RRGGBB hex.

    Raises:
        ValueError: if the value is neither.
    """
    if _HEX_RE.match(value):
        return value
    if value.lower() in CSS_COLOR_NAMES:
        return value
    raise ValueError(
        f"invalid color {value!r}: use a CSS color name or #RRGGBB hex"
    )
```

- [ ] **Step 4: Run colors tests — expect PASS**

Run: `uv run pytest tests/unit/cov/test_colors.py -v`
Expected: all PASS.

- [ ] **Step 5: Write failing tests for the typed settings**

```python
# tests/unit/models/test_settings_coverage.py
"""Typed [coverage] settings: tiers, colors, exclusions."""

import pytest
from pydantic import ValidationError

from otto.models.settings import SettingsModel

BASE = {"name": "demo", "version": "1.0.0"}


def _settings(coverage: dict) -> SettingsModel:
    return SettingsModel.model_validate({**BASE, "coverage": coverage})


def test_empty_coverage_still_valid() -> None:
    s = SettingsModel.model_validate(BASE)
    assert s.coverage.tiers == {}


def test_legacy_keys_survive_typing() -> None:
    s = _settings(
        {
            "hosts": "cov_.*",
            "gcda_remote_dir": "/tmp/gcda",
            "embedded": {"extension": "cov_ext", "builds": {"3.7": {"build_dir": "b"}}},
        }
    )
    assert s.coverage.hosts == "cov_.*"
    assert s.coverage.embedded["builds"]["3.7"]["build_dir"] == "b"


def test_tiers_parse_with_defaults() -> None:
    s = _settings(
        {
            "tiers": {
                "system": {"kind": "e2e", "precedence": 1},
                "unit": {"kind": "unit", "precedence": 2, "harvest_dirs": ["build"]},
                "manual": {"kind": "manual", "precedence": 3, "max_age": "180d"},
            }
        }
    )
    assert s.coverage.tiers["system"].kind == "e2e"
    assert s.coverage.tiers["unit"].harvest_dirs[0].name == "build"
    assert s.coverage.tiers["manual"].max_age == "180d"


def test_bad_color_rejected() -> None:
    with pytest.raises(ValidationError, match="color"):
        _settings({"tiers": {"system": {"kind": "e2e", "precedence": 1, "color": "nope"}}})


def test_good_colors_accepted() -> None:
    s = _settings(
        {"tiers": {"system": {"kind": "e2e", "precedence": 1, "color": "#112233"}}}
    )
    assert s.coverage.tiers["system"].color == "#112233"


def test_bad_max_age_rejected() -> None:
    with pytest.raises(ValidationError, match="max_age"):
        _settings({"tiers": {"manual": {"kind": "manual", "precedence": 1, "max_age": "6mo"}}})


def test_bad_kind_rejected() -> None:
    with pytest.raises(ValidationError):
        _settings({"tiers": {"x": {"kind": "smoke", "precedence": 1}}})


def test_exclusion_markers() -> None:
    s = _settings({"exclusions": {"markers": ["MYPROJ_NO_COV"]}})
    assert s.coverage.exclusions.markers == ["MYPROJ_NO_COV"]
```

- [ ] **Step 6: Run to verify failure**

Run: `uv run pytest tests/unit/models/test_settings_coverage.py -v`
Expected: FAIL — `s.coverage` is a plain dict today (`AttributeError: 'dict' object has no attribute 'tiers'`).

- [ ] **Step 7: Implement the typed settings**

In `src/otto/models/settings.py`, add above `SettingsModel` (following the existing `*Spec` class style in that file, all based on `OttoModel`):

```python
_MAX_AGE_RE = re.compile(r"^\d+d$")


class CoverageTierSpec(OttoModel):
    """One ``[coverage.tiers.<name>]`` block: a declared coverage tier."""

    kind: Literal["e2e", "unit", "manual"]
    precedence: int
    color: str | None = None
    harvest_dirs: list[Path] = Field(default_factory=list)
    max_age: str | None = None

    @field_validator("color")
    @classmethod
    def _validate_color(cls, v: str | None) -> str | None:
        if v is None:
            return v
        from ..coverage.colors import validate_color

        return validate_color(v)

    @field_validator("max_age")
    @classmethod
    def _validate_max_age(cls, v: str | None) -> str | None:
        if v is not None and _MAX_AGE_RE.match(v) is None:
            raise ValueError(f"max_age {v!r} must be '<days>d', e.g. '180d'")
        return v


class CoverageExclusionsSpec(OttoModel):
    """``[coverage.exclusions]`` — extra exclusion-marker strings."""

    markers: list[str] = Field(default_factory=list)


class CoverageSettingsSpec(OttoModel):
    """Typed ``[coverage]`` table (was a free-form dict).

    ``embedded`` stays a passthrough dict because its ``builds.<version>``
    sub-tables carry dynamic version keys.
    """

    hosts: str | None = None
    gcda_remote_dir: str = ""
    embedded: dict[str, Any] = Field(default_factory=dict)
    tiers: dict[str, CoverageTierSpec] = Field(default_factory=dict)
    exclusions: CoverageExclusionsSpec = CoverageExclusionsSpec()
```

Then change the `SettingsModel.coverage` field (line ~253) from

```python
    coverage: dict[str, Any] = Field(default_factory=dict)
```

to

```python
    coverage: CoverageSettingsSpec = CoverageSettingsSpec()
```

**Compatibility check (do not skip):** `src/otto/cli/test.py` reads the runtime dict via `repo.settings["coverage"]` (`_get_cov_config`, line ~1165) and `repo.settings.get("coverage")`. Find how `Repo.settings` is produced from `SettingsModel` (in `src/otto/configmodule/repo.py`, search `SettingsModel` / `model_dump`). If `repo.settings` holds the model-dumped dict, `CoverageSettingsSpec` dumps back to a dict and `_get_cov_config` keeps working, **but** `settings.get("coverage")` truthiness changes: an absent `[coverage]` table now dumps as `{"hosts": None, "gcda_remote_dir": "", "embedded": {}, "tiers": {}, "exclusions": {"markers": []}}`, which is truthy. Preserve the old behavior by making `_get_cov_repo` treat a config with no `gcda_remote_dir`, no `embedded`, no `tiers`, and no `hosts` as absent:

```python
def _has_cov_config(cov: dict[str, Any]) -> bool:
    """True when the repo actually declared coverage settings."""
    return bool(
        cov.get("gcda_remote_dir") or cov.get("embedded") or cov.get("tiers") or cov.get("hosts")
    )
```

and use it in `_get_cov_repo` / `_get_cov_config` in place of the bare truthiness test.

- [ ] **Step 8: Run settings tests and the existing suites that guard this file**

Run: `uv run pytest tests/unit/models/test_settings_coverage.py tests/unit/models -v -x` then `uv run pytest tests/unit/cli/test_cov.py tests/unit/cov -q`
Expected: all PASS. If existing settings tests assert `coverage == {}`, update them to the new spec default.

- [ ] **Step 9: Regenerate the exported schema**

Run: `make schema`
Expected: schema JSON regenerated with the new coverage sub-schema; `git status` shows the schema artifact(s) changed.

- [ ] **Step 10: Lint + commit**

```bash
ruff check src/otto/coverage/colors.py src/otto/models/settings.py src/otto/cli/test.py tests/unit/cov/test_colors.py tests/unit/models/test_settings_coverage.py && ruff format <same files> && ruff check <same files>
git add src/otto/coverage/colors.py src/otto/models/settings.py src/otto/cli/test.py tests/unit/cov/test_colors.py tests/unit/models/test_settings_coverage.py <schema artifacts>
git commit -m "feat(cov): typed [coverage] settings with declarative tiers, colors, exclusions"
```

---

### Task 2: Runtime tier accessor

**Files:**
- Create: `src/otto/coverage/tiers.py`
- Test: `tests/unit/cov/test_tiers.py`

**Interfaces:**
- Consumes: `CoverageSettingsSpec`-shaped dicts (what `repo.settings["coverage"]` holds at runtime).
- Produces: `TierConfig` dataclass (`name: str`, `kind: str`, `precedence: int`, `color: str`, `harvest_dirs: list[Path]`, `max_age_days: int | None`) and `load_tiers(cov_config: dict[str, Any]) -> list[TierConfig]` (sorted by precedence; empty/missing `tiers` → the implicit `[TierConfig(name="system", kind="e2e", precedence=1, color="green", harvest_dirs=[], max_age_days=None)]`), plus `resolve_get_tier(tiers: list[TierConfig], name: str | None) -> TierConfig` (None → the sole e2e tier; raises `ValueError` naming candidates when ambiguous or unknown).

- [ ] **Step 1: Write failing tests**

```python
# tests/unit/cov/test_tiers.py
"""Runtime tier config parsing from the settings dict."""

import pytest

from otto.coverage.tiers import TierConfig, load_tiers, resolve_get_tier


def test_default_when_unconfigured() -> None:
    tiers = load_tiers({})
    assert [t.name for t in tiers] == ["system"]
    assert tiers[0].kind == "e2e"
    assert tiers[0].color == "green"


def test_load_sorted_by_precedence_with_default_colors() -> None:
    cov = {
        "tiers": {
            "manual": {"kind": "manual", "precedence": 3, "max_age": "180d"},
            "system": {"kind": "e2e", "precedence": 1},
            "unit": {"kind": "unit", "precedence": 2, "harvest_dirs": ["build"]},
        }
    }
    tiers = load_tiers(cov)
    assert [t.name for t in tiers] == ["system", "unit", "manual"]
    assert [t.color for t in tiers] == ["green", "yellow", "orange"]
    assert tiers[2].max_age_days == 180


def test_explicit_color_wins() -> None:
    cov = {"tiers": {"system": {"kind": "e2e", "precedence": 1, "color": "#112233"}}}
    assert load_tiers(cov)[0].color == "#112233"


def test_resolve_default_is_sole_e2e() -> None:
    tiers = load_tiers({})
    assert resolve_get_tier(tiers, None).name == "system"


def test_resolve_ambiguous_e2e_raises() -> None:
    cov = {
        "tiers": {
            "sys_a": {"kind": "e2e", "precedence": 1},
            "sys_b": {"kind": "e2e", "precedence": 2},
        }
    }
    with pytest.raises(ValueError, match="sys_a.*sys_b|sys_b.*sys_a"):
        resolve_get_tier(load_tiers(cov), None)


def test_resolve_unknown_name_raises() -> None:
    with pytest.raises(ValueError, match="nope"):
        resolve_get_tier(load_tiers({}), "nope")
```

- [ ] **Step 2: Run — expect ModuleNotFoundError**

Run: `uv run pytest tests/unit/cov/test_tiers.py -v`

- [ ] **Step 3: Implement `src/otto/coverage/tiers.py`**

```python
"""Runtime access to declarative ``[coverage.tiers]`` config.

Settings are validated at the boundary by
:class:`otto.models.settings.CoverageSettingsSpec`; at runtime the repo
exposes plain dicts.  This module turns those dicts into ordered
:class:`TierConfig` values for the CLI and reporter.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .colors import DEFAULT_TIER_COLORS
from .store.model import TIER_SYSTEM


@dataclass(frozen=True)
class TierConfig:
    """One declared coverage tier, ready for runtime use."""

    name: str
    kind: str
    precedence: int
    color: str
    harvest_dirs: list[Path] = field(default_factory=list)
    max_age_days: int | None = None


def _tier_from_entry(name: str, entry: dict[str, Any]) -> TierConfig:
    kind = entry["kind"]
    max_age = entry.get("max_age")
    return TierConfig(
        name=name,
        kind=kind,
        precedence=int(entry["precedence"]),
        color=entry.get("color") or DEFAULT_TIER_COLORS[kind],
        harvest_dirs=[Path(p) for p in entry.get("harvest_dirs") or []],
        max_age_days=int(max_age[:-1]) if max_age else None,
    )


def load_tiers(cov_config: dict[str, Any]) -> list[TierConfig]:
    """Ordered tier configs (highest precedence first).

    An empty/missing ``tiers`` table yields the implicit ``system`` tier
    only — identical to pre-tier behavior.
    """
    raw = cov_config.get("tiers") or {}
    if not raw:
        return [
            TierConfig(
                name=TIER_SYSTEM,
                kind="e2e",
                precedence=1,
                color=DEFAULT_TIER_COLORS["e2e"],
            )
        ]
    tiers = [_tier_from_entry(name, entry) for name, entry in raw.items()]
    return sorted(tiers, key=lambda t: t.precedence)


def resolve_get_tier(tiers: list[TierConfig], name: str | None) -> TierConfig:
    """Resolve the target tier for ``otto cov get``.

    ``None`` selects the sole e2e-kind tier; ambiguity or an unknown name
    raises ``ValueError`` listing the candidates.
    """
    if name is not None:
        for t in tiers:
            if t.name == name:
                return t
        raise ValueError(
            f"unknown tier {name!r}; configured tiers: {', '.join(t.name for t in tiers)}"
        )
    e2e = [t for t in tiers if t.kind == "e2e"]
    if len(e2e) != 1:
        raise ValueError(
            "cannot pick a default tier: "
            f"{len(e2e)} e2e-kind tiers configured ({', '.join(t.name for t in e2e)}); "
            "pass --tier NAME"
        )
    return e2e[0]
```

- [ ] **Step 4: Run tests — expect PASS**

Run: `uv run pytest tests/unit/cov/test_tiers.py -v`

- [ ] **Step 5: Lint + commit**

```bash
ruff check src/otto/coverage/tiers.py tests/unit/cov/test_tiers.py && ruff format <same> && ruff check <same>
git add src/otto/coverage/tiers.py tests/unit/cov/test_tiers.py
git commit -m "feat(cov): runtime TierConfig accessor for declarative tiers"
```

---

### Task 3: Git plumbing helpers

**Files:**
- Create: `src/otto/coverage/capture/__init__.py` (empty docstring module)
- Create: `src/otto/coverage/capture/gitio.py`
- Test: `tests/unit/cov/test_gitio.py`

**Interfaces:**
- Produces (all take `repo_root: Path`, run git via `subprocess.run`, raise `GitUnavailableError(RuntimeError)` when `repo_root` is not inside a git work tree):
  - `head_commit(repo_root) -> str`
  - `is_dirty(repo_root) -> bool` (`git status --porcelain` non-empty)
  - `blob_sha(repo_root, relpath: Path, rev: str = "HEAD") -> str | None` (`git rev-parse <rev>:<relpath>`; None when the path/rev is unknown)
  - `hash_object(repo_root, path: Path) -> str` (`git hash-object <path>`)
  - `blob_exists(repo_root, sha: str) -> bool` (`git cat-file -e <sha>`)
  - `cat_blob(repo_root, sha: str) -> bytes` (`git cat-file blob <sha>`)
  - `diff_worktree_file_u0(repo_root, relpath: Path) -> str` (`git diff -U0 HEAD -- <relpath>`)
  - `diff_no_index_u0(path_a: Path, path_b: Path) -> str` (`git diff --no-index -U0 a b`; exit code 1 = differences, still success)

- [ ] **Step 1: Write failing tests** (build a scratch repo in `tmp_path` with `git init`, one commit, then mutate):

```python
# tests/unit/cov/test_gitio.py
"""Git plumbing used by coverage captures. All repos live in tmp_path."""

import subprocess
from pathlib import Path

import pytest

from otto.coverage.capture.gitio import (
    GitUnavailableError,
    blob_exists,
    blob_sha,
    cat_blob,
    diff_no_index_u0,
    diff_worktree_file_u0,
    hash_object,
    head_commit,
    is_dirty,
)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "sut"
    root.mkdir()
    def git(*args: str) -> None:
        subprocess.run(
            ["git", *args], cwd=root, check=True, capture_output=True,
            env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t",
                 "GIT_COMMITTER_EMAIL": "t@x", "HOME": str(tmp_path), "PATH": "/usr/bin:/bin"},
        )
    git("init", "-q")
    (root / "a.c").write_text("line1\nline2\nline3\n")
    git("add", "a.c")
    git("commit", "-qm", "init")
    return root


def test_head_and_dirty(repo: Path) -> None:
    sha = head_commit(repo)
    assert len(sha) == 40
    assert is_dirty(repo) is False
    (repo / "a.c").write_text("line1\nX\nline3\n")
    assert is_dirty(repo) is True


def test_blob_roundtrip(repo: Path) -> None:
    sha = blob_sha(repo, Path("a.c"))
    assert sha is not None
    assert blob_exists(repo, sha)
    assert cat_blob(repo, sha) == b"line1\nline2\nline3\n"
    assert hash_object(repo, repo / "a.c") == sha
    assert blob_sha(repo, Path("missing.c")) is None


def test_worktree_diff_u0(repo: Path) -> None:
    (repo / "a.c").write_text("line1\nADDED\nline2\nline3\n")
    out = diff_worktree_file_u0(repo, Path("a.c"))
    assert "@@" in out and "+ADDED" in out


def test_no_index_diff_exit_1_ok(tmp_path: Path) -> None:
    a = tmp_path / "a.txt"; a.write_text("x\n")
    b = tmp_path / "b.txt"; b.write_text("y\n")
    out = diff_no_index_u0(a, b)
    assert "@@" in out
    assert diff_no_index_u0(a, a) == ""


def test_not_a_repo_raises(tmp_path: Path) -> None:
    with pytest.raises(GitUnavailableError):
        head_commit(tmp_path)
```

- [ ] **Step 2: Run — expect ModuleNotFoundError**

Run: `uv run pytest tests/unit/cov/test_gitio.py -v`

- [ ] **Step 3: Implement `gitio.py`**

```python
"""Thin subprocess wrappers around the git plumbing coverage needs.

Everything is synchronous and side-effect-free on the repo (read-only
commands only).  Callers pass the sut repo root; a non-repo raises
:class:`GitUnavailableError` with a clean message.
"""

import subprocess
from pathlib import Path


class GitUnavailableError(RuntimeError):
    """Raised when git cannot answer (not a repo / git missing)."""


def _run(args: list[str], cwd: Path | None, ok_codes: tuple[int, ...] = (0,)) -> str:
    try:
        proc = subprocess.run(  # noqa: S603 — fixed git argv, no shell
            ["git", *args], cwd=cwd, capture_output=True, text=True, check=False
        )
    except FileNotFoundError as e:
        raise GitUnavailableError("git executable not found") from e
    if proc.returncode not in ok_codes:
        raise GitUnavailableError(
            f"git {' '.join(args)} failed (rc={proc.returncode}): {proc.stderr.strip()}"
        )
    return proc.stdout


def head_commit(repo_root: Path) -> str:
    return _run(["rev-parse", "HEAD"], repo_root).strip()


def is_dirty(repo_root: Path) -> bool:
    return bool(_run(["status", "--porcelain"], repo_root).strip())


def blob_sha(repo_root: Path, relpath: Path, rev: str = "HEAD") -> str | None:
    try:
        return _run(["rev-parse", f"{rev}:{relpath.as_posix()}"], repo_root).strip()
    except GitUnavailableError:
        return None


def hash_object(repo_root: Path, path: Path) -> str:
    return _run(["hash-object", str(path)], repo_root).strip()


def blob_exists(repo_root: Path, sha: str) -> bool:
    try:
        _run(["cat-file", "-e", sha], repo_root)
    except GitUnavailableError:
        return False
    return True


def cat_blob(repo_root: Path, sha: str) -> bytes:
    proc = subprocess.run(  # noqa: S603 — fixed git argv, no shell
        ["git", "cat-file", "blob", sha], cwd=repo_root, capture_output=True, check=False
    )
    if proc.returncode != 0:
        raise GitUnavailableError(f"git cat-file blob {sha} failed: {proc.stderr.decode()}")
    return proc.stdout


def diff_worktree_file_u0(repo_root: Path, relpath: Path) -> str:
    return _run(["diff", "-U0", "HEAD", "--", relpath.as_posix()], repo_root)


def diff_no_index_u0(path_a: Path, path_b: Path) -> str:
    # git diff --no-index exits 1 when the files differ — that is success here.
    return _run(
        ["diff", "--no-index", "-U0", str(path_a), str(path_b)], cwd=None, ok_codes=(0, 1)
    )
```

Note for `blob_sha`: `git rev-parse HEAD:missing` exits non-zero → `_run` raises → return None. `head_commit` on a non-repo raises — that is the wanted behavior.

- [ ] **Step 4: Run tests — expect PASS.** `uv run pytest tests/unit/cov/test_gitio.py -v`

- [ ] **Step 5: Lint + commit**

```bash
git add src/otto/coverage/capture/__init__.py src/otto/coverage/capture/gitio.py tests/unit/cov/test_gitio.py
git commit -m "feat(cov): git plumbing helpers for capture pinning"
```

---

### Task 4: Hunk remap engine

**Files:**
- Create: `src/otto/coverage/capture/remap.py`
- Test: `tests/unit/cov/test_remap.py`

**Interfaces:**
- Produces: `Hunk` dataclass (`old_start: int, old_count: int, new_start: int, new_count: int`), `parse_u0_hunks(diff_text: str) -> list[Hunk]` (single-file `-U0` diff text), and `LineRemapper` with:
  - `LineRemapper(hunks: list[Hunk])`
  - `.new_to_old(line: int) -> int | None` — map a NEW-side (modified/current) line to its OLD-side line; `None` when the line is inside a hunk (added/changed → dropped).
  - `.old_to_new(line: int) -> int | None` — the reverse mapping; `None` when the OLD line was changed/deleted.
- Semantics: a `-U0` hunk `@@ -a,b +c,d @@` marks OLD lines `[a, a+b)` and NEW lines `[c, c+d)` as changed; anything outside hunks maps 1:1 with the cumulative offset of preceding hunks. Zero-count sides use git's convention (position is the line *before* the insertion/deletion).

- [ ] **Step 1: Write failing tests** covering: pure insertion, pure deletion, replacement, multiple hunks with cumulative offsets, both directions, and hunk-boundary lines:

```python
# tests/unit/cov/test_remap.py
"""Hunk remap engine: line mapping across -U0 diffs."""

from otto.coverage.capture.remap import Hunk, LineRemapper, parse_u0_hunks

# Diff: 3 lines inserted after old line 2 (new lines 3-5).
INSERT = """--- a/f.c
+++ b/f.c
@@ -2,0 +3,3 @@
+a
+b
+c
"""

# Diff: old lines 4-5 deleted.
DELETE = """--- a/f.c
+++ b/f.c
@@ -4,2 +3,0 @@
-x
-y
"""

# Diff: old line 2 replaced by new lines 2-3, and old line 10 deleted.
MIXED = """--- a/f.c
+++ b/f.c
@@ -2,1 +2,2 @@
-old
+new1
+new2
@@ -10,1 +11,0 @@
-gone
"""


def test_parse_hunks() -> None:
    hunks = parse_u0_hunks(MIXED)
    assert hunks == [Hunk(2, 1, 2, 2), Hunk(10, 1, 11, 0)]


def test_insertion_new_to_old() -> None:
    r = LineRemapper(parse_u0_hunks(INSERT))
    assert r.new_to_old(1) == 1
    assert r.new_to_old(2) == 2
    assert r.new_to_old(3) is None  # inserted
    assert r.new_to_old(5) is None  # inserted
    assert r.new_to_old(6) == 3     # shifted by +3


def test_insertion_old_to_new() -> None:
    r = LineRemapper(parse_u0_hunks(INSERT))
    assert r.old_to_new(2) == 2
    assert r.old_to_new(3) == 6


def test_deletion_both_ways() -> None:
    r = LineRemapper(parse_u0_hunks(DELETE))
    assert r.new_to_old(3) == 3
    assert r.new_to_old(4) == 6     # old 4,5 gone; new 4 is old 6
    assert r.old_to_new(4) is None  # deleted
    assert r.old_to_new(6) == 4


def test_mixed_cumulative() -> None:
    r = LineRemapper(parse_u0_hunks(MIXED))
    assert r.new_to_old(1) == 1
    assert r.new_to_old(2) is None   # replacement
    assert r.new_to_old(3) is None
    assert r.new_to_old(4) == 3      # +1 offset after first hunk
    assert r.old_to_new(10) is None  # deleted
    assert r.old_to_new(11) == 11    # +1 then -1 → net 0
    assert r.new_to_old(11) == 11


def test_empty_diff_is_identity() -> None:
    r = LineRemapper(parse_u0_hunks(""))
    assert r.new_to_old(42) == 42
    assert r.old_to_new(42) == 42
```

- [ ] **Step 2: Run — expect ModuleNotFoundError.** `uv run pytest tests/unit/cov/test_remap.py -v`

- [ ] **Step 3: Implement `remap.py`**

```python
"""Map line numbers across a ``-U0`` unified diff.

One engine, two uses (spec §6): retrieval-time dirty-tree correction
(NEW = modified working tree → OLD = HEAD) and report-time manual
validity (OLD = pinned capture → NEW = current source).  Lines inside a
hunk on either side have no counterpart and map to ``None``.
"""

import re
from dataclasses import dataclass

_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


@dataclass(frozen=True)
class Hunk:
    """One ``@@ -a,b +c,d @@`` header (counts default to 1 when omitted)."""

    old_start: int
    old_count: int
    new_start: int
    new_count: int


def parse_u0_hunks(diff_text: str) -> list[Hunk]:
    """Parse hunk headers out of a single-file ``-U0`` diff."""
    hunks: list[Hunk] = []
    for line in diff_text.splitlines():
        m = _HUNK_RE.match(line)
        if m:
            old_start = int(m.group(1))
            old_count = int(m.group(2)) if m.group(2) is not None else 1
            new_start = int(m.group(3))
            new_count = int(m.group(4)) if m.group(4) is not None else 1
            hunks.append(Hunk(old_start, old_count, new_start, new_count))
    return hunks


class LineRemapper:
    """Bidirectional line mapping across the hunks of one file's diff."""

    def __init__(self, hunks: list[Hunk]) -> None:
        self._hunks = hunks

    def new_to_old(self, line: int) -> int | None:
        """OLD-side line for NEW-side *line*, or None inside a changed hunk."""
        offset = 0
        for h in self._hunks:
            # NEW lines occupied by this hunk: [new_start, new_start+new_count)
            # (git convention: count 0 → position is the line before, occupies nothing)
            if h.new_count > 0 and h.new_start <= line < h.new_start + h.new_count:
                return None
            hunk_end_new = h.new_start + h.new_count if h.new_count > 0 else h.new_start
            if line >= hunk_end_new and (h.new_count > 0 or line > h.new_start):
                offset += h.old_count - h.new_count
            else:
                break
        return line + offset

    def old_to_new(self, line: int) -> int | None:
        """NEW-side line for OLD-side *line*, or None when changed/deleted."""
        offset = 0
        for h in self._hunks:
            if h.old_count > 0 and h.old_start <= line < h.old_start + h.old_count:
                return None
            hunk_end_old = h.old_start + h.old_count if h.old_count > 0 else h.old_start
            if line >= hunk_end_old and (h.old_count > 0 or line > h.old_start):
                offset += h.new_count - h.old_count
            else:
                break
        return line + offset
```

- [ ] **Step 4: Run tests — expect PASS.** If a boundary case fails, fix the offset conditions until all six tests pass; these tests are the contract.

- [ ] **Step 5: Lint + commit**

```bash
git add src/otto/coverage/capture/remap.py tests/unit/cov/test_remap.py
git commit -m "feat(cov): bidirectional hunk remap engine"
```

---

### Task 5: CaptureFile model + .info conversion

**Files:**
- Create: `src/otto/coverage/capture/model.py`
- Test: `tests/unit/cov/test_capture_model.py`

**Interfaces:**
- Consumes: `LineRemapper` (Task 4) for dirty-tree correction, `gitio` (Task 3).
- Produces:
  - `CaptureFileCov` pydantic model: `blob: str | None = None`, `lines: dict[int, int]`, `branches: dict[int, list[tuple[int, int, int]]] = {}` (lineno → list of `[block, branch, taken]`).
  - `Capture` pydantic model (spec §3): `schema_version: int = 1` (serialized as `"schema"` via alias), `tier: str`, `pin: str`, `dirty_remap: bool = False`, `captured_at: str` (ISO-8601 UTC), `tester: dict[str, str] | None = None`, `ticket: str | None = None`, `note: str | None = None`, `labs: list[str] = []`, `board: str`, `files: dict[str, CaptureFileCov]` (keys = repo-relative POSIX paths). Model validator: `tier`-kind manual is not knowable here, so ticket enforcement lives in the CLI (Task 8); the model only enforces shape.
  - `Capture.save(path: Path) -> None`, `Capture.load(path: Path) -> Capture` (strict: unknown keys rejected).
  - `parse_info(info_path: Path) -> dict[str, tuple[dict[int, int], dict[int, list[tuple[int, int, int]]]]]` — minimal SF/DA/BRDA parser returning per-source-path line hits and branch records (taken `-` → count 0 entry `[block, branch, -1]`? No: use `-1` sentinel NOT allowed — represent not-reached as `taken=-1` is ugly; instead store reached branches with their count and not-reached with `0`, and keep lcov's distinction by emitting count `0` for `-`. The renderer already gets reachability from the live lcov path; captures only need counts).
  - `build_capture(*, info_path, tier, repo_root, board, labs, tester=None, ticket=None, note=None, now=None) -> Capture` — parses the .info, converts absolute source paths under `repo_root` to repo-relative POSIX (paths outside `repo_root` are skipped with a logger warning), applies `LineRemapper` per file when `gitio.is_dirty(repo_root)` (diff via `gitio.diff_worktree_file_u0`, mapping **new→old** and dropping `None`s), sets `pin=head_commit(repo_root)`, `blob=blob_sha(repo_root, relpath)` per file, `captured_at` from `now or datetime.now(timezone.utc)`.

- [ ] **Step 1: Write failing tests** — round-trip save/load, unknown-key rejection, `parse_info` on a hand-written `.info` string, and `build_capture` against a `tmp_path` git repo in both clean and dirty states (dirty: insert a line above a hit line, assert the hit shifts down by one in pin coordinates and the inserted line's hit is dropped; assert `dirty_remap is True` and per-file `blob` equals `git rev-parse HEAD:file`):

```python
# tests/unit/cov/test_capture_model.py
"""Capture JSON model, .info parsing, and dirty-tree remap on build."""

import json
import subprocess
from pathlib import Path

import pytest

from otto.coverage.capture.gitio import blob_sha, head_commit
from otto.coverage.capture.model import Capture, build_capture, parse_info

INFO = """TN:
SF:{src}
DA:1,5
DA:2,0
DA:3,7
BRDA:3,0,0,4
BRDA:3,0,1,-
end_of_record
"""


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "sut"
    root.mkdir()
    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True,
                       env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x",
                            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x",
                            "HOME": str(tmp_path), "PATH": "/usr/bin:/bin"})
    git("init", "-q")
    (root / "f.c").write_text("int a;\nint b;\nint c;\n")
    git("add", "f.c")
    git("commit", "-qm", "init")
    return root


def _write_info(tmp_path: Path, src: Path) -> Path:
    p = tmp_path / "x.info"
    p.write_text(INFO.format(src=src))
    return p


def test_parse_info(tmp_path: Path) -> None:
    src = tmp_path / "f.c"
    files = parse_info(_write_info(tmp_path, src))
    lines, branches = files[str(src)]
    assert lines == {1: 5, 2: 0, 3: 7}
    assert branches[3] == [(0, 0, 4), (0, 1, 0)]


def test_build_capture_clean(repo: Path, tmp_path: Path) -> None:
    info = _write_info(tmp_path, repo / "f.c")
    cap = build_capture(info_path=info, tier="system", repo_root=repo,
                        board="board1", labs=["lab1"])
    assert cap.pin == head_commit(repo)
    assert cap.dirty_remap is False
    fc = cap.files["f.c"]
    assert fc.lines == {1: 5, 2: 0, 3: 7}
    assert fc.blob == blob_sha(repo, Path("f.c"))


def test_build_capture_dirty_remaps(repo: Path, tmp_path: Path) -> None:
    # Insert a printf as new line 1; old line N is now N+1 in the working tree.
    (repo / "f.c").write_text('printf();\nint a;\nint b;\nint c;\n')
    info = _write_info(tmp_path, repo / "f.c")  # DA lines are worktree coords
    cap = build_capture(info_path=info, tier="manual", repo_root=repo,
                        board="b", labs=["lab1"], ticket="T-1")
    assert cap.dirty_remap is True
    # worktree line 1 (the printf) dropped; 2→1, 3→2
    assert cap.files["f.c"].lines == {1: 0, 2: 7}


def test_roundtrip_and_strictness(repo: Path, tmp_path: Path) -> None:
    info = _write_info(tmp_path, repo / "f.c")
    cap = build_capture(info_path=info, tier="system", repo_root=repo,
                        board="b", labs=[])
    out = tmp_path / "capture.json"
    cap.save(out)
    loaded = Capture.load(out)
    assert loaded == cap
    raw = json.loads(out.read_text())
    assert raw["schema"] == 1
    raw["surprise"] = True
    out.write_text(json.dumps(raw))
    with pytest.raises(ValueError):
        Capture.load(out)
```

Note the dirty test's expectation: worktree DA lines `{1:5, 2:0, 3:7}` remap new→old across hunk `@@ -0,0 +1,1 @@`: worktree 1 → None (dropped), 2 → 1 (count 0), 3 → 2 (count 7). Old line 3 got no data (worktree line 4 had none in the .info) — absent, not zero.

- [ ] **Step 2: Run — expect ModuleNotFoundError.** `uv run pytest tests/unit/cov/test_capture_model.py -v`

- [ ] **Step 3: Implement `model.py`**

```python
"""The per-board ``capture.json`` artifact (spec §3).

A capture stores line/branch data in **committed-code coordinates**,
pinned to the commit whose numbering they mean, with per-file blob SHAs
as the rebase-tolerant validity anchor.
"""

import logging
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from . import gitio
from .remap import LineRemapper, parse_u0_hunks

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1


class CaptureFileCov(BaseModel):
    """Coverage for one source file, keyed in pin coordinates."""

    model_config = ConfigDict(extra="forbid")

    blob: str | None = None
    lines: dict[int, int] = Field(default_factory=dict)
    branches: dict[int, list[tuple[int, int, int]]] = Field(default_factory=dict)


class Capture(BaseModel):
    """One board's retrieval result — the universal capture artifact."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    schema_version: int = Field(default=SCHEMA_VERSION, alias="schema")
    tier: str
    pin: str
    dirty_remap: bool = False
    captured_at: str = ""
    tester: dict[str, str] | None = None
    ticket: str | None = None
    note: str | None = None
    labs: list[str] = Field(default_factory=list)
    board: str = ""
    files: dict[str, CaptureFileCov] = Field(default_factory=dict)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.model_dump_json(by_alias=True, indent=2))

    @classmethod
    def load(cls, path: Path) -> "Capture":
        return cls.model_validate_json(path.read_text())


def parse_info(
    info_path: Path,
) -> dict[str, tuple[dict[int, int], dict[int, list[tuple[int, int, int]]]]]:
    """Minimal SF/DA/BRDA parser: source path → (line hits, branch triples)."""
    files: dict[str, tuple[dict[int, int], dict[int, list[tuple[int, int, int]]]]] = {}
    lines: dict[int, int] = {}
    branches: dict[int, list[tuple[int, int, int]]] = {}
    current: str | None = None
    with info_path.open() as f:
        for raw in f:
            line = raw.strip()
            if line.startswith("SF:"):
                current = line[3:]
                lines, branches = {}, {}
            elif line.startswith("DA:") and current is not None:
                parts = line[3:].split(",")
                lines[int(parts[0])] = lines.get(int(parts[0]), 0) + int(parts[1])
            elif line.startswith("BRDA:") and current is not None:
                lineno_s, block_s, branch_s, taken = line[5:].split(",")
                count = 0 if taken == "-" else int(taken)
                branches.setdefault(int(lineno_s), []).append(
                    (int(block_s), int(branch_s), count)
                )
            elif line == "end_of_record" and current is not None:
                files[current] = (lines, branches)
                current = None
    return files


def _remap_file(
    lines: dict[int, int],
    branches: dict[int, list[tuple[int, int, int]]],
    remapper: LineRemapper,
) -> tuple[dict[int, int], dict[int, list[tuple[int, int, int]]]]:
    """Worktree (NEW) coordinates → pin (OLD) coordinates; drop unmappables."""
    out_lines: dict[int, int] = {}
    for lineno, count in lines.items():
        old = remapper.new_to_old(lineno)
        if old is not None:
            out_lines[old] = out_lines.get(old, 0) + count
    out_branches: dict[int, list[tuple[int, int, int]]] = {}
    for lineno, triples in branches.items():
        old = remapper.new_to_old(lineno)
        if old is not None:
            out_branches[old] = triples
    return out_lines, out_branches


def build_capture(
    *,
    info_path: Path,
    tier: str,
    repo_root: Path,
    board: str,
    labs: list[str],
    tester: dict[str, str] | None = None,
    ticket: str | None = None,
    note: str | None = None,
    now: datetime | None = None,
) -> Capture:
    """Build a pinned :class:`Capture` from an lcov ``.info`` file."""
    pin = gitio.head_commit(repo_root)
    dirty = gitio.is_dirty(repo_root)
    repo_root = repo_root.resolve()

    files: dict[str, CaptureFileCov] = {}
    for src, (lines, branches) in parse_info(info_path).items():
        src_path = Path(src).resolve()
        if not src_path.is_relative_to(repo_root):
            logger.warning("Skipping source outside repo: %s", src)
            continue
        rel = src_path.relative_to(repo_root)
        if dirty:
            hunks = parse_u0_hunks(gitio.diff_worktree_file_u0(repo_root, rel))
            lines, branches = _remap_file(lines, branches, LineRemapper(hunks))
        files[rel.as_posix()] = CaptureFileCov(
            blob=gitio.blob_sha(repo_root, rel),
            lines=lines,
            branches=branches,
        )

    stamp = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return Capture(
        tier=tier,
        pin=pin,
        dirty_remap=dirty,
        captured_at=stamp,
        tester=tester,
        ticket=ticket,
        note=note,
        labs=labs,
        board=board,
        files=files,
    )
```

- [ ] **Step 4: Run tests — expect PASS.** `uv run pytest tests/unit/cov/test_capture_model.py -v`

- [ ] **Step 5: Lint + commit**

```bash
git add src/otto/coverage/capture/model.py tests/unit/cov/test_capture_model.py
git commit -m "feat(cov): Capture artifact model with dirty-tree remap and blob anchors"
```

---

### Task 6: Manual store dir + exclusion scan

**Files:**
- Create: `src/otto/coverage/capture/store_dir.py`
- Create: `src/otto/coverage/exclusions.py`
- Test: `tests/unit/cov/test_store_dir.py`, `tests/unit/cov/test_exclusions.py`

**Interfaces:**
- Produces (`store_dir.py`): `manual_store_dir(repo_root: Path) -> Path` (`repo_root/.otto/coverage/manual`), `write_manual_capture(capture: Capture, repo_root: Path) -> Path` (filename `<captured_at compressed>-<ticket-slug>-<board-slug>.json`, e.g. `20260702T184000Z-proj-123-board1.json`; slug = lowercase alnum with `-`), `load_manual_captures(repo_root: Path) -> list[Capture]` (sorted by filename; missing dir → `[]`; a malformed JSON raises `ValueError` naming the file).
- Produces (`exclusions.py`): `scan_excluded_lines(source: str, extra_markers: list[str] | None = None) -> set[int]` honoring `LCOV_EXCL_LINE` (that line), `LCOV_EXCL_START`/`LCOV_EXCL_STOP` (block inclusive of both marker lines), `LCOV_EXCL_BR_LINE`/`_BR_START`/`_BR_STOP` (treated identically for line exclusion display purposes), plus each `extra_markers` string treated as a `_LINE`-style marker. Unclosed `START` runs to EOF (matches lcov behavior).

- [ ] **Step 1: Write failing tests**

```python
# tests/unit/cov/test_store_dir.py
"""Manual capture store: naming, round-trip, listing."""

from pathlib import Path

import pytest

from otto.coverage.capture.model import Capture, CaptureFileCov
from otto.coverage.capture.store_dir import (
    load_manual_captures,
    manual_store_dir,
    write_manual_capture,
)


def _capture(ticket: str = "PROJ-123") -> Capture:
    return Capture(
        tier="manual", pin="0" * 40, captured_at="2026-07-02T18:40:00Z",
        tester={"name": "chris", "email": "c@x"}, ticket=ticket,
        labs=["lab1"], board="Board One",
        files={"f.c": CaptureFileCov(lines={1: 1})},
    )


def test_write_and_load(tmp_path: Path) -> None:
    p = write_manual_capture(_capture(), tmp_path)
    assert p.parent == manual_store_dir(tmp_path)
    assert p.name == "20260702T184000Z-proj-123-board-one.json"
    caps = load_manual_captures(tmp_path)
    assert len(caps) == 1 and caps[0].ticket == "PROJ-123"


def test_missing_dir_is_empty(tmp_path: Path) -> None:
    assert load_manual_captures(tmp_path) == []


def test_malformed_names_file(tmp_path: Path) -> None:
    d = manual_store_dir(tmp_path)
    d.mkdir(parents=True)
    (d / "bad.json").write_text("{nope")
    with pytest.raises(ValueError, match="bad.json"):
        load_manual_captures(tmp_path)
```

```python
# tests/unit/cov/test_exclusions.py
"""LCOV exclusion-marker scanning."""

from otto.coverage.exclusions import scan_excluded_lines

SRC = """int main() {
  int a = 1;             // LCOV_EXCL_LINE
  // LCOV_EXCL_START
  debug_dump();
  debug_dump2();
  // LCOV_EXCL_STOP
  if (a) {}              // LCOV_EXCL_BR_LINE
  return 0;
}
"""


def test_line_and_block_markers() -> None:
    excluded = scan_excluded_lines(SRC)
    assert excluded == {2, 3, 4, 5, 6, 7}


def test_custom_marker() -> None:
    src = "a;\nb; // MYPROJ_NO_COV\nc;\n"
    assert scan_excluded_lines(src, ["MYPROJ_NO_COV"]) == {2}


def test_unclosed_start_runs_to_eof() -> None:
    src = "a;\n// LCOV_EXCL_START\nb;\nc;\n"
    assert scan_excluded_lines(src) == {2, 3, 4}
```

- [ ] **Step 2: Run — expect import failures.**

- [ ] **Step 3: Implement both modules**

```python
# src/otto/coverage/capture/store_dir.py
"""The committed in-repo manual-capture store (spec §3)."""

import re
from pathlib import Path

from .model import Capture

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slug(value: str) -> str:
    return _SLUG_RE.sub("-", value.lower()).strip("-") or "x"


def manual_store_dir(repo_root: Path) -> Path:
    return repo_root / ".otto" / "coverage" / "manual"


def write_manual_capture(capture: Capture, repo_root: Path) -> Path:
    stamp = capture.captured_at.replace("-", "").replace(":", "")
    name = f"{stamp}-{_slug(capture.ticket or 'no-ticket')}-{_slug(capture.board)}.json"
    path = manual_store_dir(repo_root) / name
    capture.save(path)
    return path


def load_manual_captures(repo_root: Path) -> list[Capture]:
    d = manual_store_dir(repo_root)
    if not d.is_dir():
        return []
    captures: list[Capture] = []
    for p in sorted(d.glob("*.json")):
        try:
            captures.append(Capture.load(p))
        except ValueError as e:
            raise ValueError(f"malformed manual capture {p.name}: {e}") from e
    return captures
```

```python
# src/otto/coverage/exclusions.py
"""Scan source text for LCOV exclusion markers (spec §8).

lcov's geninfo already drops these regions from measured data; this scan
exists so the renderer can *show* exclusions instead of leaving them
indistinguishable from blank lines.
"""

_LINE_MARKERS = ("LCOV_EXCL_LINE", "LCOV_EXCL_BR_LINE")
_START_MARKERS = ("LCOV_EXCL_START", "LCOV_EXCL_BR_START")
_STOP_MARKERS = ("LCOV_EXCL_STOP", "LCOV_EXCL_BR_STOP")


def scan_excluded_lines(source: str, extra_markers: list[str] | None = None) -> set[int]:
    """1-based line numbers excluded by markers (block bounds inclusive)."""
    line_markers = _LINE_MARKERS + tuple(extra_markers or ())
    excluded: set[int] = set()
    in_block = False
    for lineno, text in enumerate(source.splitlines(), start=1):
        if in_block:
            excluded.add(lineno)
            if any(m in text for m in _STOP_MARKERS):
                in_block = False
            continue
        if any(m in text for m in _START_MARKERS):
            excluded.add(lineno)
            in_block = True
        elif any(m in text for m in line_markers):
            excluded.add(lineno)
    return excluded
```

- [ ] **Step 4: Run tests — expect PASS.** `uv run pytest tests/unit/cov/test_store_dir.py tests/unit/cov/test_exclusions.py -v`

- [ ] **Step 5: Lint + commit**

```bash
git add src/otto/coverage/capture/store_dir.py src/otto/coverage/exclusions.py tests/unit/cov/test_store_dir.py tests/unit/cov/test_exclusions.py
git commit -m "feat(cov): manual capture store dir + exclusion-marker scan"
```

---

### Task 7: Store states/provenance + validity pass

**Files:**
- Modify: `src/otto/coverage/store/model.py`
- Create: `src/otto/coverage/validity.py`
- Test: `tests/unit/cov/test_validity.py`; extend `tests/unit/cov/test_model.py`

**Interfaces:**
- Store changes (all additive except the stub removal):
  - DELETE `LineRecord.commit_hash/commit_author/commit_summary` and their `to_dict`/`load` keys (`commit`, `author`, `summary`). Grep `tests/` for those field names and update any fixture assertions.
  - ADD `LineRecord.state: str | None = None` (values: `"stale"`, `"aging"`; `None` = normal). `to_dict` emits `"state": rec.state`; `load` reads it back.
  - ADD `CoverageStore.provenance: list[dict[str, Any]]` (each: `{"tier", "board", "labs", "date", "tester", "ticket", "note", "dirty_remap", "pin"}`), serialized in `save()` under `"provenance"`, loaded in `load()`.
  - ADD `CoverageStore.tier_colors: dict[str, str]` serialized/loaded as `"tier_colors"`.
- `validity.py` produces:
  - `apply_manual_capture(store: CoverageStore, capture: Capture, repo_root: Path, max_age_days: int | None, today: datetime | None = None) -> None` — per file, anchor chain from spec §7; valid lines land in the store under `capture.tier` (`lr.hits.add(tier, count)`, branches via the same shape `LCOVLoader` uses); stale lines create/mark records with `state="stale"` **without** adding hits; aging (valid but `captured_at` older than `max_age_days`) adds hits AND sets `state="aging"` unless another tier already hit the line. Appends one provenance entry.
  - `_anchor_diff(capture_file, repo_root, relpath) -> str | None` returning the `-U0` diff text pin→current (empty string = unchanged), or `None` = unverifiable. Chain: `hash_object == blob` → `""`; `blob_exists(blob)` → cat blob to a temp file, `diff_no_index_u0(tmpfile, current)`; else `blob_sha(repo_root, relpath, rev=capture.pin)` resolvable → same via that blob; else `None`.

- [ ] **Step 1: Write failing tests** (scratch git repo; capture built by hand):

```python
# tests/unit/cov/test_validity.py
"""Manual-capture validity: anchor chain, stale/aging states."""

import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from otto.coverage.capture.model import Capture, CaptureFileCov
from otto.coverage.capture.gitio import blob_sha, head_commit
from otto.coverage.store.model import CoverageStore
from otto.coverage.validity import apply_manual_capture


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "sut"; root.mkdir()
    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True,
                       env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x",
                            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x",
                            "HOME": str(tmp_path), "PATH": "/usr/bin:/bin"})
    git("init", "-q")
    (root / "f.c").write_text("int a;\nint b;\nint c;\n")
    git("add", "f.c"); git("commit", "-qm", "init")
    return root


def _capture(repo: Path, captured_at: str = "2026-07-01T00:00:00Z") -> Capture:
    return Capture(
        tier="manual", pin=head_commit(repo), captured_at=captured_at,
        ticket="T-1", labs=["lab1"], board="b",
        files={"f.c": CaptureFileCov(blob=blob_sha(repo, Path("f.c")),
                                     lines={1: 2, 3: 1})},
    )


def _find(store: CoverageStore, repo: Path, lineno: int):
    (rec,) = [f for f in store.files() if f.path == (repo / "f.c").resolve()]
    return rec.lines.get(lineno)


def test_unchanged_file_all_valid(repo: Path) -> None:
    store = CoverageStore(tier_order=["manual"])
    apply_manual_capture(store, _capture(repo), repo, max_age_days=None)
    assert _find(store, repo, 1).hits.for_tier("manual") == 2
    assert _find(store, repo, 1).state is None
    assert store.provenance[0]["ticket"] == "T-1"


def test_edited_line_goes_stale(repo: Path) -> None:
    cap = _capture(repo)
    (repo / "f.c").write_text("int a;\nint b;\nint CHANGED;\n")
    subprocess.run(["git", "commit", "-aqm", "edit"], cwd=repo, check=True,
                   capture_output=True, env={"GIT_AUTHOR_NAME": "t",
                   "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t",
                   "GIT_COMMITTER_EMAIL": "t@x", "PATH": "/usr/bin:/bin"})
    store = CoverageStore(tier_order=["manual"])
    apply_manual_capture(store, cap, repo, max_age_days=None)
    assert _find(store, repo, 1).hits.for_tier("manual") == 2   # unchanged line: valid
    line3 = _find(store, repo, 3)
    assert line3.state == "stale"
    assert line3.hits.for_tier("manual") == 0                    # no credit


def test_aging_flag(repo: Path) -> None:
    store = CoverageStore(tier_order=["manual"])
    apply_manual_capture(store, _capture(repo, "2025-01-01T00:00:00Z"), repo,
                         max_age_days=180,
                         today=datetime(2026, 7, 2, tzinfo=timezone.utc))
    line1 = _find(store, repo, 1)
    assert line1.hits.for_tier("manual") == 2   # still counts
    assert line1.state == "aging"


def test_unverifiable_all_stale(repo: Path) -> None:
    cap = _capture(repo)
    bogus = cap.model_copy(update={"pin": "f" * 40})
    for fc in bogus.files.values():
        fc.blob = "e" * 40
    store = CoverageStore(tier_order=["manual"])
    apply_manual_capture(store, bogus, repo, max_age_days=None)
    assert _find(store, repo, 1).state == "stale"
```

- [ ] **Step 2: Run — expect failures** (missing module + missing `state`/`provenance` attributes).

- [ ] **Step 3: Implement the store changes** in `src/otto/coverage/store/model.py`: replace the three `commit_*` fields on `LineRecord` (lines 146-149) with `state: str | None = None`; in `FileRecord.to_dict` replace the `"commit"/"author"/"summary"` keys with `"state": rec.state`; in `CoverageStore.__init__` add `self.provenance: list[dict[str, Any]] = []` and `self.tier_colors: dict[str, str] = {}`; extend `save()`'s envelope with `"provenance": self.provenance, "tier_colors": self.tier_colors`; extend `load()` to read `state`, `provenance` (default `[]`), `tier_colors` (default `{}`), and to stop reading the removed keys. Update any existing tests in `tests/unit/cov/test_model.py` that reference the removed fields.

- [ ] **Step 4: Implement `src/otto/coverage/validity.py`**

```python
"""Report-time validity for pinned manual captures (spec §7).

Anchor chain per file: blob fast-path → blob diff → pin diff →
unverifiable (whole file stale, loud warning).  Valid lines are loaded
into the store under the capture's tier; stale lines are marked but
carry no hits; aging marks valid-but-old manual evidence.
"""

import logging
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .capture import gitio
from .capture.model import Capture, CaptureFileCov
from .capture.remap import LineRemapper, parse_u0_hunks
from .store.model import BranchHits, CoverageStore

logger = logging.getLogger(__name__)


def _anchor_diff(fc: CaptureFileCov, repo_root: Path, relpath: Path, pin: str) -> str | None:
    """-U0 diff pin→current for one file; '' = unchanged; None = unverifiable."""
    current = repo_root / relpath
    if not current.is_file():
        return None
    if fc.blob and gitio.hash_object(repo_root, current) == fc.blob:
        return ""
    base_blob = fc.blob if fc.blob and gitio.blob_exists(repo_root, fc.blob) else None
    if base_blob is None:
        base_blob = gitio.blob_sha(repo_root, relpath, rev=pin)
    if base_blob is None:
        return None
    with tempfile.NamedTemporaryFile(suffix=relpath.suffix) as tmp:
        Path(tmp.name).write_bytes(gitio.cat_blob(repo_root, base_blob))
        return gitio.diff_no_index_u0(Path(tmp.name), current)


def _is_aging(captured_at: str, max_age_days: int | None, today: datetime | None) -> bool:
    if max_age_days is None:
        return False
    captured = datetime.strptime(captured_at, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc
    )
    now = today or datetime.now(timezone.utc)
    return (now - captured).days > max_age_days


def apply_manual_capture(
    store: CoverageStore,
    capture: Capture,
    repo_root: Path,
    max_age_days: int | None,
    today: datetime | None = None,
) -> None:
    """Fold one manual capture into *store* with validity states."""
    store.register_tier(capture.tier)
    aging = _is_aging(capture.captured_at, max_age_days, today)

    for rel_str, fc in capture.files.items():
        relpath = Path(rel_str)
        diff = _anchor_diff(fc, repo_root, relpath, capture.pin)
        file_rec = store.get_or_create_file(repo_root / relpath)
        if diff is None:
            logger.warning(
                "Manual capture %s/%s is unverifiable (pin %s and blob missing) — "
                "treating as stale; re-capture to refresh.",
                capture.ticket, rel_str, capture.pin[:12],
            )
            for lineno in fc.lines:
                lr = file_rec.get_or_create_line(lineno)
                if lr.state is None and not lr.hits.is_hit():
                    lr.state = "stale"
            continue
        remapper = LineRemapper(parse_u0_hunks(diff))
        for lineno, count in fc.lines.items():
            new_line = remapper.old_to_new(lineno)
            if new_line is None:
                # Changed/deleted since capture: mark stale at the nearest
                # surviving location — the pin lineno maps nowhere, so record
                # the state on the pin line number only if it still exists.
                continue
            lr = file_rec.get_or_create_line(new_line)
            lr.hits.add(capture.tier, count)
            if aging and count > 0 and lr.state is None:
                lr.state = "aging"
        # Lines whose mapping vanished: mark stale on their *old* neighbors'
        # positions is meaningless — instead mark the file-level record: any
        # pin line with hits>0 that did not map becomes a stale marker on the
        # remapped position of the nearest preceding mappable line + 1 is
        # over-clever; keep it simple and mark nothing positional. The stale
        # rollup instead counts them per file:
        stale_count = sum(
            1 for lineno, count in fc.lines.items()
            if count > 0 and remapper.old_to_new(lineno) is None
        )
        if stale_count:
            for lineno, count in fc.lines.items():
                if count > 0 and remapper.old_to_new(lineno) is None:
                    lr = file_rec.get_or_create_line(lineno)
                    if lr.state is None and not lr.hits.is_hit():
                        lr.state = "stale"
        for lineno, triples in fc.branches.items():
            new_line = remapper.old_to_new(lineno)
            if new_line is None:
                continue
            lr = file_rec.get_or_create_line(new_line)
            existing = {(b.block, b.branch): b for b in lr.branches}
            for block, branch, taken in triples:
                key = (block, branch)
                if key not in existing:
                    bh = BranchHits(block=block, branch=branch)
                    lr.branches.append(bh)
                    existing[key] = bh
                existing[key].set_reachable(capture.tier, True)
                if taken > 0:
                    existing[key].hits.add(capture.tier, taken)

    store.provenance.append(
        {
            "tier": capture.tier,
            "board": capture.board,
            "labs": capture.labs,
            "date": capture.captured_at,
            "tester": capture.tester,
            "ticket": capture.ticket,
            "note": capture.note,
            "dirty_remap": capture.dirty_remap,
            "pin": capture.pin,
        }
    )
```

**Implementation note on the stale-marking block:** the comment-heavy middle section above shows intent but is redundant as written (`stale_count` then a second identical loop). Implement it as a single loop: for every pin line with `count > 0` whose `old_to_new` is `None`, `get_or_create_line(pin_lineno)` and set `state="stale"` when the line has no hits from any tier. The test contract (`test_edited_line_goes_stale`) pins the semantics: pin line 3 edited → line record 3 exists with `state == "stale"` and zero manual hits. Clean this up while making the tests pass — the tests, not the sketch, are authoritative.

- [ ] **Step 5: Run tests — expect PASS.** `uv run pytest tests/unit/cov/test_validity.py tests/unit/cov/test_model.py -v`

- [ ] **Step 6: Sweep for removed-field references**

Run: `grep -rn "commit_hash\|commit_author\|commit_summary" src tests --include='*.py'`
Expected: no hits outside this task's edits. Fix any stragglers.

- [ ] **Step 7: Lint + commit**

```bash
git add src/otto/coverage/store/model.py src/otto/coverage/validity.py tests/unit/cov/test_validity.py tests/unit/cov/test_model.py
git commit -m "feat(cov): line states + provenance in store; blob-anchored manual validity pass"
```

---

### Task 8: Per-board capture production

**Files:**
- Create: `src/otto/coverage/capture/produce.py`
- Test: `tests/unit/cov/test_produce.py`

**Interfaces:**
- Consumes: `LcovMerger` (`otto.coverage.correlator.merger`, `capture()` per host dir), `read_cov_toolchains`/`read_cov_source_roots`/`read_cov_source_root` (`otto.coverage.reporter`), `build_capture` (Task 5), `PathCorrelator`/`discover_path_mappings` (existing).
- Produces: `async produce_captures(cov_dir: Path, *, tier: str, repo_root: Path, labs: list[str], tester: dict[str, str] | None = None, ticket: str | None = None, note: str | None = None) -> list[Path]`:
  1. For each board dir (subdir of `cov_dir` containing `.gcda`), resolve its toolchain/source-root from `.otto_cov_meta.json` (present — written by the fetch step),
  2. run `LcovMerger(localhost).capture(...)` for that board alone into `cov_dir/<board>/board.info` (the `.gcda` and `.info` stay on disk — spec decision 18),
  3. correlate source paths to `repo_root` using `discover_path_mappings` on the board `.info`,
  4. `build_capture(...)` with `board=<dirname>`, write `cov_dir/<board>/capture.json`.
  Returns written paths. Boards with no `.gcda` are skipped with a warning.
- The exact `LcovMerger` call signature must be read from `src/otto/coverage/correlator/merger.py` at implementation time (per-host capture is `merger.capture(gcda_dir, gcno_dir, out_info, toolchain=...)` — verify the parameter names before writing code; `capture_and_merge` at `merger.py:64` shows the per-host loop to mirror).

- [ ] **Step 1: Write the failing test.** Full lcov execution needs a real toolchain — out of unit scope. Unit-test the orchestration with the merger monkeypatched:

```python
# tests/unit/cov/test_produce.py
"""produce_captures orchestration (merger stubbed, no lcov binary)."""

import subprocess
from pathlib import Path

import pytest

from otto.coverage.capture.model import Capture
from otto.coverage.capture import produce as produce_mod
from otto.coverage.capture.produce import produce_captures


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "sut"; root.mkdir()
    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True,
                       env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x",
                            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x",
                            "HOME": str(tmp_path), "PATH": "/usr/bin:/bin"})
    git("init", "-q")
    (root / "f.c").write_text("int a;\nint b;\n")
    git("add", "f.c"); git("commit", "-qm", "init")
    return root


@pytest.mark.asyncio
async def test_produce_writes_per_board_captures(
    tmp_path: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cov_dir = tmp_path / "out" / "cov"
    for board in ("board1", "board2"):
        (cov_dir / board).mkdir(parents=True)
        (cov_dir / board / "x.gcda").write_bytes(b"")
    (cov_dir / ".otto_cov_meta.json").write_text(
        '{"repo_name": "r", "sut_dir": "%s", "toolchains": {}, "source_roots": {}}'
        % repo
    )

    async def fake_capture(self, gcda_dir, gcno_dir, out_info, **kwargs):
        out_info.write_text(f"TN:\nSF:{repo / 'f.c'}\nDA:1,3\nend_of_record\n")
        return out_info

    monkeypatch.setattr(produce_mod.LcovMerger, "capture", fake_capture)

    written = await produce_captures(
        cov_dir, tier="system", repo_root=repo, labs=["lab1"]
    )
    assert sorted(p.parent.name for p in written) == ["board1", "board2"]
    cap = Capture.load(written[0])
    assert cap.tier == "system"
    assert cap.board == "board1"
    assert cap.files["f.c"].lines == {1: 3}
```

Adjust `fake_capture`'s signature to the real `LcovMerger.capture` signature discovered in Step 3 — the monkeypatch must match it.

- [ ] **Step 2: Run — expect ModuleNotFoundError.**

- [ ] **Step 3: Read `src/otto/coverage/correlator/merger.py` in full**, then implement `produce.py`: iterate `sorted(cov_dir.iterdir())` board dirs (skip non-dirs, skip dirs without `*.gcda` recursively), resolve per-board toolchain/source-root from the reporter's `read_cov_*` helpers against `[cov_dir]`, call `LcovMerger(localhost).capture(...)` per board writing `board.info` inside the board dir, then `build_capture(info_path=board_info, tier=tier, repo_root=repo_root, board=board_dir.name, labs=labs, tester=tester, ticket=ticket, note=note)` and `capture.save(board_dir / "capture.json")`. Close the `LocalHost` in a `finally:` like `CoverageReporter.run` does. Path correlation: pass the board `.info` through `discover_path_mappings(board_info, source_root, localhost)` + `PathCorrelator.resolve` to absolutize embedded paths before `build_capture` (mirror `reporter.py:313-327`); write the resolved-path `.info` next to the raw one as `board.resolved.info` and feed **that** to `build_capture`.

- [ ] **Step 4: Run tests — expect PASS.** `uv run pytest tests/unit/cov/test_produce.py -v`

- [ ] **Step 5: Lint + commit**

```bash
git add src/otto/coverage/capture/produce.py tests/unit/cov/test_produce.py
git commit -m "feat(cov): per-board capture.json production from fetched counters"
```

---

### Task 9: `otto cov get` CLI + `otto test --cov` hookup

**Files:**
- Modify: `src/otto/cli/cov.py` (add `get` command)
- Modify: `src/otto/cli/test.py` (call `produce_captures` at the end of `_run_coverage`, line ~1154)
- Test: extend `tests/unit/cli/test_cov.py`

**Interfaces:**
- Consumes: `bootstrap()` / `all_hosts` (mirror `_run_coverage` in `test.py:1076-1154`), `GcdaFetcher`, `collect_embedded_coverage`, `_write_cov_metadata` equivalents, `produce_captures` (Task 8), `load_tiers`/`resolve_get_tier` (Task 2), `gitio` (Task 3).
- Produces: `otto cov get` with options: `--output/-o PATH` (default `./cov_get`), `--tier NAME` (default: sole e2e tier), `--ticket STR`, `--note STR`, `--tester-name STR`, `--tester-email STR`, `--clean` (pre-zero remote counters before returning — for use *before* a manual session). Behavior:
  - refuse (exit 1, one-line error) when: outside a git repo; manual-kind tier without `--ticket`; ambiguous default tier; no `[coverage]` config; zero counters retrieved.
  - manual-kind tier → after producing per-board captures in the output dir, ALSO `write_manual_capture` per board into the repo store; tester name defaults to `getpass.getuser()`, email from `git config user.email` (empty → omitted).
  - All imports of coverage machinery live inside the function body (import-budget guard).
- `otto test --cov` change: at the end of `_run_coverage` (after `_write_cov_metadata`), resolve the e2e tier via `load_tiers(cov_config)`/`resolve_get_tier(tiers, None)` and call `produce_captures(cov_dir, tier=<name>, repo_root=cov_repo.sut_dir, labs=[<lab names from repo settings — use cov_repo.name as the single lab label>])`, logging but not failing the run on `GitUnavailableError` (a non-git sut keeps legacy behavior).

- [ ] **Step 1: Write failing CLI tests** in `tests/unit/cli/test_cov.py` (follow that file's existing invocation pattern — read it first; it uses Typer's `CliRunner` or subprocess per existing convention). Cover: `otto cov get --help` exits 0 and shows `--tier`/`--ticket`; manual tier without ticket exits 1 with "ticket" in output; unknown `--tier` exits 1 listing configured tiers. Host-touching paths (fetch) are NOT unit-tested here — the fetch call is monkeypatched to return a prepared cov dir, and the test asserts capture.json files appear in `-o` and (manual) in `.otto/coverage/manual/`.

- [ ] **Step 2: Run — expect failures** (no `get` command).

- [ ] **Step 3: Implement `get`** in `src/otto/cli/cov.py` following the existing `report` command's style (Annotated options, clean `CoverageDataMismatchError`-style error handling, `asyncio.run` around an async `_do_get`). The async body mirrors `_run_coverage`: `bootstrap()`-provided repos → `_get_cov_config`-equivalent (import the helpers from `..cli.test` or re-implement the 6-line lookup locally against `bootstrap().repos` — prefer a small shared helper `get_cov_repo_and_config()` added to `src/otto/coverage/tiers.py` to avoid `cli.test` imports), `all_hosts(pattern)` fetch via `GcdaFetcher(output_dir / "cov")` + `collect_embedded_coverage`, write the meta sidecar (reuse `_write_cov_metadata` by importing it from `.test` — it is module-level and takes explicit args), then `produce_captures(...)`, then manual-store writes when the tier kind is `manual`.

- [ ] **Step 4: Wire `otto test --cov`** per the Interfaces block. Run the CLI unit tests plus the existing test-command suites: `uv run pytest tests/unit/cli/test_cov.py tests/unit/cli -q -x`
Expected: PASS.

- [ ] **Step 5: Lint + commit**

```bash
git add src/otto/cli/cov.py src/otto/cli/test.py src/otto/coverage/tiers.py tests/unit/cli/test_cov.py
git commit -m "feat(cli): otto cov get — single retrieval command; test --cov emits captures"
```

---

### Task 9b: `otto cov clean`

**Files:**
- Modify: `src/otto/cli/cov.py` (add `clean` command)
- Test: extend `tests/unit/cli/test_cov.py`

**Interfaces:**
- Consumes: the same bootstrap/host-discovery path as `cov get` (Task 9), `GcdaFetcher.clean_remote(gcda_remote_dir)` (existing, `src/otto/coverage/fetcher/remote.py`), `[coverage].hosts` pattern + `gcda_remote_dir` from config.
- Produces: `otto cov clean` — zeroes `.gcda` counters on the lab's **Unix** coverage hosts (the same selection `cov get` fetches from). Prints one line per cleaned host. Errors (one clean line, exit 1): no `[coverage]` config; no `gcda_remote_dir` configured; no matching hosts. **Embedded boards are out of scope** — the command logs `"embedded boards not cleaned (requires product-side counter reset — later phase)"` when the lab has embedded coverage hosts, and exits 0. `cov get --clean` becomes a thin call into the same helper.

- [ ] **Step 1: Write failing CLI tests** (same monkeypatch pattern as Task 9): `otto cov clean --help` exits 0; with a stubbed fetcher, `clean` invokes `clean_remote` with the configured dir and exits 0; missing `gcda_remote_dir` exits 1 with "gcda_remote_dir".
- [ ] **Step 2: Run — expect failure (no `clean` command).** `uv run pytest tests/unit/cli/test_cov.py -q`
- [ ] **Step 3: Implement** `clean` in `src/otto/cli/cov.py`: extract Task 9's fetch-side setup (bootstrap → cov config → host pattern → `GcdaFetcher`) into a shared private async helper `_connect_cov_hosts()` used by both `get` and `clean`; `clean`'s body is that helper + `await fetcher.clean_remote(gcda_remote_dir)` + the embedded log line. All coverage imports stay inside the function body (import-budget guard).
- [ ] **Step 4: Run — expect PASS.** `uv run pytest tests/unit/cli/test_cov.py -q`
- [ ] **Step 5: Lint + commit**

```bash
git add src/otto/cli/cov.py tests/unit/cli/test_cov.py
git commit -m "feat(cli): otto cov clean — zero remote .gcda counters"
```

---

### Task 10: Reporter integration

**Files:**
- Modify: `src/otto/coverage/reporter.py` (`run_coverage_report`, `CoverageReporter`)
- Modify: `src/otto/cli/cov.py` (`report` command)
- Test: extend `tests/unit/cov/test_pipeline.py` + `tests/unit/cli/test_cov.py`
- Create: `tests/integration/cov/test_capture_report_cycle.py`

**Interfaces:**
- `run_coverage_report` gains keyword args: `repo_root: Path | None = None`, `tier_configs: list[TierConfig] | None = None`, `extra_markers: list[str] | None = None`. New behavior, in order:
  1. **E2E captures preferred:** for each cov dir, board subdirs containing `capture.json` load via `Capture.load` → pin guard (`capture.pin != gitio.head_commit(repo_root)` → raise `CoverageDataMismatchError` with the remedy text: `"e2e capture <path> was taken at <pin[:12]> but the tree is at <head[:12]>; re-run the test or report from the matching commit"`); matching captures load into the store under their tier via a new small `load_capture_into_store(store, capture, repo_root)` helper (hits + branches, mirroring the validity loader minus states). Board dirs *without* capture.json keep today's gcda-merge path (back-compat).
  2. **Unit harvest:** for each `tier_configs` entry with kind `unit` and non-empty `harvest_dirs`, run the existing merge machinery against those dirs (they are both gcda and gcno root: `LcovMerger.capture(harvest_dir, harvest_dir, ...)`) and `LCOVLoader.load(info, tier.name)`. `.gcda` older than newest `.gcno` under the dir → `logger.warning` and continue.
  3. **Manual store:** when `repo_root` is set, `load_manual_captures(repo_root)` → `apply_manual_capture(store, cap, repo_root, max_age_days=<tier's config>)` for each. Unknown tier names in captures register on the fly.
  4. `store.tier_colors` filled from `tier_configs` (+ `STATE_COLORS` merged in by the renderer, not here).
- `otto cov report` CLI: `output_dirs` becomes optional (`[]` allowed when a manual store exists); resolves `repo_root` via `bootstrap()`'s first repo with coverage config (fall back to `Path.cwd()` when git-less flows use `--tier NAME=PATH` only); passes `tier_configs=load_tiers(cov_config)`.

- [ ] **Step 1: Write the integration test** — the heart of the feature, end to end without lcov (captures only):

```python
# tests/integration/cov/test_capture_report_cycle.py
"""get → modify → report: valid/stale split over real git history."""

import subprocess
from pathlib import Path

import pytest

from otto.coverage.capture.gitio import blob_sha, head_commit
from otto.coverage.capture.model import Capture, CaptureFileCov
from otto.coverage.capture.store_dir import write_manual_capture
from otto.coverage.reporter import run_coverage_report
from otto.coverage.tiers import load_tiers

ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t",
       "GIT_COMMITTER_EMAIL": "t@x", "PATH": "/usr/bin:/bin"}

COV = {
    "tiers": {
        "system": {"kind": "e2e", "precedence": 1},
        "manual": {"kind": "manual", "precedence": 2, "max_age": "180d"},
    }
}


@pytest.mark.asyncio
async def test_manual_survives_unrelated_commit_and_stales_on_edit(tmp_path: Path) -> None:
    repo = tmp_path / "sut"; repo.mkdir()
    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True,
                       env={**ENV, "HOME": str(tmp_path)})
    git("init", "-q")
    (repo / "f.c").write_text("int a;\nint b;\nint c;\n")
    git("add", "f.c"); git("commit", "-qm", "init")

    cap = Capture(tier="manual", pin=head_commit(repo),
                  captured_at="2026-07-01T00:00:00Z", ticket="T-9",
                  labs=["lab1"], board="b1",
                  files={"f.c": CaptureFileCov(blob=blob_sha(repo, Path("f.c")),
                                               lines={2: 4})})
    write_manual_capture(cap, repo)

    # Unrelated commit: line 2 untouched → still covered.
    (repo / "g.c").write_text("int z;\n")
    git("add", "g.c"); git("commit", "-qm", "unrelated")

    report1 = tmp_path / "r1"
    store = await run_coverage_report(
        [], report1, repo_root=repo, tier_configs=load_tiers(COV)
    )
    (frec,) = [f for f in store.files() if f.path.name == "f.c"]
    assert frec.lines[2].hits.for_tier("manual") == 4
    assert frec.lines[2].state is None

    # Edit line 2 → stale.
    (repo / "f.c").write_text("int a;\nint EDITED;\nint c;\n")
    git("commit", "-aqm", "edit line 2")

    report2 = tmp_path / "r2"
    store2 = await run_coverage_report(
        [], report2, repo_root=repo, tier_configs=load_tiers(COV)
    )
    (frec2,) = [f for f in store2.files() if f.path.name == "f.c"]
    assert frec2.lines[2].hits.for_tier("manual") == 0
    assert frec2.lines[2].state == "stale"
    assert (report2 / "index.html").is_file()
    assert store2.provenance and store2.provenance[0]["ticket"] == "T-9"
```

- [ ] **Step 2: Run — expect failure** (`run_coverage_report` has no such kwargs).

- [ ] **Step 3: Implement** per the Interfaces block. Key structure inside `run_coverage_report`: keep the legacy path *exactly* as-is when no `capture.json`s, no `repo_root`, and no `tier_configs` are given (all existing tests must keep passing). The new `load_capture_into_store(store, capture, repo_root)` helper lives in `validity.py` (it is `apply_manual_capture` minus the anchor chain and states — factor the hits/branches insertion into a shared private `_insert_lines`).

- [ ] **Step 4: Also add the e2e pin-guard unit test** (build a capture with `pin="f"*40`, write it under `<out>/cov/board1/capture.json`, expect `CoverageDataMismatchError` naming both shas) in `tests/unit/cov/test_pipeline.py`.

- [ ] **Step 5: Run the affected suites**

Run: `uv run pytest tests/unit/cov tests/unit/cli/test_cov.py tests/integration/cov -q`
Expected: PASS (pre-existing integration test `test_coverage_pipeline.py` must remain green — it exercises the legacy path).

- [ ] **Step 6: Lint + commit**

```bash
git add src/otto/coverage/reporter.py src/otto/coverage/validity.py src/otto/cli/cov.py tests/unit/cov/test_pipeline.py tests/unit/cli/test_cov.py tests/integration/cov/test_capture_report_cycle.py
git commit -m "feat(cov): reporter consumes captures, manual store, unit harvest; e2e pin guard"
```

---

### Task 11: Renderer — colors, legend, states, provenance

**Files:**
- Modify: `src/otto/coverage/renderer/html_renderer.py`
- Modify: `src/otto/coverage/renderer/templates/index.html`, `templates/file.html`
- Modify: `src/otto/coverage/renderer/static/report.css`
- Test: extend `tests/unit/cov/` renderer tests (find the existing renderer test file via `grep -rl HtmlRenderer tests/unit`)

**Interfaces:**
- Consumes: `store.tier_colors`, `store.provenance`, `LineRecord.state`, `scan_excluded_lines` (Task 6), `STATE_COLORS` (Task 1).
- Behavior (spec §9):
  - Renderer resolves per-tier colors: `store.tier_colors.get(tier)` falling back to `DEFAULT_TIER_COLORS` by position/kind and finally `"green"`. Emits them as CSS custom properties in an inline `<style>` block on both pages: `--tier-<index>: <color>;` plus `--state-uncovered/--state-excluded/--state-stale/--state-aging` from `STATE_COLORS`.
  - Line row class resolution order (annotated source): `excluded` (from `scan_excluded_lines` on the rendered source text, using `extra_markers` passed through the reporter) → first tier in `tier_order` with hits on the line (`class="tier-<index>"`, background = that tier's color) → `state == "aging"` → `state == "stale"` → `uncovered`.
  - Legend partial (both pages): one swatch per tier (name + color) + the four states. Implement as a Jinja macro in a new `templates/_legend.html` included from both templates.
  - Index page: per-file `stale`/`aging`/`excluded` counts appended as columns; a "Captures" table below the summary when `store.provenance` is non-empty, columns: Tier, Board, Labs, Date, Tester, Ticket, Note, dirty_remap (render `✎` when true).
  - `store.save` already persists states/provenance/colors (Task 7) — `store.json` needs no renderer change.
- Excluded-line counts: `scan_excluded_lines` runs once per rendered file (the renderer already reads source text to annotate it — reuse that read).

- [ ] **Step 1: Read the current renderer + templates fully** (`html_renderer.py`, `index.html`, `file.html`, `report.css`) and the existing renderer test file. Write failing tests: (a) rendered `file.html` for a store with one covered line (tier `system`, color `#112233`), one stale line, one excluded line (marker in source) asserts the row classes `tier-0`, `state-stale`, `state-excluded` appear and the inline style contains `--tier-0: #112233`; (b) `index.html` for a store with provenance shows the ticket string and the legend contains the tier name.

- [ ] **Step 2: Run — expect failures.**

- [ ] **Step 3: Implement** renderer + templates + CSS. CSS additions (append to `report.css`):

```css
/* Tier/state row coloring driven by inline CSS custom properties. */
tr.tier-0 td.src { background: color-mix(in srgb, var(--tier-0) 22%, white); }
tr.tier-1 td.src { background: color-mix(in srgb, var(--tier-1) 22%, white); }
tr.tier-2 td.src { background: color-mix(in srgb, var(--tier-2) 22%, white); }
tr.tier-3 td.src { background: color-mix(in srgb, var(--tier-3) 22%, white); }
tr.state-uncovered td.src { background: color-mix(in srgb, var(--state-uncovered) 35%, white); }
tr.state-excluded td.src { background: color-mix(in srgb, var(--state-excluded) 18%, white); color: #777; }
tr.state-stale td.src { background: color-mix(in srgb, var(--state-stale) 25%, white); }
tr.state-aging td.src { background: color-mix(in srgb, var(--state-aging) 30%, white); }
.legend { display: flex; gap: 1em; flex-wrap: wrap; margin: 0.6em 0; }
.legend .swatch { display: inline-block; width: 0.9em; height: 0.9em; border: 1px solid #999; vertical-align: middle; margin-right: 0.3em; }
```

Match the actual `td`/class structure found in Step 1 — the selectors above assume a `td.src` cell; adapt to the real markup, keeping the class names (`tier-<i>`, `state-<s>`) as the contract the tests assert.

- [ ] **Step 4: Run renderer tests + full unit cov tree — expect PASS.** `uv run pytest tests/unit/cov -q`

- [ ] **Step 5: Lint + commit**

```bash
git add src/otto/coverage/renderer tests/unit/cov
git commit -m "feat(cov): tier-colored rendering, legend, state rows, provenance table"
```

---

### Task 12: Error-handling conformance sweep

**Files:**
- Modify: `src/otto/cli/cov.py` (catch-clauses), `src/otto/coverage/capture/produce.py`, `src/otto/coverage/validity.py` (message wording)
- Test: extend `tests/unit/cli/test_cov.py`

**Interfaces:** every failure mode from spec §10 exits with **one clean line, cause + remedy, no traceback** (mirror the existing `CoverageDataMismatchError` handling at `cov.py:180-188`):

| Failure | Where raised | Message must contain |
| --- | --- | --- |
| zero counters retrieved | `cov get` | searched host/board names + "no .gcda" |
| manual tier without `--ticket` | `cov get` | "requires --ticket" |
| ambiguous default tier | `resolve_get_tier` → `cov get` | candidate tier names |
| bad color | settings validation (already Task 1) | "color" |
| e2e pin ≠ HEAD | reporter (Task 10) | both short shas + "re-run" |
| outside a git repo | `cov get` | "not a git repository" |
| unverifiable manual pin | `validity.py` warning (not fatal) | ticket + "re-capture" |

- [ ] **Step 1: Write failing tests** asserting exit code 1 and the message fragments above for each `cov get`/`cov report` case (CliRunner, monkeypatched host layer as in Task 9).
- [ ] **Step 2: Run — collect the failures.**
- [ ] **Step 3: Implement** the catch-clauses: `GitUnavailableError`, `ValueError` from tier resolution, and the zero-counter condition each get a `logger.error(str(e))` + `typer.Exit(1)` treatment with `# noqa: TRY400` comments matching the existing pattern.
- [ ] **Step 4: Run — expect PASS.** `uv run pytest tests/unit/cli/test_cov.py -q`
- [ ] **Step 5: Commit:** `git commit -m "fix(cov): clean one-line errors for all new failure modes"` (explicit paths).

---

### Task 13: Documentation

**Files:**
- Modify: `docs/guide/coverage.md` (rewrite the Tiers section + Cookbook around declarative tiers, `otto cov get`, manual captures, attestation-metadata flags, exclusions, colors)
- Modify: `docs/architecture/monitoring-and-coverage.md` (capture pipeline description: get → capture.json → report; validity pass)
- Modify: `docs/api/cli/cov.rst` + add autodoc pages under `docs/api/coverage/` for `capture.model`, `capture.remap`, `validity`, `tiers`, `exclusions`, `colors` (mirror the existing `docs/api/coverage/*` page style)
- Test: `make docs`

**Steps:**
- [ ] **Step 1: Update the guide.** Content requirements: a `[coverage.tiers]` example identical to spec §4; the three-tier workflow walkthrough (e2e via `otto test --cov`, unit via `harvest_dirs`, manual via `otto cov get --tier manual --ticket`); the staleness/aging semantics table (stale = code changed → revoked; aging = old but valid → flagged); the exclusion-marker section; the color/legend section. Remove the now-obsolete hand-made-`.info` cookbook steps for unit/manual (keep the `--tier NAME=PATH` escape hatch documented for git-less flows).
- [ ] **Step 2: Update the architecture page + API pages.** New modules each get an `automodule` page following the existing pattern (check an existing page under `docs/api/coverage/` for the exact directive style; never `automodule` `otto.examples.*`).
- [ ] **Step 3: Run `make docs` and fix every warning** — the `-W` nitpicky gate turns warnings into failures; check the exit code directly, do not pipe through `tail`.
- [ ] **Step 4: Commit:** `git commit -m "docs(cov): declarative tiers, otto cov get, manual captures, validity semantics"`.

---

### Task 14: Full hostless verification sweep

**Files:** none new — verification + fixups only.

- [ ] **Step 1: Full hostless test tree**

Run: `uv run pytest tests/unit tests/integration -q`
Expected: PASS. Fix regressions (notably: settings-schema drift tests, import-budget golden snapshots — if the budget guard fails, find the eager import chain with `python -X importtime -c "import otto" 2>&1 | sort -k2 -n | tail -20` and push the offender behind a function-body import).

- [ ] **Step 2: Lint gate**

Run: `uv run nox -s lint`
Expected: PASS (ruff check + ruff format --check).

- [ ] **Step 3: Type gate**

Run: `uv run nox -s typecheck`
Expected: PASS. Budget real time here — `ty` sees the new modules for the first time.

- [ ] **Step 4: Docs gate re-run:** `make docs` — exit 0.

- [ ] **Step 5: Commit any fixups** (`fix(cov): typecheck/lint fixups from final sweep`), then STOP.

**Deferred, pending user lab go-ahead (do NOT run):** `make coverage`, `make nox`, `tests/e2e/cov/` (repo1 dirty-tree manual flow, repo3 embedded per-board captures), and any new e2e tests for `cov get` against live hosts. These are listed as follow-up work for after the lab is free.

---

## Work items for later phases (this workstream)

Recorded per review; none block this plan:

1. **Embedded counter reset for `otto cov clean`** — needs a product-side `cov_reset` function in the LLEXT extension (mirror of `cov_dump` in `tests/repo3/product/cov_ext.c` / NASA embedded-gcov) plus a console-driven reset in `fetcher/embedded.py`. Until then `cov clean` covers Unix hosts only.
1b. **Custom exclusion markers → lcov rc overrides** — `[coverage.exclusions] markers` currently affects only the renderer's visual scan; marked lines still count in percentages. Wiring the markers into lcov's rc options (`lcov_excl_line` et al) at capture time would make them percentage-affecting like the built-in `LCOV_EXCL_*` set (spec §8 promised this; deferred during implementation — lcov rc key semantics vary by version, needs live verification).
2. **Per-ticket rollups** — next sub-project (spec §12); `ticket` is already recorded on every manual capture.
3. **Remapping stale e2e captures** — opt-in flag to remap old output dirs onto HEAD instead of the pin-guard error.
4. **Frontend color-name capability validation** — verify configured color names against what the React frontend can render.
5. **gcno+DWARF boolean-clause linkage** — independent R&D feeding `store.json`.
6. **React/Vite/TS report frontend** — consumes the `store.json` contract; align with the monitor rework stack.
7. **`--cov-fail-under` + console summary table** — small usability items.
8. **clang source-based coverage** (`-fprofile-instr-generate`).
9. **Live e2e coverage of the new flows** — repo1 manual dirty-tree e2e, repo3 per-board capture e2e, `cov get`/`cov clean` live tests (blocked on lab availability).

---

## Self-review notes (already applied)

- Spec §5 `--clean` semantics: implemented in Task 9 as pre-zeroing before a session (matching `GcdaFetcher.clean_remote`), not post-fetch cleanup.
- Spec §3 `ticket` required on manual: enforced at the CLI boundary (Task 9/12), not in the pydantic model — e2e captures share the model and omit ticket.
- Spec §7 e2e-remap deferral honored: pin guard errors, no silent remap (Task 10).
- The `validity.py` sketch in Task 7 contains a deliberately-flagged redundant block; the task text instructs the implementer to collapse it — tests are the contract.
- Type consistency check: `TierConfig` (Task 2) is consumed by Tasks 9/10 under the same field names; `Capture`/`CaptureFileCov` (Task 5) consumed by 6/7/8/10; `parse_u0_hunks`/`LineRemapper.new_to_old/old_to_new` names match across 4/5/7.

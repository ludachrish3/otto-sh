# Project Dependency Management Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let OTTO_SUT_DIRS repos declare required/optional dependencies on each other, validated (presence, version constraints, satisfiability) in a pass between bootstrap discovery and registration, with dependency-ordered registration.

**Architecture:** A pure parsing/satisfiability module in `models/` (importable by `SettingsModel` — models/ must NEVER import from `config/`), a resolution/ordering module in `config/` consuming `Repo` objects, and wiring in `bootstrap()` that skips repos with failed required deps and registers survivors in stable topological order. Errors join the existing `BootstrapError` containment; a new warnings channel renders at the existing startup site without gating dispatch.

**Tech Stack:** Python 3.12, pydantic v2 (`OttoModel`), Typer CLI, pytest.

**Spec:** `docs/superpowers/specs/2026-07-24-project-dependencies-design.md` — read it before starting.

## Global Constraints

- `from __future__ import annotations` is BANNED repo-wide (breaks Sphinx nitpicky `-W`). Quote forward references instead.
- `src/otto/models/` must never import from `src/otto/config/` (import cycle: `config/__init__` → `repo.py` → `models.settings`). This is why the version regex is deliberately duplicated.
- Prefer lists over tuples in APIs; functions return frozen dataclasses.
- `ty` (typechecker) runs ONLY at `nox -s typecheck` — run it after every src edit, not just at the end.
- Lint gate: `nox -s lint` (ruff check + format --check). Keep rules enforced; fix properly, never exempt.
- Never edit `uv.lock`. `make schema` uses `uv run` internally — that specific target is established, leave it as is.
- Fresh worktree prerequisite: run `uv sync` once before anything (`ty`/pytest need it).
- Commit per task with conventional prefix and trailer: `Assisted-by: Claude Fable 5` (worktree branches: self-commit is OK).
- Version constraint comparison operates on `(major, minor, patch)` int triples ONLY; the `extra` tag never participates.
- Run scoped pytest per task; `make coverage` is the whole-suite gate at the end (do NOT run it per task; no heavy parallel load on this VM).

---

### Task 1: Pure dependency-entry model (`models/dependencies.py`)

**Files:**
- Create: `src/otto/models/dependencies.py`
- Test: `tests/unit/models/test_dependencies.py`

**Interfaces:**
- Consumes: nothing (stdlib only — no otto imports at all).
- Produces (used by Tasks 3, 4, 5):
  - `normalize_name(name: str) -> str`
  - `DependencyClause` frozen dataclass: `op: Op`, `version: tuple[int, int, int]`, method `matches(key: tuple[int, int, int]) -> bool`
  - `ParsedDependency` frozen dataclass: `raw: str`, `name: str`, `normalized: str`, `constraint: str`, `clauses: list[DependencyClause]`, `required: bool`
  - `parse_dependency_entry(entry: str, *, required: bool) -> ParsedDependency` (raises `ValueError`)
  - `clauses_satisfiable(clauses: Sequence[DependencyClause]) -> bool`

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/models/test_dependencies.py`:

```python
"""Dependency-entry parsing, name normalization, and satisfiability."""

import pytest

from otto.models.dependencies import (
    DependencyClause,
    clauses_satisfiable,
    normalize_name,
    parse_dependency_entry,
)


class TestNormalizeName:
    def test_lowercases(self):
        assert normalize_name("MyLib") == "mylib"

    def test_collapses_separator_runs(self):
        assert normalize_name("My_Lib") == "my-lib"
        assert normalize_name("my..__--lib") == "my-lib"

    def test_plain_name_unchanged(self):
        assert normalize_name("vantage") == "vantage"


class TestParseEntry:
    def test_bare_name_means_any_version(self):
        dep = parse_dependency_entry("vantage", required=True)
        assert dep.name == "vantage"
        assert dep.normalized == "vantage"
        assert dep.constraint == ""
        assert dep.clauses == []
        assert dep.required is True

    def test_single_clause(self):
        dep = parse_dependency_entry("vantage >= 2.1", required=False)
        assert dep.constraint == ">= 2.1"
        assert dep.clauses == [DependencyClause(op=">=", version=(2, 1, 0))]
        assert dep.required is False

    def test_comma_anded_clauses_and_zero_padding(self):
        dep = parse_dependency_entry("vantage >= 2.1, < 3", required=True)
        assert dep.clauses == [
            DependencyClause(op=">=", version=(2, 1, 0)),
            DependencyClause(op="<", version=(3, 0, 0)),
        ]

    def test_all_six_operators(self):
        entry = "x == 1, != 2, >= 3, <= 4, > 5, < 6"
        ops = [c.op for c in parse_dependency_entry(entry, required=True).clauses]
        assert ops == ["==", "!=", ">=", "<=", ">", "<"]

    def test_raw_preserved(self):
        dep = parse_dependency_entry("  vantage >= 2.1  ", required=True)
        assert dep.raw == "vantage >= 2.1"

    def test_extra_tag_in_clause_rejected(self):
        with pytest.raises(ValueError, match="extra tags are not allowed"):
            parse_dependency_entry("vantage >= 1.2.3-rc1", required=True)

    def test_bad_name_rejected(self):
        with pytest.raises(ValueError, match="name"):
            parse_dependency_entry("-badname >= 1", required=True)

    def test_name_with_space_rejected(self):
        with pytest.raises(ValueError, match="name"):
            parse_dependency_entry("my lib >= 1", required=True)

    def test_empty_clause_rejected(self):
        with pytest.raises(ValueError, match="invalid clause"):
            parse_dependency_entry("vantage >= 1,, < 2", required=True)

    def test_garbage_clause_rejected(self):
        with pytest.raises(ValueError, match="invalid clause"):
            parse_dependency_entry("vantage ~= 1.2", required=True)


def _clauses(entry: str) -> list[DependencyClause]:
    return parse_dependency_entry(f"x {entry}", required=True).clauses


class TestClauseMatches:
    def test_eq(self):
        (c,) = _clauses("== 1.2.3")
        assert c.matches((1, 2, 3))
        assert not c.matches((1, 2, 4))

    def test_bounds(self):
        lo, hi = _clauses(">= 1.2, < 2")
        assert lo.matches((1, 2, 0)) and hi.matches((1, 2, 0))
        assert not lo.matches((1, 1, 9))
        assert not hi.matches((2, 0, 0))


class TestSatisfiable:
    def test_empty_clause_list(self):
        assert clauses_satisfiable([])

    def test_ordinary_range(self):
        assert clauses_satisfiable(_clauses(">= 1.2, < 2"))

    def test_crossed_bounds(self):
        assert not clauses_satisfiable(_clauses(">= 3, < 2"))

    def test_touching_bounds_inclusive_ok(self):
        assert clauses_satisfiable(_clauses(">= 2, <= 2"))

    def test_touching_bounds_exclusive_empty(self):
        assert not clauses_satisfiable(_clauses(">= 2, < 2"))
        assert not clauses_satisfiable(_clauses("> 2, <= 2"))

    def test_no_triple_between_consecutive_patches(self):
        assert not clauses_satisfiable(_clauses("> 1.2.3, < 1.2.4"))

    def test_conflicting_pins(self):
        assert not clauses_satisfiable(_clauses("== 1.2.3, == 1.2.4"))

    def test_pin_outside_bounds(self):
        assert not clauses_satisfiable(_clauses("== 1.2.3, >= 2"))

    def test_pin_excluded(self):
        assert not clauses_satisfiable(_clauses("== 1.2.3, != 1.2.3"))

    def test_pin_inside_bounds_ok(self):
        assert clauses_satisfiable(_clauses("== 1.5.0, >= 1, < 2"))

    def test_finite_point_set_fully_excluded(self):
        assert not clauses_satisfiable(_clauses(">= 1.2.3, <= 1.2.4, != 1.2.3, != 1.2.4"))

    def test_finite_point_set_partially_excluded_ok(self):
        assert clauses_satisfiable(_clauses(">= 1.2.3, <= 1.2.5, != 1.2.3, != 1.2.4"))

    def test_exclusions_cannot_empty_infinite_range(self):
        assert clauses_satisfiable(_clauses(">= 1.2, < 2, != 1.2.0"))
        assert clauses_satisfiable(_clauses(">= 1.2, != 1.2.0"))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/models/test_dependencies.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'otto.models.dependencies'`

- [ ] **Step 3: Write the implementation**

Create `src/otto/models/dependencies.py`:

```python
"""Dependency-entry parsing and constraint satisfiability for ``[dependencies]``.

Pure module (stdlib only): deliberately free of ``otto.config`` imports so
``models.settings`` can use it at validation time — like the duplicated
version regex, ``models/`` must never trigger the config package's
import-time surface.

Constraint semantics: clauses compare zero-padded ``(major, minor, patch)``
integer triples; a Version's ``extra`` tag never participates, and the
grammar rejects extra tags inside clauses to keep that promise enforceable.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, cast

Op = Literal["==", "!=", ">=", "<=", ">", "<"]

_NORMALIZE_RE = re.compile(r"[-_.]+")
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_CLAUSE_RE = re.compile(r"^(==|!=|>=|<=|>|<)\s*(\d+(?:\.\d+){0,2})$")
_OP_START_RE = re.compile(r"[=!<>]")


def normalize_name(name: str) -> str:
    """PEP-503-style normalization: lowercase, collapse ``[-_.]+`` runs to ``-``."""
    return _NORMALIZE_RE.sub("-", name.lower())


@dataclass(frozen=True)
class DependencyClause:
    """One constraint clause: an operator against a zero-padded version triple."""

    op: Op
    version: tuple[int, int, int]

    def matches(self, key: tuple[int, int, int]) -> bool:
        """True when *key* (a Version's major/minor/patch triple) satisfies this clause."""
        if self.op == "==":
            return key == self.version
        if self.op == "!=":
            return key != self.version
        if self.op == ">=":
            return key >= self.version
        if self.op == "<=":
            return key <= self.version
        if self.op == ">":
            return key > self.version
        return key < self.version


@dataclass(frozen=True)
class ParsedDependency:
    """One parsed ``[dependencies]`` entry."""

    raw: str
    """The entry as written (stripped)."""

    name: str
    """Declared project name, pre-normalization."""

    normalized: str
    """PEP-503-normalized name used for matching."""

    constraint: str
    """Clause text after the name (``""`` = any version)."""

    clauses: list[DependencyClause]
    """Parsed comma-ANDed clauses (empty = any version)."""

    required: bool
    """True for a ``required`` entry, False for ``optional``."""


def parse_dependency_entry(entry: str, *, required: bool) -> ParsedDependency:
    """Parse ``"name"`` or ``"name <op> N[.N[.N]], ..."``; raise ``ValueError`` if malformed."""
    text = entry.strip()
    m = _OP_START_RE.search(text)
    name = text[: m.start()].strip() if m else text
    constraint = text[m.start() :].strip() if m else ""
    if not _NAME_RE.match(name):
        raise ValueError(
            f"dependency {entry!r}: name {name!r} must start with a letter or digit "
            "and contain only letters, digits, '.', '_' and '-'"
        )
    clauses: list[DependencyClause] = []
    if constraint:
        for part in (p.strip() for p in constraint.split(",")):
            cm = _CLAUSE_RE.match(part)
            if cm is None:
                raise ValueError(
                    f"dependency {entry!r}: invalid clause {part!r} "
                    "(format: <op> N[.N[.N]] with op one of == != >= <= > <; "
                    "extra tags are not allowed in constraints)"
                )
            nums = [int(x) for x in cm.group(2).split(".")]
            nums += [0] * (3 - len(nums))
            clauses.append(
                DependencyClause(op=cast("Op", cm.group(1)), version=(nums[0], nums[1], nums[2]))
            )
    return ParsedDependency(
        raw=text,
        name=name,
        normalized=normalize_name(name),
        constraint=constraint,
        clauses=clauses,
        required=required,
    )


def clauses_satisfiable(clauses: Sequence[DependencyClause]) -> bool:
    """True iff at least one version triple satisfies every clause.

    Exactly decidable over integer triples: fold clauses into a lower bound,
    an upper bound, ``==`` pins and ``!=`` exclusions. Empty iff the bounds
    cross, pins conflict (with each other, the bounds, or an exclusion), or
    the bounds confine the range to a finite point set (identical
    major.minor) fully covered by exclusions — an upper-unbounded or
    major/minor-spanning range is infinite, which no finite exclusion set
    can empty.
    """
    lo: tuple[int, int, int] = (0, 0, 0)
    lo_inc = True
    hi: tuple[int, int, int] | None = None
    hi_inc = True
    pins = {c.version for c in clauses if c.op == "=="}
    if len(pins) > 1:
        return False
    exclusions = {c.version for c in clauses if c.op == "!="}
    for c in clauses:
        if c.op in (">", ">="):
            inc = c.op == ">="
            if c.version > lo or (c.version == lo and lo_inc and not inc):
                lo, lo_inc = c.version, inc
        elif c.op in ("<", "<="):
            inc = c.op == "<="
            if hi is None or c.version < hi or (c.version == hi and hi_inc and not inc):
                hi, hi_inc = c.version, inc
    if pins:
        pin = next(iter(pins))
        if pin in exclusions:
            return False
        if pin < lo or (pin == lo and not lo_inc):
            return False
        return not (hi is not None and (pin > hi or (pin == hi and not hi_inc)))
    if hi is not None:
        if lo > hi or (lo == hi and not (lo_inc and hi_inc)):
            return False
        if lo[:2] == hi[:2]:
            start = lo[2] + (0 if lo_inc else 1)
            end = hi[2] - (0 if hi_inc else 1)
            if start > end:
                return False
            if end - start + 1 <= len(exclusions) and all(
                (lo[0], lo[1], p) in exclusions for p in range(start, end + 1)
            ):
                return False
    return True
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/models/test_dependencies.py -v`
Expected: all PASS

- [ ] **Step 5: Lint + typecheck**

Run: `nox -s lint typecheck`
Expected: both pass (fix any findings properly — no ignores)

- [ ] **Step 6: Commit**

```bash
git add src/otto/models/dependencies.py tests/unit/models/test_dependencies.py
git commit -m "feat(deps): pure dependency-entry parser and satisfiability check

Assisted-by: Claude Fable 5"
```

---

### Task 2: Version extra tag

**Files:**
- Modify: `src/otto/config/version.py`
- Modify: `src/otto/models/settings.py:251-253` (`_VERSION_RE` + comment) and `:367-376` (`_validate_version_format` message)
- Test: `tests/unit/config/test_version.py` (extend)

**Interfaces:**
- Produces (used by Tasks 4, 5): `Version.extra: str | None` (includes leading separator, e.g. `"-rc1"`), `Version.key -> tuple[int, int, int]` property, `repr(Version("1.2.3-rc1")) == "1.2.3-rc1"`.
- BEHAVIOR CHANGE: `Version("1.2.3garbage")` previously truncated silently; now raises `ValueError`. Same for `SettingsModel.version`.

- [ ] **Step 1: Check for fixtures/tests relying on loose suffixes**

Run: `grep -rn 'version = "' tests/ --include="*.toml" --include="*.py" | grep -vE '"\d+\.\d+\.\d+"' | grep -v schema`
Expected: no hits with a non-`X.Y.Z` unquoted-garbage suffix. If any hit has a suffix not starting with `-`/`+`/`.`, fix the fixture to a legal version in this task.

Run: `grep -rn "must start with" tests/ src/`
Expected: note every test asserting the old validator message — update them in Step 4.

- [ ] **Step 2: Write the failing tests**

Append to `tests/unit/config/test_version.py`:

```python
def test_version_extra_tag_parsed():
    v = Version("1.2.3-rc1")
    assert (v.major, v.minor, v.patch) == (1, 2, 3)
    assert v.extra == "-rc1"
    assert repr(v) == "1.2.3-rc1"


def test_version_extra_plus_and_dot_separators():
    assert Version("1.2.3+build.5").extra == "+build.5"
    assert Version("1.2.3.dev1").extra == ".dev1"


def test_version_without_extra_has_none():
    v = Version("1.2.3")
    assert v.extra is None
    assert repr(v) == "1.2.3"


def test_version_key_ignores_extra():
    assert Version("1.2.3-rc1").key == (1, 2, 3)
    assert Version("1.2.3").key == (1, 2, 3)


def test_version_garbage_suffix_now_rejected():
    with pytest.raises(ValueError):
        Version("1.2.3garbage")


def test_version_bare_separator_rejected():
    with pytest.raises(ValueError):
        Version("1.2.3-")


def test_settings_regex_drift_lockstep():
    """models/settings.py deliberately duplicates the pattern — keep behavior identical."""
    from otto.config.version import version_re
    from otto.models.settings import _VERSION_RE

    probes = [
        "1.2.3", "1.2.3-rc1", "1.2.3+build.5", "1.2.3.dev1", "10.20.30",
        "1.2.3garbage", "1.2.3-", "1.2", "1.2.3 ", "x.y.z", "",
    ]
    for probe in probes:
        assert (version_re.match(probe) is None) == (_VERSION_RE.match(probe) is None), probe
```

Add `import pytest` at the top if not already present.

- [ ] **Step 3: Run tests to verify they fail**

Run: `pytest tests/unit/config/test_version.py -v`
Expected: new tests FAIL (`extra` attribute missing; garbage accepted; drift probe `1.2.3garbage` mismatches)

- [ ] **Step 4: Implement**

In `src/otto/config/version.py` replace the regex and extend the class:

```python
version_re = compile_re(
    r"(?P<major>\d+)\."
    r"(?P<minor>\d+)\."
    r"(?P<patch>\d+)"
    r"(?P<extra>[-+.][0-9A-Za-z.+-]+)?"
    r"$"
)
```

In the class docstring, replace the last two sentences with: constructed from
``"major.minor.patch"`` plus an optional extra tag beginning with ``-``, ``+``
or ``.`` (e.g. ``1.2.3-rc1``); ``repr`` round-trips the full string; ordering
and constraint matching use :attr:`key` and deliberately ignore ``extra``
(a documented limitation: ``1.2.3-rc1`` compares equal to ``1.2.3`` for
constraint purposes — SemVer prerelease precedence is intentionally not
implemented).

Add the field and property:

```python
    extra: str | None
    """Optional extra tag including its leading separator (``"-rc1"``), or ``None``."""
```

In `__init__`, after the three int assignments:

```python
        self.extra = version_dict["extra"]
```

Replace `__repr__`:

```python
    @override
    def __repr__(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}{self.extra or ''}"
```

Add:

```python
    @property
    def key(self) -> tuple[int, int, int]:
        """The comparison triple — constraint matching deliberately ignores ``extra``."""
        return (self.major, self.minor, self.patch)
```

In `src/otto/models/settings.py`, replace lines 251-253 with:

```python
# settings.toml version format: X.Y.Z with an optional extra tag beginning
# with '-', '+' or '.'. Mirrors config.version.version_re; duplicated (not
# imported) so models/ stays free of the config bootstrap. A drift test in
# tests/unit/config/test_version.py keeps the two in behavioral lockstep.
_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+(?:[-+.][0-9A-Za-z.+-]+)?$")
```

Replace the `_validate_version_format` body's message (and its stale comment about prefix matching):

```python
    @field_validator("version")
    @classmethod
    def _validate_version_format(cls, v: str) -> str:
        if _VERSION_RE.match(v) is None:
            raise ValueError(
                f"version {v!r} must be MAJOR.MINOR.PATCH with an optional "
                "'-', '+' or '.' suffix (e.g. 1.2.3, 1.2.3-rc1)"
            )
        return v
```

Update any tests found in Step 1 asserting the old "must start with" message.

- [ ] **Step 5: Run tests**

Run: `pytest tests/unit/config/test_version.py tests/unit/models/test_settings.py -v`
Expected: all PASS

- [ ] **Step 6: Lint + typecheck**

Run: `nox -s lint typecheck`
Expected: pass

- [ ] **Step 7: Commit**

```bash
git add src/otto/config/version.py src/otto/models/settings.py tests/unit/config/test_version.py
git commit -m "feat(version): formal extra tag on Version; tighten format validation

Assisted-by: Claude Fable 5"
```

(Include any fixture/test files updated in Steps 1/4 in the `git add`.)

---

### Task 3: `[dependencies]` settings surface + Repo declared fields

**Files:**
- Modify: `src/otto/models/settings.py` (new `DependenciesSpec`, field on `SettingsModel`, identity validator)
- Modify: `src/otto/config/repo.py` (two new fields; `parse_settings` assignment)
- Test: `tests/unit/models/test_settings_dependencies.py` (create)

**Interfaces:**
- Consumes: Task 1's `parse_dependency_entry`, `clauses_satisfiable`, `normalize_name` (same-package import — allowed).
- Produces (used by Tasks 4, 6): `SettingsModel.dependencies: DependenciesSpec` (`required: list[str]`, `optional: list[str]`, both default `[]`); `Repo.declared_dependencies: list[ParsedDependency]` (required entries first, then optional, declaration order preserved within each); `Repo.dependencies` placeholder field defaulting to `[]` — in THIS task annotate it `list[Any]` (the `ResolvedDependency` type does not exist yet); Task 4 retypes it to `list["ResolvedDependency"]` with a `TYPE_CHECKING` import.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/models/test_settings_dependencies.py`:

```python
"""[dependencies] table validation on SettingsModel."""

import pytest
from pydantic import ValidationError

from otto.models.settings import SettingsModel

BASE = {"name": "widget", "version": "1.0.0"}


def _model(**dependencies):
    return SettingsModel.model_validate({**BASE, "dependencies": dependencies})


def test_dependencies_default_empty():
    model = SettingsModel.model_validate(BASE)
    assert model.dependencies.required == []
    assert model.dependencies.optional == []


def test_valid_entries_accepted():
    model = _model(required=["vantage >= 2.1, < 3"], optional=["metrics"])
    assert model.dependencies.required == ["vantage >= 2.1, < 3"]


def test_malformed_entry_rejected():
    with pytest.raises(ValidationError, match="invalid clause"):
        _model(required=["vantage ~= 1.2"])


def test_self_contradictory_entry_rejected():
    with pytest.raises(ValidationError, match="can never be satisfied"):
        _model(required=["vantage >= 3, < 2"])


def test_self_dependency_rejected():
    with pytest.raises(ValidationError, match="cannot depend on itself"):
        _model(required=["Widget >= 1"])  # normalized match against name


def test_same_name_in_both_lists_rejected():
    with pytest.raises(ValidationError, match="both required and optional"):
        _model(required=["My_Lib"], optional=["my-lib >= 1"])


def test_unknown_dependencies_key_rejected():
    with pytest.raises(ValidationError):
        _model(requird=["x"])  # typo'd key — extra='forbid'


def test_repo_parses_declared_dependencies(tmp_path):
    from otto.config.repo import Repo

    (tmp_path / ".otto").mkdir()
    (tmp_path / ".otto" / "settings.toml").write_text(
        'name = "widget"\nversion = "1.0.0"\n\n'
        "[dependencies]\n"
        'required = ["vantage >= 2.1"]\noptional = ["metrics"]\n'
    )
    repo = Repo(sut_dir=tmp_path)
    assert [(d.normalized, d.required) for d in repo.declared_dependencies] == [
        ("vantage", True),
        ("metrics", False),
    ]
    assert repo.dependencies == []  # resolution has not run
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/models/test_settings_dependencies.py -v`
Expected: FAIL — `dependencies` unknown field (`extra='forbid'`)

- [ ] **Step 3: Implement the spec**

In `src/otto/models/settings.py`, add to the module imports:

```python
from .dependencies import clauses_satisfiable, normalize_name, parse_dependency_entry
```

(also add `ValidationInfo` to the existing `pydantic` import line.)

Add the spec class before `SettingsModel` (next to the other sub-table specs):

```python
class DependenciesSpec(OttoModel):
    """``[dependencies]`` — inter-project dependencies on other ``OTTO_SUT_DIRS`` repos.

    Entries are ``"name"`` or ``"name <op> N[.N[.N]], ..."``; names match other
    repos' ``name`` fields PEP-503-normalized. Parsed here only to validate —
    the resolution pass re-parses via ``Repo.declared_dependencies``.
    """

    required: list[str] = Field(default_factory=list)
    optional: list[str] = Field(default_factory=list)

    @field_validator("required", "optional")
    @classmethod
    def _validate_entries(cls, v: list[str], info: ValidationInfo) -> list[str]:
        for entry in v:
            parsed = parse_dependency_entry(entry, required=info.field_name == "required")
            if not clauses_satisfiable(parsed.clauses):
                raise ValueError(
                    f"dependency {entry!r} can never be satisfied: "
                    "its clauses are mutually exclusive"
                )
        return v
```

On `SettingsModel`, add the field next to the other structured sub-tables:

```python
    dependencies: DependenciesSpec = DependenciesSpec()
```

and add the cross-field validator after `_validate_host_preferences`:

```python
    @model_validator(mode="after")
    def _validate_dependency_names(self) -> "SettingsModel":
        """Self-dependency and required∩optional are author errors, caught here."""
        req = {
            parse_dependency_entry(e, required=True).normalized
            for e in self.dependencies.required
        }
        opt = {
            parse_dependency_entry(e, required=False).normalized
            for e in self.dependencies.optional
        }
        both = sorted(req & opt)
        if both:
            raise ValueError(
                f"dependencies declared both required and optional: {', '.join(both)}"
            )
        if normalize_name(self.name) in req | opt:
            raise ValueError(f"project {self.name!r} cannot depend on itself")
        return self
```

In `src/otto/config/repo.py`, add two fields after the `tests` field (follow the existing `field(init=False)` style):

```python
    declared_dependencies: list["ParsedDependency"] = field(default_factory=list, init=False)
    """Parsed ``[dependencies]`` entries — required first, then optional, declaration order."""

    dependencies: list[Any] = field(default_factory=list, init=False)  # Task 4 retypes to list["ResolvedDependency"]
    """Per-dependency resolution outcome; populated by bootstrap's dependency pass.

    This list is the runtime query surface: ``bootstrap().repos`` gives global
    name→version, this gives the structured per-repo view."""
```

Add to the `TYPE_CHECKING` imports in repo.py: `from ..models.dependencies import ParsedDependency`.

In `parse_settings`, after `self.init = list(model.init)`, add:

```python
        from ..models.dependencies import parse_dependency_entry

        self.declared_dependencies = [
            parse_dependency_entry(e, required=True) for e in model.dependencies.required
        ] + [parse_dependency_entry(e, required=False) for e in model.dependencies.optional]
```

(The lazy import matches the file's existing pattern for `models` imports.)

- [ ] **Step 4: Run tests**

Run: `pytest tests/unit/models/test_settings_dependencies.py tests/unit/models/test_settings.py tests/unit/config/test_repo.py -v`
Expected: all PASS

- [ ] **Step 5: Lint + typecheck**

Run: `nox -s lint typecheck`
Expected: pass

- [ ] **Step 6: Commit**

```bash
git add src/otto/models/settings.py src/otto/config/repo.py tests/unit/models/test_settings_dependencies.py
git commit -m "feat(deps): [dependencies] settings table and Repo declared fields

Assisted-by: Claude Fable 5"
```

---

### Task 4: Error/warning types + resolution statuses (`config/dependencies.py` part 1)

**Files:**
- Modify: `src/otto/bootstrap.py` (add `DependencyError`, `BootstrapWarning`, `BootstrapResult.warnings`)
- Create: `src/otto/config/dependencies.py`
- Test: `tests/unit/config/test_dependencies_resolution.py` (create)

**Interfaces:**
- Consumes: Task 1 (`normalize_name`, `clauses_satisfiable`, `DependencyClause`), Task 2 (`Version.key`), Task 3 (`Repo.declared_dependencies`).
- Produces (used by Tasks 5, 6, 7):
  - `bootstrap.DependencyError(sut_dir, message)` — subclass of `BootstrapError`; `str()` = `"repo <sut_dir>: <message>"`; `source == "dependencies"`; no `__cause__`.
  - `bootstrap.BootstrapWarning` frozen dataclass: `sut_dir: Any`, `message: str` (message includes its own `repo <dir>:` framing).
  - `bootstrap.BootstrapResult.warnings: list[BootstrapWarning]` (default `[]`).
  - `config.dependencies.ResolvedDependency` frozen dataclass (fields per spec, status `Literal["satisfied", "missing", "incompatible", "ambiguous"]`).
  - Internal seam for Task 5: `_resolve_statuses(repos) -> _StatusOutcome` where `_StatusOutcome` is a frozen dataclass with `errors: list[DependencyError]`, `warnings: list[BootstrapWarning]`, `skip_reason: dict[int, str]` (repo index → root-cause text), `required_edges: set[tuple[int, int]]` ((provider_idx, dependent_idx) for satisfied required deps), `soft_edges: list[tuple[int, int]]` (satisfied optional deps, in (dependent sut-dir index, declaration order)). Side effect: sets `repo.dependencies` on every repo.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/config/test_dependencies_resolution.py`:

```python
"""Dependency resolution: statuses, cross-repo satisfiability, skip set."""

import pytest

from otto.bootstrap import BootstrapWarning, DependencyError
from otto.config.dependencies import _resolve_statuses
from otto.config.repo import Repo


def _repo(tmp_path, name, version="1.0.0", *, required=(), optional=(), dirname=None):
    root = tmp_path / (dirname or name)
    (root / ".otto").mkdir(parents=True)
    req = ", ".join(f'"{e}"' for e in required)
    opt = ", ".join(f'"{e}"' for e in optional)
    (root / ".otto" / "settings.toml").write_text(
        f'name = "{name}"\nversion = "{version}"\n\n'
        f"[dependencies]\nrequired = [{req}]\noptional = [{opt}]\n"
    )
    return Repo(sut_dir=root)


def test_satisfied_required(tmp_path):
    a = _repo(tmp_path, "a", required=["b >= 1"])
    b = _repo(tmp_path, "b", version="1.2.0")
    out = _resolve_statuses([a, b])
    assert out.errors == [] and out.warnings == [] and out.skip_reason == {}
    (dep,) = a.dependencies
    assert dep.status == "satisfied"
    assert dep.provider_version is b.version
    assert out.required_edges == {(1, 0)}


def test_missing_required_errors_and_skips(tmp_path):
    a = _repo(tmp_path, "a", required=["ghost"])
    out = _resolve_statuses([a])
    (err,) = out.errors
    assert isinstance(err, DependencyError)
    assert "no project named 'ghost'" in str(err)
    assert str(a.sut_dir) in str(err)
    assert 0 in out.skip_reason
    assert a.dependencies[0].status == "missing"
    assert a.dependencies[0].provider_version is None


def test_incompatible_required(tmp_path):
    a = _repo(tmp_path, "a", required=["b >= 2"])
    _b = _repo(tmp_path, "b", version="1.2.0")
    out = _resolve_statuses([a, _b])
    (err,) = out.errors
    assert "not satisfied: found b 1.2.0" in str(err)
    assert a.dependencies[0].status == "incompatible"
    assert 0 in out.skip_reason


def test_case_and_punctuation_insensitive_match(tmp_path):
    a = _repo(tmp_path, "a", required=["My_Lib >= 1"])
    _b = _repo(tmp_path, "My.Lib", version="1.0.0", dirname="mylib")
    out = _resolve_statuses([a, _b])
    assert out.errors == []
    assert a.dependencies[0].status == "satisfied"


def test_extra_tag_ignored_by_constraints(tmp_path):
    a = _repo(tmp_path, "a", required=["b == 1.2.3"])
    _b = _repo(tmp_path, "b", version="1.2.3-rc1")
    out = _resolve_statuses([a, _b])
    assert out.errors == []
    assert a.dependencies[0].status == "satisfied"


def test_optional_absent_is_silent(tmp_path):
    a = _repo(tmp_path, "a", optional=["ghost"])
    out = _resolve_statuses([a])
    assert out.errors == [] and out.warnings == [] and out.skip_reason == {}
    assert a.dependencies[0].status == "missing"


def test_optional_incompatible_warns_only(tmp_path):
    a = _repo(tmp_path, "a", optional=["metrics >= 1.4"])
    _m = _repo(tmp_path, "metrics", version="1.2.0")
    out = _resolve_statuses([a, _m])
    assert out.errors == [] and out.skip_reason == {}
    (warn,) = out.warnings
    assert isinstance(warn, BootstrapWarning)
    assert "optional dependency" in warn.message
    assert "found 1.2.0" in warn.message
    assert "feature disabled" in warn.message
    assert warn.message.startswith(f"repo {a.sut_dir}:")
    assert a.dependencies[0].status == "incompatible"
    assert out.soft_edges == []  # incompatible optional contributes no edge


def test_optional_satisfied_soft_edge(tmp_path):
    a = _repo(tmp_path, "a", optional=["metrics >= 1"])
    _m = _repo(tmp_path, "metrics", version="1.4.0")
    out = _resolve_statuses([a, _m])
    assert out.soft_edges == [(1, 0)]


def test_ambiguous_name_errors_when_referenced(tmp_path):
    a = _repo(tmp_path, "a", required=["twin"])
    _t1 = _repo(tmp_path, "twin", dirname="twin1")
    _t2 = _repo(tmp_path, "Twin", dirname="twin2")
    out = _resolve_statuses([a, _t1, _t2])
    (err,) = out.errors
    assert "ambiguous" in str(err)
    assert "twin1" in str(err) and "twin2" in str(err)
    assert a.dependencies[0].status == "ambiguous"
    assert 0 in out.skip_reason


def test_duplicate_names_unreferenced_no_error(tmp_path):
    t1 = _repo(tmp_path, "twin", dirname="twin1")
    t2 = _repo(tmp_path, "twin", dirname="twin2")
    out = _resolve_statuses([t1, t2])
    assert out.errors == []


def test_cross_repo_unsatisfiable_errors_all_participants(tmp_path):
    a = _repo(tmp_path, "a", required=["x >= 2"])
    b = _repo(tmp_path, "b", required=["x < 2"])
    _x = _repo(tmp_path, "x", version="2.5.0")
    out = _resolve_statuses([a, b, _x])
    unsat = [e for e in out.errors if "no possible version" in str(e)]
    assert len(unsat) == 2  # one per participating repo
    for err in unsat:
        assert "a requires" in str(err) and "b requires" in str(err)
    assert 0 in out.skip_reason and 1 in out.skip_reason
    # b ALSO gets the concrete incompatibility error (found 2.5.0)
    assert any("not satisfied: found x 2.5.0" in str(e) for e in out.errors)


def test_cross_repo_unsat_within_one_repo_two_entries(tmp_path):
    a = _repo(tmp_path, "a", required=["x >= 2", "x < 2"])
    _x = _repo(tmp_path, "x", version="2.5.0")
    out = _resolve_statuses([a, _x])
    unsat = [e for e in out.errors if "no possible version" in str(e)]
    assert len(unsat) == 1  # deduped per repo
    assert 0 in out.skip_reason
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/config/test_dependencies_resolution.py -v`
Expected: FAIL — `ImportError` (`DependencyError`, `otto.config.dependencies` missing)

- [ ] **Step 3: Add the bootstrap types**

In `src/otto/bootstrap.py`, after `BootstrapError`:

```python
class DependencyError(BootstrapError):
    """A repo's declared dependencies cannot be satisfied (or a dependency was skipped)."""

    def __init__(self, sut_dir: Any, message: str) -> None:
        """Frame *message* as ``repo <sut_dir>: <message>``."""
        Exception.__init__(self, f"repo {sut_dir}: {message}")
        self.sut_dir = sut_dir
        self.source = "dependencies"


@dataclass(frozen=True)
class BootstrapWarning:
    """A non-fatal dependency finding: rendered at startup, never gates dispatch."""

    sut_dir: Any
    message: str
```

Extend `BootstrapResult`:

```python
    warnings: list[BootstrapWarning] = field(default_factory=list)
```

- [ ] **Step 4: Write the resolution module**

Create `src/otto/config/dependencies.py`:

```python
"""Inter-repo dependency resolution: statuses, satisfiability, skip set, ordering.

Runs inside ``bootstrap()`` between phase-1 discovery and phase-2
registration. Everything here is index-based over the discovered repo list
(``Repo`` defines ``__eq__`` via dataclass, so instances are unhashable).
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from ..bootstrap import BootstrapWarning, DependencyError
from ..models.dependencies import DependencyClause, clauses_satisfiable, normalize_name

if TYPE_CHECKING:
    from ..models.dependencies import ParsedDependency
    from .repo import Repo
    from .version import Version

Status = Literal["satisfied", "missing", "incompatible", "ambiguous"]


@dataclass(frozen=True)
class ResolvedDependency:
    """Resolution outcome for one declared dependency of one repo."""

    name: str
    """Dependency name as declared."""

    normalized: str
    """PEP-503-normalized name used for matching."""

    constraint: str
    """Raw clause text (``""`` = any version)."""

    required: bool
    status: Status
    provider_version: "Version | None"
    """The providing repo's version; ``None`` for ``missing``/``ambiguous``."""


@dataclass(frozen=True)
class _StatusOutcome:
    """Intermediate product of the status pass (Task 5 builds ordering on top)."""

    errors: list[DependencyError]
    warnings: list[BootstrapWarning]
    skip_reason: dict[int, str]
    required_edges: set[tuple[int, int]]
    soft_edges: list[tuple[int, int]]


def _resolve_statuses(repos: "list[Repo]") -> _StatusOutcome:
    """Resolve every declared dep; populate ``repo.dependencies`` on each repo."""
    errors: list[DependencyError] = []
    warnings: list[BootstrapWarning] = []
    skip_reason: dict[int, str] = {}
    required_edges: set[tuple[int, int]] = set()
    soft_edges: list[tuple[int, int]] = []

    providers: dict[str, list[int]] = {}
    for i, repo in enumerate(repos):
        providers.setdefault(normalize_name(repo.name), []).append(i)

    for i, repo in enumerate(repos):
        resolved: list[ResolvedDependency] = []
        for dep in repo.declared_dependencies:
            status, version = _resolve_one(
                i, dep, repos, providers, errors, warnings, required_edges, soft_edges
            )
            resolved.append(
                ResolvedDependency(
                    name=dep.name,
                    normalized=dep.normalized,
                    constraint=dep.constraint,
                    required=dep.required,
                    status=status,
                    provider_version=version,
                )
            )
            if dep.required and status != "satisfied" and i not in skip_reason:
                skip_reason[i] = f"dependency {dep.raw!r} ({status})"
        repo.dependencies = resolved

    _check_combined_satisfiability(repos, errors, skip_reason)
    return _StatusOutcome(
        errors=errors,
        warnings=warnings,
        skip_reason=skip_reason,
        required_edges=required_edges,
        soft_edges=soft_edges,
    )


def _resolve_one(
    i: int,
    dep: "ParsedDependency",
    repos: "list[Repo]",
    providers: dict[str, list[int]],
    errors: list[DependencyError],
    warnings: list[BootstrapWarning],
    required_edges: set[tuple[int, int]],
    soft_edges: list[tuple[int, int]],
) -> "tuple[Status, Version | None]":
    """Status + provider version for one dep; append its error/warning/edge."""
    repo = repos[i]
    candidates = providers.get(dep.normalized, [])
    if len(candidates) > 1:
        if dep.required:
            dirs = ", ".join(str(repos[c].sut_dir) for c in candidates)
            errors.append(
                DependencyError(
                    repo.sut_dir,
                    f"dependency {dep.raw!r}: name {dep.name!r} is ambiguous — "
                    f"provided by {dirs}",
                )
            )
        return "ambiguous", None
    if not candidates:
        if dep.required:
            errors.append(
                DependencyError(
                    repo.sut_dir,
                    f"dependency {dep.raw!r} is not satisfied: no project named "
                    f"{dep.name!r} in OTTO_SUT_DIRS",
                )
            )
        return "missing", None
    provider_idx = candidates[0]
    version = repos[provider_idx].version
    if all(c.matches(version.key) for c in dep.clauses):
        if dep.required:
            required_edges.add((provider_idx, i))
        else:
            soft_edges.append((provider_idx, i))
        return "satisfied", version
    if dep.required:
        errors.append(
            DependencyError(
                repo.sut_dir,
                f"dependency {dep.raw!r} is not satisfied: found {dep.name} {version}",
            )
        )
    else:
        warnings.append(
            BootstrapWarning(
                repo.sut_dir,
                f"repo {repo.sut_dir}: optional dependency {dep.raw!r} not satisfied "
                f"(found {version}) — feature disabled",
            )
        )
    return "incompatible", version


def _check_combined_satisfiability(
    repos: "list[Repo]",
    errors: list[DependencyError],
    skip_reason: dict[int, str],
) -> None:
    """Error every repo whose required constraints on a name can NEVER all hold."""
    by_name: dict[str, list[tuple[int, "ParsedDependency"]]] = {}
    for i, repo in enumerate(repos):
        for dep in repo.declared_dependencies:
            if dep.required:
                by_name.setdefault(dep.normalized, []).append((i, dep))
    for _norm, entries in sorted(by_name.items()):
        if len(entries) < 2:
            continue  # single entries were proven satisfiable at settings parse
        combined: list[DependencyClause] = [c for _, dep in entries for c in dep.clauses]
        if clauses_satisfiable(combined):
            continue
        detail = ", ".join(
            f"{repos[i].name} requires {(dep.constraint or 'any')!r}" for i, dep in entries
        )
        seen: set[int] = set()
        for i, dep in entries:
            if i in seen:
                continue
            seen.add(i)
            errors.append(
                DependencyError(
                    repos[i].sut_dir,
                    f"no possible version of {dep.name!r} satisfies all required "
                    f"constraints: {detail}",
                )
            )
            skip_reason.setdefault(i, f"unsatisfiable combined constraints on {dep.name!r}")
```

Also in `src/otto/config/repo.py`: if Task 3 left `dependencies` annotated as `list[Any]`, change it now to `list["ResolvedDependency"]` with `from .dependencies import ResolvedDependency` added to the `TYPE_CHECKING` block.

Also in `src/otto/config/__init__.py`, add the public re-export alongside `Version`:

```python
from .dependencies import (
    ResolvedDependency as ResolvedDependency,
)
```

- [ ] **Step 5: Run tests**

Run: `pytest tests/unit/config/test_dependencies_resolution.py tests/unit/bootstrap/test_bootstrap.py -v`
Expected: all PASS

- [ ] **Step 6: Lint + typecheck**

Run: `nox -s lint typecheck`
Expected: pass

- [ ] **Step 7: Commit**

```bash
git add src/otto/bootstrap.py src/otto/config/dependencies.py src/otto/config/repo.py src/otto/config/__init__.py tests/unit/config/test_dependencies_resolution.py
git commit -m "feat(deps): resolution statuses, cross-repo satisfiability, error/warning types

Assisted-by: Claude Fable 5"
```

---

### Task 5: Propagation, cycles, stable ordering (`config/dependencies.py` part 2)

**Files:**
- Modify: `src/otto/config/dependencies.py`
- Test: `tests/unit/config/test_dependencies_ordering.py` (create)

**Interfaces:**
- Consumes: Task 4's `_resolve_statuses` / `_StatusOutcome`.
- Produces (used by Task 6):
  - `ResolutionOutcome` frozen dataclass: `ordered: list[Repo]` (non-skipped repos, dependency order), `errors: list[DependencyError]`, `warnings: list[BootstrapWarning]`.
  - `resolve_dependencies(repos: list[Repo]) -> ResolutionOutcome` — the single public entry; side effect: `repo.dependencies` set on every repo.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/config/test_dependencies_ordering.py`:

```python
"""Skip propagation, required cycles, soft edges, stable topological order."""

from otto.config.dependencies import resolve_dependencies
from otto.config.repo import Repo


def _repo(tmp_path, name, version="1.0.0", *, required=(), optional=(), dirname=None):
    root = tmp_path / (dirname or name)
    (root / ".otto").mkdir(parents=True)
    req = ", ".join(f'"{e}"' for e in required)
    opt = ", ".join(f'"{e}"' for e in optional)
    (root / ".otto" / "settings.toml").write_text(
        f'name = "{name}"\nversion = "{version}"\n\n'
        f"[dependencies]\nrequired = [{req}]\noptional = [{opt}]\n"
    )
    return Repo(sut_dir=root)


def _names(repos):
    return [r.name for r in repos]


def test_dep_free_setup_keeps_sut_dir_order(tmp_path):
    repos = [_repo(tmp_path, n) for n in ("c", "a", "b")]
    out = resolve_dependencies(repos)
    assert _names(out.ordered) == ["c", "a", "b"]
    assert out.errors == [] and out.warnings == []


def test_required_dep_registers_provider_first(tmp_path):
    b = _repo(tmp_path, "b", required=["a >= 1"])
    a = _repo(tmp_path, "a")
    out = resolve_dependencies([b, a])  # b listed first in sut dirs
    assert _names(out.ordered) == ["a", "b"]


def test_stable_tiebreak_among_ready(tmp_path):
    # z and m both depend on a; among simultaneously-ready repos sut-dir order holds
    z = _repo(tmp_path, "z", required=["a"])
    m = _repo(tmp_path, "m", required=["a"])
    a = _repo(tmp_path, "a")
    out = resolve_dependencies([z, m, a])
    assert _names(out.ordered) == ["a", "z", "m"]


def test_skip_propagates_to_dependents(tmp_path):
    a = _repo(tmp_path, "a", required=["b"])
    b = _repo(tmp_path, "b", required=["ghost"])
    out = resolve_dependencies([a, b])
    assert out.ordered == []
    msgs = [str(e) for e in out.errors]
    assert any("no project named 'ghost'" in m for m in msgs)
    prop = [m for m in msgs if "was skipped" in m]
    assert len(prop) == 1
    assert "'b'" in prop[0] and "root cause" in prop[0] and "ghost" in prop[0]


def test_propagation_is_dependency_only_not_import_errors(tmp_path):
    # covered again at bootstrap level; here: a healthy dep graph never skips
    a = _repo(tmp_path, "a", required=["b"])
    b = _repo(tmp_path, "b")
    out = resolve_dependencies([a, b])
    assert _names(out.ordered) == ["b", "a"]


def test_required_cycle_errors_and_skips_members(tmp_path):
    a = _repo(tmp_path, "a", required=["b"])
    b = _repo(tmp_path, "b", required=["a"])
    out = resolve_dependencies([a, b])
    assert out.ordered == []
    cyc = [str(e) for e in out.errors if "cycle" in str(e)]
    assert len(cyc) == 2
    assert any("a -> b -> a" in m or "b -> a -> b" in m for m in cyc)


def test_downstream_of_cycle_skipped_with_pointer(tmp_path):
    a = _repo(tmp_path, "a", required=["b"])
    b = _repo(tmp_path, "b", required=["a"])
    c = _repo(tmp_path, "c", required=["a"])
    out = resolve_dependencies([a, b, c])
    assert out.ordered == []
    downstream = [str(e) for e in out.errors if "part of a dependency cycle" in str(e)]
    assert len(downstream) == 1
    assert str(c.sut_dir) in downstream[0]


def test_soft_edge_orders_optional_provider_first(tmp_path):
    b = _repo(tmp_path, "b", optional=["a >= 1"])
    a = _repo(tmp_path, "a")
    out = resolve_dependencies([b, a])
    assert _names(out.ordered) == ["a", "b"]


def test_soft_edge_dropped_on_cycle_no_error(tmp_path):
    # required a->b plus optional b->a: the soft edge would close a cycle — dropped
    a = _repo(tmp_path, "a", required=["b"])
    b = _repo(tmp_path, "b", optional=["a"])
    out = resolve_dependencies([a, b])
    assert _names(out.ordered) == ["b", "a"]  # required edge wins
    assert out.errors == []


def test_absent_optional_contributes_no_edge(tmp_path):
    b = _repo(tmp_path, "b", optional=["ghost"])
    a = _repo(tmp_path, "a")
    out = resolve_dependencies([b, a])
    assert _names(out.ordered) == ["b", "a"]  # sut-dir order preserved
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/config/test_dependencies_ordering.py -v`
Expected: FAIL — `ImportError: cannot import name 'resolve_dependencies'`

- [ ] **Step 3: Implement**

Append to `src/otto/config/dependencies.py` (add `import bisect` to the module imports):

```python
@dataclass(frozen=True)
class ResolutionOutcome:
    """Everything the dependency pass produced for ``bootstrap()``."""

    ordered: "list[Repo]"
    """Non-skipped repos in registration order (stable topo sort, sut-dir tie-break)."""

    errors: list[DependencyError]
    warnings: list[BootstrapWarning]


def resolve_dependencies(repos: "list[Repo]") -> ResolutionOutcome:
    """Resolve declared dependencies; return ordering, errors, warnings.

    Side effect: populates ``repo.dependencies`` on every repo (including
    skipped ones — the statuses are the diagnostic).
    """
    status = _resolve_statuses(repos)
    errors = list(status.errors)
    skip_reason = dict(status.skip_reason)

    _propagate_skips(repos, status.required_edges, skip_reason, errors)
    alive = [i for i in range(len(repos)) if i not in skip_reason]
    survivors = _skip_required_cycles(repos, alive, status.required_edges, skip_reason, errors)
    order = _stable_topo_order(survivors, status.required_edges, status.soft_edges)
    return ResolutionOutcome(
        ordered=[repos[i] for i in order],
        errors=errors,
        warnings=status.warnings,
    )


def _propagate_skips(
    repos: "list[Repo]",
    required_edges: set[tuple[int, int]],
    skip_reason: dict[int, str],
    errors: list[DependencyError],
) -> None:
    """BFS along satisfied-required edges: a dependent of a skipped repo is skipped."""
    dependents: dict[int, list[int]] = {}
    for provider, dependent in sorted(required_edges):
        dependents.setdefault(provider, []).append(dependent)
    queue = sorted(skip_reason)
    while queue:
        current = queue.pop(0)
        for dependent in dependents.get(current, []):
            if dependent in skip_reason:
                continue
            skip_reason[dependent] = skip_reason[current]
            errors.append(
                DependencyError(
                    repos[dependent].sut_dir,
                    f"skipped: required dependency {repos[current].name!r} "
                    f"(repo {repos[current].sut_dir}) was skipped — "
                    f"root cause: {skip_reason[current]}",
                )
            )
            queue.append(dependent)


def _skip_required_cycles(
    repos: "list[Repo]",
    alive: list[int],
    required_edges: set[tuple[int, int]],
    skip_reason: dict[int, str],
    errors: list[DependencyError],
) -> list[int]:
    """Kahn over required edges; leftover nodes are in (or downstream of) a cycle."""
    indeg = dict.fromkeys(alive, 0)
    out: dict[int, list[int]] = {i: [] for i in alive}
    preds: dict[int, list[int]] = {i: [] for i in alive}
    for provider, dependent in sorted(required_edges):
        if provider in indeg and dependent in indeg:
            out[provider].append(dependent)
            preds[dependent].append(provider)
            indeg[dependent] += 1
    ready = sorted(i for i in alive if indeg[i] == 0)
    visited: set[int] = set()
    while ready:
        current = ready.pop(0)
        visited.add(current)
        for dependent in out[current]:
            indeg[dependent] -= 1
            if indeg[dependent] == 0:
                bisect.insort(ready, dependent)
    leftover = [i for i in alive if i not in visited]
    if not leftover:
        return alive
    in_cycle: set[int] = set()
    for cycle in _find_cycles(leftover, preds):
        in_cycle.update(cycle)
        path = " -> ".join(repos[i].name for i in [*cycle, cycle[0]])
        for i in cycle:
            skip_reason[i] = f"required dependency cycle: {path}"
            errors.append(
                DependencyError(repos[i].sut_dir, f"required dependency cycle: {path}")
            )
    for i in leftover:
        if i not in in_cycle:
            skip_reason[i] = "downstream of a required dependency cycle"
            errors.append(
                DependencyError(
                    repos[i].sut_dir,
                    "skipped: a required dependency is part of a dependency cycle "
                    "(see cycle errors above)",
                )
            )
    return [i for i in alive if i not in skip_reason]


def _find_cycles(leftover: list[int], preds: dict[int, list[int]]) -> list[list[int]]:
    """Walk predecessors within *leftover* — every node there has one, so a walk
    must revisit its own path; the revisited segment is a cycle (reversed for
    dependency-direction display). Each node lands in at most one cycle."""
    leftover_set = set(leftover)
    assigned: set[int] = set()
    cycles: list[list[int]] = []
    for start in leftover:
        if start in assigned:
            continue
        path: list[int] = []
        on_path: dict[int, int] = {}
        current = start
        while current not in assigned and current not in on_path:
            on_path[current] = len(path)
            path.append(current)
            current = next(p for p in preds[current] if p in leftover_set)
        if current in on_path:
            cycle = list(reversed(path[on_path[current] :]))
            cycles.append(cycle)
            assigned.update(path)
        else:
            assigned.update(path)
    return cycles


def _stable_topo_order(
    survivors: list[int],
    required_edges: set[tuple[int, int]],
    soft_edges: list[tuple[int, int]],
) -> list[int]:
    """Kahn with a sut-dir-ordered ready queue; soft edges dropped if cycle-closing."""
    graph: dict[int, set[int]] = {i: set() for i in survivors}
    for provider, dependent in sorted(required_edges):
        if provider in graph and dependent in graph:
            graph[provider].add(dependent)
    for provider, dependent in soft_edges:  # already (dependent sut-dir, declaration) order
        if provider not in graph or dependent not in graph:
            continue
        if _reachable(graph, start=dependent, target=provider):
            continue  # soft edge would close a cycle — drop silently
        graph[provider].add(dependent)
    indeg = dict.fromkeys(survivors, 0)
    for _provider, dependent in ((p, d) for p, targets in graph.items() for d in targets):
        indeg[dependent] += 1
    ready = sorted(i for i in survivors if indeg[i] == 0)
    order: list[int] = []
    while ready:
        current = ready.pop(0)
        order.append(current)
        for dependent in sorted(graph[current]):
            indeg[dependent] -= 1
            if indeg[dependent] == 0:
                bisect.insort(ready, dependent)
    return order


def _reachable(graph: dict[int, set[int]], *, start: int, target: int) -> bool:
    """True when *target* is reachable from *start* along *graph* edges."""
    stack = [start]
    seen = {start}
    while stack:
        node = stack.pop()
        if node == target:
            return True
        for nxt in graph.get(node, ()):
            if nxt not in seen:
                seen.add(nxt)
                stack.append(nxt)
    return False
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/unit/config/test_dependencies_ordering.py tests/unit/config/test_dependencies_resolution.py -v`
Expected: all PASS

- [ ] **Step 5: Lint + typecheck**

Run: `nox -s lint typecheck`
Expected: pass

- [ ] **Step 6: Commit**

```bash
git add src/otto/config/dependencies.py tests/unit/config/test_dependencies_ordering.py
git commit -m "feat(deps): skip propagation, cycle detection, stable topo ordering

Assisted-by: Claude Fable 5"
```

---

### Task 6: Bootstrap wiring

**Files:**
- Modify: `src/otto/bootstrap.py:76-96` (`bootstrap()`) and the module docstring
- Test: `tests/unit/bootstrap/test_bootstrap_dependencies.py` (create)

**Interfaces:**
- Consumes: Task 5's `resolve_dependencies` / `ResolutionOutcome`.
- Produces: `bootstrap().warnings` populated; phase-2 registration runs only over `resolution.ordered`, in that order. `bootstrap().repos` still contains ALL discovered repos (skipped ones included — with their `dependencies` statuses).

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/bootstrap/test_bootstrap_dependencies.py`:

```python
"""bootstrap(): dependency pass wiring — skip, warnings, registration order."""

import pytest

from otto import bootstrap as bs


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    bs._reset()
    yield
    bs._reset()


def _write_repo(tmp_path, name, version="1.0.0", *, required=(), optional=()) -> str:
    """A repo whose init module appends its name to $OTTO_TEST_ORDER_FILE."""
    repo = tmp_path / name
    (repo / ".otto").mkdir(parents=True)
    req = ", ".join(f'"{e}"' for e in required)
    opt = ", ".join(f'"{e}"' for e in optional)
    (repo / ".otto" / "settings.toml").write_text(
        f'name = "{name}"\nversion = "{version}"\n'
        f'libs = ["${{sut_dir}}"]\ninit = ["{name}_init"]\n\n'
        f"[dependencies]\nrequired = [{req}]\noptional = [{opt}]\n"
    )
    (repo / f"{name}_init.py").write_text(
        "import os, pathlib\n"
        "with pathlib.Path(os.environ['OTTO_TEST_ORDER_FILE']).open('a') as f:\n"
        f"    f.write('{name}\\n')\n"
    )
    return str(repo)


@pytest.fixture
def order_file(tmp_path, monkeypatch):
    path = tmp_path / "order.txt"
    path.touch()
    monkeypatch.setenv("OTTO_TEST_ORDER_FILE", str(path))
    return path


def _order(order_file):
    return order_file.read_text().split()


def test_dep_free_setup_registers_in_sut_dir_order(tmp_path, monkeypatch, order_file):
    dirs = [_write_repo(tmp_path, n) for n in ("c", "a", "b")]
    monkeypatch.setenv("OTTO_SUT_DIRS", ",".join(dirs))
    result = bs.bootstrap()
    assert result.errors == [] and result.warnings == []
    assert _order(order_file) == ["c", "a", "b"]


def test_required_dep_reorders_registration(tmp_path, monkeypatch, order_file):
    b = _write_repo(tmp_path, "b", required=["a >= 1"])
    a = _write_repo(tmp_path, "a")
    monkeypatch.setenv("OTTO_SUT_DIRS", f"{b},{a}")
    result = bs.bootstrap()
    assert result.errors == []
    assert _order(order_file) == ["a", "b"]


def test_missing_required_skips_registration(tmp_path, monkeypatch, order_file):
    a = _write_repo(tmp_path, "a", required=["ghost"])
    b = _write_repo(tmp_path, "b")
    monkeypatch.setenv("OTTO_SUT_DIRS", f"{a},{b}")
    result = bs.bootstrap()
    assert _order(order_file) == ["b"]  # a never registered
    assert len(result.repos) == 2  # but still discovered/visible
    (err,) = result.errors
    assert "ghost" in str(err)
    a_repo = next(r for r in result.repos if r.name == "a")
    assert a_repo.dependencies[0].status == "missing"


def test_optional_incompatible_warns_and_registers(tmp_path, monkeypatch, order_file):
    a = _write_repo(tmp_path, "a", optional=["metrics >= 1.4"])
    m = _write_repo(tmp_path, "metrics", version="1.2.0")
    monkeypatch.setenv("OTTO_SUT_DIRS", f"{a},{m}")
    result = bs.bootstrap()
    assert result.errors == []
    (warn,) = result.warnings
    assert "feature disabled" in warn.message
    assert sorted(_order(order_file)) == ["a", "metrics"]


def test_skip_propagation_through_bootstrap(tmp_path, monkeypatch, order_file):
    a = _write_repo(tmp_path, "a", required=["b"])
    b = _write_repo(tmp_path, "b", required=["ghost"])
    monkeypatch.setenv("OTTO_SUT_DIRS", f"{a},{b}")
    result = bs.bootstrap()
    assert _order(order_file) == []
    assert len(result.errors) == 2  # missing + propagation


def test_import_error_does_not_propagate_like_dep_failure(tmp_path, monkeypatch, order_file):
    # b's init module raises; a requires b (satisfied). a must STILL register.
    a = _write_repo(tmp_path, "a", required=["b"])
    b = _write_repo(tmp_path, "b")
    (tmp_path / "b" / "b_init.py").write_text("raise RuntimeError('boom')\n")
    monkeypatch.setenv("OTTO_SUT_DIRS", f"{a},{b}")
    result = bs.bootstrap()
    assert _order(order_file) == ["a"]  # b's marker never ran, a's did
    assert len(result.errors) == 1  # only the contained import error
    assert "failed to load" in str(result.errors[0])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/bootstrap/test_bootstrap_dependencies.py -v`
Expected: FAIL — ordering tests see sut-dir order; skip tests see the repo registered; `warnings` empty

- [ ] **Step 3: Implement**

In `src/otto/bootstrap.py`, replace the `bootstrap()` body's registration section:

```python
def bootstrap() -> BootstrapResult:
    """Run the composition root (idempotent): discovery + dependency pass + contained registration."""
    global _result  # noqa: PLW0603 — module-level singleton/cache
    if _result is not None:
        return _result
    env, repos = discover()
    errors: list[BootstrapError] = list(_discovery_errors)
    from .config.dependencies import resolve_dependencies

    resolution = resolve_dependencies(repos)
    errors.extend(resolution.errors)
    for repo in resolution.ordered:
        repo.add_libs_to_pythonpath()
        for mod in repo.init:
            try:
                importlib.import_module(mod)
            except Exception as e:  # noqa: PERF203,BLE001 — containment seam: per-item resilience, ANY user-code failure becomes a framed error
                errors.append(BootstrapError(repo.sut_dir, mod, e))
        for test_file in repo.iter_test_files():
            try:
                repo.import_test_file(test_file)
            except Exception as e:  # noqa: PERF203,BLE001 — containment seam: per-item resilience, ANY user-code failure becomes a framed error
                errors.append(BootstrapError(repo.sut_dir, test_file.name, e))
    _result = BootstrapResult(env=env, repos=repos, errors=errors, warnings=resolution.warnings)
    return _result
```

Update the module docstring's phase description: between phase 1 and phase 2 the
*dependency pass* (``config.dependencies``) validates each repo's declared
dependencies, skips repos whose required deps are unsatisfied (framed
``DependencyError``s), and orders phase-2 registration topologically
(stable — sut-dir order when no deps are declared).

- [ ] **Step 4: Run tests**

Run: `pytest tests/unit/bootstrap/ -v`
Expected: all PASS (including the pre-existing `test_bootstrap.py` — none of its repos declare deps, so order/behavior is unchanged)

- [ ] **Step 5: Lint + typecheck**

Run: `nox -s lint typecheck`
Expected: pass

- [ ] **Step 6: Commit**

```bash
git add src/otto/bootstrap.py tests/unit/bootstrap/test_bootstrap_dependencies.py
git commit -m "feat(deps): wire dependency pass into bootstrap; ordered, skip-aware registration

Assisted-by: Claude Fable 5"
```

---

### Task 7: CLI render + dispatch gate

**Files:**
- Modify: `src/otto/cli/main.py:647-648` (add warnings loop after the errors loop)
- Test: `tests/unit/cli/test_bootstrap_gate.py` (create)

**Interfaces:**
- Consumes: Task 6's `BootstrapResult.warnings`; existing `fail_loud_on_bootstrap_errors()` at `src/otto/cli/invoke.py:404`.
- Produces: `_emit_bootstrap_findings(result: BootstrapResult) -> None` in `cli/main.py` — the nameable render site: one `warning: <str(err)>` stderr line per error, then one `warning: <warn.message>` line per warning. `fail_loud_on_bootstrap_errors` is NOT modified — the test pins that warnings never gate dispatch.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/cli/test_bootstrap_gate.py`:

```python
"""Dispatch gate: errors block, warnings never do; entry renders both."""

import pytest
import typer

from otto import bootstrap as bs
from otto.cli.invoke import fail_loud_on_bootstrap_errors


@pytest.fixture(autouse=True)
def _fresh():
    bs._reset()
    yield
    bs._reset()


def _install_result(monkeypatch, *, errors=(), warnings=()):
    result = bs.BootstrapResult(env=None, repos=[], errors=list(errors), warnings=list(warnings))
    monkeypatch.setattr(bs, "_result", result)
    return result


def test_gate_ignores_warnings(monkeypatch):
    _install_result(
        monkeypatch,
        warnings=[bs.BootstrapWarning(sut_dir="x", message="repo x: optional dependency down")],
    )
    fail_loud_on_bootstrap_errors()  # must not raise


def test_gate_still_blocks_on_errors(monkeypatch):
    _install_result(monkeypatch, errors=[bs.DependencyError("x", "dependency 'y' is not satisfied")])
    with pytest.raises(typer.Exit):
        fail_loud_on_bootstrap_errors()


def test_dependency_error_framing():
    err = bs.DependencyError("/suts/a", "dependency 'b >= 2' is not satisfied: found b 1.0.0")
    assert str(err) == "repo /suts/a: dependency 'b >= 2' is not satisfied: found b 1.0.0"
    assert err.source == "dependencies"


def test_emit_renders_errors_then_warnings(capsys):
    from otto.cli.main import _emit_bootstrap_findings

    result = bs.BootstrapResult(
        env=None,
        repos=[],
        errors=[bs.DependencyError("/suts/a", "dependency 'b' is not satisfied")],
        warnings=[bs.BootstrapWarning(sut_dir="/suts/c", message="repo /suts/c: optional dependency 'm >= 2' not satisfied (found 1.0.0) — feature disabled")],
    )
    _emit_bootstrap_findings(result)
    err_out = capsys.readouterr().err
    assert "warning: repo /suts/a: dependency 'b' is not satisfied\n" in err_out
    assert "warning: repo /suts/c: optional dependency 'm >= 2' not satisfied (found 1.0.0) — feature disabled\n" in err_out
    assert err_out.index("/suts/a") < err_out.index("/suts/c")  # errors first
```

Note: `env=None` is fine at runtime (the result is a plain dataclass); if `ty`
flags it, construct with `env=None  # type: ignore[arg-type]` is NOT the way —
instead build a minimal `OttoEnvSettings()` via
`otto.models.settings.OttoEnvSettings(sut_dirs=[])` (check its constructor
defaults in `src/otto/models/settings.py:433` and use the minimal valid form).

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/cli/test_bootstrap_gate.py -v`
Expected: `test_emit_renders_errors_then_warnings` FAILS (`ImportError: cannot import name '_emit_bootstrap_findings'`); the three gate/framing tests PASS already (their types landed in Task 4 — they pin existing behavior).

- [ ] **Step 3: Implement the render helper**

In `src/otto/cli/main.py`, add a module-level function near `entry()` (this is THE nameable render site for bootstrap findings):

```python
def _emit_bootstrap_findings(result: "BootstrapResult") -> None:
    """Startup render site for contained bootstrap findings: errors, then warnings.

    Errors gate dispatch later (``fail_loud_on_bootstrap_errors``); warnings
    never do — both surface here as ``warning:`` stderr lines.
    """
    for err in result.errors:
        typer.echo(f"warning: {err}", err=True)
    for warn in result.warnings:
        typer.echo(f"warning: {warn.message}", err=True)
```

(Import `BootstrapResult` under `TYPE_CHECKING` from `..bootstrap` if not already available in the module.) Then replace the existing inline loop at line 647-648:

```python
        _emit_bootstrap_findings(result)
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/unit/cli/test_bootstrap_gate.py tests/unit/cli/ -v`
Expected: all PASS

- [ ] **Step 5: Lint + typecheck**

Run: `nox -s lint typecheck`
Expected: pass

- [ ] **Step 6: Commit**

```bash
git add src/otto/cli/main.py tests/unit/cli/test_bootstrap_gate.py
git commit -m "feat(deps): render dependency warnings at startup without gating dispatch

Assisted-by: Claude Fable 5"
```

---

### Task 8: `otto init` template + schema refresh

**Files:**
- Modify: `src/otto/cli/init_templates.py:13-32` (`SETTINGS_TEMPLATE`)
- Test: `tests/unit/cli/test_init_scaffold.py` (extend)

**Interfaces:**
- Consumes: `DependenciesSpec` (Task 3) — the scaffolded file must validate through `SettingsModel` unchanged (the block is fully commented out).
- Produces: commented `[dependencies]` block in every scaffolded `settings.toml`.

- [ ] **Step 1: Write the failing test**

Add to `tests/unit/cli/test_init_scaffold.py` (mimic the existing template-substring assertions around line 45):

```python
def test_settings_template_has_commented_dependencies_block(tmp_path):
    from otto.cli.init_templates import SETTINGS_TEMPLATE

    text = SETTINGS_TEMPLATE.format(name="widget", version="0.1.0", init_module="widget_instructions")
    assert "#[dependencies]" in text
    assert '#required = ["other-project >= 1.0"]' in text
    assert '#optional = ["nice-to-have-project"]' in text
```

Check how existing tests in the file render the template (some go through the
scaffold area function rather than `.format()` directly) — follow the file's
existing pattern if it differs from the above.

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/cli/test_init_scaffold.py -v`
Expected: new test FAILS (`#[dependencies]` not in template)

- [ ] **Step 3: Implement**

In `src/otto/cli/init_templates.py`, insert after the `version = "{version}"` line (before the "Where otto looks for things" comment block), following the file's sshd_config comment convention:

```text
# --- [dependencies] — other OTTO_SUT_DIRS projects this repo depends on ------
# Entries are "name" or "name <op> X.Y.Z[, <op> X.Y.Z ...]"; names match other
# repos' `name` fields (case/punctuation-insensitive). Required deps must be
# present and compatible or this repo fails to load; optional deps warn when
# present but incompatible.
#[dependencies]
#required = ["other-project >= 1.0"]
#optional = ["nice-to-have-project"]
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/unit/cli/test_init_scaffold.py tests/unit/cli/test_init_validate.py tests/unit/models/test_jsonschema.py -v`
Expected: all PASS (schema tests confirm `DependenciesSpec` flows into the generated schema)

- [ ] **Step 5: Refresh the local editor schemas**

Run: `make schema`
Expected: regenerates `schemas/` (git-ignored — nothing to commit from this)

- [ ] **Step 6: Lint + typecheck**

Run: `nox -s lint typecheck`
Expected: pass

- [ ] **Step 7: Commit**

```bash
git add src/otto/cli/init_templates.py tests/unit/cli/test_init_scaffold.py
git commit -m "feat(deps): scaffold commented [dependencies] block in otto init template

Assisted-by: Claude Fable 5"
```

---

### Task 9: Docs + whole-suite gates

**Files:**
- Modify: `docs/guide/setup/repo-setup.md` (extend the "Multiple repos" section)

**Interfaces:**
- Consumes: everything above (documentation of final behavior).

- [ ] **Step 1: Write the docs**

In `docs/guide/setup/repo-setup.md`, extend the `## Multiple repos` section with a `### Declaring dependencies between repos` subsection containing, in order: the intro sentence, a `toml` code block with the example table, and the semantics prose/bullets below.

~~~markdown
### Declaring dependencies between repos

When one repo's libs or tests build on another's, declare it in
`.otto/settings.toml`:

```toml
[dependencies]
required = ["vantage >= 2.1, < 3", "common-libs"]
optional = ["metrics >= 1.4"]
```

Entries are a project name (matched against other repos' `name` fields,
case- and punctuation-insensitively) optionally followed by comma-ANDed
version constraints using `==`, `!=`, `>=`, `<=`, `>`, `<`. Versions may be
shortened (`< 3` means `< 3.0.0`). A version's extra tag (`1.2.3-rc1`) is
never compared — `1.2.3-rc1` satisfies `>= 1.2.3` — and constraints may not
carry one.

At startup, otto validates the declarations after discovering every repo in
`OTTO_SUT_DIRS`:

- A **required** dependency that is missing or version-incompatible fails
  that repo loudly (its instructions and tests do not load; other repos are
  unaffected). Repos that require a failed repo are skipped too, with the
  root cause named.
- An **optional** dependency that is absent is fine. Present but
  incompatible prints a startup warning (the feature stays disabled) and
  never blocks commands.
- otto also checks that a compatible version is *possible* at all: if two
  repos' required constraints on the same project can never both hold
  (`>= 2` vs `< 2`), every participant gets an error naming all the
  constraints — no version hunt can fix a contradictory declaration set.
- Registration order follows the dependency graph (a dependency's libs and
  init modules load before its dependents'). Repos with no declared
  dependencies keep `OTTO_SUT_DIRS` order exactly.

Inspect the outcome at runtime via `otto.config.get_repos()` — each repo
carries a `dependencies` list with per-dependency status and the provider's
version.
~~~

- [ ] **Step 2: Docs build (clean — incremental Sphinx misses broken refs)**

Run: `nox -s docs`
Expected: PASS with zero warnings (the session builds clean by default; if it errors on `:doc:` refs, fix them — never relax `-W`)

- [ ] **Step 3: Whole-suite gate**

Run: `make coverage`
Expected: PASS. This is the sanctioned per-task gate for the repo — scoped pytest passing does NOT guarantee the suite passes. Budget real time for it; do not kill it at a tight timeout.

- [ ] **Step 4: Commit**

```bash
git add docs/guide/setup/repo-setup.md
git commit -m "docs(deps): document [dependencies] declarations in repo setup guide

Assisted-by: Claude Fable 5"
```

---

## Completion checklist (mapping back to the spec)

- Declaration surface + grammar → Tasks 1, 3
- Version extra tag + drift lockstep → Task 2
- Resolution pass, statuses, ambiguous-only-when-referenced → Task 4
- Per-entry satisfiability (settings) → Task 3; cross-repo satisfiability → Task 4
- Failure semantics: DependencyError containment, skip + propagate, warn-on-optional → Tasks 4, 5, 6
- Required cycles + downstream skip → Task 5
- Stable topo ordering + soft edges → Tasks 5, 6
- Startup render + dispatch gate untouched by warnings → Task 7
- `otto init` scaffold + schema flow → Task 8
- Docs + full gates → Task 9

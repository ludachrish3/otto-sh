# Project dependency management — design

**Date:** 2026-07-24
**Status:** approved for planning

## Problem

Projects in `OTTO_SUT_DIRS` frequently depend on one another (one repo's init
modules and tests import from another repo's libs), but otto has no way to
declare, validate, or order those relationships. Worse, sut-dir order is
already silently load-bearing: `bootstrap()` adds each repo's libs to
`PYTHONPATH` and imports its init modules *before the next repo's libs are
added* (`src/otto/bootstrap.py:83-89`), so a repo that imports from a
later-listed repo breaks — and the failure is an unattributed `ImportError`.

otto is not a package manager: nothing is fetched or resolved, the project set
is already materialized on disk. The model is the *checker* half of dependency
tooling (`pip check`, Debian's install-time verification), plus deterministic
registration ordering. Constraint solving, lock files, and transitive closure
beyond declared edges are explicitly out of scope.

## Declaration surface

New `[dependencies]` table in `.otto/settings.toml`, both lists empty by
default:

```toml
[dependencies]
required = ["vantage >= 2.1, < 3", "common-libs"]
optional = ["metrics >= 1.4"]
```

Grammar per entry (whitespace-tolerant):

```
entry   := name [ clause ("," clause)* ]
name    := [A-Za-z0-9][A-Za-z0-9._-]*
clause  := op version
op      := "==" | "!=" | ">=" | "<=" | ">" | "<"
version := N | N.N | N.N.N        # zero-padded to 3 components; NO extra tag
```

- A bare name means "any version".
- Clause versions may be shortened: `< 3` ≡ `< 3.0.0`, `== 1.2` ≡ `== 1.2.0`.
- Constraint clauses may **not** carry an extra tag (`>= 1.2.3-rc1` is a parse
  error). Extras are never compared, and the parser enforces that promise.
- Name matching is PEP-503-normalized on both sides: lowercase, collapse
  `[-_.]+` runs to `-` (so `My_Lib` matches `my-lib`).

Validation lives in a new `DependenciesSpec` on `SettingsModel`
(`src/otto/models/settings.py`), so a malformed entry is a per-repo contained
config error at discovery, like any other settings failure. Settings-level
errors (independent of other repos): a self-dependency, the same normalized
name in both `required` and `optional`, or an entry whose own clauses are
unsatisfiable (`"vantage >= 3, < 2"` — no version can ever match; the error
names the conflicting clauses).

`Repo` stores the parsed entries as `declared_dependencies` (list of a small
`DependencySpec`-style parsed form: name, normalized name, clause list,
required flag).

## Version extra tag

`config.version.Version` grows `extra: str | None`:

- Grammar: `X.Y.Z` optionally followed by a suffix beginning with `-`, `+`,
  or `.`, then at least one character from `[0-9A-Za-z.+-]`. The stored
  `extra` includes the leading separator (`"-rc1"`); `repr` round-trips the
  full string, so repo panels display it for free.
- This **tightens** the current prefix-match: `1.2.3-rc1` parses formally,
  `1.2.3garbage` becomes a validation error instead of silently truncating.
- Comparison operators ignore `extra` entirely: `1.2.3-rc1` satisfies both
  `>= 1.2.3` and `== 1.2.3`. Documented limitation; SemVer prerelease
  precedence can be slotted behind this seam later if ever needed.
- The deliberately duplicated regex in `SettingsModel._validate_version_format`
  is updated in lockstep; a drift test ties the two patterns together.

## Resolution pass

New pure-function module `src/otto/config/dependencies.py`, called from
`bootstrap()` between discovery and registration. Inputs: the discovered
`Repo` list. Outputs, per repo:

```python
@dataclass(frozen=True)
class ResolvedDependency:
    name: str          # as declared
    normalized: str    # PEP 503 form used for matching
    constraint: str    # raw clause text ("" = any version)
    required: bool
    status: Literal["satisfied", "missing", "incompatible", "ambiguous"]
    provider_version: Version | None   # None iff status is "missing"/"ambiguous"
```

Stored as `repo.dependencies: list[ResolvedDependency]` (empty when nothing is
declared). This list is the runtime query surface: `bootstrap().repos` gives
global name→version, `repo.dependencies` gives the structured per-repo view.
A convenience accessor can wrap it later with no schema change.

Duplicate normalized names across sut dirs are an error **only when some
dep references the ambiguous name** — dep-free setups with duplicate names
keep working. A dep that references a duplicated name resolves with status
`ambiguous` (required → error + skip, like `missing`).

### Cross-repo constraint satisfiability

Before checking anything against versions actually present, the pass verifies
that a compatible version is *possible* at all: for each dependency name, the
**required** constraints from all repos that declare it are intersected. An
empty intersection is an error on every repo that required the name, with a
message that shows each participant's constraint rather than the incidental
on-disk version:

```text
no possible version of 'vantage' satisfies all required constraints:
repo-a requires '>= 2.1, < 3', repo-b requires '< 2'
```

Rationale for erroring all participants (not just whichever one the present
version happens to fail): the declaration set itself is incoherent — someone
must change a constraint — and failing only the unlucky repo would make the
diagnosis depend on which version is on disk. All participants skip
registration (they could never all load together anyway).

Satisfiability over integer triples is exactly decidable: fold clauses into a
lower bound (max of `>`/`>=`), an upper bound (min of `<`/`<=`), `==` pins,
and `!=` exclusions. The set is empty iff the bounds cross, `==` pins
conflict with each other / the bounds / an exclusion, or the bounds confine
the range to a finite point set (same major.minor) that the `!=` exclusions
fully cover.

Optional constraints do not participate in this static check: an optional
that conflicts with the required set simply can never enable, and the
runtime incompatible-optional warning already surfaces that whenever the
dependency is actually present.

## Failure semantics

Errors reuse the existing containment architecture: a `DependencyError`
subclass of `BootstrapError` with its own framing
(`repo <sut_dir>: dependency '<entry>' <problem>`), attributed to the
*dependent* repo, joining `BootstrapResult.errors`. The existing render site
(`src/otto/cli/main.py:647-648`) prints them at startup; the existing loud
gate (`src/otto/cli/invoke.py:415`) blocks dispatch.

- **Required missing or incompatible** → error; the repo **skips phase-2
  registration entirely** (no PYTHONPATH add, no init/test imports — importing
  with a declared requirement absent would just produce a noisier, less
  attributable ImportError).
- **Skip propagation**: if A requires B and B was skipped *for dependency
  reasons*, A is skipped too, each error naming the root cause. Propagation
  applies only to dependency-resolution failures — an ordinary contained
  import error inside B does not skip A (unchanged from today).
- **Cross-repo unsatisfiable required constraints** → error on every repo
  that required the name (see "Cross-repo constraint satisfiability"); all
  of them skip registration.
- **Required-dep cycle** → error on every repo in the cycle
  (`required dependency cycle: A -> B -> A`); all cycle members skip
  registration; repos downstream of a cycle get propagation errors.
- **Optional absent** → silent, by design.
- **Optional present but incompatible** → warning, not error. New
  `BootstrapResult.warnings: list[BootstrapWarning]` (frozen dataclass:
  `sut_dir`, `message`), printed at the same startup render site:
  `warning: repo A: optional dependency 'metrics >= 1.4' not satisfied
  (found 1.2.0) — feature disabled`. The dep is treated as absent everywhere
  else (status `incompatible`, no ordering edge); dispatch is not blocked.

## Registration ordering

Phase 2 iterates repos in dependency order: Kahn's algorithm with an ordered
ready-queue seeded by sut-dir position — a **stable topological sort**, so any
setup declaring no dependencies registers in exactly today's order. Edges
point dependency → dependent (dependency registers first).

- Required edges come from satisfied required deps.
- Present-and-compatible optional deps contribute **soft edges**: added in
  deterministic order (dependent's sut-dir index, then declaration order),
  and an edge that would close a cycle is dropped silently rather than
  erroring.
- Resolution (statuses, errors, skip set) runs first; the sort covers only
  non-skipped repos; phase 2 then runs in sorted order.

## `otto init` scaffolding

`SETTINGS_TEMPLATE` (`src/otto/cli/init_templates.py`) gains a commented
block following the file's sshd_config convention, placed after the identity
fields:

```toml
# --- [dependencies] — other OTTO_SUT_DIRS projects this repo depends on ------
# Entries are "name" or "name <op> X.Y.Z[, <op> X.Y.Z ...]"; names match other
# repos' `name` fields (case/punctuation-insensitive). Required deps must be
# present and compatible or this repo fails to load; optional deps warn when
# present but incompatible.
#[dependencies]
#required = ["other-project >= 1.0"]
#optional = ["nice-to-have-project"]
```

The exported settings schema regenerates from `SettingsModel`
(`otto schema export`), so `DependenciesSpec` flows into editor autocomplete.
The repo's own `schemas/` dir is git-ignored; refresh it with `make schema`
as part of the change (no checked-in artifact to update).

## Testing

- **Unit (pure functions):** constraint parsing incl. rejection cases
  (extra tag in clause, malformed op, bad name), PEP 503 normalization,
  extra-tag grammar (accept `-rc1`/`+build5`/`.dev1`, reject `garbage`),
  compare-ignores-extra, resolution over fake repo descriptors (all three
  statuses, duplicate-name-only-when-referenced, self-dep, both-lists),
  satisfiability (per-entry self-contradiction; cross-repo empty
  intersection; bounds-cross, conflicting `==` pins, and the
  finite-point-set-fully-excluded-by-`!=` case; optional constraints
  excluded from the static check),
  topo-sort stability / required-cycle / soft-edge-drop behavior,
  skip propagation incl. the root-cause naming.
- **Integration:** tmp-dir sut fixtures exercising skip-and-propagate through
  real bootstrap, the warning render line, dispatch blocked on required
  failure but not on warnings, and byte-identical registration order for
  dep-free setups.
- **Drift:** version-regex lockstep test (`models/settings.py` vs
  `config/version.py`); schema fixture regeneration guard.

## Out of scope

Constraint solving, lock files, transitive closure beyond declared edges,
extra-tag ordering (SemVer prerelease precedence), `~=`/`^` operator sugar,
and a dedicated runtime query API beyond `repo.dependencies`.

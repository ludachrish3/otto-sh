# Promote `@options` to a discoverable, recommended default

**Status:** Approved design — ready for implementation plan
**Date:** 2026-06-29
**Author:** Chris Collins (with Claude)
**Source:** `todo/TODO.md` — "Repo and command options need to be promoted to a more visible location"

## Problem

The `@options` decorator (`from otto import options`) is the standard way to
define validated CLI options on repos, instructions, and test suites, but it is
effectively undiscoverable:

- The only prose coverage is one section of `docs/getting-started.md`
  (lines 383–447). The guide pages where authors actually define things —
  `repo-setup.md`, `test.md`, `run.md`, `cookbook/suite-recipes.md` — show plain
  `@dataclass` with **zero links** to the validation story.
- The `tests/repo*` fixtures all define their Options classes as plain
  `@dataclass`, modelling the un-validated pattern for anyone reading examples.
- The `otto.examples` sample package ships **no** options example at all.
- Nothing frames options as part of otto's lifecycle. They are presented (where
  presented) as a local detail rather than the contract that threads project
  definition → instruction execution → test suite runs.

The decorator's *docstrings* (`suite/suite.py`, `cli/run.py`) explain it well;
the gap is entirely in the narrative documentation, the shipped examples, and
cross-linking.

## Goals

1. Make `@options` the visible, recommended default for defining options on
   repos, instructions, and suites.
2. Present options as a **noticeable, recurring part of otto's lifecycle story**:
   defined once at project definition, referenced at every execution surface.
3. Give the documentation a single canonical hub for options + validation, and
   link to it from every authoring touchpoint.
4. Stop modelling plain `@dataclass` in the docs and shipped examples.

## Non-goals

- **No runtime enforcement and no production code change.** otto's
  option-expansion machinery is dataclass-generic and stays exactly as-is; we
  change only what we *document and ship*. The `@options` symbol already exists.
- No change to the option-expansion machinery, validation flow, or any
  `src/otto` production behaviour. The `@options` symbol already exists.
- No broader docs restructure beyond the touchpoints listed here.

## Decisions (and why)

These were settled during brainstorming:

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Positioning of `@options` | `@options` (a pydantic dataclass) is the standard | One validated, standardized options format. |
| Canonical doc home | **New** dedicated guide page `docs/guide/options.md` | A single hub for "more links" to point at. |
| Example conversion scope | Everything: doc code blocks **and** `tests/repo*` fixtures | Nothing should model the old pattern; consistent inheritance chains. |
| `otto.examples` | Add a sample options module | The sample package currently ships no options example. |
| Runtime enforcement | **None** — documentation + examples only, no production change | Standardizing in docs/examples suffices; a check to forbid other dataclasses would be code for no benefit. |
| Lifecycle framing | Weave options into the user-guide narrative | Options are a through-line, not a footnote. |

### What `@options` is, precisely

`otto.options` is a lazy **re-export of `pydantic.dataclasses.dataclass`**
(`src/otto/__init__.py:40`, resolved via PEP 562 `__getattr__`). It is the *same
decorator object* — not a wrapper, not a `functools.partial` with a preset
`ConfigDict`, not a subclass. Decorating an options class with `@options`
therefore makes it a **pydantic dataclass**: its fields are validated at
construction.

The otto-branded name buys two things: ergonomics (one import from `otto`, a name
that matches otto's "Options class" vocabulary) and a **standardization seam** —
because user code funnels through `otto.options`, otto can later attach a default
`ConfigDict` or policy centrally without touching any options class. Today it adds
nothing on top of pydantic's decorator.

Documentation must say *pydantic* dataclass explicitly. Calling `@options`
"compatible with dataclasses" is misleading — readers assume the standard
library's `@dataclass`. The accurate statement is: **`@options` gives you a
pydantic dataclass.**

### Why we add no runtime enforcement

otto has no external users yet, so there is no backwards-compatibility constraint
and no history to document. Making `@options` the standard is implemented purely
in docs and examples:

- otto's option-expansion machinery is **dataclass-generic** —
  `suite/register.py:90,102`, `cli/run.py:159,198`, and `params.py:48` introspect
  via `dataclasses.is_dataclass` / `dataclasses.fields`, and `params.py:31` only
  catches `pydantic.ValidationError` when the class is a pydantic dataclass. It
  already accepts `@options` classes; we change nothing here.
- Forbidding non-pydantic dataclasses would mean *adding* a check, an error type,
  and tests — code for no benefit now.

If one blessed format ever needs to be enforced at registration, that is a small,
separate change, deliberately out of scope here.

### Where options may be defined (precision)

Shared/repo-wide options become available by being **importable from a module
listed in the `init` setting** of `.otto/settings.toml`. Where that module
physically lives is the author's choice. A `libs` path such as `pylib/` (added to
`sys.path` at startup) is one common convention — used by the current fixtures —
but it is **not** a requirement. Documentation must present `pylib/` as one valid
location, with `init`-importability as the actual rule, and must not imply options
*must* live in `pylib`.

## Design

### 1. Lifecycle framing (the through-line)

Options appear at three stages of otto's lifecycle. This framing leads the new
hub page and is reinforced at each execution surface:

- **Project definition** — repo-wide shared options, defined in a module named in
  `init`, are the common vocabulary every instruction and suite inherits.
- **Instruction execution** — `@instruction(options=...)` expands the fields
  (including inherited ones) into `otto run` flags, injected as `opts`.
- **Test suite runs** — `OttoSuite[Options]` + `@register_suite()` expands the
  fields into `otto test` flags, injected as `suite_options`.

### 2. New canonical guide page: `docs/guide/options.md`

Registered in `docs/guide/index.rst`, placed adjacent to `repo-setup` and before
`run`/`test` (it is defined at setup and consumed by both). Outline:

1. **Options across otto's lifecycle** — the three stages above. Leads the page so
   the reader sees the *why/where* before the *how*.
2. **Options classes 101** — the `Annotated[T, typer.Option(help=...)]` field →
   CLI flag bridge; how fields become `--flags` and reach test/instruction code.
3. **`@options` — what it is** — `@options` (`from otto import options`) is otto's
   ergonomic name for **pydantic's** dataclass decorator
   (`pydantic.dataclasses.dataclass`) — the same decorator object, re-exported.
   Decorating an options class with it makes the class a **pydantic dataclass**,
   so its fields are validated. State plainly that this is *pydantic's* dataclass,
   **not** the standard library's `@dataclass`.
4. **Validation** — `Field(ge=0, …)` constraints; how a bad value surfaces as a
   clean CLI error (exit 2 + field name, via `build_options` →
   `typer.BadParameter`).
5. **Sharing repo-wide options** — define a base options class in any module named
   in `init` (commonly under a `libs` path like `pylib/`, but the location is
   yours); inherit it in suites and instructions.
6. **Using options with suites and instructions** — concrete `OttoSuite[Options]`
   and `@instruction(options=...)` examples; pointer to the `otto.examples`
   sample.
7. **Naming clarity (stated once, here only)** — `@options` *is* a pydantic
   dataclass; it is equivalent to decorating with `pydantic.dataclasses.dataclass`
   and is **not** the standard-library `@dataclass`. The page does not present a
   stdlib `@dataclass` as an alternative.

Includes one executable `{doctest}` block demonstrating validation, matching the
existing convention. Constraints: Sphinx nitpicky `-W` clean (page must be in the
toctree, all xrefs resolvable); **no** `from __future__ import annotations` (it
trips otto's nitpicky docs gate); real 3.10+ annotations, module-top imports.

### 3. `otto.examples` sample

Add `src/otto/examples/options.py` modelling the recommended pattern: a shared
repo-wide `@options` base plus one suite Options subclass and one instruction
options subclass that inherit it. The new guide page references it as the
importable, real-world example. Follow the existing style of
`src/otto/examples/lab_repository.py` and `reservations.py` (module docstring,
doctest-friendly).

### 4. Cross-link + narrative integration

| Page | Change |
|------|--------|
| `docs/guide/repo-setup.md` | Add a short **narrative subsection** placing "defining options" as a first-class step in the project-definition lifecycle it already documents (the `init` / startup sequence, lines 65–69 / 98–124), then link to `guide/options`. This is the primary onboarding touchpoint. |
| `docs/guide/run.md` | Add a framing sentence tying options to the **instruction-execution** stage; convert the options example to `@options`; link to `guide/options`. |
| `docs/guide/test.md` | Add a framing sentence tying options to the **test-suite-run** stage; convert the options example to `@options`; link to `guide/options`. |
| `docs/getting-started.md` | Keep the working first-suite example (already uses `@options`). Rewrite the inline explanation to describe `@options` as **pydantic's** dataclass decorator under otto's name (validated fields); remove the existing "plain `@dataclass` still works / validation is opt-in" wording (lines 424–429); link to `guide/options`. |
| `docs/cookbook/suite-recipes.md` | Convert the "inheriting shared options" recipe to `@options`; link to `guide/options`. |
| `docs/overview.md` | Add a one-line pointer to `guide/options` if it carries a feature/links list. |
| `docs/guide/os-profiles.md` | Audit the existing `@options`/`@dataclass` hit; convert **only** if it is a repo/instruction/suite Options class (likely an unrelated OS-profile dataclass — leave alone if so). |

### 5. Convert example/fixture code to `@options`

All seven `tests/repo*` fixture Options classes (confirmed Options classes) plus
any doc code blocks still using `@dataclass`. Per site: confirm it is a CLI
Options class, swap `@dataclass` → `@options`, update the import to
`from otto import options`.

Fixture sites:

| File | Class | Notes |
|------|-------|-------|
| `tests/repo1/pylib/repo1_common/options.py:15` | `RepoOptions` (shared base) | Convert first; subclasses must stay consistent. |
| `tests/repo1/pylib/repo1_instructions/nc_smoke.py:44` | `_Options(RepoOptions)` | Instruction options. |
| `tests/repo1/pylib/repo1_instructions/install.py:18` | `_Options(RepoOptions)` | Instruction options. |
| `tests/repo1/tests/test_device.py:22` | `_Options(RepoOptions)` | Suite options. |
| `tests/repo1/tests/test_coverage_product.py:50` | `_Options` (empty) | Bare; converts cleanly. |
| `tests/repo1/tests/test_stability_fixture.py:36` | `_Options(RepoOptions)` (empty) | Inherits base. |
| `tests/repo3/tests/test_embedded_coverage.py:157` | `_Options` (empty) | Bare; converts cleanly. |

Consistency requirement: a subclass of a now-`@options` base must itself be
`@options` (homogeneous pydantic-dataclass inheritance chains); do not leave a
plain `@dataclass` subclass inheriting a pydantic-dataclass base.

### 6. No compatibility shim

After conversion, nothing in docs, examples, or fixtures uses the standard-library
`@dataclass` for options — everything is `@options`. otto has no external users,
so there is no compatibility contract to preserve and **no stdlib-`@dataclass`
guard test is added**. The expansion machinery stays dataclass-generic as an
implementation detail, but otto documents and ships only `@options`. The existing
`@options`-with-constraints tests in `test_options_validation.py` cover the
validated path.

## Testing & verification

- **Per-conversion verification.** pydantic dataclasses are stricter than stdlib:
  mutable defaults, non-CLI field types, and mixed inheritance can break a site.
  Verify each converted fixture individually; in particular confirm no existing
  test passes an out-of-range value that plain `@dataclass` silently accepted and
  `@options` now rejects.
- **Run the full `tests/unit`** plus any integration that loads `tests/repo1` and
  `tests/repo3` — not a scoped subset (these fixtures back live-bed and embedded
  suites).
- **Docs gate:** new page is in the toctree, Sphinx builds with `-W` and 0
  warnings, all doctests pass, all xrefs resolve.
- **Full gate before declaring done:** `make coverage`, then nox, typecheck, and
  docs.
- **Staged only.** No self-commit in otto-sh (the prepare-commit-msg hook needs
  `/dev/tty` and agent commits mis-tag AI assistance). Provide a paste-able
  commit message; Chris commits.

## Risks

- A fixture conversion newly *rejects* a value some test relies on → caught by the
  full unit + integration run; fix the test data or the field.
- A converted fixture field type pydantic can't model → adjust the field or set
  `ConfigDict(arbitrary_types_allowed=True)` on that options class; do not revert
  to a stdlib `@dataclass`.
- New page introduces an unresolved xref or falls outside the toctree → Sphinx
  `-W` fails the docs gate; fix before done.

## File-change inventory (estimate)

- **New:** `docs/guide/options.md`, `src/otto/examples/options.py`.
- **Edit (docs):** `docs/guide/index.rst` (toctree), `repo-setup.md`, `run.md`,
  `test.md`, `getting-started.md`, `cookbook/suite-recipes.md`, `overview.md`,
  and `os-profiles.md` (audit-only, edit if applicable).
- **Edit (fixtures):** the 7 files in the table above.
- **Edit (tests):** none expected beyond fixing any test whose fixture conversion
  changes behaviour.
- **Production code:** none.

## Out of scope

- Runtime enforcement / deprecation warnings for non-`@options` dataclasses (a
  small, separate change if ever wanted).
- Adding `@options` to the API reference index (`docs/api/`) — optional follow-up.
- Any change to option expansion, validation flow, or schema export.

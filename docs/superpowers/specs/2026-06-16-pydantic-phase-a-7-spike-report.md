# Pydantic Phase A — §7 Spike Report

**Date:** 2026-06-16
**Status:** Complete — informational. Gates *where* Pydantic Phase B lands; implements nothing.
**Spec:** `docs/superpowers/specs/2026-06-14-pydantic-phase-a-design.md` §7.

This is the written deliverable of the Phase A spike. It runs two probes and reports
findings; it changes no shipping code. All claims below were verified empirically (the
commands are reproduced inline so the work is auditable).

---

## TL;DR

1. **Compatibility hinge → YES (drop-in).** `pydantic.dataclasses.dataclass` satisfies
   otto's exact structural contract (`dataclasses.is_dataclass` / `fields` /
   `get_type_hints(include_extras=True)` / `options_params()`), so the suite/instruction
   `Options` types can move to pydantic **without breaking user subclassers** and
   **without otto changing its introspection**. The migration is backward-compatible and
   can be opt-in (a user swaps `@dataclass` → `@pydantic.dataclasses.dataclass` on their
   own `Options` to gain validation; otto is unchanged). **No base-class decision needs to
   be pulled pre-freeze.**

2. **Typer 0.26 scope → confirmed in the option/CLI-wiring layer, not the state layer.**
   Under `typer 0.26.7` + `click 8.3.1`, 25 of ~320 cli/suite unit tests fail. The load-
   bearing cluster (18 failures) is the parent-runner **option forwarding via the Typer
   `Context` object** (`cli/test.py` writes `ctx.obj[...]`, suite subcommands read it back)
   — exactly the option-expansion / CLI-wiring layer that Phase B rewrites. The remaining
   ~7 are thin-surface typer/click API adaptations (the `typer.Exit` exception moved to
   `typer._click.exceptions.Exit`; click 8.2+ split CliRunner stdout/stderr). Notably, the
   synthesized-signature machinery the design flagged (`options_params`,
   `_wrap_with_options`, `register_suite`) **still works under 0.26** — it is *not* the break.

3. **Recommendation → Phase B stays fully post-freeze (default assumption holds).** The
   typer bump rides with Phase B's options-layer rewrite (same layer). The thin API
   adaptations are test-level and can be done independently at any time.

---

## Probe 1 — Compatibility hinge

**Question (from §7).** Can the user-facing `Options` types (the suite `Options` inner
class / repo-wide `RepoOptions` base, currently stdlib `@dataclass`) move to a pydantic
type without breaking user subclassers (`class Options(RepoOptions): ...`)? Probe
`pydantic.dataclasses.dataclass` as a near drop-in.

**How otto consumes `Options` today (the contract that must survive).** `Options` is a
structural duck type, not an otto-provided base. otto introspects whatever class a user
attaches:

- `otto/params.py::options_params()` reads `dataclasses.fields(opts_cls)` +
  `get_type_hints(opts_cls, include_extras=True)` and emits a `list[inspect.Parameter]`
  (KEYWORD_ONLY, annotated `Annotated[T, typer.Option(...)]`).
- `otto/suite/register.py::register_suite()` guards on `dataclasses.is_dataclass(opts_cls)`,
  synthesizes a `runner.__signature__` from those params, and hands it to
  `sub_app.command(...)`.
- `otto/cli/run.py::_wrap_with_options()` does the same for `@instruction(options=...)`.
- Both instantiate the user's class with `opts_cls(**kw)` from parsed CLI values.

So the migration is safe iff a pydantic `Options` still answers `is_dataclass` / `fields` /
`get_type_hints` and is constructible by keyword — and iff the user's `class
Options(Base)` pattern is preserved.

**Empirical results** (`uv run python`, pydantic 2.x, typer 0.25.1):

| Case | Setup | `is_dataclass` | `fields` (MRO) | `options_params()` | Validation on bad input |
| --- | --- | :--: | :--: | :--: | :--: |
| A | `@pydantic.dataclasses.dataclass` base | ✅ True | ✅ `['lab_env']` | ✅ works | — |
| B | plain `@dataclass` subclass of a pydantic base | ✅ True | ✅ `['lab_env','firmware']` | ✅ works | ❌ none (plain subclass) |
| C | `@pydantic.dataclasses.dataclass` subclass of pydantic base | ✅ | ✅ both fields | ✅ | ✅ `ValidationError` |
| D | `@pydantic.dataclasses.dataclass` subclass of a **stdlib** base | ✅ | ✅ both fields | ✅ | ✅ `ValidationError` |

**Reading of the results.**

- **(A)** A pydantic dataclass *is* a stdlib-compatible dataclass: otto's
  `options_params()` runs on it unchanged, Annotated metadata is preserved, defaults are
  read correctly. otto needs **zero** introspection changes.
- **(D)** is the decisive compatibility case: a user can keep a stdlib `RepoOptions` base
  and upgrade **only their own** `Options` subclass to `@pydantic.dataclasses.dataclass`,
  gaining full validation (`ValidationError` on a mistyped field) with no change to otto
  and no change to the `class Options(Base)` subclassing pattern.
- **(B)** is the only gotcha and it is benign: a *plain* `@dataclass` subclass of a
  pydantic base does **not** validate its own new fields. This never bites the migration,
  because validation is a property of *each* `Options` class's decorator, not something
  otto forces — a user opts in per class.

**Conclusion.** The hinge answer is **YES — drop-in**. Because pydantic dataclasses keep
the stdlib dataclass surface, the options-type migration is *backward-compatible* and can
be *incremental/opt-in*. There is therefore **no breaking base-class shape decision that
must be pulled pre-freeze**. (Contrast: moving `Options` to a pydantic *`BaseModel`* would
break the contract — `is_dataclass` is False and `dataclasses.fields` raises — forcing a
rewrite of `options_params`. The drop-in path specifically requires
`pydantic.dataclasses.dataclass`, not `BaseModel`.)

---

## Probe 2 — Typer 0.26 scope read

**Question (from §7).** Reproduce the failing `typer` 0.26 bump
([ludachrish3/otto-sh#47](https://github.com/ludachrish3/otto-sh/pull/47)) far enough to
confirm the break is in the option-expansion / signature-introspection layer, not the
state layer — so Phase B can absorb it.

**Current pin.** `pyproject.toml`: `typer>=0.24.0,<0.26` (installed 0.25.1). The bump to
0.26 pulls `click 8.3.1`.

**Method.** Ran otto's real cli/suite unit suites under the candidate stack without
changing the lockfile:

```bash
uv run --with 'typer>=0.26' python -c "import typer, click; print(typer.__version__, click.__version__)"
# -> 0.26.7 8.3.1
uv run --with 'typer>=0.26' pytest tests/unit/cli tests/unit/suite -o addopts="" -q
# -> 25 failed, 294 passed, 1 xfailed
```

**The load-bearing break — option forwarding via the Typer `Context` (18 failures).** The
failures concentrate in `TestParentRunnerOptionsCtx` (6), `TestCovDirOption` (5), and
`TestCovReportOption` (8) in `tests/unit/cli/test_test.py`. They are *behavioral*, not
output-assertion noise:

```
tests/unit/cli/test_test.py:426: assert ctx_obj.get('iterations') == 5
E   AssertionError: assert None == 5
tests/unit/cli/test_test.py:576: assert ctx_obj['cov'] is True
E   KeyError: 'cov'
```

The mechanism is in `otto/cli/test.py`: the parent `@suite_app.callback()` does
`ctx.ensure_object(dict)` and writes the shared run options — `ctx.obj['iterations']`,
`ctx.obj['cov']`, `ctx.obj['markers']`, the monitor flags, etc. (lines ~471–498) — and the
suite subcommands read them back (`if isinstance(ctx.obj, dict) and 'cov' in ctx.obj:`,
line ~175). Under typer 0.26 / click 8.3 that **parent-callback → subcommand `ctx.obj`
handoff yields an empty `{}`**, so every shared parent-runner option is lost. This is
squarely the **option-expansion / CLI-wiring layer**, which Phase B owns.

**What is *not* broken — the synthesized-signature machinery.** The design's stated
worry was `options_params` / `_wrap_with_options` / `register_suite` (synthesizing a
`__signature__` of `Annotated[T, typer.Option(...)]` params for Typer to introspect). That
path **survives 0.26**:

- `tests/unit/suite/test_import_and_register.py::...::test_options_passed_through_to_suite`
  **passes** under 0.26 — per-suite `Options` still reach the suite via the
  `OttoOptionsPlugin` fixture.
- An isolated reproduction of otto's exact synthesis (hand-built `inspect.Signature` of
  KEYWORD_ONLY `Annotated[..., typer.Option()]` params, both defaulted and required,
  handed to `sub_app.command()`) builds and introspects cleanly under 0.26.7 — `--help`
  renders the options and invocation runs.

So the break is one layer *above* the signature synthesis: the cross-command `Context.obj`
propagation, not the per-command parameter expansion.

**Orthogonal thin-surface adaptations (~7 failures), test-level only.** These are generic
typer/click API drift, not otto architecture:

- **`typer.Exit` relocated.** Under 0.26 the exception is
  `typer._click.exceptions.Exit`; tests that call a command function directly and catch
  the old type miss it (e.g. `test_reservation.py::test_whoami_exits_1_*`,
  `test_monitor.py::...::test_unsupported_extension_raises_exit`).
- **click 8.2+ CliRunner stdout/stderr split.** `result.output` no longer includes stderr
  (the old `mix_stderr` default is gone), so error-message assertions must read
  `result.stderr` (e.g. the lab-gate `test_lab_needing_path_without_lab_reports_missing_option`
  added in Plan 6, and the reservation/stability error-text checks).

These are independent of the state layer and can be fixed at any time, including pre-freeze.

**Conclusion.** The 0.26 break is confirmed in the **option-expansion / CLI-wiring layer**
(specifically `Context.obj` forwarding in `cli/test.py`), plus thin typer/click API drift —
**not** the state layer (`OttoContext`, reservations, lab loading: all green under 0.26).
Phase B, which rewrites the option-expansion layer, can absorb the typer bump.

---

## Recommendation — Phase B placement

**Phase B stays fully post-freeze** (the design's default assumption holds, now evidenced):

- The **compat hinge is open** (Probe 1): the options-type move is backward-compatible and
  opt-in via `pydantic.dataclasses.dataclass`, so nothing about the base-class shape needs
  to be frozen early. Phase B can do the whole options-layer change after the freeze
  without a breaking pre-freeze commitment.
- The **typer 0.26 break lives in the same option-expansion layer Phase B rewrites**
  (Probe 2), so the bump naturally rides with Phase B rather than forcing a separate
  pre-freeze scramble. Concretely, Phase B's options-layer work should also re-home the
  parent-runner shared options off the fragile `ctx.obj` handoff (or re-establish it under
  the new click) and lift the typer pin.
- The **thin API adaptations** (`typer.Exit` import site, CliRunner stderr) are test-level
  and may be landed independently and early if convenient — they do not gate the freeze and
  do not depend on the pydantic work.

No change to the Phase A → freeze → Phase B sequencing is warranted by this spike.

---

## Caveats / scope

- The probe used `typer 0.26.7` / `click 8.3.1` (latest at time of writing); exact failure
  counts may shift with point releases, but the *layer* of the break (Context.obj
  forwarding) is structural.
- This spike did **not** attempt the fix, did not change the lockfile/pin, and left no
  residue (`uv run --with` is ephemeral). The 25 failures are observed under the candidate
  stack only; the committed suite remains green on the pinned `typer<0.26`.
- Probe 1 validated the structural contract and validation behavior; it did not migrate any
  real otto `Options` class — that is Phase B implementation work.

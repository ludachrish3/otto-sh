# Test harness DECLARES registration metadata instead of READING it

> **Do this together with two siblings.** Same problem, three places:
> registration truth lives in more than one location, so otto's own path and
> the path everyone else uses can drift. See
> [registry_builtin_registration_symmetry.md](registry_builtin_registration_symmetry.md)
> (built-ins seeded into the backing dict at import, bypassing the public
> `register_*()` everyone else uses) and
> [cli-dispatch-metadata-one-declaration-2026-08-15.md](cli-dispatch-metadata-one-declaration-2026-08-15.md)
> (dispatch metadata declared across three signatures and two readers).
> **Read `registry_builtin_registration_symmetry.md` first of the three** — its
> "Resolved (WS#4)" section already demonstrates the fix direction the other
> two need: empty seed dict plus a `_register_builtin_*()` bootstrap through
> the public function, so one authority is read by everyone including otto
> itself.
>
> Each of these reads as a small local nit on its own, which is exactly why
> they keep being deferred one at a time.

## The pattern

`tests/_fixtures/dispatch.DispatchRunner` builds its own
`otto.cli.registry.CommandSpec` instead of reading the one otto ships. Every
test driven through it therefore certifies **a command that does not exist** —
a harness-declared registration whose metadata can differ, field by field, from
the production one. The tests stay green while the shipped command behaves
differently, and the divergence is silent because nothing compares the two.

This is the mirrored-default drift this codebase keeps getting bitten by,
specialised to registration: not two implementations of one behaviour, but two
*declarations* of one fact.

## Three sightings

1. **Task 3 (dry-run workstream, 2026-08-15) — the mirror itself.**
   `DispatchRunner.invoke` constructs `CommandSpec(name=…, loader=app,
   lab_free=True, output_dir=False, …)` at `tests/_fixtures/dispatch.py:869`.
   Found while adding the `--dry-run` seam; recorded, not fixed.

2. **Task 4 — the `dry_run_preview` half, fixed.** The harness now READS the
   shipped flag (`shipped_dry_run_preview()` → `CLI_COMMANDS`), and a separate
   registration-shape pin hard-codes the intent
   (`tests/unit/cli/test_dry_run_seam.py::TestTheShippedRegistrationCarriesTheFlag`).
   Two independent authorities, deliberately. The reviewer reproduced both
   arms: with the registry read in place, dropping the production flag reddens
   8 tests; with the harness hard-coding the flag instead, the same mutation
   reddens only the pin and all six link tests stay green.
   **The mirror itself remained — only one field was rescued.**

3. **Task 7 (2026-08-16) — the `lab_free` half, and it hid a crash.**
   The harness registers every spec `lab_free=True`. The leaf preamble calls
   `ensure_lab_session` only `if not spec.lab_free`
   (`src/otto/cli/invoke.py`), so **`ensure_cli_session` never executes in any
   of the 91 `--dry-run` seam tests.** The provenance log line inside it
   (`f"{repo.sut_dir}: {repo.commit}"`) tracebacked with `CommandNotRunError`
   under a dry run and took out **every `otto host <id> <verb> -n`
   invocation** — exit 1, full traceback, the flagship surface — while the
   whole seam suite stayed green. `tests/unit/cli/test_host.py` compounds it by
   patching `otto.cli.invoke.ensure_cli_session` out by name, so the `otto
   host` CLI unit tests could not reach it either.
   Fixed by an e2e subprocess guard
   (`tests/e2e/cli/test_dry_run_preamble_e2e.py`), which is the only harness in
   the tree that reproduces the real preamble ordering.

The escalation is the point: sighting 1 was a note, sighting 2 was one field,
sighting 3 was a shipped crash on the most-used command group.

## Shape of a real fix

The harness should not author a `CommandSpec` at all. Two directions, both
worth costing before choosing:

- **Read the shipped spec and override explicitly.** Look up
  `CLI_COMMANDS.get(name)` and `dataclasses.replace(...)` only the fields a
  sub-app unit test genuinely must simplify, so every deviation from
  production is written down at the call site rather than inherited from a
  synthetic default. A test that needs a fabricated spec (an app built inside
  the test, with no shipped registration) passes one explicitly.
- **Stop simplifying `lab_free`/`output_dir` at all** and give the harness a
  real bootstrap/lab stub, so the preamble runs end to end. Truer, and more
  expensive: it is the change that ripples across the suite.

**Whichever is chosen, add the missing-coverage guard first**: a test asserting
that the harness's effective spec matches the shipped one for every field it
does not deliberately override. Without it, the next field to drift is free.

## Do not attempt piecemeal

Changing `lab_free` in `DispatchRunner` today ripples across the whole sub-app
test suite (every preamble that currently no-ops would start running). Deferred
deliberately from the dry-run workstream for that reason — the branch was
already nine commits long. The e2e guard above covers the one path that was
actually broken; this file covers the class.

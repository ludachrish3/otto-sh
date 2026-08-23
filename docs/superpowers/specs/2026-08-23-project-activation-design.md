# Project activation: lab inference and per-project switches

**Status:** approved design, not yet planned.
**Companion:** `2026-08-23-multi-project-environment-design.md` (builds on the
`active()` predicate this spec defines; lands second).

## Motivation and provenance

otto's goal is multi-project workflows with minimal manual glue. Two measured
loose ends undermine that today (brainstorm of 2026-08-23, findings verified
against source):

1. **A broken sibling degrades an unrelated run.** Bootstrap phase 2 imports
   every discovered repo's `init` modules unconditionally
   (`src/otto/bootstrap.py`, the `resolution.ordered` loop). A repo whose init
   import fails — most commonly a third-party import its venv story does not
   satisfy, see the companion spec — is contained as a per-repo
   `BootstrapError`, but real dispatch then fails loud **even when the loaded
   labs make that repo irrelevant to the run**. Project A's `-l unix` install
   should not care that repo B cannot import paramiko.
2. **"Off" is not off.** The project-action walks already skip repos whose
   `[project]` declaration admits no loaded lab
   (`otto.project.orchestrator._applicable`, D3) — but the skipped repo's
   instructions remain registered and invokable, and there is no way for an
   operator to switch a repo off (or force one on) for a single invocation.

The user-facing promise this spec implements: **the loaded labs are the
default signal for which projects a command deals with, and explicit
per-project switches override that signal in either direction.**

## Decision record (2026-08-23)

| decision | choice | rejected |
| --- | --- | --- |
| Enforcement point | **At use**: preflight severity, action walks, instruction dispatch. Bootstrap keeps importing everything that imports cleanly. | Hard gate at bootstrap (requires sniffing `-l` from argv before bootstrap; help/completion would see per-lab command sets; completion cache keys would grow a lab axis). Walks-only (leaves both loose ends open). |
| Switch spelling | `--include-projects` / `--exclude-projects`, short `-I` / `-E`, repeatable, comma-splittable, top-level. | `--with NAME` / `--no NAME` (terse but generic — nothing in the spelling says *projects*, and both words could govern almost any noun; 2026-08-23 follow-up ruling). Literal `--no-<name>` synthesized flags (collide with real options, flaky completion). One `--projects a,-b` mini-language. |
| Precedence | explicit switch > lab inference > default-on. | — |
| No labs loaded | Everything active (status quo for lab-free commands, bare `otto`, help, completion). | — |
| Required-dep vs explicit off | Dependent proceeds, provider's steps skipped, WARNING (the "I installed B by hand" case). | Refusing (would make the override useless for exactly its motivating case). |
| Required-dep vs lab-inferred off | Loud error naming both fixes. | Silent proceed (hides a contradictory configuration: the labs say the provider cannot be here, the dependent says it must be handled). |

## 1. The active-set predicate

One authority, consulted by every enforcement point:

```python
def active(repo_name: str, ctx: OttoContext) -> bool
```

Resolution order:

1. Excluded (`-E`/`--exclude-projects`) → `False`. Included
   (`-I`/`--include-projects`) → `True`.
2. No labs loaded this invocation → `True`.
3. Otherwise delegate to the repo's resolved `ProjectScope` verdict in
   `ctx.scopes` — the same object `_applicable` reads today. A repo with an
   unusable scope (`excluded`, or lab-matched but host-starved —
   `otto.config.scope.unusable_scope`) is inactive; an undeclared repo
   (no `[project]` table) is active, preserving the whole-lab fallback (§6 of
   the scoping spec).

The predicate lives in `otto.config.scope` beside `repo_targets` and
`unusable_scope`, so scoping keeps a single home. It is pure over
`(switches, scopes)`; no I/O.

Switch storage: two tuple fields on the context, populated by the top-level
CLI callback. Nothing else may re-derive activation from raw switches.

## 2. The switches

Top-level options on the main app, sitting beside `-l/--lab`:

```
otto -E repo2 -l unix run install
otto --include-projects repo3 --exclude-projects repo2 run status
otto -I repo3,repo4 run status
```

- `--include-projects` / `-I` and `--exclude-projects` / `-E` (`-h -l -x -n
  -R` are the taken shorts). Repeatable, and each occurrence accepts a
  comma-separated list — the same list convention `OTTO_SUT_DIRS` parsing
  already accepts.
- Names are matched PEP-503-normalized against the **discovered** repo set
  (`bootstrap().repos`), not the active set — you can include a repo the labs
  would exclude; that is the point.
- Unknown name → usage error (exit 2) with `difflib` close-match suggestion:
  `no project 'repoo2' — did you mean 'repo2'?`
- The same name in both include and exclude → usage error (exit 2). Exclude
  does not silently win; a contradictory command line is a typo.
- Shell completion for both options offers the discovered repo names via the
  existing completer machinery (`cli/completers.py`).

## 3. Enforcement: bootstrap-error demotion

Bootstrap keeps recording per-repo `BootstrapError`s exactly as today. What
changes is dispatch-time fatality: errors whose repo is **inactive** for this
invocation are reported as warnings (one line each, naming the repo and that
it is inactive) instead of failing the dispatch. Errors from **active** repos
stay fatal, unchanged.

This is a filter at the error-surfacing seam, not a change to bootstrap
itself — bootstrap cannot know the labs, and does not need to.

## 4. Enforcement: the action walks

`_applicable` (orchestrator) grows the switch axis: its verdict becomes
`active()` instead of `not unusable_scope(scope)` alone. Skip messages keep
their current shape and gain the switch case:

```
repo 'repo2' switched off for this run (--exclude-projects repo2) — skipping it for install
```

Required-dependency interaction, per the decision record:

- Dependent active, required provider explicitly excluded → dependent walks;
  WARNING: `repo 'repo4' requires 'repo1', which was switched off
  (--exclude-projects repo1) — proceeding as though repo1 is handled
  externally`.
- Dependent active, required provider lab-inactive → error before any walk:
  `repo 'repo4' requires 'repo1', but repo1 is not applicable to the loaded
  lab(s) [unix_alt] (lab_patterns: unix). Load a lab repo1 applies to, or pass
  --exclude-projects repo1 to declare it handled externally.` Exit 1.
- Optional provider inactive, either way → the existing "optional dependency
  not satisfied" WARNING, with the activation reason appended.

## 5. Enforcement: instruction dispatch

`InstructionEntry` grows one field:

```python
registered_by: str | None  # get_registering_repo() at registration; None = first-party
```

`run_app`'s dispatch refuses an instruction whose owner is inactive:

```
error: 'flash-b' belongs to repo 'repo2', which is inactive for the loaded
lab(s) [unix] (lab_patterns: unix_alt)
  activate it: -l unix_alt    or: -I repo2
```

Exit 1 (not 2 — the command line is well-formed; the configuration excludes
it). The message mirrors whichever of the two skip shapes applies — excluded
(no loaded lab matches, shown above) or host-starved (labs match, no host
does) — the same distinction `_skip_message` already draws for the walks.
First-party instructions (`registered_by is None`) are never refused.
Help and completion continue to list every registered instruction: activation
is per-invocation, and hiding entries would make `-I` undiscoverable.

## 6. Testing

Unit (hostless):

- Predicate table: every cell of (switch state × labs-loaded × scope verdict)
  against `active()` — including the undeclared-repo fallback and the
  host-starved case.
- Switch parsing: normalization, comma-splitting, unknown-name suggestion,
  include+exclude conflict.
- `InstructionEntry.registered_by`: recorded under `registering_repo`, `None`
  outside it.
- Required/optional interaction messages, from fakes at the orchestrator seam
  (the existing `test_orchestrator.py` bed).
- Demotion: a repo with a failing init (the `tests/repo_broken` pattern)
  inactive → dispatch succeeds with the warning; active → fails as today.
  Both arms, so the guard cannot pass by suppressing everything.

E2E (hostless, CLI subprocess bed):

- `otto -E repo2 -l unix run status` skips repo2's row with the switch
  message; without the switch, unchanged output (the discriminator).
- Dispatching a repo2-owned instruction under `-l unix` → the §5 refusal;
  under `-l unix_alt` → runs. Existing repo1/repo2 samples suffice — they
  already declare disjoint lab sources (tech1/tech2); no new sample repos in
  this spec.

Every guard follows the house rule: inject the hostile condition (a switch, a
broken init, a non-matching lab), never inherit it, and prove each new
assertion red by mutation before landing.

## 7. Out of scope

- The dependency preflight, `otto env create/sync`, and everything venv —
  the companion spec.
- Hard bootstrap gating by lab.
- Per-instruction (rather than per-repo) switches.
- Persisting switches in settings (exclusion is per-invocation; a durable
  "this repo is retired" belongs in `OTTO_SUT_DIRS`).

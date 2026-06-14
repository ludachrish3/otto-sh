# WS#2 — Naming Sweep: kickoff context for a fresh planning session

> Handoff note written 2026-06-13 at the end of the WS#1 session, to tee up
> WS#2's brainstorm → spec → plan in a fresh context. This is **not** the spec —
> it records current state, locked decisions, and gotchas a fresh session can't
> cheaply re-derive. The authoritative scope/rationale is the roadmap.

## Authoritative inputs (read these first)
- **Roadmap (the order + the freeze line):** `docs/superpowers/specs/2026-06-13-fable-review-sequencing-design.md`, **item #2 "Naming sweep"** (lines ~59-70). ⚠ This file is currently **untracked** (not committed) — same for the WS#1 spec/plan and the embedded-binary docs. Commit them before they're lost.
- **Findings + locked decisions:** `todo/fable_review_outcome.md`
- **Migration this serves (the freeze):** `todo/migration_plan.md`

## State after WS#1 (merged to main)
WS#1 (OttoContext + lifecycle) is **merged** — folded into the `feat(context): explicit OttoContext runtime…` commit. The WS#1 fix for the cross-tier contextvar leak is in the same commit. (Don't trust SHAs in notes — main gets rebased; see gotchas.)

**Already snake_cased by WS#1 — do NOT re-plan these:** the config-access / context layer. `getLab`→`load_lab` (+ new `get_lab()`); `ConfigModule`/`getConfigModule`/`setConfigModule`/`tryGetConfigModule` **deleted** (replaced by `OttoContext` + `get_context`/`try_get_context`/`set_context`/`open_context`); the fleet accessors `all_hosts` / `get_host` / `do_for_all_hosts` / `run_on_all_hosts` are already snake_case; dry-run/log globals deleted.

## What the sweep still has to cover (representative, not exhaustive — the brainstorm enumerates)
- **Module filenames (import-affecting):** `src/otto/host/{dockerHost,embeddedHost,localHost,remoteHost,unixHost}.py` → `*_host.py`. Update every import site, `__all__`, docs, and any dynamic/registry references (host-class string→class registry; check `storage/factory.py` and any `importlib`).
- **JSON / lab-data + dataclass field names:** `osType`/`osName`/`osVersion`/`neId`/`isVirtual`/`hwVersion`/`swVersion`/`dockerCapable`/`defaultDestDir`/`maxFilenameLen` → snake_case. This touches on-disk lab JSON, test fixtures, and the `storage/factory.py` parse/merge path. Do it now (Pydantic Phase A comes *after* and should be authored in the final names).
- **Misc still-camelCase helpers to sweep:** e.g. `getRepos`/`getEnv`/`addHost`/`applyRepoSettings` (configmodule), `getOttoLogger`, `Repo.addLibsToPythonpath`, host `isDryRun`/`getLoggingCommandOutputEnabled`. Verify the full set during brainstorming.

## Locked decisions (from the roadmap — apply, don't re-litigate)
- **Keep the telecom vocabulary:** `ne` / `ne_id` stay conceptually (snake_case the spelling, keep the meaning). Don't "English-ify" to `node`/`element`.
- **Zero users + no-backcompat policy:** this is a *clean rename*, not a deprecation. **No alias shims, no `@deprecated` forwarders** — that's the whole reason it's pre-freeze and cheap now.
- **Why pre-freeze:** every renamed symbol and JSON field is frozen-contract surface; renaming after the freeze is a breaking change.

## Required WS#2 task — add the `typer<0.26` version ceiling (do NOT lose this)
Carried over from WS#1: the roadmap assigned this as a one-line hygiene step (item #1's "add a `typer<0.26` ceiling… as a one-line hygiene step during item #1"), but it was **never done** — the pin is still only `typer>=0.24.0`. This is now a **required WS#2 deliverable**, independent of the rename, and can ship as the first (standalone) commit of WS#2 so it isn't gated on the sweep.

- [ ] In `pyproject.toml` (line ~57), change `"typer>=0.24.0",` → `"typer>=0.24.0,<0.26",`
- [ ] Run `uv lock` and confirm the resolved `typer` stays `< 0.26`.
- [ ] Confirm Dependabot no longer proposes the 0.26 bump (the ceiling is what suppresses PR #47's reappearance).

**Why:** the typer 0.26 bump breaks the option-expansion / signature-introspection layer (PR #47); the fix is deferred to Pydantic **Phase B** (post-freeze). The ceiling records that deferral *in code*. **Remove the ceiling when Phase B lands** and the bridge is reworked.

## Execution gotchas
- **Verify across tiers, not just no-VM unit.** A broad rename can break the integration/embedded paths and the on-disk lab parsing. Run the VM tiers and at least one *combined* `make coverage` (the WS#1 leak only showed up in the single-process combined run, never in the separated tiers).
- **Git: main gets rebased.** Dependabot bumps get inserted under HEAD and main is force-pushed, so SHAs churn (WS#1's context commit went a8f218a→3385606→8867b17 across two rewrites). Branch off the **latest** main, and **rebase your branch onto current main right before merging** — otherwise the 3-way merge falls back to a pre-context base and throws spurious conflicts (this happened merging the WS#1 fix).
- **Self-commit:** don't — the `prepare-commit-msg` hook needs `/dev/tty`. Hand Chris a paste-able conventional-commit message; he commits.

## Sequencing reminder
WS#2 → **Pydantic Phase A** (+ the compatibility/Typer scope spike) → Registry public API → **FREEZE**. Author Phase A models in the final snake_case names.

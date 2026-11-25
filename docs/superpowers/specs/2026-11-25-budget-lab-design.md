# The import-budget project gets its own lab, and a fixture the base cannot run stops the delta: #1289

**Status:** design for approval, 2026-11-25. Checked once by OpenAI gpt-6-astra and Claude Fable 5.2, independently. They did not disagree. Their findings are applied, except the one under "Review findings not taken".

## Why

The import-budget delta measures the tip and the previous release tag with the tip's harness, each side under its own otto. The harness's generated project reads its lab from the bed's frozen golden, `tests/_fixtures/goldens/tech1_lab.json`.

The bed's lab gains fields with most features. Two of them are unknown to v0.20.0:

- `console_options.escapes_iac` on `bb1350` (#935, 2026-11-13). v0.20.0 rejects the record, skips it, and the link `test1:bbeth-1350 <-> bb1350-qemu:eth0` then names a host that is not loaded. The lab fails to load: `Invalid link in lab.json ...: unknown host 'bb1350-qemu'`.
- `command_frame: zephyr-inline` on `zephyr27_fat`. v0.20.0 rejects that record too. No link names it, so alone it would not stop the load.

Both were confirmed by loading today's golden, host by host, with v0.20.0's own source.

The fixture's canary surface, `dispatch_project_warm`, therefore exits 1 at the base, and the delta lists every repo surface (`REPO_SURFACES`) as "not measurable at v0.20.0". CI's `import-budget-delta` job shows this on pull request #1313, on every minor, and the job is green: a surface the base cannot run is reported and the exit is 0. Every pull request since about 2026-11-13 has had its project surfaces unjudged, and nothing said so.

## Rulings (Chris, 2026-11-24 and 2026-11-25)

1. **The budget project gets its own small lab**, separate from the bed's.
2. **A fixture the base cannot run is red.** When the fixture's canary fails at the base, the delta stops, on pull requests and at the release. A surface the base simply lacks stays a note.

## Goals

- Every repo surface is measurable at v0.20.0 again, as far as the lab is the cause.
- A change to the bed's lab can no longer make the base unmeasurable.
- If the fixture stops running at the base for any reason, the delta is red on the pull request that caused it.

## Not goals

- No change to how growth is judged, approved or attributed.
- No change to the bed's lab or its golden.
- Surfaces a base cannot run for their own reason stay notes (see "Known notes at v0.20.0").
- #1305, the misleading "unknown host" message for a skipped record, is a separate issue.

## A consequence to decide before the pull request: unmasked growth

The repo surfaces have not been judged against v0.20.0 since about 2026-11-13, and several changes that touch start-up cost have landed since. `tests/unit/import_budget/approved_growth.json` holds entries for non-project surfaces only. Once the repo surfaces are measurable again, any that grew since v0.20.0 will stop the delta, on this pull request too.

So the work measures the delta on one minor first, locally, and brings Chris the list of surfaces that grew, with the buckets and paths that moved. For each, he decides: approve it (an entry with his `why`, landed in this pull request) or treat it as a regression to fix first. No entry is written without his decision. If nothing grew, this section is moot.

## Design

### The budget project's lab

A new committed file, `tests/_fixtures/budget_project_lab.json`, named by a constant `BUDGET_PROJECT_LAB` in `tests/_fixtures/paths.py`. (`budget_lab/` is already the generated project's SSH lab; this name keeps the two apart.) `generate_project(realistic=True)` in `tests/_fixtures/generated_project.py` names it in the project's JSON lab source, where it names `TECH1_GOLDEN` today. It is read in place, outside the generated project's root, so the lab's reads stay out of the `workspace` counter as they do now. It stays under `tests/_fixtures`, which the delta's instrument key and CI's cache key already cover.

Contents, taken from the golden as it stands and trimmed:

| Element | Kind | Why it is there |
|---|---|---|
| `test1`, `test2`, `test3` | Ubuntu VMs | the surfaces that select a lab select `unix`; `test1` is the observed docker parent |
| `test4` | Ubuntu VM | `completion_hosts_past_sep_handover` expects `test1,test4` |
| `bb1310` | BusyBox guest behind `test1` | keeps a hop and the `ash` frame in the measured load |
| `zephyr37_fat` | Zephyr host | keeps the embedded family in the measured load |

- **One lab, `unix`, and every element is a member of it.** The base validates the members of the selected lab strictly and skips a record it cannot read anywhere else. With all six in `unix`, the canary's dispatch makes the base read every record in the file, so a record the base rejects fails the canary instead of being skipped. The golden's `busybox`, `embedded` and `chaos` labs are not carried over, in the top-level table or in any element's own `labs`.
- **`labs.unix.resources` stays.** `completion_remote_path_hit_reserved` derives the resources it must reserve from it and fails when the set is empty.
- **Links:** `test1:eth2 <-> test2:eth2`, the only golden link whose two ends are both kept. The interfaces that existed only for dropped links (`test1.bbeth-1350`, `test4.ubeth-data`) are removed.

All six host records load under v0.20.0's source (checked with the same probe that showed the two rejections). No surface's expected answer or exit changes at the tip: the answers that come from this lab name only `test1` to `test4`, and `bb1310` and `zephyr37_fat` are not on `test1`'s subnet, so `completion_hosts_past_sep_handover`'s exact set holds. The other hosts surfaces name (`local`, `budget-ssh`, `tmpl-0`) come from elsewhere.

Moving the two extra hosts into `unix` changes what a `unix` load costs at the tip. That is measured with the rest (see "Ceilings").

**The rule for this file:** it holds only fields the previous release also reads. It is not rendered from the bed's Jsonnet and no test ties it to the golden. When a deliberate breaking change to lab fields lands, the file is edited in the same pull request so both sides still load it. A field or host both sides cannot read is removed from it; it is a fixture and may shrink.

`TECH1_GOLDEN` keeps its other users (`tests/unit/labs/`). The other callers of `generate_project(realistic=True)` (`tests/e2e/cli/test_literal_output_e2e.py`, `tests/unit/config/test_corpus_snapshot.py`, `tests/unit/shim/test_subvariant_cost.py`, `tests/unit/cli/test_host_unreachable.py`) get the new lab too. None names a host outside the six; the plan runs them.

### The stop

In `scripts/import_budget_delta.py`, a verdict carries a stop when a surface of `FIXTURE_CANARIES` did not end as expected at the base:

```
<minor> the generated repo does not run at <base>: <canary>: <the harness's reason>.
Make the fixture load on both sides (tests/_fixtures/budget_project_lab.json,
tests/_fixtures/generated_project.py).
```

- **Exit status 1**, the policy stop. The measurement is valid and the other surfaces are still judged; status 2 stays for malformed evidence and operational failures. The repo surfaces are still listed under "not measurable", as now.
- **No approval lifts it.** An approval names a growth, and here nothing was measured. The approvals file a growth stop prints does not mention it.
- **It survives the confirming pairs.** When growth or a shuffle on another surface asks for a second and third pair, the verdict that replaces the first must still carry the fixture stop if any pair's base canary failed, and the pending output must still print the reason.
- **A base whose canary failed is never reused from the cache.** `validate_side` accepts a record whose surface ended wrong, and CI saves the base's records under a key that does not change until the fixtures do. One transient failure would then turn every later pull request red. A cached base side whose canary ended wrong is measured afresh.
- **No false "unused" notes.** A minor stopped this way has no evidence for the repo surfaces, so the staleness pass must not report their approvals as "approved but did not grow". Entries whose surface is unmeasurable on a minor are skipped for that minor.

A non-canary surface that did not end as expected at the base is still reported and not a stop.

### Known notes at v0.20.0

Two repo surfaces cannot run at v0.20.0 for their own reasons and stay notes after this change. The pull request names them and any other the measurement shows:

- `completion_host_warm`: its project has the templated lab, whose settings declare `[[inventories]]`. v0.20.0's settings model refuses that key, and v0.20.0 has no `jsonnet` backend.
- `tracked_lab`: v0.20.0 has no `lab` verb.

The canary's project has none of the generated project's variants (the SSH lab, the templated lab, reservations). A variant that stops running at a later base shows as notes on the surfaces that use it, not as a stop. That is ruling 2's line: the fixture's core is red, a surface with its own reason is a note.

### Ceilings

The tip's counts for the repo surfaces change once: the lab file is smaller, and `unix` now holds a BusyBox guest and a Zephyr host. `make profile` judges each surface against its per-minor ceiling (`tests/unit/import_budget/ceilings/`, six files, 3.10 to 3.15). A lower count fails nothing; it is an advisory note.

- The affected entries are regenerated with `make import-snapshot` (`ONLY=<the repo surfaces>` to limit it), as `docs/contributing.md` describes. Ceilings carry headroom, so they are not the measured values themselves.
- A count that goes **up** against today's ceiling: stop and report to Chris before regenerating anything.
- `tests/unit/import_budget/measurements/*.md` are history and are not touched.

### Tests

- `tests/unit/import_budget/test_generated_project.py`: the realistic project names `BUDGET_PROJECT_LAB`, a JSON source, outside the project root.
- A new unit test on the budget project's lab at the tip: it loads through the lab repository; every element is a member of `unix` and there is no other lab; `test1` to `test4` are present; the kept link resolves; `labs.unix.resources` is not empty. A second case corrupts one non-VM record (an unknown field on `bb1310`) and asserts the `unix` load fails, which is the property the canary relies on.
- `tests/unit/scripts/test_import_budget_delta.py`:
  - `test_a_canary_the_base_cannot_run_lists_every_repo_surface` now expects the stop line and exit 1;
  - a non-canary surface ending wrong at the base is still exit 0;
  - a canary failure together with growth on a non-repo surface keeps the fixture stop through the confirming pairs;
  - a cached base side whose canary ended wrong is not reused;
  - an approval for a repo surface is not reported unused on a minor the canary stopped.
- The proof on the pull request: CI's `import-budget-delta` legs list no repo surface as unmeasurable for the lab's reason, on every minor. The legs are green only once the growth decision above is settled.

### Docs

- `docs/release_process.md`, "The import-budget delta" and its "A stop" paragraph: a stop also happens when the generated project does not run at the previous tag, with what to do, and no approval lifts it.
- `docs/contributing.md`, the import-budget section: the budget project's lab is its own file, with its rule; and the paragraph that says a surface the base cannot run is "listed, not judged" gains the canary exception.
- The module docstring's exit-status paragraph and the `FIXTURE_CANARIES` docstring in `scripts/import_budget_delta.py`, and the two Makefile comments that restate the exit statuses.
- `docs/architecture/quality-gates.md`: the delta's row, if it states what a surface the base cannot run does.

## Approaches considered

1. **Own frozen lab, and a red canary (chosen).** Removes the coupling that caused the drift and makes any later break loud.
2. **Take the fixture from the base tag's tree for the base side.** The fixture's path and format moved since v0.20.0, the harness pins its fixtures to the tip on purpose (`INSTRUMENT_PATHS`), and the two sides would measure different inputs.
3. **Strip unknown fields from the golden at generation time.** Needs a list of what each release knows, which is the same drift in another place.
4. **Keep the golden and only make the canary red.** Every bed-lab field added would turn every pull request red until the next release.

## Review findings not taken

- **Pin the expected set of unmeasurable surfaces per base tag**, so a new note is loud. Not taken: ruling 2 keeps a surface with its own reason as a note, and a pinned set is a second list to maintain at every release. The pull request names the notes it leaves.

## Order of work

1. The lab, the stop and their tests.
2. Measure the delta on one minor locally. Bring Chris the growth list, if any, and the count changes against today's ceilings.
3. After his decisions: approvals entries (his `why`s), regenerated ceilings, docs.
4. One pull request: the spec commit, then one commit for the issue. The stop and the lab land together, because the stop alone would turn every pull request red.

## Left to the builder

- The exact wording of the stop line.
- Whether the kept elements keep the golden's `metadata`, as long as both sides load it.

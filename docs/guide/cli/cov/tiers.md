(coverage-tier-kinds)=
# Coverage tiers

Every tier's `kind` selects how `otto cov report` collects its data:

| Kind | Collected by | Storage |
|------|---------------|---------|
| `e2e` | `otto test --cov` / `otto cov get` | `<output_dir>/cov/<board_id>/capture.json` — not committed, same lifecycle as other run artifacts |
| `unit` | Nothing otto runs for you — build and run your instrumented unit tests as usual; `otto cov report` harvests `.gcda` from the tier's `harvest_dirs` in the **current build tree** at report time | no capture file |
| `manual` | `otto cov get --tier <name> --ticket <ref>` | `.otto/coverage/manual/<utc-timestamp>-<ticket-slug>-<board-slug>.json`, committed to the SUT repo |

**Only manual captures are committed to the repo** — every capture
(manual or e2e) is anchored to a `base_commit`.  E2E data comes from
the output directories of previous otto runs; unit data is swept fresh
from the build tree every time a report is generated — there is no run
discipline imposed on it.

## Declarative Tiers

A *tier* is a named layer of coverage data.  Tiers are declared under
`[coverage.tiers.<name>]` in `.otto/settings.toml` — no more ad-hoc
`--tier NAME=PATH` flags for data otto can collect itself:

```toml
[coverage.tiers.system]
kind = "e2e"                 # collected by `otto test --cov` / `otto cov get`
precedence = 1                # lower number = wins winner-take-all coloring
color = "green"                # CSS color name or "#RRGGBB"; per-kind default if omitted

[coverage.tiers.unit]
kind = "unit"
precedence = 2
harvest_dirs = ["build"]     # swept for .gcda at report time; relative to the repo root
color = "yellow"

[coverage.tiers.manual]
kind = "manual"
precedence = 3
max_age = "180d"             # optional; flag-only aging
color = "orange"

[coverage.exclusions]
markers = ["MYPROJ_NO_COV"]  # optional additions to the LCOV_EXCL_* set
```

Each `[coverage.tiers.<name>]` block:

| Field | Meaning |
|-------|---------|
| `kind` | One of `e2e`, `unit`, `manual`. Selects the collection machinery — see {ref}`coverage-tier-kinds`. |
| `precedence` | Integer; lower wins the winner-take-all row coloring when multiple tiers cover the same line. |
| `color` | Optional CSS named color or `#RRGGBB` hex, validated at settings load. Defaults to a per-`kind` color when omitted (`e2e` = green, `unit` = yellow, `manual` = orange). |
| `harvest_dirs` | `unit`-kind only: build directories swept for `.gcda` at report time. Relative paths resolve against the repo root (see {doc}`../../configuration/settings`). |
| `max_age` | `manual`-kind only: `"<days>d"` (e.g. `"180d"`); enables the *aging* flag (see {ref}`coverage-validity`). Optional, off by default. |

Tier **names are free-form** and multiple tiers may share a `kind` —
for example two manual tiers, `manual_qa` and `manual_dev`, both
`kind = "manual"`, distinguished by name, precedence, and color.

**Backward compatibility:** a settings file with no `[coverage.tiers]`
section behaves exactly as before — an implicit `system` tier
(`kind = "e2e"`, precedence 1) is assumed.

## Three-tier walkthrough

**e2e** — run the suite with coverage on:

```bash
otto test --cov TestMyDevice
```

**unit** — build your unit tests with coverage instrumentation and run
them as you always have; `.gcda` files land next to the `.gcno` files
under the tier's configured `harvest_dirs` (e.g. `build/`):

```bash
cmake -DCMAKE_C_FLAGS="--coverage" -DCMAKE_CXX_FLAGS="--coverage" \
      -DCMAKE_EXE_LINKER_FLAGS="--coverage" -B build ..
cmake --build build --target my_unit_tests
./build/my_unit_tests
```

No lcov invocation and no `--tier unit=...` flag are needed — as long
as `[coverage.tiers.unit].harvest_dirs` points at `build`, `otto cov
report` finds and merges the counters itself.

**manual** — retrieve and anchor a session against the instrumented
target, attaching a ticket:

```bash
otto cov get --tier manual --ticket PROJ-123 --note "verified failover via GDB"
git add .otto/coverage/manual/
git commit -m "cov: manual verification for PROJ-123"
```

Then generate a single report covering all three:

```bash
otto cov report path/to/e2e_run_output/ --dir ./cov_report
```

`otto cov report` reads the e2e capture(s) from the given output
directory, harvests the unit tier's `harvest_dirs` from the current
build tree, and loads every committed manual capture automatically —
no path arguments needed for the unit or manual tiers.

(coverage-validity)=
## Staleness and aging

Manual captures are anchored evidence — as the repo moves on, otto must
decide whether that evidence still applies.  A tree-wide diff against the
capture's `base_commit` resolves every file's lines in one pass — renames
followed, whitespace ignored.  Only when `base_commit` itself cannot be
resolved (a squash-merged branch, a shallow clone) does otto fall back to
checking each file individually against its recorded blob SHA.  Either
path resolves each capture's lines to one of these states at report time:

| State | Meaning | Effect on coverage |
|-------|---------|---------------------|
| **valid** | Line unchanged since the capture's `base_commit` (verified by blob SHA, which survives rebases, or by diffing against `base_commit` when the blob is unreachable) | Counts normally |
| **stale** | Code changed since the capture — the evidence no longer describes this line | Coverage is **revoked**; rendered as "needs re-verification" |
| **aging** | Code is unchanged (still *valid*), but the capture is older than the tier's `max_age` | Coverage is **retained** (flag-only — `max_age` never silently drops data) and tallied/rendered separately, flagging the line for re-verification because surrounding behavior may have drifted |
| **unverifiable** | Neither the blob nor `base_commit` can be resolved | Treated as **stale**, with a loud per-capture warning naming the remedy (re-capture) |

Stale vs. aging, precisely: **stale = the code changed** out from under
the evidence; **aging = the code is unchanged but the evidence is
old**.

The anchor-chain diff is **whitespace-insensitive** (`git diff -w`), so a
pure reformat — reindentation, tabs↔spaces, trailing-whitespace strips —
does not stale a manually-covered line: the evidence carries through, and
lines merely shifted by such edits remap to their new numbers.  Only a
change to the code itself revokes coverage, and only the lines that
actually changed — the rest of the file stays valid.  (The SUTs are
C/C++, where whitespace is not semantically load-bearing; the single case
this also forgives — a whitespace change *inside a string literal* — is
treated as immaterial to coverage.)  Line-ending-only changes (a file
flipped CRLF↔LF) are immune the same way — `-w` treats them as
whitespace, not content.

Encoding changes are not exempt from that revocation: a BOM addition or
a charset transcode changes the file's bytes, and `-w` only ignores
whitespace, not arbitrary byte differences — the affected lines revoke
and must be re-proven, the same as any other edit.  Because only the
byte-differing lines are affected, a transcode that leaves most of a
file's bytes untouched (adding a BOM, re-encoding otherwise-ASCII
content) revokes only the handful of lines it actually changed, not the
whole file.

```{warning}
A conversion that trips git's own binary-file heuristic — encoding to
UTF-16, or any charset that introduces NUL bytes — is **not detected**
by the anchor chain today. `git diff` reports the file as
`Binary files ... differ` with no line hunks, so the tree diff drops it
entirely; a file present on disk but absent from that diff reads as
unchanged, so coverage on it stays valid even though every byte was
rewritten.
Re-prove coverage by hand after this kind of charset conversion — the
anchor chain will not catch it for you.
```

Renames are followed as far as `git diff -M` tracks them: a capture taken
against `foo.c` still resolves cleanly after a plain `git mv foo.c
bar.c`.  File **splits or copies** are not rename-tracked by git and so
are not followed either — restructuring code that way means re-proving
coverage against the new files.

A few more rulings that fall out of how captures are anchored and
resolved:

- A **newer manual capture with the same run label and host** entirely
  replaces the older one — the superseded capture's credits do not
  accumulate, and it drops out of the run table (see
  {ref}`coverage-runs`).
- On a **shallow clone**, a capture older than the clone's fetch depth has
  a `base_commit` git cannot resolve here; validity falls back to the
  per-file blob check instead of crashing — files whose current blob
  still matches the recorded one stay valid, and only the files (or
  lines) that no longer match degrade, with the report naming the fix
  (`git fetch --unshallow`) rather than failing silently.
- A capture whose `base_commit` has been **squash-merged away** (the
  commit was garbage-collected once its branch folded into `main`) can no
  longer be diffed against directly, so otto verifies each of that
  capture's files individually against its recorded blob SHA instead.
  That per-file fallback is batched into a small, constant number of git
  calls per capture regardless of file count, so validity checking stays
  fast even on large repos served over NFS.

(coverage-runs)=
## Runs: which run covered this line?

Every coverage input becomes a **run** at report time: each manual
or e2e capture is one run (labelled by the host's display name; hover for
tier, ticket, note, date, and base_commit), and each unit-tier harvest or legacy
`.info` load gets a synthetic per-tier run.  On a file's annotated page,
the right-hand **runs** column expands per line to list every run that hit
it, colored by tier, with per-run hit counts.  A stale line lists the
revoked run struck through — the ticket to re-verify.  The index's
Captures table is the full run table, and `store.json` carries it
(`runs` plus per-line `run`/`stale_run`) for downstream consumers.

`--ticket` and `--note` on `otto cov get` annotate captures of **every**
tier kind (`--ticket` remains required for manual-kind tiers; tester
attribution stays manual-only).

Validity only applies to the **manual** tier. E2E captures use a
strict `base_commit` **merge guard** instead — see
{ref}`coverage-report-stale-builds`.  Unit tiers carry no validity
states; they're harvested fresh every report, so there's nothing to go
stale (a `.gcda` older than its `.gcno` only produces a "may be stale"
warning, never a revoke).

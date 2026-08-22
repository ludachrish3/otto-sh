# otto cov get

`otto cov get` is the single retrieval command.  It fetches `.gcda`
counters from every host matched by `[coverage].hosts` — Unix hosts
over the network, embedded boards over the console — parses them with
the discovered toolchain, and writes one `capture.json` per board
(anchored to `base_commit`) plus debug artifacts (the raw `.gcda` and the
`.info` tracefiles lcov captured from them) into the command's output
directory:

```text
<output>/
  cov/
    <board_id>/
      capture.json
      *.gcda
      board.info
      board.resolved.info
```

By default `otto cov get` targets the lab's sole `e2e`-kind tier and
writes a capture that is **not** committed anywhere — it lives in the
output directory, the same as a run's other artifacts.  Selecting a
`manual`-kind tier switches the command into manual-capture mode: it
requires `--ticket`, annotates tester identity onto the capture, and
additionally copies the capture into the repo's committed store at
`.otto/coverage/manual/`:

```bash
# Default: retrieve against the sole e2e-kind tier.
otto cov get

# Manual session: anchor a capture, attach a ticket, commit it.
otto cov get --tier manual --ticket PROJ-123 --note "verified failover via GDB"
```

## Options

| Option | Description | Default |
|--------|-------------|---------|
| `--output, -o PATH` | Directory to write fetched coverage and per-board captures into | the command's standard per-invocation output directory |
| `--tier NAME` | Coverage tier to annotate onto each capture | the lab's sole `e2e`-kind tier (error if ambiguous or unknown, listing the configured tiers) |
| `--ticket STR` | Ticket reference annotated onto each capture. **Required** when `--tier` resolves to a `manual`-kind tier | none |
| `--note STR` | Free-text note annotated onto each capture (`manual`-kind tiers only) | none |
| `--tester-name STR` | Tester name annotated onto each capture (`manual`-kind tiers only) | `getpass.getuser()` |
| `--tester-email STR` | Tester email annotated onto each capture (`manual`-kind tiers only) | `git config user.email`, omitted entirely (not annotated empty) when unset |
| `--clean` | Zero the fetched Unix hosts' remote `.gcda` counters after a successful retrieval — for use before starting a manual session | off |

`--ticket`, `--note`, `--tester-name`, and `--tester-email` are only
meaningful for a `manual`-kind retrieval; passing them against an
`e2e`-kind tier has no effect (an automated pull has no human tester to
attribute).

Retrieval requires a git repository — resolving `base_commit` and, for
a dirty tree, the offset remap both need it.  Outside a git repo,
`otto cov get` refuses with a clean error; `otto cov report`'s
`--tier NAME=PATH` escape hatch remains available for git-less flows
(see {ref}`coverage-tier-name-path`).  The SUT directory does not have
to be the repository root: a SUT checked out as a subdirectory of a
larger repository (a monorepo layout) anchors its captures against the
enclosing repo — its `HEAD` is the `base_commit`, and its working-tree
state decides dirtiness.

(coverage-dirty-remap)=
## Locally-modified builds

Manual testing frequently happens against a **locally modified**
build — printf-and-recompile, a GDB session poking at a running
binary.  These sessions still run instrumented code, so real counters
exist, but their line numbers describe the modified tree, not the
committed one.  `otto cov get` detects a dirty working tree
(`git status --porcelain` non-empty) automatically and remaps the
retrieved hits onto **committed-code line numbers** before writing the
capture — added/changed lines' hits are dropped (crediting untested
code would be wrong), unchanged lines remap exactly even when they've
shifted.  The capture records `dirty_remap: true`, which shows up in
the report's run table (see {ref}`coverage-runs`); no diff is
stored.

## The capture file

Each board's `capture.json` records line/branch hits in
committed-code coordinates, the commit they're anchored to, and — for
a manual capture — the human metadata:

```json
{
  "schema": 2,
  "tier": "manual",
  "base_commit": "<commit sha>",
  "dirty_remap": true,
  "captured_at": "2026-07-02T18:40:00Z",
  "tester": {"name": "chris", "email": "chriscoll93@gmail.com"},
  "ticket": "PROJ-123",
  "note": "verified failover via GDB",
  "labs": ["lab1"],
  "board": "mps2_an385",
  "files": {
    "src/foo.c": {
      "blob": "<git blob sha of src/foo.c at base_commit>",
      "lines": {"12": 3, "13": 1},
      "branches": {"12": [[0, 0, 2], [0, 1, 0]]}
    }
  }
}
```

`base_commit` is the commit whose coordinates the line numbers mean;
each file's `blob` is the git blob SHA of that file at `base_commit`
— the rebase-tolerant anchor {ref}`coverage-validity` checks against.
An `e2e`-kind capture has the same shape but omits `tester`/`ticket`/
`note`; at report time its `base_commit` acts as a strict guard — it
must equal the tree's current `HEAD` — and a dirty working tree only
triggers a line-number remap onto the current tree, never the manual
tier's validity pass (see {ref}`coverage-report-stale-builds`).

# Branch-to-Target Mapping (deferred research)

Goal: in the HTML coverage report, when a line has N branches and branch K
was taken, show the user *which* source-level destination that branch
jumps to. Hovering over a branch pill would visibly highlight the target
line in the annotated source. Useful on dense lines with 4+ branches
(typical for short-circuit boolean chains and `switch` statements) where
the current `block.branch` index is opaque.

## Why this is hard

The formats otto already consumes don't carry destination info:

- **lcov `.info` (BRDA)**: `BRDA:<line>,<block>,<branch>,<taken>`. No
  destination field — only an opaque `(block, branch)` index and a count.
- **`gcov --json-format`**: Per-line branch counts, still no jump targets.
- **`gcov` text output (`-b`)**: Shows `branch 0 taken N` lines but not
  where they go.

gcov branches are *basic-block CFG edges*, not source-line jumps. Multiple
branches on a single source line correspond to different decision points
inside the block (e.g. short-circuit evaluation of `a && b && c` creates
three conditional branches all reported on the same source line). To map
each branch back to a meaningful source location we need the CFG plus a
basic-block → source-line mapping.

Note: this is an HTML-only ergonomic improvement. The JSON, Coveralls,
Cobertura, LCOV, and text output formats should stick to the conventional
per-branch hit-count schema and must not carry destination labels.

## The `.gcno` + DWARF approach

GCC emits two companion files per translation unit:

- **`.gcno`** (gcov notes) — written at compile time. Contains the static
  control-flow graph: basic blocks, block-to-block edges, and a mapping
  from blocks to source file + line. Well-documented binary format in
  `gcc/gcov-io.h`. gcovr has a parser (`gcovr/gcov_parser.py`) we can
  study as a reference. Not shipped with `lcov` output — we'd need to
  collect `.gcno` files alongside `.gcda` files on remote hosts (otto
  already has the fetch infrastructure for this).
- **DWARF debug info** in the compiled binary — maps instruction addresses
  to source lines with more granularity than `.gcno` alone. Python's
  `pyelftools` can parse this. Useful when `.gcno` block-to-line info is
  coarse (whole-block granularity) and we want to disambiguate multiple
  decisions on the same line.

### Proposed pipeline

1. **Collection**: extend `otto test --cov` to also fetch `.gcno` files
   from remote hosts (they sit next to `.gcda` files, and unlike `.gcda`
   they don't change between runs so they could be cached).
2. **Parsing**: implement a `.gcno` reader in
   `src/otto/coverage/correlator/gcno_parser.py`. Start by porting the
   minimum subset of gcovr's parser needed for blocks + edges + line
   numbers. Cross-reference with `gcc/gcov-io.h` in the GCC source tree
   for the record format.
3. **CFG model**: new dataclasses `BasicBlock` and `Edge` under
   `src/otto/coverage/store/cfg.py`. A `FunctionCFG` holds `blocks:
   list[BasicBlock]` and `edges: list[Edge]`. Each edge knows its source
   and destination blocks; each block knows its source line range.
4. **Branch → edge correlation**: lcov's `(block, branch)` index maps to
   a specific outgoing edge of a basic block. The mapping is positional:
   `branch` indexes into the block's outgoing edges in the order GCC
   emits them. Validate this empirically against a known test case
   before trusting it.
5. **Edge → target line**: from the destination block, read the first
   source line. If the destination is itself a decision block with no
   "real" statement of its own, follow fall-through edges until we hit
   one that does. If `.gcno` resolution is too coarse, use DWARF line
   info to pick a more precise target.
6. **Optional DWARF refinement**: open the instrumented binary with
   `pyelftools`, look up the DWARF line program entries for the
   destination block's address range, pick the best source line.
7. **Data model**: extend `BranchHits` (or add a sibling field on
   `LineRecord`) with `targets: list[BranchTarget]` where
   `BranchTarget = (file: Path, line: int, label: str | None)`. Purely
   additive; backward-compatible with the dict-keyed tier model.
8. **Renderer**: the HTML template adds `data-target-file` and
   `data-target-line` attributes to each branch pill. A small JS snippet
   in `static/report.js` listens for `mouseenter` on pills and adds a
   highlight class to the matching source row (if the target is in the
   same file) or shows a tooltip with a clickable link to the other
   file.

## Open questions / risks

- **`.gcno` format stability across GCC versions.** The record format
  has changed across major GCC releases. gcovr deals with this; we'd
  need to either match gcovr's version matrix or pin a minimum GCC.
- **Clang `.gcno` compatibility.** Clang writes gcov-compatible files
  but historically with subtle format differences. May need separate
  handling or explicit "not supported on Clang builds" messaging.
- **Coverage from stripped binaries.** Release builds may not carry
  DWARF info; the `.gcno`-only path needs to be usable by itself.
- **Cross-host path resolution.** `.gcno` files hold build-host paths;
  they'd flow through the same `PathCorrelator` used for `.info` files.
- **Scale.** Parsing `.gcno` for every translation unit in a large C++
  project may be slow. Profile before optimizing.
- **Fetch overhead.** `.gcno` files are typically larger than `.gcda`
  files but change only on rebuild. Cache them by (host, build id).

## Suggested phased implementation

1. **Spike**: parse `.gcno` for a single small fixture, print the CFG,
   verify the positional branch → edge assumption.
2. **Integration**: wire `.gcno` fetch into `otto test --cov`, add a
   `CFGStore` alongside `CoverageStore`, expose `get_branch_target()`.
3. **Renderer**: HTML-only hover/highlight UI. No changes to JSON,
   Cobertura, Coveralls, LCOV, CSV, or text output.
4. **DWARF refinement** (optional): only if `.gcno`-only resolution is
   too coarse in practice.

## References

- `gcc/gcov-io.h` in the GCC source tree — canonical `.gcno` record
  format documentation.
- gcovr's `gcovr/gcov_parser.py` — reference implementation in Python.
- [pyelftools](https://github.com/eliben/pyelftools) for DWARF.
- DWARF 5 spec, section 6.2 (line number information).

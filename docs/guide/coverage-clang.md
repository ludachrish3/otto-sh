(coverage-clang)=
# Clang Products

Products compiled with ``clang --coverage`` emit gcov-*compatible*
counters in the GCC 4.8-era file format (clang stamps ``408*``), which
modern GNU ``gcov`` refuses.  They must be read by ``llvm-cov gcov``:

- **Auto-discovery**: with ``llvm-cov`` (or a versioned
  ``llvm-cov-<N>``) on ``PATH``, otto detects the clang stamp and uses
  it automatically — no configuration needed.
- **Explicit config**: point the host toolchain's ``gcov`` at an
  ``llvm-cov`` binary; otto substitutes the required one-word
  ``llvm-cov gcov`` wrapper for ``lcov --gcov-tool`` at capture time.

```json
{
  "toolchain": {
    "sysroot": "/usr/lib/llvm-18",
    "gcov": "bin/llvm-cov"
  }
}
```

```{warning}
Do not force clang to imitate a GCC version stamp
(``-Xclang -coverage-version=…``): clang still writes its own record
layout, and GNU gcov trusts the stamp — it crashes or silently emits
empty data. Let otto route clang counters through ``llvm-cov`` instead.
```

Branch coverage (`BRDA` records) flows through the llvm path as well;
note that ``llvm-cov``'s branch *counts* are coarser than GNU gcov's
(hit/not-hit is reliable, exact execution counts may differ).

The otto host needs `llvm-cov` installed (the `llvm` package) in addition
to the `lcov` prerequisite from the main {doc}`coverage` page.

(coverage-clang-stale-deploys)=
## Stale deploys: why the GCC stamp guard doesn't transfer

The {ref}`.gcno stamp guard <coverage-gcc-stamp-guard>` from the GCC
page does not transfer to clang, because clang's stamp behaves
differently — so otto verifies the pairing itself, at collection:

- **The stamp is a hash of the program's structure, not a
  per-compilation value.**  Rebuilding unchanged code reproduces the
  identical stamp, so a rebuild between deploy and collection is
  harmless — under GCC the same rebuild invalidates every previously
  shipped binary.
- **A stale deploy fails silently at the toolchain level — so otto
  checks the files directly.**  When the shipped binary's code differs
  from the current `.gcno`, `llvm-cov gcov` rejects the counters
  (*"file checksums do not match"* / *"Invalid .gcda File!"*) but still
  exits 0, and through `lcov` the affected files come back with
  all-zero hit counts and no error text at all — nothing to parse.
  otto therefore verifies the pairing structurally before invoking
  `lcov`: the 12-byte header stamps must agree, and — because clang's
  structure stamp survives edits that merely *shift* lines (a comment
  or `#include` added above the code) — every function record in the
  `.gcda` (ident plus line-number and control-flow checksums, the same
  triplet `llvm-cov` verifies) must appear in the `.gcno`.  Either
  disagreement fails collection with the same friendly
  stamp-mismatch error GCC products get, naming the files; the remedy
  is the usual one — redeploy the current build and re-collect.
- **One drift stays invisible to any gcov-level check**: an in-place
  edit that changes neither control flow nor line positions (tweaking
  a constant).  `llvm-cov` accepts the stale binary's counters as
  valid, and the files genuinely carry nothing to tell the difference;
  in otto's e2e flow the
  {ref}`base_commit guard <coverage-report-stale-builds>` is what
  covers that class.
- **There is no ship-step binary scan for clang.** clang does not lay
  out a `gcov_info` struct; the stamp is an inline constant in
  generated code, so the GCC page's build-time scan has no anchor —
  the collection-time check above is the guard.

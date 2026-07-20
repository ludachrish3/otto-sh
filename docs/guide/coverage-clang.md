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
page neither works nor is usually needed under clang, because clang's
stamp behaves differently:

- **The stamp is a hash of the source, not a per-compilation value.**
  Rebuilding unchanged code reproduces the identical stamp, so a
  rebuild between deploy and collection is harmless — under GCC the
  same rebuild invalidates every previously shipped binary.
- **A genuinely stale deploy fails *silently*.** When the shipped
  binary was built from *different* code than the current `.gcno`,
  `llvm-cov gcov` rejects the counters (*"file checksums do not
  match"* / *"Invalid .gcda File!"*) but still exits 0 — and through
  `lcov` the affected files come back with all-zero hit counts and no
  error at all.  otto's typed stamp-mismatch error recognizes only the
  GNU gcov wording, so it does not fire here.  An unexplained 0% file
  in a clang report is the signature; the remedy is the usual one —
  redeploy the current build and re-collect.
- **There is nothing to scan for in the binary.** clang does not lay
  out a `gcov_info` struct; the stamp is an inline constant in
  generated code, so the ship-step binary scan has no anchor.  For a
  pre-report check, compare the 32-bit word at byte offset 8 of a
  fetched `.gcda` against the same word of its `.gcno` — the two file
  formats agree on that header slot (magic, version, stamp), under
  both GCC and clang.

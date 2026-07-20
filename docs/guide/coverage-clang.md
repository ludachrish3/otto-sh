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

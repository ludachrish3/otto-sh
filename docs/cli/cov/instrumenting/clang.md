(coverage-clang)=
# Clang products

Products compiled with ``clang --coverage`` emit gcov-*compatible*
counters in a GCC file format: GCC 4.8's up to clang 20 (stamped
``408*``) and GCC 11.1's from clang 21 (stamped ``B11*``). otto reads both
with ``llvm-cov gcov``, chosen by the stamp
({ref}`step 2 <coverage-gcov-resolution>` on the main page):

- **Auto-discovery**: with ``llvm-cov`` (or a versioned
  ``llvm-cov-<N>``) on ``PATH``, otto reads the stamp from the
  product's own ``.gcda`` files and uses it — no configuration, for any
  host whose record does not name a gcov of its own. Without ``llvm-cov``
  the report fails naming the ``llvm`` package.
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

(coverage-clang-b11)=
## Clang 21 and gcc 11.1.0

From clang 21, LLVM writes gcc 11.1's record layout and stamps it ``B11*``,
gcc 11.1.0's own stamp. Its notes record the compile directory as ``"."``,
a placeholder otto ignores: names resolve beside the notes, as they do for
older clang. Because the stamp alone chooses the reader, a gcc 11.1.0 build
is read by ``llvm-cov`` too unless it names its own gcov; the main page says
how ({ref}`coverage-gcov-resolution`).

```{warning}
Do not force clang to imitate a GCC version stamp
(``-Xclang -coverage-version=…``). otto reads ``408*`` and ``B11*``, the
stamps clang writes, with ``llvm-cov``, and every other GCC stamp with GNU
gcov, so forcing clang to another GCC stamp sends its data to GNU gcov,
which reads it incompletely, emits empty data, or crashes. Leave the stamp
as clang writes it.
```

The otto host needs `llvm-cov` installed (the `llvm` package) in addition
to the `lcov` prerequisite from the main {doc}`../index` page.

The GCC page's {ref}`one build requirement <coverage-build-requirement>`
applies to clang builds too, and so does its advice to clean the counters
after a rebuild. For clang the clean matters more: clang's runtime adds new
counts into an old `.gcda` without checking that it belongs to the same
build, so skipping the clean gives wrong counts and no error.

(coverage-clang-branch-counts)=
## Branch counts

A clang product's branch records (`BRDA`) say whether each branch was
taken, not how often: `1` for taken, `0` for not taken and `-` for a branch
the program never reached. Line and function counts are exact, and so is the
branch coverage percentage; only the number of times each branch went its
way is missing. This is how lcov reads `llvm-cov`: `llvm-cov gcov` can write
either annotated `.gcov` text files or a shorter *intermediate* listing, and
lcov asks for the listing, which says whether a branch was taken but not how
often. Every lcov from 2.0 on does this.

The two settings below go in `lcov_args`: on a host, the record's
`toolchain.lcov_args` in `lab.json`
({doc}`../../../configuration/lab/coverage-toolchain`); for a unit tier,
its `toolchain` table in `.otto/settings.toml` ({ref}`coverage-unit-toolchain`).
otto passes no setting of its own for either, so the entry is all it takes.

### Real branch counts

This setting makes lcov read the `.gcov` text files, which have the counts:

```json
{
  "toolchain": {
    "lcov_args": ["--rc", "geninfo_intermediate=0"]
  }
}
```

```toml
[coverage.tiers.unit.toolchain]
lcov_args = ["--rc", "geninfo_intermediate=0"]
```

Use it only when both of these hold:

- **The build recorded absolute source paths.** clang records a source by
  the name it was compiled as, shortened to a relative name when the source
  lies under the directory the compiler ran in: `clang -c foo.c` records
  `foo.c`, and so does `clang -c "$PWD/foo.c"`. A source named by absolute
  path from outside that directory, such as from a separate build
  directory, is recorded whole. `strings -n 3 foo.gcno` lists the names a
  notes file recorded.
- **The sources are readable at those paths** on the machine running
  `otto cov`.

When either fails, that file leaves the report without failing it: `llvm-cov` writes
an empty report for it, and lcov skips it with one warning in the run's log.
This is why otto leaves lcov's default alone: under it, the capture does not
need the sources at all. Before relying on the setting, report once with it
and once without, and compare the file count `otto cov report` logs on
success. A lower count with the setting means a dropped file.

### C++ end-line errors

A clang C++ capture can fail with a `geninfo: ERROR` such as `function …
end line N less than start line` or `Cannot derive function end line`,
usually naming a destructor or a standard-library header. lcov works out
where each function ends from the line data, and for clang's C++ output it
sometimes gets that wrong. `["--rc", "derive_function_end_line=0"]` in the
same `lcov_args` turns that step off, so functions are recorded without an
end line. It works with or without the text files above, and both go in one
list: `["--rc", "geninfo_intermediate=0", "--rc",
"derive_function_end_line=0"]`. Prefer it to `["--ignore-errors",
"inconsistent"]`, which also gets the capture through but hides every other
consistency error lcov reports.

(coverage-clang-stale-deploys)=
## Stale deploys

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
  stamp-mismatch error GCC products get, naming the files; the remedies
  are the usual ones ({ref}`coverage-gcc-stamp-guard`).
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

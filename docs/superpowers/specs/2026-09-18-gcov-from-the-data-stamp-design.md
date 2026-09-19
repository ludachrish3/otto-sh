# The gcov that reads a product's counters comes from the data's own stamp — design

**Date:** 2026-09-18
**Status:** approved in brainstorming (Chris, 2026-09-18); this document is the written form
**Issue:** #384

## 1. Problem

`otto cov report` reads a Unix host's counters with the gcov its host record names,
and a Unix host's record is never silent: `write_cov_meta`
(`src/otto/coverage/collect.py`) records `host.toolchain` for every fetched
non-container host, the default (`sysroot /`, `usr/bin/gcov`, `usr/bin/lcov`)
included, and `Reporter._resolve_toolchains` (`src/otto/coverage/reporter.py`) takes
a recorded entry before `.gcno` discovery. So `discover_toolchain_from_gcno` is
reachable only for embedded hosts left at the default toolchain and for containers.

The premise behind recording the default ("a Unix host at the default toolchain:
the runner's gcov is genuinely the right answer") is false whenever the product was
built by a compiler other than the runner's default gcov's — a `gcc-12` build on a
`gcc-13` machine, or any `clang --coverage` build. Both fail the report with
geninfo's "Incompatible GCC/GCOV version found" until the host record names
`/usr/bin/gcov-12` or `llvm-cov`. The clang page (`docs/cli/cov/instrumenting/clang.md`)
promises auto-discovery with no configuration; that is false for a Unix host, and it
contradicts the kernel-modules page, which states the true rule.

The otto_kgcov toolchain matrix (`make kgcov`) hit this on six of seven toolchains and
works around it by giving the bed hosts each compiler's gcov through an overlay SUT
repo — the documented per-host contract, not a fix.

## 2. Goal

A product's counters are read by the gcov that wrote them, found from the data itself,
unless the host record names a gcov on purpose. No configuration for the common
cases: several gccs on one build machine, clang. A cross gcov, which no stamp can name,
stays configured.

## 3. Non-goals

- The lcov `--ignore-errors` / `--base-directory` knob (#385).
- Any change to the capture, store or run-tree formats. The cov metadata keeps its
  keys; only which hosts get a `toolchains` entry changes (§4.3).
- Cross toolchains: a stamp names a compiler family and a gcc major, never a target;
  the host record remains the only way to name a cross gcov.
- Embedded hosts' existing behaviour beyond what §4 states uniformly: the resolver is
  per gcda directory and does not know host kinds; an embedded host whose record is
  silent gets the same rule as a Unix one.

## 4. Design

### 4.1 Precedence (approved: "A")

For each `<cov>/<host>/<product>` gcda directory the reporter resolves one toolchain,
in this order:

1. **An explicit host toolchain wins.** A recorded `toolchains[<host>]` whose values
   differ from `Toolchain()`'s defaults is used as is. A record that disagrees with the
   data keeps today's outcome: geninfo refuses and the report fails with
   `CoverageToolVersionError`, which names the remedy. otto does not second-guess a
   configured gcov.
2. **A silent record means the data decides.** "Silent" is: no entry for the host, or an
   entry equal to the defaults (old run trees recorded the default; they benefit
   without re-collection). The resolver reads the version word from the directory's
   own `.gcda` headers (bytes 4:8, either byte order — the merger's header reader) and
   chooses:
   - an LLVM stamp (`402*`, `408*`) → `llvm-cov` from PATH (the existing lookup:
     plain name, else the highest `llvm-cov-<N>`);
   - a GCC stamp whose major equals the system gcov's → the default toolchain;
   - a GCC stamp with another major → `gcov-<major>` from PATH;
   - a stamp that decodes to no major → the default toolchain, with one WARNING naming
     the directory and the raw word.
   The GCC major is decoded from the version word as gcc writes it: the first character
   `'A'` plus a digit is major 5–9, `'B'` plus a digit is 10–19 (`'A95*'` = 9.5,
   `'B24*'` = 12.4). The system gcov's major comes from one `gcov --version` call per
   report, cached; if that call fails, every GCC stamp is treated as "another major"
   and resolved by name.
3. **A directory with no readable `.gcda` header** is left to lcov's own diagnostics,
   as today.

### 4.2 Failures are named

A `gcov-<major>` or `llvm-cov` the data needs that is not on PATH fails the report
before lcov runs, with one error naming the host, the product, the stamp and the major
it decodes to, the tool looked for, the install command (`apt install gcc-<major>`,
which ships `gcov-<major>`; `apt install llvm`), and the override (`toolchain.gcov` in
the host record). Nothing skips and nothing falls through to a gcov that geninfo would
refuse a moment later.

### 4.3 Collect stops recording the default

`write_cov_meta` writes a `toolchains[<host>]` entry only for a host whose toolchain
differs from the defaults, for every host kind (containers were already skipped on
kind; the kind check goes, the value check replaces it). The metadata then says only
what was configured. The reporter's "equal to the defaults" rule in §4.1 covers run
trees collected before this change.

### 4.4 Where it lives

- `src/otto/host/toolchain_discovery.py`: the stamp decoder (`gcov_stamp_major`), the
  `gcov-<major>` lookup, the system gcov major probe, and a new per-directory entry
  point `discover_toolchain_from_gcda(gcda_dir, *, system_major) -> Toolchain | None`
  that returns the toolchain the stamp names (or `None` for "the default"), raising the
  named error of §4.2. `discover_toolchain_from_gcno` stays for the embedded build-dir
  path that calls it at collect time.
- `src/otto/coverage/reporter.py`: `_resolve_toolchains` applies §4.1 per gcda
  directory; the per-run `.gcno` fallback it has today becomes the per-directory
  `.gcda` rule (the `.gcno` sample under the source root was the wrong granularity:
  one run can hold a clang product and a gcc product).
- `src/otto/coverage/collect.py`: §4.3, and the comment that argued the opposite goes.
- `src/otto/coverage/errors.py`: the new error class for §4.2, an `OttoError`.

### 4.5 Visibility

One INFO line per directory whose gcov was chosen from the stamp: host, product, the
stamp, the tool. A configured record logs nothing new.

## 5. Docs

- `docs/cli/cov/instrumenting/clang.md`: the auto-discovery bullet is true again for a
  Unix host; say when it applies (a record that names no gcov).
- `docs/cli/cov/instrumenting/gcc.md`: the multi-gcc rule — a product built by another
  gcc major is read by `gcov-<major>` when it is installed; the failure names the
  package otherwise.
- `docs/cli/cov/index.md`: the toolchain resolution order becomes §4.1's three steps;
  the other pages link to it rather than restate it.
- `docs/configuration/lab-config.md`, the `toolchain` row: "Omit it to leave the host on
  the gcov its data names (the system gcov, `gcov-<major>` or `llvm-cov`)".
- `docs/cli/cov/instrumenting/kernel-modules.md`: the paragraph that tells a user to
  set `toolchain.gcov` for a non-default compiler shrinks to a link to the index page;
  the clang lcov sentence stays (#385).

## 6. Tests

- Unit (`tests/unit/cov/test_toolchain_discovery.py`): the decoder on `'A95*'`,
  `'B05*'`, `'B24*'`, `'408*'` and an undecodable word; `discover_toolchain_from_gcda`
  on synthetic headers in a tmp directory (LLVM → llvm-cov; same major → `None`; other
  major → `gcov-<major>`; undecodable → `None` plus the warning; the tool absent → the
  named error with every part §4.2 lists), with `shutil.which` and the system-major
  probe patched.
- Unit (`tests/unit/cov/test_collect.py`): a Unix host at the default gets no
  `toolchains` entry; a configured one still does.
- Unit (reporter): a recorded default is silent; an explicit record wins over a
  disagreeing stamp; a mixed run (one LLVM directory, one GCC) resolves each on its own.
- Live: the kgcov matrix (`tests/e2e/cov/test_kgcov_toolchains_e2e.py`) stops
  building the gcov half of its overlay — `overlay_lab` names only `toolchain.lcov`,
  and only for clang — so `make kgcov` proves discovery on the bed for gcc-9 to gcc-14
  and for clang. The helper tests in `tests/e2e/cov/test_repo5_build_helper.py` follow.
  The kgcov spec (`2026-09-18-kgcov-toolchains-and-cross-compiling-design.md` §8) is
  amended to say so.

## 7. Compatibility

No format changes. Run trees collected before this change carry recorded defaults,
which §4.1 treats as silent, so `otto cov report` on an old run now finds the right gcov
too. A host whose record names the system gcov explicitly is indistinguishable from a
silent one in the metadata; such a record's data is then read by the gcov its stamp
names, which is the gcov that can read it — the only behaviour that changes for that
record is a failure becoming a success.

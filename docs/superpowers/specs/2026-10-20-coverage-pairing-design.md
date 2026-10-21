# Coverage pairing, accumulation and the unit-tier filter — design

**Issues:** #665 (centre), #666, #667, #668, #669, #670, #680, #693. **Status:** decisions taken with Chris on 2026-10-20 (§13); reviewed by Astra and Fable, then cross-reviewed; all findings folded in or ruled on (§14).

All eight issues come from the #594 naive-user study, round 1 (2026-10-19). Every setup agent reached its coverage goal only after workarounds, and most of those trace to one cause: otto pairs a fetched `.gcda` with a `.gcno` by **basename**, after throwing the `.gcda`'s directory away. This design replaces basename pairing with path pairing, and fixes the problems that share its code path.

**Toolchain facts this design rests on** (verified by Fable with gcc 13.3, clang 18 and lcov 2.0; cited by Astra from the gcc, LLVM and lcov sources):

- gcc and clang embed each object's coverage path in the instrumented binary as a NUL-terminated string: the **absolute** path of the object's coverage stem plus `.gcda`, even when the object was compiled with a relative `-o`. The stem is the object name minus its last suffix: CMake's `add.cpp.o` has stem `add.cpp` and writes `add.cpp.gcda` / `add.cpp.gcno`. `-ffile-prefix-map` and `-fprofile-prefix-map` do not change it; `-fprofile-dir` mangles it into one path component.
- `GCOV_PREFIX` prepends a directory to that path; `GCOV_PREFIX_STRIP=n` first drops `n` leading components.
- Both runtimes merge into an existing `.gcda` on exit. gcc's merges compatible data and overwrites on an object-checksum mismatch; clang's merges counters without equivalent checksum validation.
- `lcov --capture --directory <dir>` with each `.gcda` beside its `.gcno` (copied or symlinked) works for gcc and clang, from any working directory, whatever the original object path.
- lcov 2's per-file failures on gtest code (`mismatched end line`, `unknown line`) are raised by `geninfo` **during capture**. A capture-time `--include`/`--exclude` avoids them; `lcov --extract`, which works on a finished tracefile, cannot.
- `geninfo` matches `--include` patterns as unanchored globs against the source paths **it** computed (made absolute before filtering), not against otto's resolved paths. A pattern that matches nothing is an error unless `--ignore-errors unused` is passed. No `--include` at all means everything is included.
- A clang `.gcno` stores source names as the compiler saw them (often relative, e.g. `../src/lib/add.c`) and no compilation directory; `geninfo` resolves a relative name against the `.gcda`'s directory.
- With `--ignore-errors source`, a capture whose source files are absent still produces a full tracefile. `geninfo` reads sources when they are present (for `LCOV_EXCL` markers and similar filters).

## 1. Goal and acceptance

**Goal.** Coverage works for a product however its build is laid out: any number of object directories, the same basename in several products or directories, unit and gtest trees under the project root, builds done inside a container. The user follows one build rule (§2) and configures nothing else.

**Acceptance** (each is a test in §11):

1. A product whose objects span several directories (a library plus an executable, CMake's per-target object dirs) captures and reports, with no setting.
2. Two products that both compile `main.cpp`, a unit-test tree under the project root that compiles the product's sources, and a second (stale or release) build tree of the product change nothing in either product's e2e report.
3. A kernel module loaded, exercised and unloaded twice in one `otto test --cov` session reports both loads' hits.
4. The unit tier's report contains only product source files; test files, gtest and mocks are dropped and counted, with no setting, and the gtest code never reaches `geninfo`.
5. A shell product running inside a compose container host, named by a `[coverage].hosts` regex that matches only the container, captures and reports.
6. Every e2e and manual run in `store.json` records its host and product.
7. A capture from an older commit is reported as such, not as a rebuild.
8. A `geninfo` failure prints its first `ERROR:` line and a short tail, not the full output, once.
9. A capture whose source paths all exist logs no mapping warnings.

## 2. The one build rule

**Each object file needs a unique path: absolute, or relative to the project root.**

This is the only coverage build requirement the user docs state. In toolchain terms (the facts above): the path a `.gcda` is written to on the device — the object's absolute coverage path, shortened by `GCOV_PREFIX_STRIP` when set — must be unique within a product. An ordinary build meets it without effort, because the compiler embeds absolute paths; it can only fail when `GCOV_PREFIX_STRIP` strips so deep that two objects' remaining paths coincide (for example both become `main.cpp.gcda`). Then the runtime merges both objects' counters into one file on the device, and otto says so (§5.3).

**Unsupported:** `-fprofile-dir`, which flattens the coverage path into one component. The docs name it.

**Rebuilds.** When a product is rebuilt, its stored counters must be cleared (`--cov-clean`, on by default for `otto test --cov`, or `otto cov clean`). gcc's runtime overwrites a mismatched file; clang's merges new counters into the old file without validation, so a clang rebuild without a clean corrupts the counts. The docs say this on the gcc and clang instrumenting pages.

## 3. Fetch keeps paths

Today `host.get` copies every remote `.gcda` into `<cov>/<host>/<product>/` under its own basename (remote.py:102), so two `.gcda` with one basename collide and the directory structure is lost.

**New:** for each instrumented product, otto archives the **whole** `cov_dir` on the host in **one** archive with a plain tar, written to a file `mktemp` makes in the host's temp dir: `t=$(mktemp "${TMPDIR:-/tmp}/otto-fetch-<id>.XXXXXX") && echo "<marker>$t" && cd <cov_dir> && tar -cf "$t" .`. The explicit template is a form GNU and BusyBox `mktemp` both accept. Its unique token lets otto remove the archive on every path, including a raised command, with `rm -f "${TMPDIR:-/tmp}"/otto-fetch-<id>.*`. There is no `find`, no file list and no `-T`, and nothing is written into `cov_dir`, so the fetching user needs only read access to it, plus a writable temp dir. otto fetches the archive only after the command exits 0 and extracts only its `.gcda` members to `<cov>/<host>/<product>/`. Each `.gcda` lands at `<cov>/<host>/<product>/<path relative to cov_dir>`. A non-zero exit from `mktemp`, `cd` or `tar` fails that product's fetch, with no fallback, so a partial archive is never fetched. That covers a read-only or missing temp dir, an unreadable file or directory, and a host with no tar. The product's counters then stay on the host for the next collection. A product's default reset deletes only `.gcda` under `cov_dir`, never the directory itself, because `cov_dir` is user-configurable. One transfer per product, whatever the object count; container hosts use the same commands through their own exec and transfer.

**Embedded boards** decode their console dump into the same layout: `decode_cov_dump` (embedded.py:82) keys by the full `.gcda` path from the dump, not the basename, and `_collect_one_embedded_host` writes each file at its path under the product directory.

**Post-fetch clean, scoped.** Today the clean after collection resets every instrumented product on every host that contributed any product (collect.py:193, :353), so one product's success can delete another product's unfetched data. **New:** `clean_coverage` takes the (host, product) pairs to clean, and the clean after collection passes only the pairs whose fetch **and capture** succeeded; the dry-run (`NotRun`) handling stays per product. A product whose fetch or capture failed keeps its counters on the host, so after fixing the cause (a toolchain, a build tree) the user simply runs `otto cov get` again, which fetches and captures it afresh. The next `otto test --cov` clears it as usual through `--cov-clean`. Counts written between a product's fetch and its clean (a process exiting in that window, a kmod's live counters after its prepare dump) are lost, as today; the docs say to collect after the product's processes have stopped.

## 4. The product's object list

For each instrumented product otto reads the **object list** from its local artifact: the absolute coverage path (the `.gcda` path minus `.gcda`, e.g. `/home/u/acme/build/CMakeFiles/app.dir/src/main.cpp`) of every object built into it.

- **Scan.** otto scans bytes for NUL-terminated strings ending in `.gcda` (pure Python). Duplicates are dropped.
- **Per kind.**
  - `shell`: the declared `artifact`. A directory artifact is walked (as `scan_for_instrumentation` does today, product.py:598). An archive artifact (`ARCHIVE_SUFFIXES`, product.py:555) is read member by member, gzip included. For an ELF executable, otto also follows its `DT_NEEDED` entries: a needed library found under the project root by its soname (exactly one match) is scanned as part of the product. Libraries loaded with `dlopen` cannot be discovered; a product that wants them counted declares a directory or archive artifact that contains them, and the docs say so.
  - `kmod`: the declared `.ko`.
  - `docker_image`, tarball form: the `docker save` archive (`.tar`, `.tar.gz`, `.tgz`) is read without a daemon. otto builds the image's **effective filesystem** — later layers replace earlier files, and OCI whiteouts (`.wh.<name>`, `.wh..wh..opq`) delete them — and scans every regular file in it that starts with the ELF magic.
  - `docker_image`, reference form: the image lives on the lab host's daemon (docker_image_kind.py:196-235 runs every `docker` command through `host.exec`). e2e and manual pairing do not need its list (§5.2). The unit tier does (§7.3): in the product's **running container**, otto lists the regular files that contain the string `.gcda` (one `grep -rl --binary-files=binary '\.gcda'` over the container's filesystem, skipping `/proc`, `/sys` and `/dev`), fetches only those files, scans them, and caches the list keyed by image ID. A container that is not running is an error that says to install the product first. No whole-image transfer.
  - Embedded products: each loaded product extension's own artifact (embedded_kind.py), scanned the same way. The declared `build_dir` / `builds.<version>.build_dir` stays the search root for that board's `.gcno` (§5.1).
- **Caching.** One read per artifact per report, keyed by path, size and mtime (image ID for the reference form).
- **Shared libraries and plugins** the product loads are not in the executable's list. Their `.gcda` take §5.2 (§5.1 step 1 finding no candidate is not an error).

## 5. Pairing

Let `R` be a fetched `.gcda`'s path relative to its product directory (§3), minus `.gcda`. "Ends with" is always component-wise: `xmain.cpp` never matches `main.cpp`.

### 5.1 With the object list

1. **The object.** Take the product's object paths that end with `R`.
   - **One:** that is the object.
   - **Several:** the device merged several objects' counters into one file (§2); a pairing error names the file, the objects and `GCOV_PREFIX_STRIP`.
   - **None:** the `.gcda` is not one of the artifact's own objects (a shared library, a plugin); pair it by §5.2, with one INFO line per product listing such files.
2. **The notes.** If `<object path>.gcno` exists on this machine, take it. Otherwise (a build done elsewhere: in a container at `/build/...`, on a CI machine) search the search root (the project root; an embedded board's declared `build_dir`) for `.gcno` files that end with the object path's own file name, then:
   1. keep those whose header **version and stamp** match the `.gcda`, and, for clang notes, whose per-function checksums match (the existing structural checks, merger.py:78-131);
   2. of those, keep the ones with the longest common tail with the object path;
   3. if one remains, take it; if several remain and they are **byte-identical** files, take any one (they produce identical results; DEBUG names the others); if several remain and differ, it is an error (§5.3).
3. **Stamp check.** The chosen pair's version and stamp (and clang checksums) must match. A mismatch is `CoverageDataMismatchError` (the product was rebuilt after collection), naming both files.

The search in step 2 only happens for a build done elsewhere. A product built on this machine finds its notes at `<object path>.gcno` directly, so a unit tree, a gtest tree or a stale second build tree under the project root is never even considered. For a relocated build, step 2.1's structural checks reject notes from a different compilation in most cases: gcc's stamp is per compilation. clang's stamp is a structure hash, so two separate clang builds of unchanged code can both pass; step 2.3 then takes one only when the files are identical, and otherwise asks the user to remove the stale tree. It never picks between differing notes.

### 5.2 Without the object list

When the product has no readable object list (a missing or uninstrumented artifact, the reference form of `docker_image` outside the unit tier), or for a `.gcda` that step 1 placed here, the `.gcda`'s own `R` replaces the object path in step 2: tail search, stamp filter, longest tail, exactly one. One INFO line per product says why the list was unavailable.

### 5.3 Errors

Every pairing error names the `.gcda`, every candidate considered and why it was rejected, and what to do:

- **Several objects for one `.gcda`:** "the device wrote these objects' counters to one file; lower `GCOV_PREFIX_STRIP`."
- **Several build trees hold differing notes that all match this object:** "these build trees each hold notes that match this object, and otto cannot tell which built it; remove the stale tree." 
- **No notes:** "no `.gcno` for this object under <search root>; keep the build tree that produced the product."

otto never falls back to basename pairing. The whole-project-root `rglob` for matching stems and the per-directory `--build-directory` list are removed.

## 6. Capture and source paths

### 6.1 One capture per product

For each (host, product), otto builds a staging tree in which each paired `.gcda` sits beside a symlink to its `.gcno`, at `R`. It runs **one** `lcov --capture --directory <staging> --gcov-tool <tool> --rc branch_coverage=1 [lcov_args] [--include <pattern> ...]`, with no `--build-directory`. The capture must not **depend** on source files: otto passes `--ignore-errors source`, so a source that is absent on this machine (a build done elsewhere) is not an error. When sources are present, `geninfo` reads them as usual, so `LCOV_EXCL` markers keep working. A test pins capture with the sources absent.

### 6.2 Source paths (#666)

Source resolution runs on the capture's `SF:` lines, after capture, in this order:

1. **Under the staging tree** (a clang relative source name that `geninfo` resolved against the staged `.gcda`'s directory): the staging prefix is stripped, `..` components are normalised away, and the rest is tail-matched as in 3.
2. **Absolute and exists:** used as is, no log line.
3. **Absolute and missing** (a build done elsewhere): tail-matched under the project root. A unique longest-tail match is used; otherwise one WARNING names the path and the raw path is kept.
- **Outside the project root** (kernel headers, `/usr/include`): dropped in `build_capture` (capture/model.py:219) as today, now summarised as one INFO line per capture with the count, each file at DEBUG.

The one-prefix `Auto-discovered path mapping` and its `strings`-on-`.gcno` fallback are removed.

### 6.3 Failures: short, once, per product (#669)

- `lcov`/`geninfo` output goes to the run's log file only.
- A failed product capture raises with the first `ERROR:` line, the last 20 lines and the log file path. The typed errors (`CoverageDataMismatchError`, `CoverageToolVersionError`) keep their framing and carry the same short detail.
- `produce_captures` (produce.py:136-162) no longer stops at the first failing product: it captures every product, writes `capture.failed` (the short detail) in a failed product's directory, and raises one error listing the failed products after the loop.
- The report walk treats `capture.json` as captured, `capture.failed` as failed (reported in the summary, not retried), and a product directory with neither as **not yet captured** (§9.1). So `otto test --cov` reports each failure once and builds its report from the products that captured.

## 7. The unit tier (#680)

### 7.1 Harvest and pairing

For each directory of a unit tier (`harvest_dirs`, or a `products` view's directories), anchored to the project root, otto finds every `.gcda` recursively. A unit build writes each `.gcda` next to its object, so its notes are `<same path>.gcno` with the §5.1 stamp check; no tail match. The capture runs **in place** — one `lcov --capture --directory <tier directory>` with §6.1's other options — with no staging tree: in place, `geninfo` resolves clang's relative source names against the real build tree.

### 7.2 Toolchain

The local sweep runs with the toolchain of the active lab's `local` host. A lab may declare its own `local` host (builtin_hosts.py:37-38), and that declared host's `toolchain` (including `lcov_args`) is what the sweep uses; with none, the built-in `LocalHost`'s defaults apply as today. The harvesting merger (reporter.py:661) is constructed with that toolchain. This is the #680 knob; there is no new setting. The docs on the unit tier show the declaration.

### 7.3 Product source filter

- **Which products.** A `products` view filters by its own product. Plain `harvest_dirs` filter by every product of the tier's project except those declared `instrumented = false`. A product whose instrumentation is unknown takes part and is validated (§7.4); `instrumented_products` (instrumentation.py:112), which drops unknown verdicts, is not used for this selection.
- **A product's source set** is every source file named by the `.gcno` of every object on its object list (§4) — functions' files and the header files their line records name — resolved as in §6.2.
- **Filter in two passes.**
  1. *At capture (coarse).* otto reads the unit `.gcno` files' source names, resolves them as in §6.2, and keeps those in the source set. For each kept file it passes one `--include '*/<path relative to the project root>'` — a tail glob, so it matches whatever absolute form `geninfo` computes — plus `--ignore-errors unused`. A glob metacharacter (`*`, `?`, `[`) in a kept path is replaced by `?` in its pattern, so the pattern still matches the file. Dropped files never reach `geninfo`'s per-file processing, which is where gtest's `TEST()` bodies fail lcov 2. **If no file is kept, otto does not run the capture** (no `--include` would mean "include everything"); the INFO line says so.
  2. *After capture (exact).* A tail glob is unanchored, so `*/src/add.c` also matches `tests/src/add.c`. otto filters the resolved `SF:` paths against the source set exactly, and only those count.
- **Limits** (documented): the filter works per file. A file whose functions belong partly to the product and partly to tests is kept whole. A source file no product links (one only a future product will use) is dropped.
- **Logging.** One INFO line per capture: `unit <view>: kept 41 product source files, dropped 17 not in any product`. At DEBUG, per file: `unit <view>: keep src/core/add.c (product pfctl)` or `unit <view>: drop tests/test_add.cpp (not in any product)`.

There is no `exclude` setting.

### 7.4 Errors, before any capture

- **A product the tier filters by is not instrumented** — its artifact is missing, or holds no embedded `.gcda` paths: an error names the product and its artifact path and says to build the product with coverage instrumentation (`--coverage`) before collecting unit coverage. (Products declared `instrumented = false` are not filtered by, so they never cause it; a product whose instrumentation is unknown is checked here.)
- **No `.gcda` under a tier directory:** today this is a WARNING and the directory is skipped (reporter.py:699-705); it stays a warning, and its text says to run the unit tests first.

## 8. Counts accumulate on every emit (#670)

Counts on a host accumulate every time they are written to disk, until a clean. There are no per-load or per-run subdirectories.

**User space** (shell and container products): already true; both runtimes merge on exit (the facts above, and §2 on rebuilds).

**Kernel modules** (`otto_kmodcov`): each object's accumulator is seeded once per load from the file on disk.

- **Seed states**, per object: `pending` at load; `seeded` after a successful import or when there is no file; `skipped` after a reset or a validated mismatch. Only `pending` imports, at that object's next dump, before the write; `seeded` and `skipped` never import again during this load.
- **Operational read failure** (an I/O error, an allocation failure): the object stays `pending`, the existing file is **not** overwritten by a sysfs `dump` (the accumulator keeps the counts, the next dump retries), and one `pr_warn` says so. At unload — the last chance — the import is retried once; if it still fails, the load's counts are written and the warning says the earlier loads' counts in that file were lost.
- **Reset** (`echo 1 > .../kmodcov/reset`) zeroes live counters and accumulators and moves every object to `skipped`. So otto's clean — reset, then delete the files — cannot re-import the files it is about to delete, even if a dump runs between the two steps; that dump writes zeroed counters.
- **Decode.** The reader is format-aware: gcc before 12 (no checksum word), gcc 12 and later (an extra header word, byte-counted lengths) and clang (three header words, word-counted lengths) each have their own header and length rules (kmodcov_gcc.c:422, kmodcov_clang.c:412). Each backend gets an import hook that adds decoded counters into its own accumulator type.
- **Validate, then mutate.** The whole file is decoded and checked against the loaded object first: magic, version, stamp, the function list with each function's checksums, and every counter array's length bounded by the file size. Only a fully valid file is imported. A validated mismatch leaves the accumulator untouched, marks the object `skipped`, prints one `pr_warn` naming the file and the reason, and the dump overwrites the file.
- **Versioning and deployment.** `KMODCOV_INTERFACE` (kmodcov.h:29), the consumer-facing ABI, does not change: consumer modules need no rebuild. The runtime's `KMODCOV_OTTO_VERSION` is generated from the exporting otto version (library.py:155), so the change ships as an otto release; a lab picks it up by re-exporting and rebuilding the `otto_kmodcov` dev tool (`otto cov kmodcov export`), and `interface_problem` (kmod_tool_kind.py:312) keeps checking the runtime library it loads.
- **Unchanged:** the existing race between a dump's add-then-zero and instrumented code running on another CPU is pre-existing and out of scope. The file's header comment ("no .gcda is ever parsed") is rewritten to the new contract.
- **Clean is unchanged:** `--cov-clean` at suite start (on by default), `otto cov clean`, `otto cov get --clean` and the clean after collection (now scoped to products that fetched and captured, §3) reset a loaded module and delete files recursively. Reset never deletes files.

## 9. The smaller fixes

### 9.1 Every run records host and product (#668)

- Every e2e and manual run in `store.json` is registered from a per-(host, product) capture.
- The legacy path is **removed**: capture every `.gcda` directory, merge into one `merged.info`, register one run with no host or product (reporter.py:444; `capture_and_merge`, `discover_gcda_dirs`, `CoverageReporter(gcda_dirs=…)`). The bare `--tier system` form (`TIER_SYSTEM`, reporter.py:326-336) that fed it is removed; `--tier name=file.info` stays.
- A product directory that is **not yet captured** (§6.3: neither `capture.json` nor `capture.failed`) is captured at report time by the same code as collection. Its tier and `base_commit` are the ones collection recorded in `.otto_cov_meta.json`, which collection now always writes (a run collected with `otto cov get --tier manual` stays manual). A directory with no recorded tier or commit is an error naming it, never anchored to the current `HEAD` or guessed as e2e.
- A run from `--tier name=file.info` is a user-supplied tracefile: it records the tier only, and the docs say so.

### 9.2 Container hosts in `[coverage].hosts` (#667)

Every coverage walk passes `include_containers=True`. Today only the embedded collector (fetcher/embedded.py:200-204) misses it. The empty-selection error's advice becomes actionable for CLI users: when a CLI command (not a library call) hides every match behind a membership flag, the message says the command does not walk those hosts and that the regex is correct, instead of telling the user to pass a Python keyword argument.

### 9.3 A capture from an older commit (#693)

The commit check (reporter.py:626) raises a new `CaptureCommitError`, not `CoverageDataMismatchError`. It names the capture's commit and the tree's commit and gives both remedies: check out the capture's commit, or re-run `otto test --cov` at the current one. The build-stamp framing stays for real stamp mismatches.

## 10. Docs

- `docs/cli/cov/instrumenting/gcc.md`: the rule in §2, why (pairing by path), `GCOV_PREFIX` / `GCOV_PREFIX_STRIP` as choosing the written path, `-fprofile-dir` unsupported, clean on rebuild, the pairing errors.
- The clang instrumenting text: clean on rebuild (clang merges without validation).
- `docs/cli/cov/tiers.md`: the unit tier's product source filter, its limits and log lines, the instrumented-products requirement, the `local` toolchain declaration; the "each view needs its own build or object directory" guidance rechecked against §7.
- `docs/cli/cov/instrumenting/kernel-modules.md`: counts accumulate across loads; reset stops a seed; the mismatch warning.
- `docs/cli/cov/instrumenting/containers.md`: the container-only `hosts` regex works.
- `docs/cli/cov/during-tests.md`: the run tree's new layout — `.gcda` under `<host>/<product>/` at their paths relative to `cov_dir`, mirroring the object tree, instead of flat; collect after the product's processes stop; a failed capture is recovered by `otto cov get` again.
- `docs/cli/cov/report.md`: stamp-mismatch and commit-moved errors, separately; `capture.failed`; explicit `--tier` tracefiles record the tier only; bare `--tier system` gone.
- `docs/architecture/subsystems/coverage/merging.md`: the legacy merge removed; pairing, staging and the capture states described.
- The #594 quickstart tracks (L4) state §2 as the one build requirement.

## 11. Testing

**Unit (hostless):**
- Pairing: one, several and no step-1 candidates; a strip collision; a local build whose stale second tree and unit tree are never searched; a relocated gcc build with a stale tree (stamp filter); a relocated clang build with two independently compiled trees of unchanged code — byte-identical (one taken) and differing (the stale-tree error); a container `/build/...` object path; a CMake `foo.cpp.gcda` stem; a kernel `.tmp_` notes name; component-wise matching.
- Artifact scan: gcc and clang binaries, a `.ko`, a directory artifact, a `.tar.gz` artifact, a `docker save` archive with an ELF replaced in a later layer and one deleted by a whiteout, a reference-form image read from its running container, a non-instrumented binary, de-duplication, the cache.
- Fetch: the tar stream extracts to `<product>/<path>`; the scoped clean leaves a failed product's files alone; embedded decode keeps full paths.
- Capture: staging, the single command line, capture with sources absent, symlinked and copied notes.
- Source paths: exists (no log), absolute tail match, relative tail match, ambiguous (one warning), outside the root (one INFO count).
- Unit filter: in-place capture of a clang unit build with relative source names; tail-glob `--include` with `--ignore-errors unused`; a kept path with `[` in it; zero kept files skips the capture; the exact second pass drops `tests/src/add.c` when `src/add.c` is kept; keep/drop DEBUG lines and the INFO count; the not-instrumented error; `instrumented = false` ignored and unknown validated; the declared `local` toolchain reaching the merger; a `DT_NEEDED` library's sources kept.
- Capture states: `capture.failed` written, reported once, not retried; a failed product's counters left on the host and captured by a second `otto cov get`; a recorded `manual` tier kept at report time; a not-yet-captured directory captured at report time with its recorded commit; no recorded commit is an error.
- Errors: each §5.3 text, the short `geninfo` failure, `CaptureCommitError`, the CLI empty-selection advice.
- kmodcov reader, built in user space from the same C: round trip for gcc before 12, gcc 12+ and clang; stamp, version and function-checksum mismatch; truncated file; lengths overrunning the file; no file (`seeded`); reset moves `pending` to `skipped`; an injected read failure keeps `pending`, does not overwrite on a sysfs dump, and retries at unload.
- `store.json`: every e2e and manual run has host and product.

**Bed lane** (the coverage arc end to end on the lab VMs):
- **Hierarchical example:** a library (shared, found through `DT_NEEDED`) and an executable in separate object directories; two products that both compile `main.cpp`; a gtest tree under the project root compiling the product's sources; a stale second build tree. Acceptance 1, 2, 4.
- **Container shell product:** a shell product running inside a compose container host with `GCOV_PREFIX`, built inside the image at `/build/...`, with `[coverage].hosts` naming only the container. Acceptance 5.
- **Kernel module reload:** load, exercise, unload, load, exercise, unload in one session; both loads' hits appear (the #670 repro: 20 hits, not 2). Acceptance 3.

## 12. Behaviour changes and breaking marks

User-facing and library changes, each a breaking (`!`) commit by the project's rule:

- The unit tier drops source files not in any product (§7.3).
- Unit coverage refuses to run when a product it filters by is not instrumented (§7.4).
- An ambiguous pairing, or a `.gcda` with no notes, fails instead of being left to lcov (§5.3).
- The run tree stores `.gcda` at `<product>/<path>`, not flat (§3).
- The legacy merge, the bare `--tier system` form, and the library's `capture_and_merge`, `discover_gcda_dirs` and `CoverageReporter(gcda_dirs=…)` are removed (§9.1).
- A capture from another commit raises `CaptureCommitError`, no longer `CoverageDataMismatchError` (§9.3).
- A kernel module's counts accumulate across loads (§8).
- `lcov` output leaves the console (§6.3).
- The clean after collection touches only the products it fetched and captured (§3); `clean_coverage` takes (host, product) pairs.

## 13. Decisions (Chris, 2026-10-20)

1. The build rule is a unique object path, absolute or relative to the project root (§2).
2. A path that does not exist locally is tail-matched within the project root, unique match required (§5).
3. No per-load or per-run subdirectories: counts accumulate on every emit (§8).
4. Reset never deletes stored data; `--cov-clean` at suite start and `otto cov clean` already clear everything, recursively, and stay as they are (§8).
5. The clean after collection deletes what was fetched — only once the product's capture succeeded, so `otto cov get` again is the recovery from a failed capture, with no extra option (§3).
6. The unit tier's product sources come from the local product build: products are built alongside the unit tests (§7.3).
7. Unit coverage without instrumented products is an error that tells the user to build them instrumented (§7.4).
8. DEBUG lines name each file kept as product source and each file dropped (§7.3).
9. `docker_image` products: otto reads the instrumented binaries out of the image (§4).
10. #693 and the container shell product bed case are folded in.

## 14. Review record

Round 1 (Astra, Fable), folded in:
- Unit filtering moved from `lcov --extract` to capture-time `--include` (both; Fable verified).
- Pairing step 2 filters by stamp and clang checksums before comparing tails; a strip collision and copied trees get their own errors; shared libraries fall through to §5.2 (both).
- Source resolution happens after a capture that never reads sources; relative clang paths are tail-matched (Astra).
- The `local` toolchain route made explicit and wired into the harvesting merger (Astra, Fable).
- kmodcov: a format-aware decoder per ABI, validate-then-mutate, seed states with reset suppression, `KMODCOV_OTTO_VERSION` not `KMODCOV_INTERFACE` (Astra, Fable).
- Post-fetch clean scoped to fetched products (Astra).
- Artifact scope: directories, archives, gzip tarballs, embedded extensions, de-duplication; the reference-form `docker_image` read on its own host for the unit tier only (both).
- Capture states (`capture.json`, `capture.failed`, not yet captured) resolve the retry contradiction; report-time capture uses the recorded commit (both).
- Fetch as one tar stream per product; embedded decode keeps full paths (Fable).
- Rebuild guidance per runtime (Astra).
- §12 completed; landing 4 depends on landing 1 (Fable).

Cross-review (round 2), folded in:
- **Ruling on candidate identity.** Fable: take the first of several notes that pass every check. Astra: matching stamps do not prove one build for clang. Ruled: take one only when the files are byte-identical (identical results by construction); otherwise error and name them. And the search only runs for builds done elsewhere, so local stale trees never compete (§5.1).
- The unit tier captures in place with tail-glob includes, `--ignore-errors unused`, a skip when nothing is kept, and an exact second pass on resolved paths; glob metacharacters replaced by `?` (Fable verified the include and unused behaviour; Astra the pre-remapping paths and the empty case).
- "Never reads sources" replaced by "does not depend on sources": `LCOV_EXCL` markers keep working when sources are present (Astra noted `geninfo` reads sources for its filters; disabling them with `--no-markers` would break users' markers).
- Staging-relative clang paths resolved first (Fable).
- Product selection for the unit filter: everything except `instrumented = false`, then validated (Astra).
- `DT_NEEDED` libraries scanned; `dlopen` plugins via directory or archive artifacts (Astra).
- Docker effective filesystem with whiteouts (Astra); reference images read from the running container instead of `docker save` (Fable).
- kmodcov: operational read failures keep history and retry (Astra); deployment described as release, re-export, rebuild (Astra).
- `tar -T -`, `.gcda` only (Fable), later replaced by a plain `tar` of the whole `cov_dir` into a `mktemp` file in the host's temp dir, keeping only `.gcda` locally (Chris); recorded tier at report time; the failed-capture recovery (Fable) — ruled by Chris as `otto cov get` again, by cleaning only after a successful capture, instead of a new `--recapture` option; `clean_coverage` (host, product) pairs (Fable).

## 15. Out of scope

- Container hosts in `run install`, `status` and `cleanup` walks, and backgrounded installs on container hosts: the docker batch (#676, #683, #678).
- Coverage on uncommitted changes (#687).
- A CLI summary per product and tier (#681).
- The pre-existing dump/execution race in kmodcov (§8).

## 16. Landings

One spec, four landings; each lands with its tests and docs:

1. **Pairing** — §3–§6, §9.1, §9.3 (#665, #666, #668, #669, #693). Bed: the hierarchical example.
2. **Unit tier** — §7 (#680). After landing 1 (object list, staging, capture).
3. **kmodcov accumulation** — §8 (#670). Independent; can run in parallel with 1 and 2. Bed: the reload case.
4. **Container hosts** — §9.2 (#667). After landing 1 (its bed case needs the path-keeping fetch and the tail match). Bed: the container shell product.

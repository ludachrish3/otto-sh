# Embedded (Zephyr) Code Coverage — Design

Status: **design / roadmap** (no implementation yet). This document is the agreed
shape for adding embedded code coverage; it absorbs the *Code Coverage* item that
previously lived in `realtime_os.md`.

## Goal

Give embedded OS targets (Zephyr first) the **same end-to-end coverage experience
otto already has for Unix** — compile → install → run → collect → `lcov` merge →
HTML report — so NASA's embedded coverage logic can be demonstrated and tested the
way Unix coverage is today (repo1's sample C product against the *veggies* lab).

## Why this is hard

Unix and embedded products are fundamentally incompatible:

| | Unix product (repo1) | Zephyr product |
|---|---|---|
| build | host `gcc --coverage` | Zephyr SDK cross-compile |
| deploy | `scp` a binary | no exec/process model |
| run | run with `GCOV_PREFIX` | runs inside the RTOS image |
| `.gcda` | written to disk | no filesystem to write to |
| transport | scp/sftp/ftp | console only |

NASA's [embedded-gcov](https://github.com/nasa-jpl/embedded-gcov) bridges the gap:
it keeps gcov counters in memory and **dumps them as text over the console**, and
a host-side decoder reconstructs the `.gcda` files. After that, the existing
`lcov` → report pipeline applies unchanged.

## Decisions

- **A new `tests/repo3`** dedicated to embedded coverage. Keeps repo1 (Unix
  coverage) and repo2 (multi-repo + Docker fixture) intact, isolates the heavy
  Zephyr SDK/west toolchain, and mirrors the real world where embedded products
  are separate codebases. (Repurposing repo2 would destroy its multi-repo/Docker
  e2e fixture value; putting the embedded product in repo1 would couple repo1's
  light `make` example to the SDK.)
- **embedded-gcov is a git submodule** at `tests/repo3/third_party/embedded-gcov`.
- **Target architecture is `qemu_cortex_m3`**, not `qemu_x86` — see below.

## The seam: `.gcda` is OS-agnostic

Once a decoded `.gcda` + the build-time `.gcno` + the matching (cross) `gcov`
exist on the host, **everything downstream is already generic and reused as-is**:

- `LcovMerger` — `src/otto/coverage/correlator/merger.py`
- `PathCorrelator` / auto-discovery — `src/otto/coverage/correlator/paths.py`
- `LCOVLoader` — `src/otto/coverage/correlator/lcov_loader.py`
- `CoverageReporter`, `run_coverage_report`, `HtmlRenderer` — `src/otto/coverage/reporter.py`
- `otto cov report` / `otto test --cov-report` — `src/otto/cli/cov.py`
- The per-host toolchain mechanism in `.otto_cov_meta.json`
  (`reporter.py` `read_cov_toolchains`) — already lets each host carry its own
  `gcov`/`lcov`, so a Zephyr cross-`gcov` slots in with no API change.

### Unix flow (today) vs. embedded flow (designed)

```
Unix:     gcc --coverage  → scp binary → run w/ GCOV_PREFIX → .gcda on disk
          → scp fetch (GcdaFetcher) → lcov capture (host gcov) → .info → report

Embedded: build product as an llext w/ --coverage + embedded-gcov (.gcno in build
          dir) → llext load_hex over console → call_fn ops → call_fn cov_dump
          → capture hex over console → decode → reconstruct .gcda on host
          → lcov capture (Zephyr cross gcov) → .info → report   ← SAME pipeline
```

## Product modularity: LLEXT (no permanent firmware changes)

Zephyr's kernel-module analogue is **LLEXT (Linkable Loadable Extensions)**. It
lets the product be installed/uninstalled at runtime *without* permanently
altering the QEMU images:

- LLEXT loads relocatable-ELF "extensions" at runtime, links them against the base
  image's symbol table, invokes their exported functions, and **unloads them when
  done** — install/uninstall, not a re-flash.
- **One-time, product-agnostic enablement:** rebuild the base firmware *once* with
  `CONFIG_LLEXT=y` + `CONFIG_LLEXT_SHELL=y`. That enables the *loader* (a generic
  capability), not the product. This is the only permanent image change.
- **The transport already matches.** The llext shell module exposes `llext
  load_hex` (load an ELF **encoded as hex straight from the console**), `unload`,
  `list`, `list_symbols`, and `call_fn`. otto's embedded transport is *already
  hex-over-console* (`EmbeddedFileTransfer`, `ZephyrFrame`), so install is
  literally "send the extension hex via `llext load_hex`" — no device filesystem
  required.
- **Repo-defined install/uninstall** (the Unix parallel): repo3's install
  instruction = hex-encode the extension ELF + `llext load_hex`; uninstall =
  `llext unload <name>` — direct analogues of repo1's `_install_on_host` /
  `_uninstall_from_host`.

## Target architecture: ARM Cortex-M (`qemu_cortex_m3`)

ARM Cortex-M is Zephyr's first-class, most widely tested architecture — the bulk
of real boards (STM32, Nordic nRF, NXP, Microchip SAM, TI) are Cortex-M, and it
has a dedicated *Arm Cortex-M Developer Guide*. x86 is supported but is mainly a
fast QEMU CI-emulation convenience; its LLEXT support is new (basic, merged
May 2025, with a `pinned_text` limitation flagged in the PR). ARM (with Xtensa) is
among LLEXT's original, well-trodden targets, and embedded-gcov is itself oriented
toward ARM Cortex-class targets. So the coverage bed targets **`qemu_cortex_m3`**
via a new lab instance. The cross-`gcov` becomes the SDK's `arm-zephyr-eabi-gcov`,
carried per-host in `.otto_cov_meta.json`.

Migrating the *existing* x86 embedded bed to Cortex-M is tracked separately in
`embedded_cortex_m_migration.md` so it does not block coverage work.

## The three pieces to build

### 1. Embedded sample product (an LLEXT extension)

- An otto-authored extension mirroring repo1's `math_ops` (add/sub/mul/div/clamp),
  built with `add_llext_target` + `llext_compile_options(-fprofile-arcs
  -ftest-coverage)`; the vendored embedded-gcov runtime is compiled *into* the
  extension. `.gcno` land in the extension's build dir — that path is the report
  step's `source_root`.
- **Exported entry points** invoked via `llext call_fn` (no reliance on
  constructors running at load): one per operation to exercise code paths, plus
  `cov_dump`, which walks the extension's own gcov info and prints the `.gcda`
  content as hex over the console.
- **Lifecycle mirrors Unix:** install (`llext load_hex`) → exercise
  (`call_fn <op>`) → collect (`call_fn cov_dump`) → uninstall (`llext unload`).

### 2. Embedded coverage collector (the one new framework module)

- New `src/otto/coverage/fetcher/embedded.py` with an `EmbeddedGcdaCollector`
  that, per embedded host: opens the console (reusing `EmbeddedHost` session /
  `ZephyrFrame` plumbing), issues `llext call_fn cov_dump`, captures the hex
  stream, and **decodes it to `.gcda` files under `staging_root/<host_id>/`** —
  the same layout `GcdaFetcher` produces, so nothing downstream changes. The
  decoder follows embedded-gcov's documented format (its host-side decode script
  is the reference).
- **Hook point:** `src/otto/coverage/fetcher/remote.py` currently *skips*
  `EmbeddedHost`. Replace that skip with routing to the new collector inside the
  `otto test --cov` collection path (`src/otto/cli/test.py`,
  `_run_coverage`/`_fetch_one_host`).
- **Metadata:** write `.otto_cov_meta.json` with `sut_dir` = the embedded build
  dir (so `.gcno` discovery + path mapping work) and a per-host toolchain whose
  `gcov` points at the Zephyr SDK cross-`gcov`. `discover_toolchain_from_gcno`
  (`src/otto/host/toolchain_discovery.py`) may auto-derive this by `strings`-ing
  the `.gcno`, mirroring the Unix path.

### 3. Embedded coverage test suite (mirrors `TestCoverageProduct`)

- A new `OttoSuite` in repo3 that exercises each operation over the console on the
  embedded host(s) and triggers + captures the dump during `--cov`. Reuse the
  repo1 trick of running one branch (e.g. divide-by-zero, clamp) on a single
  instance so *merged* coverage across instances exceeds any single instance —
  proving cross-host merge works for embedded too. Reference:
  `tests/repo1/tests/test_coverage_product.py`.

### repo3 skeleton

```
tests/repo3/
  .otto/settings.toml         # labs = embedded hosts (reuse tech1 via --lab embedded
                              #   or a dedicated lab_data dir); [coverage] section;
                              #   reuse the zephyr-* os_profiles pattern from repo1
  pylib/repo3_instructions/   # init module + install/uninstall instructions
                              #   (llext load_hex / llext unload)
  product/                    # math_ops analogue built as an llext extension
                              #   (add_llext_target + coverage flags); exports
                              #   the ops + cov_dump entry points
  third_party/embedded-gcov/  # git submodule (nasa-jpl/embedded-gcov)
  tests/test_embedded_coverage.py
```

Wiring to update when implemented: `tests/conftest.py` discovery/backend matrix,
`docs/guide/coverage.md` (embedded section), CI matrix.

## Cross-OS merging (valued — preserved)

repo3 does not sacrifice cross-OS merging. Because the merge/report layer operates
purely on `.gcda`/`.info` (not host type), a single report can stitch Unix and
embedded coverage together:

- `otto cov report <unix_run_dir> <embedded_run_dir>` already accepts multiple run
  dirs; `discover_gcda_dirs` collects every per-host subdir and `CoverageReporter`
  merges them all.
- Per-host toolchains are keyed by host-dir name in `.otto_cov_meta.json`, so a
  Unix host's `gcov` and a Zephyr host's cross-`gcov` coexist in one report.
- Tiers (`--tier`) can separate "unix" vs "embedded" while still rolling up.

**Sequencing:** stand up embedded coverage *standalone* first, then enable the
combined report. One known rough edge to resolve then: `run_coverage_report` reads
a **single** `source_root` from the first meta file, whereas a combined report
spans two build roots (Unix sutDir + Zephyr build dir). This is already tracked as
"Support for multiple build roots" in `coverage_roadmap.md` (PathMapping
auto-discovery).

## Verification (for the eventual implementation)

- **LLEXT feasibility gate (first):** stand up a `qemu_cortex_m3` instance with
  `CONFIG_LLEXT=y` + `CONFIG_LLEXT_SHELL=y` and prove a *coverage-instrumented*
  toy extension can be `llext load_hex`-ed, `call_fn`-ed, dumped, and `unload`-ed
  before building out the full product + collector.
- **Unit (no device):** feed a captured sample console dump to the decoder →
  assert reconstructed `.gcda` bytes; assert the collector lays out
  `cov/<host>/*.gcda`.
- **Integration (live lab):** `otto test --cov <EmbeddedCoverageSuite>
  --lab embedded`, then `otto cov report <run_dir> --report ./report`; confirm the
  instrumented functions are covered and that merging across instances raises
  coverage.

## References

- embedded-gcov: <https://github.com/nasa-jpl/embedded-gcov>
- Zephyr LLEXT: <https://docs.zephyrproject.org/latest/services/llext/index.html>
- LLEXT shell loader sample (`load_hex`/`unload`/`call_fn`):
  <https://docs.zephyrproject.org/latest/samples/subsys/llext/shell_loader/README.html>
- Building LLEXT extensions (`add_llext_target`):
  <https://docs.zephyrproject.org/latest/services/llext/build.html>

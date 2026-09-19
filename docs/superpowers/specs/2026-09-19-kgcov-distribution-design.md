# otto_kgcov distribution and the kernel-module dev-tool kinds

**Status:** approved 2026-09-19.
**Follows:** the kernel-module product kind (`otto.host.kmod_kind`), the
kgcov toolchain proofs and the kgcov compatibility matrix
(`2026-09-18-kgcov-compatibility-matrix-design.md`).

## 1. Goal

A user who installs otto from a wheel can put `otto_kgcov` into their own
build system and their own repo with one command, build it with their own
toolchain against each of their kernels, and have otto know which build
belongs on which host, load it before any coverage-instrumented module
needs it, remove it at cleanup, and refuse a build that does not match the
library interface this otto drives. Minimal configuration: one
`[[dev_tools]]` entry per kernel.

## 2. Today

- The library's sources live only in `docs/examples/kgcov/`. The wheel ships
  everything under `src/otto/` and nothing else, so a wheel user has no copy.
- The demo repo (`tests/repo5`) declares `otto_kgcov` as its *first*
  `[[products]]` kmod entry and relies on declaration order for the load
  order; nothing checks that the library is resident before a
  `coverage = "module"` consumer loads, and the library counts as a product
  under test in `is_installed` and `status`, which it is not.
- `[[dev_tools]]` entries share the `Product` shape with a different
  lifecycle (installed by `install-tools` before any product, removed by
  `cleanup` after every product, never part of the product `is_installed`
  answer) and bind to registered kinds. Only a `shell` kind exists.
- Host-global `toolchain.tools` in lab data only place files; they have no
  load/unload verbs and are not per-repo. They are not the seam for this.

## 3. Decisions and rules

1. **The user's build system builds the `.ko`, always.** otto ships sources
   and never runs kbuild. This keeps the one hard rule in one place: the
   library and its consumers are built by the same compiler family, and for
   gcc by the same major.
2. **The library is vendored and committed** in the user's repo, exported by
   otto. Build machines need no otto, and version control is the record of
   what a re-export changed.
3. **The `.ko` is a dev tool**, declared by a `[[dev_tools]]` entry of kind
   `kgcov`, one per kernel, mapped to hosts by `match`. A host matched by two
   kgcov entries is a bootstrap error naming both.
4. **A `coverage = "module"` kmod product loads the library itself** at
   `install` when it is not resident; `cleanup` removes it after the
   products, in the order it already uses.
5. **Strict on the interface, advisory on the sources.** A `.ko` whose
   interface number is not the installed otto's is a bootstrap error; a
   vendored copy that differs from the installed otto's is reported and
   blocks nothing.
6. **Two commands and one init area** share one implementation:
   `otto cov kgcov export|check`, and `otto init --kgcov` for the full wiring.
7. **A `kmod` dev-tool kind is the base**; `kgcov` is its coverage-aware
   subtype. Other kernel-module tools use the base as is.
8. **Kernel differences are customised through one override header** that
   the check ignores, so a sanctioned change never reads as drift.
   Confirming a customised library is a follow-up (§7).

## 4. Design

### 4.1 The shipped library

The sources move from `docs/examples/kgcov/` to the package directory
`src/otto/kgcov/` (library `.c`/`.h`, `Kbuild`, `Makefile`, `build.sh`,
`consumer.mk`, `kgcov.h`, `README.md`). Every docs literalinclude that
named the old path names the new one; the matrix and toolchain fixtures
build from the new one. Nothing else in the library changes except:

- **The interface number.** `kgcov.h` gains `#define KGCOV_INTERFACE 1`.
  It changes only when the debugfs layout (`/sys/kernel/debug/otto_kgcov/
  <module>/{dump,reset}`), the `gcov_dir` module parameter, or the consumer
  macros (`KGCOV_DECLARE`, `KGCOV_INIT`, `KGCOV_EXIT`, the sentinels)
  change. `otto.kgcov.INTERFACE` is the same number on the Python side, and
  a guard holds the two equal.
- **The version header.** `kgcov_version.h`, one line written by export
  (`#define KGCOV_OTTO_VERSION "1.6.0"`), and the library's
  `MODULE_VERSION()` is `KGCOV_OTTO_VERSION "+kgcov" <KGCOV_INTERFACE>`, for
  example `1.6.0+kgcov1`. So `modinfo -F version otto_kgcov.ko` says which
  otto exported the sources and which interface they implement, beside
  what a `.ko` already carries: `vermagic` (the kernel release) and
  `.comment` (the compiler). The package directory carries the header for
  otto's own version so the fixture builds report it too. This is the
  whole stamp; there is no separate stamp file.
- **The override header.** `kgcov_gcov.h` includes `kgcov_local.h` when it
  exists (a `Kbuild` `-include` guarded by `$(wildcard …)`). That file is the
  user's, never exported, ignored by the check: it may redefine the small set of
  kernel-facing names the library isolates for it (the allocation and free
  helpers, the lock type and its init/lock/unlock, the debugfs create and
  remove helpers). The library's defaults stay in `kgcov_gcov.h` under
  `#ifndef` so an override replaces exactly one name at a time.

### 4.2 The exported tree and the check

`otto.kgcov.export_tree(dest) -> ExportResult` writes every shipped file
into the directory, `kgcov_version.h` included, overwriting what is there
and reporting which files changed. It never refuses: the copy is committed,
so the user's own diff is the review of what a re-export changed.

`otto.kgcov.check_tree(dir) -> CheckResult` compares the directory with the
shipped tree byte for byte, ignoring `kgcov_local.h` (reported as "local
override present"):

| State | Meaning | `check` exit |
|---|---|---|
| current | every shipped file present and identical | 0 |
| differs | some file differs or is missing; the message names them and the otto version the directory's `kgcov_version.h` says it came from | 1 |
| absent | no `kgcov.h` there | 2 |

The same two functions serve the commands and the init area; there is no
other state to keep.

### 4.3 The commands and the init area

`otto cov kgcov export <dir>` and `otto cov kgcov check <dir>` are thin
over §4.2; `check` prints one line and exits with the code above, so CI can
run it.

`otto init --kgcov [<dir>]` (default `third_party/otto_kgcov`) is one more
area beside settings, schemas, lab, tests and instructions, with the same
detect / validate / scaffold contract:

- **detect**: a `[[dev_tools]]` entry of kind `kgcov` in
  `.otto/settings.toml`, or a `kgcov.h` at the default path.
- **validate** (`otto init --validate`): `check_tree` on every directory a
  kgcov entry's `source` names, reported as advisory drift like the schemas
  area (never a failure).
- **scaffold**: `export_tree` into the directory (the one place the area
  does overwrite, since those files are otto's); append a commented
  `[[dev_tools]]` entry (below) with the artifact and `match` as
  placeholders, only if no kgcov entry exists; write a consumer starter
  beside the export (`consumer-starter/kgcov_begin.c`,
  `kgcov_end.c`, a `Kbuild.example` showing `KGCOV := $(src)/../otto_kgcov`
  and `include $(KGCOV)/consumer.mk`, and a README pointing at the
  kernel-modules docs page), never overwriting a starter file that exists.

### 4.4 The `kmod` and `kgcov` dev-tool kinds

Both register in `otto.host.dev_tool`'s `DEV_TOOL_KINDS`; the code lives in
`otto.host.kmod_tool_kind`.

**`kmod`** (base), keys: `artifact` (a `.ko`, anchored to the repo,
required), `module_name` (default: the artifact stem with `-` → `_`),
`params` (insmod parameters, placeholders expanded as the product kind
does), `match`. Verbs: `stage` no-op, `install` = `host.load`, `uninstall` =
`host.unload`, `is_installed` = name in `host.lsmod`. A host without the
module verbs is refused at bootstrap with the product kind's wording.

**`kgcov`** (subtype), keys: the base's plus optional `source` (the vendored
directory, repo-anchored). Fixed: `module_name = "otto_kgcov"`; `params`
must not set `gcov_dir`. Bootstrap checks, before any host is contacted:

1. The artifact's `.modinfo` carries `version=<x>+kgcov<n>` with `n ==
   otto.kgcov.INTERFACE`; otherwise an error naming the tool, the artifact,
   the version it carries, and `otto cov kgcov export <source or dir>` as
   the remedy. The strings are read from the file's `.modinfo` section in
   Python (NUL-separated `key=value`), so no host tool is needed and a
   foreign-ISA `.ko` reads the same.
2. No host matches two kgcov entries (error naming both entries and the
   host).

The demo repo switches to this shape:

```toml
[[dev_tools]]
name = "kgcov-6.8"
kind = "kgcov"
artifact = "build/lib/otto_kgcov.ko"
source = "third_party/otto_kgcov"
match = { id = "test[12]" }
```

### 4.5 The consumer's dependency

`otto.host.kmod_kind`:

- **Bootstrap**: after the declared dev tools are attached, a
  `coverage = "module"` product on a host with no kgcov dev tool is an error
  naming the product, the host and the missing entry.
- **`install`**: resolve the host's kgcov tool; if its `is_installed` is
  false, call its `install` first (its failure is reported as the tool's,
  with the kernel log line for a `vermagic` mismatch); then `insmod` the
  consumer with `gcov_dir` as today. A consumer that fails after the library
  loaded is reported as the consumer's failure with the library named as
  resident.
- **`uninstall`**: the consumer only. **`cleanup`**: unchanged order,
  products then dev tools, which removes the library after its consumers.
- **`prepare_coverage` / `reset_coverage`**: a missing debugfs file names the
  kgcov tool that owns it and whether it is resident.
- **Dry run**: every new verb declines like the existing kmod verbs
  (`NotRun` propagates, never mapped to an error).

### 4.6 Documentation

- `docs/cli/cov/instrumenting/kernel-modules.md`: a "Getting the library"
  section ahead of "Instrumenting a module" (init area, bare export, check
  in CI, the override header), the build section pointing at the vendored
  copy, and the dev-tool shape replacing the first-product shape.
- `docs/configuration/declared-products-tools.md`: the `kmod` and `kgcov`
  dev-tool kinds beside `shell`, their keys, the two bootstrap rules.
- The `otto cov` and `otto init` CLI pages: the new commands and area.
- `docs/cli/run/defaults.md`: one sentence on the on-demand library load.

One home per topic; every other mention links.

### 4.7 Testing

- **Unit**: `export_tree`/`check_tree` over every state in §4.2 including
  a re-export over an edited copy and `kgcov_local.h`; the `.modinfo` reader
  against a real fixture `.ko` and a hand-built ELF; the init area's
  detect/validate/scaffold in the existing area harness; both kinds' param
  validation; the three bootstrap rules. Every guard shown red first.
- **Host-level** (fake-host doubles): `install` loads the library only when
  absent; `cleanup` order; dry-run declines on every new verb.
- **Live bed**: `tests/repo5` through the dev tool in the existing kmod e2e
  (the on-demand load end to end); the kgcov matrix lane unchanged in shape,
  building from the package directory.
- **Guards**: `otto.kgcov.INTERFACE == KGCOV_INTERFACE` in `kgcov.h`; the
  package directory's own `kgcov_version.h` names otto's version; the demo
  repo's vendored copy (`tests/repo5/third_party/otto_kgcov/`) reads as
  `current`, so the two in-tree copies cannot drift apart; the docs
  literalincludes resolve after the move; `docs/examples/kgcov/` is gone.

## 5. Files

- Move: `docs/examples/kgcov/*` → `src/otto/kgcov/`
- Create: `src/otto/kgcov/__init__.py` (INTERFACE, export_tree, check_tree,
  the `.modinfo` reader), `src/otto/kgcov/kgcov_version.h`,
  `src/otto/host/kmod_tool_kind.py`, `tests/unit/test_kgcov_export.py`,
  `tests/unit/test_kmod_tool_kind.py`, `tests/repo5/third_party/otto_kgcov/`
  (the demo's vendored copy)
- Modify: `src/otto/host/kmod_kind.py`, `src/otto/cli/cov.py`,
  `src/otto/cli/init.py`, `src/otto/cli/init_templates.py`,
  `src/otto/kgcov/kgcov_gcov.h`, `Kbuild`, `tests/repo5/.otto/settings.toml`,
  `tests/repo5/kmod/build.sh`, `tests/e2e/cov/_repo5_build.py`, the four
  docs pages in §4.6, `docs/conf.py` if the literalinclude root moves.

## 6. Out of scope

- otto building the library (rule 1).
- A build-time (non-vendored) export mode.
- Any change to coverage capture, fetch, merge or report.

## 7. Follow-ups (issues, filed once this spec is committed)

- **Confirming a customised library**: a way to run the matrix's kgcov
  contracts against a user's build (their kernel, their compiler, their
  `kgcov_local.h`) and report the same measured-ok/measured-broken verdicts,
  so a compatibility change can be proven rather than assumed.
- Other `kmod` dev-tool subtypes as they arise.

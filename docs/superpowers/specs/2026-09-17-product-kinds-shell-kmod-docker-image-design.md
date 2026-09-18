# Product kinds: `shell`, `kmod`, `docker_image` — design

> Captured 2026-09-17 from a brainstorm with Chris, after the per-product
> coverage branch (spec `2026-09-16-per-product-coverage-and-logs-design.md`)
> landed on main. Approved section by section in that conversation.

## 1. Context and motivation

The per-product coverage work gave every product a `cov_dir`, a place in the
run tree, and an instrumentation verdict, and it modelled an embedded LLEXT
extension as a product kind (`llext`). That left the built-in `file` kind
looking like a peer of `llext` when it is not: `llext`'s verbs are driven by
the host's binary loader, while `file`'s verbs are three shell commands the
user writes. The kind's name described the artifact's shape, which is the
wrong axis, and it invited the question "why not `elf`, `python`, …".

The rule this spec establishes: **a kind is named for what drives its verbs
(install / check / uninstall / coverage dump), never for the artifact's
format.** A kind exists when otto brings runtime knowledge to those verbs.
Under that rule the shell-driven kind is renamed `shell`, and two kinds with
real drivers are added: `kmod` (the Linux kernel's module loader) and
`docker_image` (a docker daemon). Coverage retrieval for kernel modules has
two methods, selected per product, and one of them needs a shared runtime
library shipped as a companion module.

## 2. Goals and non-goals

**Goals**

- Rename `file` → `shell` as a hard cutover (settings key, registry, class,
  scaffold, fixtures, docs); a `kind = "file"` entry fails validation with a
  message naming the rename.
- Two `Product` hooks, `prepare_coverage(host)` and `reset_coverage(host)`,
  that the fetcher and the clean paths call, so a kind can materialise or
  zero its counters its own way (§4).
- `kmod` kind: load / check / unload through `UnixHost.load` / `lsmod` /
  `unload`, with a `coverage` method of `none`, `module`, or `kernel` (§5).
- `otto_kgcov`: a GPL companion kernel module that instrumented modules link
  against, dumping `.gcda` files into the product's `cov_dir` with merge
  semantics, on demand and at unregister (§6).
- A worked, non-trivial demo module (`otto_kmod_demo`) with several
  translation units and steerable branches, used by the docs and the fixture
  repo, with an e2e that asserts exact line and branch hits, including
  deliberately uncovered branches and the exit routine (§6.3, §9).
- `docker_image` kind: run an image (tarball or reference, optional pull)
  with `cov_dir` bind-mounted so the ordinary fetcher collects from it (§7).
- Instrumentation scan: an archive artifact reads `unknown`, not `no` (§8).
- Docs: the getting-started coverage page gains the kernel-module walkthrough
  (demo module as its example) and a docker-image section; the reference
  pages gain the two kinds and the naming rule (§10).

**Non-goals**

- Decision/condition coverage (`-fcondition-coverage` needs gcc 14; the
  toolchain is 13.3). The store's reserved `decision` slot stays unused.
- A first-class `CoverageCollector` object model (Approach 2 in the
  brainstorm). The two hooks are the seam; refactoring `llext`'s console
  collector onto them is a later mechanical change.
- A `docker_image` product that becomes a `DockerContainerHost` (Approach B).
  Testing *inside* a container is the compose use-case path.
- Rebuilding bed kernels with `CONFIG_GCOV_KERNEL`. The `kernel` method is
  proven against a fake; the `module` method is proven live.
- Kinds for `package`, `python`, `firmware`, … — named here only to show the
  rule generalises.

## 3. The `shell` kind (rename)

`src/otto/host/file_kind.py` → `shell_kind.py`; `FileProduct` →
`ShellProduct`; `PRODUCT_KINDS`/`DEV_TOOL_KINDS` register `"shell"`. `LlextProduct`
keeps subclassing the artifact/stage half but not the shell verbs (today's
inheritance already overrides all three). Params are unchanged: `artifact`,
`dest_dir`, `install`, `check`, `uninstall`, `cov_dir`, `debug_log_globs`,
`instrumented`. Validation of `kind = "file"` raises
`ValueError("kind 'file' was renamed 'shell' (its verbs are the shell commands
you write); update the entry")` at lab ingest (`KindRegistry.build`, the same
site that refuses an unknown kind — settings parsing deliberately knows no
kind names), so the failure names the fix. No alias, no deprecation window
(project rule: hard cutovers).

Everything that spelled `file` follows: `init_templates.py`, `tests/repo1`
and `tests/repo3` settings, every doc page, the API pages, the
`declared-products-tools.md` table, and the getting-started example. The docs
state the naming rule once, in `declared-products-tools.md`, and list the
kinds in a table: what drives the verbs, how coverage is collected.

## 4. The coverage hooks on `Product`

```python
class Product(ABC):
    async def prepare_coverage(self, host: "Host") -> Result:
        """Materialise this product's counters under cov_dir. Default: no-op success."""
    async def reset_coverage(self, host: "Host") -> Result:
        """Zero this product's counters. Default: find <cov_dir> -name '*.gcda' -delete."""
```

- `GcdaFetcher._fetch_one_product` awaits `prepare_coverage(host)` before its
  `find`; a non-success result is logged (`host:product: prepare_coverage
  failed: <msg>`) and the product is skipped for the run, the same path a
  failed fetch takes (no leaf is created).
- The pre-run clean (`--cov-clean`) and `otto cov clean` call
  `reset_coverage` per instrumented product instead of the inline delete;
  the default body is the delete they run today, so `shell`, `docker_image`
  and `llext` keep their behaviour byte for byte.
- `llext` overrides neither; `EmbeddedGcdaCollector` is untouched.
- Both hooks are `@cli_exposed`-free: they are pipeline seams, not verbs.

## 5. The `kmod` kind

Registered for products only (a kernel module is never a dev tool). The
builder refuses a host that lacks `load`, `unload` and `lsmod` (today only
`UnixHost`), naming the host.

| Param | Meaning |
|---|---|
| `artifact` (required) | the `.ko`, staged like any file |
| `module_name` | defaults to the artifact stem with `-` → `_` (what `/proc/modules` shows) |
| `params` | string appended to `insmod` (`debug=1 queue=8`); `{cov_dir}`/`{name}` placeholders |
| `coverage` | `"none"` (default), `"module"`, `"kernel"` |
| `gcov_path` | required with `coverage = "kernel"`: the module's subtree of the kernel's gcov tree as a full path, `/sys/kernel/debug/gcov/<absolute build dir>` (the kernel mirrors each object's absolute path there); validation refuses a value outside `/sys/kernel/debug/gcov/` |
| `cov_dir`, `instrumented`, `debug_log_globs` | as every product |

Verbs:

- `install`: stage, then `host.load(dest, name=module_name, params=<params>)`.
  With `coverage = "module"` otto appends `gcov_dir=<cov_dir>` to the params
  so the library knows where to write; a user-supplied `gcov_dir=` in
  `params` is a validation error (one owner).
- `is_installed`: `module_name` present in `host.lsmod()`.
- `uninstall`: `host.unload(module_name)` (idempotent, as today).

Hooks by method:

| `coverage` | `prepare_coverage` | `reset_coverage` |
|---|---|---|
| `none` | default | default |
| `module` | module loaded: write `1` to `/sys/kernel/debug/otto_kgcov/<module_name>/dump` (sudo). Module not loaded (a teardown unloaded it): success without touching anything — the exit dump already wrote the files. | module loaded: write `1` to `…/reset`; then the default delete, under sudo |
| `kernel` | `cp -r` the `gcov_path` subtree's `.gcda` entries into `cov_dir` (sudo), keeping each entry's path relative to `/sys/kernel/debug/gcov/` (debugfs entries report size 0, so `scp` cannot fetch them; this copy is the materialise step) | write `1` to each `.gcda` entry under `gcov_path` — never the global `/sys/kernel/debug/gcov/reset`, which zeroes every module's counters; then the default delete, under sudo |

Every method lands files in the layout `GCOV_PREFIX=<cov_dir>` would have
produced with no strip — `<cov_dir>/<absolute object path>.gcda` — so the
fetcher, the run tree and the report treat a kernel module exactly like a
user-space product. The deletes run under sudo because the kernel wrote the
files as root.

A missing debugfs path when the module is loaded (kernel without
`CONFIG_GCOV_KERNEL`, or the library not loaded / the consumer built without
the snippet) fails `prepare_coverage` with the path in the message; the run
proceeds without that product, per §4.

**`UnixHost.load` gains `params: str = ""`** (appended verbatim after the
module path; `insmod` takes `key=value` tokens). This is a host verb change,
so the CLI synthesizer's per-class parser picks up the new option; the
`Host` protocol is unchanged (only `UnixHost` has `load`).

**Exit-routine coverage.** gcov counts arcs as they are entered, so a dump
taken inside `module_exit` already holds everything the exit routine
executed before the dump call. For `coverage = "module"` the library dumps at
`kgcov_unregister`, which the consumer snippet places last in the exit
routine (§6); for `coverage = "kernel"` the kernel keeps a module's data
after unload (`gcov_persist=1`, the default). In both cases exit coverage
reaches a run's report when the unload precedes the post-run fetch, which is
what a suite teardown does; `check` reinstalls on the next run.

**Detection.** The `.ko` is scanned like any artifact (`-fprofile-arcs`
leaves the markers; a `coverage = "kernel"` module carries them too).

## 6. `otto_kgcov`: the runtime library (companion module)

A "library" in kernel space is a loadable module that exports symbols; every
instrumented module links against the one copy. This is what "shared, never
reimplemented" means here.

### 6.1 Why a runtime is needed at all

On the bed's kernel series (Ubuntu 6.8 generic, verified on the dev VM):
`CONFIG_GCOV_KERNEL` is off (no in-kernel gcov, no debugfs tree) and
`CONFIG_CONSTRUCTORS` is off (the kernel never runs a module's constructors,
so plain `-fprofile-arcs` registration does nothing). The way through:
consumers compile with `-fprofile-info-section`, which records each
translation unit's `gcov_info` pointer in a `.gcov_info` section instead of a
constructor, and a runtime walks that section.

### 6.2 What the library is

Shipped under `docs/examples/kgcov/` (documented, tested; the fixture repo
points at it rather than copying it):

- `kgcov.c`, `kgcov.h`, `Kbuild` → `otto_kgcov.ko`, `MODULE_LICENSE("GPL")`.
- Exports, as empty functions, the same set the in-kernel gcov
  (`kernel/gcov/base.c`) exports so any instrumented object links:
  `__gcov_init`, `__gcov_exit`, `__gcov_merge_add`, `__gcov_merge_single`,
  `__gcov_merge_delta`, `__gcov_merge_ior`, `__gcov_merge_time_profile`,
  `__gcov_merge_icall_topn` (the kernel's legacy spelling) and
  `__gcov_merge_topn` (gcc 13's), plus `__gcov_flush` (plain
  `-fprofile-arcs` objects reference only `__gcov_merge_add`; with
  `-fprofile-info-section` nothing calls `__gcov_init`). Merging is the
  library's own addition, not these stubs.
  And the API:

  ```c
  int  kgcov_register(struct module *mod, const struct gcov_info *const *begin,
                      const struct gcov_info *const *end, const char *dir);
  void kgcov_unregister(struct module *mod);
  ```

- On register: remember the section bounds and `dir`, create
  `/sys/kernel/debug/otto_kgcov/<module>/dump` and `/reset`.
- Per registered module the library keeps an **accumulator**: a private
  copy of each object's `gcov_info` (the kernel's `gcov_info_dup`, zeroed at
  register). `dump` adds the live counters into the accumulator
  (`gcov_info_add`), zeroes the live counters (`gcov_info_reset`), and
  writes the accumulator to `<dir>/<absolute object path>.gcda`,
  overwriting the file. The file therefore always holds the module's total
  since register (or since the last `reset`), and consecutive dumps never
  double count: a runtime dump followed by the exit dump accumulates within
  a run without the library ever parsing a `.gcda`. Files are written with
  kernel file I/O (`filp_open`/`kernel_write`, mode 0644); the intermediate
  directories are created with `kern_path_create`/`vfs_mkdir` (mode 0755).
  The serialiser is the kernel's GPL `kernel/gcov/gcc_4_7.c`
  `convert_to_gcda`, vendored together with its `gcov_info_*` helpers; it
  reads the gcov format version from each `gcov_info`, so the toolchain that
  built the consumer is what the file claims.
- `reset`: zero the live counters and the accumulator (the files on disk
  are otto's to delete; §5).
- `kgcov_unregister`: dump once more, free the accumulator, remove the
  debugfs entries.
- `consumer.mk`: the Kbuild fragment a consumer includes. It defines
  `KGCOV_CFLAGS` (`-fprofile-arcs -ftest-coverage -fprofile-info-section`)
  and adds the library's include path. The consumer's own `Kbuild` applies
  `KGCOV_CFLAGS` to its instrumented objects and lists `kgcov_begin.o` first
  and `kgcov_end.o` last in its object list (the two sentinels that bound the
  `.gcov_info` section — the section name is not a C identifier, so the
  linker synthesises no `__start_`/`__stop_` symbols for it; `ld -r` keeps
  input order), and its `Makefile` passes `KBUILD_EXTRA_SYMBOLS` naming the
  library's `Module.symvers` (required under `CONFIG_MODVERSIONS`). A
  fragment cannot order another module's object list, so those two parts
  stay in the consumer; the demo module is the worked example.
- `kgcov.h` provides the three macros a consumer writes: `KGCOV_DECLARE()`
  at file scope (declares the `gcov_dir` charp module parameter, read-only
  in sysfs), `KGCOV_INIT()` first in the init routine (registers with the
  sentinels and `gcov_dir`; a register failure fails the init with the
  reason), `KGCOV_EXIT()` last in the exit routine.

### 6.3 The worked example: `otto_kmod_demo`

`tests/repo5/kmod/demo/` — the fixture repo owns its product's sources, as
`tests/repo1` does, because a capture anchors every measured file to a
committed blob under the SUT repo (`build_capture` skips anything else);
the module is built in place there and the docs `{literalinclude}` it from
that path. Three translation units (`demo_main.c`: init,
exit, the debugfs control file; `demo_parse.c`: command parsing;
`demo_policy.c`: a bounded queue with three eviction policies), about 200
lines. Writing to `/sys/kernel/debug/otto_kmod_demo/ctl` drives it:
`enqueue N`, `drain`, `policy fifo|lifo|drop-oldest`, `limit N`; reading
returns counters and the last error. Control flow on purpose: a `switch` over
commands, bounds and parse errors with early returns, a loop with a
conditional break, the three-way policy branch, and one defensive branch
that is unreachable by design. The exit routine drains the queue and frees
it (real cleanup code, so exit coverage is meaningful) and calls
`KGCOV_EXIT()` last.

## 7. The `docker_image` kind

Registered for products only. The builder accepts any host that can `run`;
`install` checks `docker` is on the host's `PATH` and fails loud naming the
host when it is not (a container host without a daemon fails here, not at
declaration).

| Param | Meaning |
|---|---|
| `image` (required) | a reference (`registry/name:tag`) or the path of a `docker save` tarball |
| `pull` | `false` (default): a reference must already be present (`docker image inspect`, fail loud with the name); `true`: `docker pull` first |
| `run_args` | extra `docker run` arguments; `{cov_dir}`/`{name}` placeholders |
| `container_name` | defaults to the product name |
| `cov_dir`, `instrumented`, `debug_log_globs` | as every product |

- `install`: a path is staged and `docker load -i`ed; a reference is pulled
  or verified. Then `docker run -d --name <container_name> -v
  <cov_dir>:<cov_dir> <run_args> <image>`. The bind mount means an
  instrumented binary inside the container (`GCOV_PREFIX=<cov_dir>`) writes
  its counters onto the daemon host, where the ordinary fetcher and the
  default hooks already work — no collector, no host class.
- `is_installed`: `docker inspect -f '{{.State.Running}}' <container_name>`
  is `true`.
- `uninstall`: `docker rm -f <container_name>`; `docker rmi` only for an
  image otto loaded from a tarball (a pulled or pre-existing reference stays
  in the daemon's cache).
- `reset_coverage`: the default delete, under sudo (the container usually
  writes as root, so the lab user cannot remove the files).
- Product logs: `debug_log_globs` on the daemon host, plus `docker logs
  <container_name>` captured to `logs/<host>/<product>/debug/container.log`.
- Detection: a tarball is scanned (a `docker save` tar holds uncompressed
  layer tars, so markers are usually visible; a compressed one reads
  `unknown` per §8); a reference cannot be scanned and is `unknown` unless
  `instrumented` is set.

## 8. Instrumentation scan: archives read `unknown`

`scan_for_instrumentation` returns `None` for a regular file whose name ends
in `.tar`, `.tar.gz`, `.tgz`, `.tar.xz`, `.tar.bz2`, `.zip`, `.gz`, `.xz`,
`.bz2`, `.zst`, before reading it. Today a compressed tarball of instrumented
binaries reads `no` and is silently skipped; `unknown` shows the remedy
caption (`instrumented = true`) and matches what the docs already claim.

## 9. Testing

**Unit.** Kind builders (params, refusals, placeholder substitution, the
`file`-renamed message); both hooks' defaults and the fetcher/clean call
sites; `kmod` hooks per method against a recording host (the `kernel`
method's debugfs copy and per-entry reset over a fake tree, including data
persisted after unload); `docker_image` verbs against a recording host
(load vs pull vs present, bind mount in the `run` line, `rmi` only after
load); the archive rule in the scan; `UnixHost.load(params=)`.

**Bed (live, sequential).** Modules are built on the dev VM and staged to
the bed, so the prerequisite is the headers package for the *bed's* kernel
release installed on the dev VM; nothing is installed on the bed. Verified
2026-09-17 with otto's lab credentials: `test1`, `test2` and `test3` all run
`6.8.0-86-generic` on `aarch64` — the dev VM's own kernel — with
passwordless sudo, no compiler, `CONFIG_GCOV_KERNEL` unset,
`CONFIG_MODVERSIONS=y` and `CONFIG_DEBUG_FS=y`. The archive no longer
carries that ABI's headers, so `linux-headers-6.8.0-86` and
`linux-headers-6.8.0-86-generic` (6.8.0-86.87) were fetched from
Launchpad's librarian and installed on the dev VM the same day
(`/lib/modules/6.8.0-86-generic/build` is complete, `Module.symvers`
included), and a probe module built with the gcov flags against them
carries the bed's exact `vermagic`. A `build.sh` under
`docs/examples/kgcov/` builds the library out of tree into a build dir;
`tests/repo5/build.sh` runs it into `tests/repo5/build/lib/` (git-ignored)
and builds the demo in place under `tests/repo5/kmod/demo/`, so the
`.gcno` files sit beside committed sources inside the SUT repo (the same
shape as repo3's `build.sh` for the LLEXT product); the e2e runs it when
the artifacts are missing, and `build.sh` compares each `.ko`'s
`vermagic` with the target release before declaring success. A new fixture repo `tests/repo5`
(`tests/repo4` is the installable-sample fixture) on `--lab unix`, the
`kmod` products matched to `test1`/`test2` and the `docker_image` product
matched to `test3`:

- `[[products]]`: `otto_kgcov` (`kind = "kmod"`, `coverage = "none"`, declared
  first, `instrumented = false` because its `.ko` carries `__gcov_` strings
  the scan would misread) and `otto_kmod_demo` (`kind = "kmod"`,
  `coverage = "module"`), with repo-relative artifacts
  `build/lib/otto_kgcov.ko` and `kmod/demo/otto_kmod_demo.ko`.
- A suite that drives the demo down chosen paths on each host (different
  command mixes per host), writes `dump` once mid-suite on one host (so that
  host's file holds a runtime dump merged with the exit dump), uninstalls
  in teardown, and asserts nothing itself beyond the verbs.
- e2e: `otto test` auto-enables retrieval for the demo (not for the library,
  which scans `no`); the capture holds one `.gcda` per translation unit;
  exact line and branch hits per object, the deliberately uncovered branches
  (one policy, one error path) reported uncovered, the exit routine's lines
  hit; on the mid-suite-dump host the counters equal the sum of both dumps
  (merge semantics), on the other host the exit dump alone.
- `docker_image`: test3 already runs a daemon (the compose lane) and can
  pull. The dev VM also runs a daemon, so the image is built there: the
  `tests/repo1/product` sources compiled statically with `--coverage` on
  the dev VM, copied into an `alpine:3.20` image, and `docker save`d to a
  git-ignored tarball under `tests/repo5/docker/` by the e2e's build fixture.
  Declared as a `docker_image` product with `run_args` that launch it with
  `GCOV_PREFIX={cov_dir}`; the e2e asserts `capture.json` under
  `cov/test3/<product>/` and the bind-mounted counters. The reference form
  is proven with the same image, already loaded, as a second product with
  `pull = false`.

**Gates.** Per task: targeted selections + the invariant suites (API
snapshot, import budget, error taxonomy) + `make typecheck-python`; whole
branch: lint, typecheck, coverage (all tiers), docs, gate-fresh.

## 10. Docs

- `docs/getting-started/coverage.md`: the user-space walkthrough stays; new
  sections "A kernel module" (build the demo against the library, declare
  the two `kmod` products, run, read the report; the exit-coverage rule in
  one paragraph) and "A container image" (tarball form; `pull` for a
  reference). `docs/examples/kgcov/` is the source of every fragment.
- `docs/guide/configuration/declared-products-tools.md`: the naming rule,
  the kinds table (`shell`, `llext`, `kmod`, `docker_image`; verbs driver;
  coverage method), the `kmod` and `docker_image` param tables, the
  `coverage` methods with their prerequisites (`CONFIG_GCOV_KERNEL` +
  `gcov_persist` for `kernel`; the library for `module`).
- `docs/guide/cli/cov/instrumenting/`: a `kernel-modules.md` page (the
  library, the consumer snippet, `-fprofile-info-section`, why constructors
  do not run, the two methods) and a `containers.md` page (bind-mounted
  `cov_dir`); `gcc.md` links both.
- `docs/guide/cli/host/…`: `load` gains `params`.
- Architecture: `docs/architecture/subsystems/coverage/index.md` gains the
  two hooks; a `kinds.md` under host describes the driver rule.
- CHANGELOG is generated from commit subjects; the squash subject carries
  `!` (the `file` → `shell` rename and the `load` signature).

## 11. Breaking changes

- `kind = "file"` → `kind = "shell"` (validation error names the rename).
- `FileProduct` → `ShellProduct` (import path `otto.host.shell_kind`).
- `UnixHost.load(file, name=None, params="")` — additive, but the CLI verb
  gains an option.
- `Product` gains two hooks with defaults — additive for subclasses.

## 12. Delivery slices

One branch, three slices in dependency order, each squashed to its own
commit and gated green on its own (the project's one-commit-per-logical-item
rule), so the rename can land before the kernel work is proven on the bed:

1. **Rename and seams** — §3, §4, §8, `UnixHost.load(params=)`, the docs
   naming rule and kinds table.
2. **Kernel modules** — §5, §6, §6.3, `tests/repo5`, the bed e2e, the
   kernel-module docs pages and the getting-started walkthrough.
3. **Docker images** — §7, the test3 e2e, the container docs pages and the
   getting-started section.

## 13. Open questions settled during the brainstorm

- Rename target: `shell` (verbs are shell commands), not `elf`/`artifact`.
- Docker: product on the daemon host (A), both artifact forms, `pull` flag
  default off (labs are air-gapped by policy).
- Kernel coverage: two methods on one kind; the library is a companion
  module (runtime), not a source library; the worked example is the
  non-trivial `otto_kmod_demo`, not a hello module.
- Hooks (Approach 1) over collector objects (Approach 2).
- Decision coverage deferred (gcc 14).

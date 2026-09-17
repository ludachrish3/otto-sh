# Per-product coverage and logs: `cov_dir`, one run tree, a product filter, and instrumentation detection

**Status:** approved design, not yet planned.
**Breaks:** `[coverage].gcda_remote_dir` and `[coverage.embedded].extension`
are removed; the one-level `cov/<host>/` tree is no longer read; the logs
tree gains a product level; `CAPTURE_FORMAT_VERSION` 2 → 3;
`STORE_FORMAT_VERSION` 7 → 8; `OTTO_COV_DATA_FORMAT` 2 → 3. One
`feat(cov)!` subject carries all of it.
**Source:** `todo/coverage-improvements.md` plus the brainstorm of
2026-09-16 (rulings recorded in §1).

## 1. Motivation and provenance

otto keys every coverage and log artifact by host. A host runs several
products, and their counters and logs fold into one pile. Verified against
source on 2026-09-16:

1. The remote `.gcda` location is one repo-wide string,
   `[coverage].gcda_remote_dir`, read from the first repo that declares a
   `[coverage]` table (`src/otto/coverage/config.py:33-44`) and applied to
   every matched host (`src/otto/coverage/collect.py:190`). Nothing on a
   `Product` names a coverage directory; `cov_dir` appears nowhere in
   `src/`.
2. The fetcher stages into `cov/<host.id>/`
   (`src/otto/coverage/fetcher/remote.py:89`,
   `src/otto/coverage/fetcher/embedded.py:124`); `Capture` and `RunRecord`
   carry `board`/`host` but no product
   (`src/otto/coverage/capture/model.py:44-60`,
   `src/otto/coverage/store/model.py:222-264`). The report exposes four
   filter dimensions (tier, context, ticket, hide-asserted) and no product.
3. Product logs from every product on a host land in one shared
   `logs/<host-id>/product/` (`src/otto/host/host.py:2002`); two products
   writing `app.log` collide silently. That path is spelled in four places
   (`host.py:2002`, `host.py:2079`, `src/otto/project/actions.py:470`, the
   protocol docstring at `host.py:951`). No layout module exists; the logs
   and coverage trees mirror each other only by prose convention
   (`host.py:1973-1979`).
4. A unit tier folds all its `harvest_dirs` into one run record
   (`src/otto/coverage/reporter.py:628,651-655`), so two unit executables
   built from one tree with different `#define`s cannot be told apart.
5. The fetcher skips `DockerContainerHost` on the assumption a container
   never compiles the SUT (`remote.py:50-64`). Compose-built container hosts
   also never receive product ingest: the four `apply_*` calls run only in
   the host factory (`src/otto/host/factory.py:220-223`), never in
   `src/otto/docker/compose.py:459`.
6. The embedded (Zephyr LLEXT) extension is not a `Product`. repo3's suite
   loads it by hand (`tests/repo3/tests/test_embedded_coverage.py:204-214`),
   and otto learns its name only from `[coverage.embedded].extension`
   (`embedded.py:203-207`).
7. The getting-started docs have no coverage page.

Chris's rulings from the brainstorm:

- Tree shape: kind first, then host, then product; beneath the product a
  `product/` dir for product logs and a `debug/` dir for product-specific
  debug logs, because product debug logs must not mix with other products'.
- `gcda_remote_dir` goes: products are the only e2e coverage source, hard
  cutover, no deprecation period.
- Declared `[[products]]` commands reference the directory through a
  `{cov_dir}` placeholder substituted in the `file` kind factory only.
- Instrumentation is detected automatically and turns retrieval on; `--cov`
  with nothing instrumented is a hard error before the run; a partial set
  warns and lists every uninstrumented product.
- Embedded extensions get the same detection by becoming products (a new
  `llext` kind), because the `.gcda` filename strings the detector looks for
  live in the extension's own `.rodata` (the console dump prints them from
  `gcov_info.filename`).
- The getting-started docs cover configuring `cov_dir`, its availability
  as a product member when composing install and run commands, and the fact
  that `otto cov` retrieval reads from it. Several products in one Docker
  container must work.

## 2. Goal and scope

Coverage and logs become per-product everywhere: on the host (each product
writes its counters under its own `cov_dir`), in the run tree (one
`<host>/<product>` level shared by logs and coverage and owned by one
module), in the capture and store (a `product` field), in the report (a
product filter that composes with tier, context, and ticket), and in unit
tiers (named product views). Retrieval switches itself on when an
instrumented product is present and refuses loudly when `--cov` finds none.

### Non-goals (documented, deliberately out)

- Per-test-case or per-suite coverage attribution. A run stays one capture
  per (host, product) per invocation.
- Building or instrumenting products for the user. Detection reads what the
  build produced; it never adds flags.
- Migration shims for v2 captures, v7 stores, or one-level `cov/` trees.
  otto has no users to migrate; the loaders stay exact-match.
- A product filter in the `tickets.json` export. That schema is versioned on
  its own and gets its own change when a consumer asks.
- `--cov-fail-under` and the console summary from `todo/coverage_roadmap.md`.

## 3. The run-tree contract and `otto.layout`

One new module, `src/otto/layout.py`, owns every path below a run dir that
is keyed by host or product. It is pure (no I/O) and both the host layer and
the coverage pipeline import it. The tree:

```text
<run>/
  logs/<host_id>/<product>/product/    Product.get_logs output
  logs/<host_id>/<product>/debug/      the product's debug_log_globs haul
  logs/<host_id>/debug/                the host's debug_log_globs haul
  cov/<host_id>/<product>/             .gcda (nested as fetched), board.info,
                                       board.resolved.info, capture.json
  cov/.otto_cov_meta.json              unchanged
  cov_report/                          unchanged
  <Suite>/<test_node>/                 unchanged, separate lineage
```

API (all return `Path`, none create directories):

| Function | Returns |
|---|---|
| `host_logs_dir(base, host_id)` | `base/logs/<host_id>` |
| `host_debug_dir(base, host_id)` | `base/logs/<host_id>/debug` |
| `product_logs_dir(base, host_id, product)` | `base/logs/<host_id>/<product>/product` |
| `product_debug_dir(base, host_id, product)` | `base/logs/<host_id>/<product>/debug` |
| `cov_host_dir(base, host_id)` | `base/cov/<host_id>` |
| `cov_product_dir(base, host_id, product)` | `base/cov/<host_id>/<product>` |
| `validate_product_name(name)` | raises `ValueError` |

`validate_product_name` rejects the empty string, `.` and `..`, any name
containing `/`, `\`, or NUL, and the reserved name `debug` (it is a sibling
of product dirs under `logs/<host_id>/`). It runs at the ingest chokepoint
(§4) and on unit-tier product keys (§11), so a bad name fails at settings or
lab load, never at first write.

`BaseHost.log_dest(dest)` keeps resolving the base (explicit `dest`, else
`ctx.output_dir`, else CWD) and then delegates to `host_logs_dir`. Every
spelling listed in §1 item 3, both fetcher `staging_root / label` sites, and
`produce_captures` move onto the module. A contract test
(`tests/unit/test_layout.py`) pins each template from one table; the
host-logs contract test and the coverage e2e tests assert the same shapes
from the outside.

## 4. Product model

`Product` (`src/otto/host/product.py`) gains three members and two hooks.
Dev tools gain nothing; the seams stay asymmetric on purpose.

```python
class Product(ABC):
    name: str
    owner: str | None = None
    cov_dir: str | None = None
    debug_log_globs: Sequence[str] = ()

    def instrumented(self) -> bool | None: ...          # default None
    async def get_debug_logs(self, host, dest) -> Result  # concrete
```

- **`cov_dir`** is the host-side directory the instrumented product writes
  its coverage counters under (today `.gcda`; the name is format-neutral on
  purpose so a later format can reuse it), the value the product hands to
  `GCOV_PREFIX`. It is a string, not a `Path`, because it lives in the
  host's path domain and only ever appears in shell commands (`find`,
  `GCOV_PREFIX=`). It is distinct from the CLI's `--cov-dir` and
  `RunOptions.cov_dir`, which name the **local** staging root (`<run>/cov`)
  that retrieval writes into; the product member says where counters are
  written on the host, the option says where they land on the otto machine. `None` means "the
  default". Ingest stamps `f"/tmp/{name}"` onto a `None` the same way it
  stamps `owner` today, so after ingest `self.cov_dir` is always concrete
  and a code product can read it when composing its install or run command.
  `cov_dir_of(product)` in `product.py` applies the same default for
  products used outside ingest (tests, library callers).
- **`debug_log_globs`** mirrors the host-level member. The ABC default is an
  empty tuple typed as a `Sequence` so the class attribute is immutable;
  `FileProduct` declares it as a dataclass `list[str]` field.
- **`get_debug_logs(host, dest)`** is concrete on the ABC and hauls the
  globs exactly as `BaseHost.get_debug_logs` does today (literal entries
  fetched as declared; a glob needs the host's `glob` and fails loud
  without it). The two share one helper so the rules cannot drift.
- **`instrumented()`** is synchronous and local. `None` means unknown.
  `FileProduct` implements it: a regular-file artifact is scanned for the
  markers `b".gcda"`, `b"__gcov_"`, and `b"__llvm_gcov"` (any hit is
  `True`; a clean scan is `False`); a directory artifact is `True` if any
  regular file under it scans `True`, else `None` (the scan cannot see
  inside archives); a missing artifact is `None`. The `.gcda` marker is the
  load-bearing one: GCC and clang both embed each translation unit's
  `.gcda` filename in the object, and it survives `strip`. The scan reads
  in chunks with an overlap of the longest marker so a boundary cannot hide
  one.

The ingest chokepoint (`apply_declared_products` then
`apply_product_providers`, factored into one `apply_providers(host)` helper
in `factory.py` that §13 reuses) validates every product name and stamps
`cov_dir`.

## 5. Declared `file` kind

`[[products]]` entries of `kind = "file"` accept three new params. Dev-tool
entries reject all three by name (the factory reads `entry.seam`).

| Param | Type | Meaning |
|---|---|---|
| `cov_dir` | string | as §4; default `/tmp/<name>` |
| `debug_log_globs` | list of strings | as §4 |
| `instrumented` | bool | overrides the scan (for archives the scan cannot see inside) |

`install`, `uninstall`, and `check` undergo placeholder substitution in the
factory, after validation, for products only: `{cov_dir}` and `{name}`.
Substitution is strict (`str.format_map` over a mapping that raises on any
other key), so an unknown placeholder fails at lab load naming the entry
and the two valid names. Literal braces are written `{{` and `}}`, and the
error message says so, because `awk '{print $1}'` in a product command is
the case that will hit it. The unknown-param error's `valid:` list gains
the three names.

```toml
[[products]]
name = "app"
kind = "file"
artifact = "build/app"
cov_dir = "/var/cov/app"                 # default: /tmp/app
debug_log_globs = ["/var/log/app/*.log"]
install = "GCOV_PREFIX={cov_dir} GCOV_PREFIX_STRIP=3 ./app &"
```

## 6. The `llext` product kind

A built-in `llext` kind and its class `LlextProduct(FileProduct)` model an
embedded extension as a product, so detection, the tree, and the filter
treat boards like every other host.

| Verb | Behaviour |
|---|---|
| `stage` | no-op success: there is no filesystem, the load is the transfer |
| `install` | `host.load(artifact, name=name)`, then one `loader.call_command(name, fn)` per `call_after_load` entry |
| `uninstall` | `host.unload(name)` (drains to eviction, idempotent) |
| `is_installed` | `loader.list_command()` parsed by `loader.is_loaded(name, output)` |
| `instrumented` | the §4 scan (an LLEXT object carries the `.gcda` strings) |
| `cov_dir` | stamped like any product, unused: the counters come over the console |

Params: `artifact` (required), `call_after_load` (list of function names,
default empty; repo3 uses `["cov_init"]` so the gcov constructor runs before
any dump), `dump_fn` (default `cov_dump`), `instrumented`,
`debug_log_globs`. `BinaryLoader` gains `list_command()`,
`is_loaded(name, output)`, and `call_command(name, fn)`; `LlextHexLoader`
implements them over `llext list` and `llext call_fn`. The factory raises at
lab load when the matched host has no binary loader.

`[coverage.embedded].extension` is removed. The embedded collector walks
each embedded host's instrumented `llext` products and issues
`loader.call_command(name, dump_fn)` per product, staging into
`cov/<board>/<name>/`. `build_dir` and `builds.<version>.build_dir` stay as
they are (they locate the `.gcno` for the cross-gcov).

repo3 migrates: a `[[products]]` `llext` entry replaces the fixture's
hand-rolled load loop, and the fixture calls `uninstall` then `install`
explicitly so a rebuilt extension replaces the resident bytes (the existing
stamp-mismatch rule; `ensure_installed` alone would keep stale bytes loaded
because `is_installed` is true).

## 7. Retrieval

`collect_coverage` no longer reads `gcda_remote_dir`. For every host the
`[coverage].hosts` selector matches, the fetcher walks `host.products` and,
for each product whose §8 verdict is `True`, runs one discovery:

```text
find <cov_dir> -name '*.gcda' -type f
```

Hits are pulled with `host.get` into `cov_product_dir(cov_dir, host.id,
product.name)`. The per-product dir is created only once files are known to
exist (today's rule, kept). `GcdaFetcher.fetch_all` returns a dict keyed by
`(host_id, product)`; `CollectResult` and `.otto_cov_meta.json` keep their
per-host toolchain and source-root maps unchanged (both are per host, not
per product).

- **Clean.** Clean-after-fetch and `otto cov clean` become one
  `find <cov_dir> -name '*.gcda' -type f -delete` per instrumented product.
  `otto cov get --clean` keeps its Unix-only scoping.
- **Docker.** The fetcher skips only `LocalHost` and `EmbeddedHost`. A
  container host's `exec` is `docker exec` and its `get` is a two-step
  `docker cp`, both already implemented, so several products in one
  container each get their own `cov_dir` (distinct `/tmp/<name>` defaults)
  and are discovered and pulled separately.
- **Embedded.** §6.
- **Nothing found.** A host with no products contributes nothing and logs
  it at info level. `NoCoverageDataError` lists every (host, product,
  cov_dir) triple searched.

## 8. Instrumentation detection and the `--cov` tri-state

New module `src/otto/coverage/instrumentation.py`:

```python
@dataclass
class InstrumentationRow:
    host_id: str
    product: str
    verdict: bool | None          # True, False, None = unknown

@dataclass
class InstrumentationReport:
    rows: list[InstrumentationRow]
    def instrumented(self) -> list[InstrumentationRow]
    def missing(self) -> list[InstrumentationRow]   # False or None
    def table(self) -> Table                         # Rich, rounded

def detect(hosts: Iterable[Host]) -> InstrumentationReport
```

`detect` runs once, after hosts are built and products ingested and before
anything executes, over every product on every host the `[coverage].hosts`
selector matches (every host in the lab when there is no `[coverage]` table,
which is the selector's own default). It is local (the §4 scan reads the artifact on the otto
machine), so it costs no host round trip.

`otto test --cov` becomes `--cov/--no-cov` with a `bool | None` default of
`None` (auto). `RunOptions.cov` becomes `bool | None`. `--cov-dir` and
`--cov-report` keep implying on; `--no-cov` combined with either is a usage
error. The decision table:

| Mode | Instrumented set | `[coverage]` present | Outcome |
|---|---|---|---|
| auto | some | yes | retrieval on; table logged at info |
| auto | some | no | warning names the instrumented products and what to configure; retrieval off |
| auto | none | any | nothing; table at debug |
| `--cov` or `otto cov get` | none | any | `CoverageNotInstrumentedError` before the run, with the full table and, for unknowns, the hook to override |
| any with retrieval on | partial | yes | warning lists every missing product; retrieval proceeds for the instrumented ones |

Unknown counts as not instrumented for the auto decision and the error; the
table labels it `unknown` so the fix (override `Product.instrumented`, or
set `instrumented = true`) is visible. The library run entry in
`src/otto/suite/run.py` takes the same tri-state. `CoverageNotInstrumentedError` subclasses
`CoverageConfigError` so the CLI's existing handling turns it into one
user-facing line plus the table.

## 9. Capture v3 and store v8

- `Capture` gains `product: str` (required). `CAPTURE_FORMAT_VERSION` 2 → 3;
  the exact-match loader rejects v2 as it rejects any other version.
  `build_capture` and `produce_captures` take the product from the directory
  they are producing for: the walk is `cov/<host>/<product>/`, one
  `board.info`, `board.resolved.info`, and `capture.json` per product dir.
- The committed manual-capture filename gains the product:
  `<captured_at>-<ticket>-<board>-<product>.json`.
- `RunRecord` gains `product: str = ""`, `add_run(product=...)`, and
  `to_dict` emits it. `STORE_FORMAT_VERSION` 7 → 8; a v7 store is rejected on
  load as any mismatch is.
- `_partition_board_dirs` walks two levels. A `capture.json` or `.gcda`
  found directly under `cov/<host>/` raises `CoverageConfigError` naming the
  expected `cov/<host>/<product>/` layout.
- Unit runs carry the product from tier config (§11); the unnamed harvest
  carries `""`.

## 10. Report: the product filter

A fourth pinned value with the same lifecycle as context, ticket, and
hide-asserted (`focus.tsx`): `?product=` in the hash query,
`otto-cov:<stamp>:product` in storage, hash wins over storage, boot
reconciliation, hashchange adopt and reassert, `setProduct`. Chrome: a
`ProductChip` next to `TicketChip` and a menu section in `AppShell.tsx`; a
product chip row beside the tier chips on `RunsPage.tsx`; the runs search
haystack includes product; each context's host list renders
`<host> · <product>` per member run.

Semantics mirror the context pin. The product filter narrows the
**numerator** to runs tagged with that product. It composes with tier chips
(intersection), with the ticket pin (ticket owns the denominator, product
the numerator, declining at tree granularity exactly where context
declines today), and with a context pin through a precomputed per-context
per-product count.

Data: `IndexPayload.products: string[]` (sorted, distinct, non-empty run
products); `RunJson.product`; per stat bucket `product_lines:
Record<product, number>` beside `ctx_lines`, and `ctx_product_lines:
Record<label, Record<product, number>>`, both computed in `_file_stats` from
`run_hits` through `runs_by_id` and rolled up by `_add_stats`. The key
column header (`keyColumnLabel`) names the product when pinned.
`OTTO_COV_DATA_FORMAT` and `EXPECTED_DATA_FORMAT` move 2 → 3 together, and
`tests/_fixtures/covapp_contract.json` gains the new keys
(`index_payload_keys`, `run_json_keys`, `stats_keys`) so both suites assert
them.

## 11. Unit coverage per product

`CoverageTierSpec` gains `products: dict[str, list[Path]]` (keys validated
by `validate_product_name`), `TierConfig` mirrors it, and
`_harvest_unit_tiers` produces one run per product plus one unnamed run for
the plain `harvest_dirs` list. Work files are named
`unit_<tier>_<product>_<idx>.info`.

```toml
[coverage.tiers.unit]
kind = "unit"
precedence = 2
harvest_dirs = ["build/tests"]            # one unnamed run

[coverage.tiers.unit.products]
app   = ["build/app-tests"]               # one run, product = "app"
agent = ["build/agent-tests"]
```

Product names here need not appear in `[[products]]`; using the same names
is what makes one product filter show a product's e2e and unit runs
together, and the docs say so. The docs also state the requirement for
distinct data: each view compiles into its own build or object directory,
because `.gcno` files sit beside the objects and two views built from one
tree with different defines cannot share them. `GCOV_PREFIX` relocates
`.gcda` only and does not help.

## 12. Logs

- `get_product_logs(dest, owner)` hands each product
  `product_logs_dir(base, host.id, product.name)` for its `get_logs` hook,
  then `product_debug_dir(...)` for its `get_debug_logs` hook. Both dirs are
  created before the hook runs (today's rule). Best-effort ordering and
  first-failure reporting are unchanged.
- `get_debug_logs` (host level) moves onto `host_debug_dir`, otherwise
  unchanged.
- `Host.get_logs` and `Actions.get_logs` derive the product subtrees from
  the layout module; the `require_product_logs` before/after diff walks
  every `logs/<host>/<product>/` subtree.
- The `otto host <id> get-product-logs` verb keeps its name and now hauls
  both product subdirs.

## 13. Docker container hosts get product ingest

Both `DockerContainerHost` construction sites in `src/otto/docker/compose.py`
(the resolved hosts at `:459` and the placeholders at `:965` and `:1049`)
call the `apply_providers(host)` helper from §4 after the `source_lab` and
`lab_info` stamps, so `[[products]]` match tables and code providers attach
to containers the way they attach to factory-built hosts. With §7 this is
what makes several products in one container collectable.

## 14. Configuration surface

Before:

```toml
[coverage]
gcda_remote_dir = "/var/coverage/product"
[coverage.embedded]
extension = "covext"
build_dir = "product/build"
```

After:

```toml
[[products]]
name = "app"
kind = "file"
artifact = "build/app"
cov_dir = "/var/cov/app"
install = "GCOV_PREFIX={cov_dir} GCOV_PREFIX_STRIP=3 ./app &"

[[products]]
name = "covext"
kind = "llext"
artifact = "product/build/zephyr/covext.stripped.llext"
call_after_load = ["cov_init"]
match = { os_name = "Zephyr" }

[coverage]
hosts = "test[123]|zephyr.*"
[coverage.embedded]
build_dir = "product/build"
[coverage.tiers.unit]
kind = "unit"
precedence = 2
[coverage.tiers.unit.products]
app = ["build/app-tests"]
```

`otto init`'s scaffold gains the commented `cov_dir` line under
`[[products]]` and loses `gcda_remote_dir`.

## 15. Failure modes

| Condition | Where | Behaviour |
|---|---|---|
| product name empty, `.`/`..`, contains a separator, or `debug` | ingest / tier load | `ValueError` naming the product and the rule |
| unknown `{placeholder}` in a product command | `file` kind factory | `ValueError` naming the entry, the valid names, and `{{` escaping |
| `cov_dir`, `debug_log_globs`, `instrumented` on a dev-tool entry | `file` kind factory | `ValueError` (dev tools have no coverage) |
| `llext` entry matched to a host without a binary loader | `llext` kind factory | `ValueError` naming the host |
| `--cov` / `otto cov get` with no instrumented product | before the run | `CoverageNotInstrumentedError` + table |
| `--no-cov` with `--cov-dir` or `--cov-report` | CLI | usage error |
| instrumented products but no `[coverage]` (auto) | before the run | warning, retrieval off |
| partial instrumentation with retrieval on | before the run | warning listing the missing products |
| `.gcda` or `capture.json` directly under `cov/<host>/` | report | `CoverageConfigError` naming the two-level layout |
| capture v2 / store v7 on disk | load | rejected as a version mismatch |
| no `.gcda` on any (host, product) | collect | `NoCoverageDataError` listing every triple searched |

## 16. Testing

Unit (fast, hostless):

- `tests/unit/test_layout.py`: every template from one table; the reserved
  and malformed names; both trees identical below `<kind>/<host>/`.
- Product model: default stamping, `cov_dir_of`, the `Sequence` default,
  the scan on synthetic byte fixtures written to `tmp_path` (a marker
  embedded mid-file, a marker straddling the chunk boundary, a clean file),
  directory and missing artifacts returning the documented verdicts. No
  binary is committed; the real-ELF proof is the bed (repo1's built product
  and repo3's extension both scan `True`).
- `file` kind: the three params, dev-tool rejection, placeholder
  substitution, unknown placeholder, `{{` escaping, `valid:` list.
- `llext` kind: verbs against a fake embedded host, loader-less host
  refusal, `call_after_load` ordering, `dump_fn`.
- Fetcher: per-product discovery and pull on a fake Unix host and a fake
  container host, empty-product skip, clean per product, Docker no longer
  skipped, embedded per-product dump.
- Detection: every row of the §8 table, including the tri-state CLI parsing
  and the `--no-cov` usage error.
- Capture v3 and store v8: round trip, v2/v7 rejection, product on
  `to_dict`, manual store filename.
- Reporter: two-level walk, the one-level error, unit products producing
  separate runs, the unnamed run.
- Renderer and contract: `products`, `product_lines`,
  `ctx_product_lines`, the format bump, `covapp_contract.json` asserted from
  both suites; vitest for `focus.tsx` (`?product=`, storage, reconciliation),
  the chips, the runs page row and haystack.
- Logs: per-product dirs, product debug haul, host debug unchanged,
  `require_product_logs` walking the new subtrees.
- Existing guards that will move: the host-logs contract test, the API
  snapshot golden (new `Host`/`Product` surface), the settings-template
  drift test, and the docs-gap sync test.

Bed (live, sequential, never in parallel with another gate):

- `tests/repo1` migrates to a `[[products]]` entry with `cov_dir`, and
  `test_coverage_product.py` sets `GCOV_PREFIX` from it; the coverage e2e
  asserts `cov/<host>/<product>/capture.json` and the product filter data.
- `tests/repo3` migrates to the `llext` kind; the embedded e2e proves load,
  `cov_init`, dump, and the two-level tree on the Zephyr board.
- Docker: if the docker bed lane has an instrumented image, an e2e proves
  two products in one container; otherwise the container-host fake at
  integration level stands in, and the branch report says which.
- The auto-on path is proven once end to end: `otto test` without `--cov`
  on repo1 produces captures.

## 17. Documentation

- **New** `docs/getting-started/coverage.md`: instrument the build, set
  `cov_dir` per product, use `{cov_dir}` (or `self.cov_dir`) in the
  install command with `GCOV_PREFIX` and `GCOV_PREFIX_STRIP`, run
  `otto test` (auto-on) and `otto test --cov`, read the table, `otto cov get`
  and `otto cov report`, the run tree, the product filter, the Docker note,
  and a pointer to unit views. Linked from the getting-started index and the
  products tutorial.
- `docs/guide/configuration/declared-products-tools.md`: the `file` params
  table gains three rows and a placeholder section; a new `llext` kind
  section.
- `docs/getting-started/defining-products-and-tools.md` and
  `docs/examples/getting-started/libs/gs_example/products.py`: `cov_dir` on
  the code example.
- `docs/guide/cli/index.md` "Output directories" becomes the single home for
  the run tree (§3 diagram); `docs/guide/cli/run/defaults.md`,
  `docs/guide/cli/host/capabilities/products.md`, and
  `docs/guide/cli/host/index.md` link there instead of restating.
- `docs/guide/cli/cov/get.md`, `during-tests.md` (tri-state, auto-on, the
  table), `tiers.md` (unit products and the separate-build-dir rule),
  `report.md` (the product filter), `instrumenting/gcc.md` and
  `instrumenting/embedded.md` (`cov_dir` and the `llext` kind replace the
  removed keys), `docs/guide/configuration/settings.md`.
- `docs/architecture/subsystems/coverage/index.md` and `types.md` (store
  v8, capture v3, the product dimension); the logging architecture page
  links the tree.
- `src/otto/cli/init_templates.py` scaffold.
- The branch closes with a naive-reader walkthrough of the getting-started
  page.

## 18. Out of scope

Per-test-case attribution; automatic build instrumentation; migration shims;
`tickets.json` product fields; roadmap items from `todo/coverage_roadmap.md`
not named here. This spec supersedes `todo/coverage-improvements.md`, which
can be retired once the branch lands.

# Products: one definition, a public base, and debug/field variants

**Date:** 2026-10-03
**Status:** approved in chat (Chris, 2026-10-03: "Refuse + `class =` key"; "`variant` key, first match wins"; one `OTTO_FIELD_*` env var; `--list-products` marks coverage instrumentation); this document is the written form for review before planning.
**Origin:** first external-user feedback, `todo/user-feedback-design-items-2026-10-02.md` §2.1 and §2.2 — the getting-started page leads with a five-method `Product` subclass; a product defined in data and in code is silently deduplicated; the root `--field/--debug` flag is parsed, documented and consumed by nothing.

## 1. Intent

Three things a project author needs and does not have today:

- **Sane defaults in a public class.** The class with the honest defaults (`DeclaredShell`: stage via `put`, install/uninstall as declared command strings or a no-op, `is_installed` False unless a `check` command says otherwise) is internal. A custom product should be "subclass, override `install`", not five abstract methods.
- **One definition per name.** Today config wins and code fills the gaps: a provider product whose name a declared entry already placed on the host is dropped with a `logger.debug`. A name is defined in data **or** in code; both is a mistake otto names.
- **A meaning for `--field/--debug`.** Products gain a variant. The flag selects it; a debug build that carries coverage instrumentation and a field build that does not are two entries with one name.

And one thing the listing should say: whether each product's artifact carries coverage instrumentation, since that is the question a debug variant exists to answer.

Not changed: `[[products]]`/`[[dev_tools]]` syntax for the existing kinds; the `match` table; `register_product_provider` and the provider contract (its place in the docs moves); the kind registries (`PRODUCT_KINDS`, `DEV_TOOL_KINDS`) and their built-in kinds; `slug`, host ids, `--list-products` structure (columns are added, none removed).

## 2. `DeclaredProduct` — the public base

`DeclaredShell` (`src/otto/host/shell_kind.py`) becomes `DeclaredProduct`: the same dataclass, moved and renamed, serving both seams exactly as today (it subclasses `ShellProduct` and `DevTool`; which seam an instance lives in is decided by the registry that built it, never by the type). It lives in its own module, `otto/host/declared_product.py` — `otto.host.dev_tool` imports `otto.host.product`, so a class that subclasses both cannot sit in either — and is exported lazily from the package as `otto.host.DeclaredProduct`, which is the spelling the docs teach (`from otto.host import DeclaredProduct`). `kind = "shell"` keeps building it, unchanged. `DeclaredShell` is not kept as an alias — there is one name.

Fields (all existing): `artifact`, `name` (defaults to the artifact's basename), `stage_dir`, `cov_dir`, `debug_log_globs`, `instrumented_override`, `install_cmd`, `uninstall_cmd`, `check_cmd`. Hooks a subclass may override: `stage`, `install`, `uninstall`, `is_installed`, `get_logs`, `plan`, `instrumented`.

**Plan honesty.** `plan()` describes the declared command strings. A subclass that overrides `install` but not `plan` would otherwise be previewed with an `install_cmd` that never runs — or with nothing, which `otto run install --dry-run` would read as "installs nothing". So `DeclaredProduct.plan` checks each hook against its own: when `type(self).install is not DeclaredProduct.install` (likewise `uninstall`, `stage`), that step is reported as **unchecked** — `install: Firmware.install (code; no plan)` — never as the declared string. A subclass that wants a real preview overrides `plan`. This is the install-preview principle (reports never fake an unmeasured step) applied to the one place a subclass can silently invalidate the default.

## 3. `class =` — a declared entry with custom behaviour

### 3.1 The entry

The reserved keys of a `[[products]]`/`[[dev_tools]]` entry become `name`, `match`, `variant` (§5), and **exactly one of** `kind` / `class`. `DeclaredEntrySpec` refuses an entry with both or neither at parse, naming the entry:

```
[[products]] 'firmware': set exactly one of `kind` or `class`
```

`class = "pkg.mod:ClassName"` names a class importable from the repo's `libs` (already on `sys.path` by the time entries are built). Every other key is a field of that class.

```toml
[[products]]
name     = "firmware"
class    = "acme.products:Firmware"
artifact = "build/fw.bin"
stage_dir = "/opt/fw"
check    = "test -f /opt/fw/fw.bin"
match    = { os_type = "linux" }
```

```python
from otto.host.product import DeclaredProduct

class Firmware(DeclaredProduct):
    async def install(self, host):
        return await host.run(f"fwload {self.stage_dir}/{self.artifact.name}")
```

### 3.2 Building it

One generic factory, `otto.host.shell_kind.class_entry(entry, host)`, serves every `class =` entry in both seams. Each `KindRegistry` is constructed with it as its `class_factory` (a `Ref`, resolved on first use like the built-in kinds), so `KindRegistry.build` gains one branch: an entry with `class` set is built by the registry's class factory instead of by a registered kind. It:

1. imports the class with `otto.registry.Ref`'s existing import-and-validate path (module, then attribute); an import or attribute failure is refused naming the entry and the path — `[[products]] 'firmware': class 'acme.products:Firmware' — No module named 'acme'`;
2. refuses a class that is not a `DeclaredProduct` subclass, naming the entry and the class;
3. maps the entry's params onto the class's dataclass fields by the same rules `_shell_kind` applies today — `install`/`uninstall`/`check` to `install_cmd`/`uninstall_cmd`/`check_cmd`, `artifact` anchored to the repo, `stage_dir` validated, the coverage params refused on the `dev_tools` seam, `{cov_dir}`/`{name}` placeholders expanded — then any **subclass-added** dataclass field by name (the subclass is decorated `@dataclass` to add fields; a bare annotation is not a field), checked against the field's annotation when it is `str`, `int`, `bool`, `Path` (built as `Path(value)`, in whichever domain the subclass means) or `list[str]`, and passed through verbatim for any other annotation; an unknown key is refused listing the valid keys for that class; a required field (no default) left unset is refused naming it. `_shell_kind` becomes a thin call into the same mapping (`build_declared(entry, host, DeclaredProduct)`) with `DeclaredProduct` as the class, so the shell kind and the `class` route cannot drift.
4. stamps `kind = "<pkg.mod:ClassName>"` (the path as written), `origin = "declared"`, `source_entry` and `owner` exactly as a kind-built instance is stamped, so `--list-products` shows where the behaviour lives.

`DeclaredEntry` gains `cls: str | None` beside `kind`, and `kind` becomes `str | None` (one of the two is set). Every reader of `entry.kind` that renders it (`--list-products`, error prefixes, `UnusedEntry`) renders `entry.kind or entry.cls`.

## 4. The one-definition rule

A product (or dev tool) name is defined by a declared entry **or** by a provider, never both. At the ingest chokepoint, where `apply_product_providers` (and its dev-tool twin) today skips a provider instance whose name the host already carries, the skip becomes a **refusal** when the holder is declared (`origin == "declared"`):

```
[[products]] 'firmware' (repo acme) is also defined by provider
acme.init:products (repo acme) — a product is defined in data OR in code; to
give a declared entry custom behaviour, set `class = "pkg.mod:Class"` on it
```

The refusal is the same `ValueError` the registry raises for an unknown kind: it fails ingest, loudly, on the first host both reach — not a warning, not a listing footnote. The declared side is named by seam, name and the holder's `owner`; the provider side by `module:qualname` and its registering repo (both already in hand at that site).

Unchanged, deliberately:

- Two **providers** returning one name: first wins, the loser goes to `shadowed_products` and the listing reports it. That is code-vs-code ordering, which providers have always had.
- Two **entries** with one name: declaration order, first match per name wins ("specific first, generic fallback last" — `KindRegistry.build`). That is selection, not a second definition, and §5 builds on it.
- A provider instance whose name an **earlier provider** placed stays shadowed, not refused.

## 5. Variants

### 5.1 The entry key

`variant = "debug" | "field"` is a reserved entry key, optional. `KindRegistry.build` consults it before `match`: an entry whose `variant` is set and differs from the run's variant is skipped as if its match had failed; an entry without `variant` matches any run. Any other value is refused at parse naming the entry and the two valid values.

Because same-name entries already resolve first-match-wins in declaration order, a variant is just a more specific entry — no new merge rule, no overlay tables:

```toml
[[products]]
name = "fw"
kind = "shell"
variant = "field"
artifact = "build/fw-field.bin"

[[products]]            # fallback: any variant
name = "fw"
kind = "shell"
artifact = "build/fw-debug.bin"
```

`otto run install` stages `fw-debug.bin`; `otto --field run install` stages `fw-field.bin`. Ordering is the author's: the generic fallback goes last, as it does for `match` today. An entry skipped for its variant is reported by `--list-products` under *unused* with the reason `variant 'field' (run is debug)` — the same place a non-matching entry is reported today.

### 5.2 Where the run's variant lives

The run's variant is a `ContextVar` in `otto.context` — `variant() -> Literal["debug", "field"]` and `set_variant(value) -> Token` — not a field on `OttoContext`: the lab is ingested (every declared entry built) inside `ensure_lab_context` *before* `OttoContext` is installed, so a context field would be invisible to the registry under the CLI. The root callback calls `set_variant` as it stashes `RootOptions`, before any lab loads; `KindRegistry.build` and providers both read `variant()`; a library caller that never set it reads `"debug"`. The root conftest's ContextVar snapshot fixture restores it per test as it restores the context itself.

### 5.3 The flag and its one env var

`--field/--debug` keeps its shape (a boolean pair, default `--debug`). Its env var is `OTTO_FIELD_PRODUCTS` alone, Typer's boolean parsing (`1`/`true`/`yes` → field). `OTTO_FIELD_DEFAULT` is deleted outright — `FIELD_DEFAULT_ENV_VAR`, `_field_default` in `cli/main.py`, `OttoEnvSettings.field_default`, its tests, its `docs/cli/index.md` row — per the no-hidden-options rule: one switch, one variable. The root callback stores the value in `RootOptions.field: bool` and calls `set_variant("field" if field else "debug")`; the `# noqa: ARG001` on the option goes away because the value is now read.

## 6. `--list-products` / `--list-tools`

Both tables (the lab view and the no-lab declared view) gain two columns:

- **variant** — the entry's `variant`, or `any`. A provider instance shows `any`: a provider decides per run and otto does not know which branch it took.
- **instrumented** — `yes`, `no`, `unknown` or `missing`. From `Product.instrumented()` on the lab view (`ShellProduct` and its descendants scan the artifact; a code product answers what it overrides; the ABC default is `None`). On the no-lab view the scan runs over the same anchored path the `artifact` column shows, when the kind has one. `None` renders `unknown`, except that an artifact path which does not exist on the otto machine renders `missing` — the reader should not have to guess whether "unknown" means an archive or an unbuilt tree. The scan is local and synchronous (`scan_for_instrumentation`); the listing still contacts no host. Dev tools show the column too — a dev tool with `instrumented = true` declared is the kmodcov case, and the listing should say so rather than render a blank.

The unused section gains the variant reason from §5.1. Nothing is removed.

**Invariant: local only.** `--list-products`/`--list-tools` answer from local configuration and files present on the otto machine — the repos' settings, the lab's data, the artifacts' bytes. Neither view opens a connection to a host, runs a command, or needs the bed to be up; `instrumented` is answered from the artifact on disk, never from the host. The live questions — what is installed right now, whether the counters exist — belong to `otto run status` and `otto cov`, and the listing page says so in one line. The invariant is pinned by a test that injects the hostile condition rather than inheriting a quiet one: the lab view runs over a lab whose hosts carry a transport that raises on connect, and completes with every column filled.

## 7. Documentation

- `docs/getting-started/defining-products-and-tools.md` is reordered to the path a new user should take: (1) a `[[products]]` entry with `kind = "shell"` — no Python; (2) "when a command string is not enough": `class =` and a `DeclaredProduct` subclass overriding `install` — the worked example's `AgentBinary` becomes this (the `GCOV_PREFIX` install stays as the overridden method; `artifact`, `stage_dir`, `check` and `cov_dir` move to the entry, `uninstall` becomes a declared string, `instrumented` comes free from the artifact scan); (3) a pointer to the Advanced page for providers. The `[project]`-required note stays, linked to its home as today. The example project's `.otto/settings.toml` and `gs_example/products.py` change accordingly, and `gs_example.products` leaves `init` (the class is imported by the entry, not by an init module).
- A new page `docs/cookbook/extending/product-providers.md` ("Programmatic products: version permutations and run-time choices") holds the provider contract that the getting-started page drops, with the pattern written out: one `DeclaredProduct` per version from the project's own configuration, `otto.context.variant()` where the variant matters, keyed on the host's product-agnostic attributes. Its fragment is a `literalinclude` of `docs/examples/getting-started/libs/gs_example/versions.py`: the provider function and its `register_product_provider` call, not listed in the example project's `init` (the worked example defines `agent` in data, and §4 forbids defining it twice). The existing provider API docs link here instead of restating.
- `docs/configuration/declared-products-tools.md`: the reserved-key table gains `class` and `variant`; a new "One definition" section carries the §4 rule with the refusal text; the `DeclaredProduct` API reference replaces the internal `DeclaredShell` mentions; the "Custom kinds" section says when a registered kind is still the right tool (a factory that needs the host, or a kind many entries share) versus `class =`.
- `docs/cli/index.md`: the `--field/--debug` row says what it selects; the `OTTO_FIELD_DEFAULT` row goes; the `--list-products` page shows the two new columns.

One home per topic: the rule text lives in `declared-products-tools.md`; every other page links.

## 8. Honesty: the tests

- **Parse** (`tests/unit/models/test_settings.py`): `kind`+`class` both and neither refused naming the entry; `variant` outside the two values refused naming the entry and the values.
- **Registry** (`tests/unit/declared/`): variant selection — unset matches any run, set-and-equal matches, set-and-different skips, declaration order decides between a variant entry and a fallback in both orders; no context means `debug`. `class` build: fields mapped, placeholders expanded, a subclass-added field set from TOML, unknown key refused listing valid keys, missing required field refused naming it, import failure and non-subclass refused naming entry and path. The shell kind and the `class` route build an identical `DeclaredProduct` from identical params (a differential, since §3.2 says they share one mapping).
- **Ingest** (`tests/unit/host/test_product_providers.py` or beside it): data + provider for one name refused naming both sites; provider + provider still shadows; declared + declared still first-match-wins. The refusal reaches the user through the CLI once, under `GITHUB_ACTIONS=true TERM=dumb`.
- **Plan honesty**: a `DeclaredProduct` subclass overriding `install` and not `plan` previews an unchecked `install` line naming the class and method, never the declared `install_cmd`; overriding `plan` as well shows the subclass's plan. The mutation that proves the test: remove the override check and the declared string reappears.
- **Flag plumbing**: `--field` → `RootOptions.field` → `otto.context.variant() == "field"` while the command runs; `OTTO_FIELD_PRODUCTS=1` does the same; `OTTO_FIELD_DEFAULT` set in the environment changes nothing (the test that today asserts `env.field_default` is replaced by one that asserts the name is gone from `OttoEnvSettings`).
- **Listing**: the two columns on both views; `instrumented` renders `yes`/`no` from a scanned fixture artifact, `missing` for an absent path, `unknown` for an archive suffix; a variant-skipped entry appears under unused with its reason; rendered once under CI's 80-column terminal. The local-only invariant (§6): both views complete over a lab whose hosts' transport raises on connect, and a spy on the transport's connect records zero calls.
- **Docs**: `tests/unit/docs/test_getting_started_example.py` builds the worked example's `agent` entry through `KindRegistry.build` and asserts it is an `AgentBinary` that is a `DeclaredProduct`, and imports `gs_example.versions` **inside the test body** (the module registers its provider at import, which is the complete pattern the page shows; a module-scope import would run at collection inside the test-load guard) under the registry-isolation fixture (`tests/conftest.py`, the `_PRODUCT_PROVIDERS` snapshot), then calls its provider function on a fake host and asserts the per-version names.
- **Invariant**: every `[[products]]`/`[[dev_tools]]` entry in the shipped example projects parses under the new reserved-key rule (`docs/examples/**/.otto/settings.toml`).

## 9. Out of scope

- A variant on container products (`docker_image` kind entries may carry `variant` like any entry; the compose/image machinery does not change).
- Variants beyond the two names; a per-host variant.
- Cross-repo one-definition: two repos declaring one name keep today's ordering.
- `--list-products` reporting which variant a *provider* chose; providers are opaque.
- The broader docs restructure (todo §3) — this spec reorders one page and adds one.

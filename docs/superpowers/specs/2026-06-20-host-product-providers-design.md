# Host Product Providers — Design

**Date:** 2026-06-20
**Status:** Approved (brainstorm) — ready for implementation plan
**Supersedes:** the "lab-data product declaration (`ProductSpec` + `register_product`)"
future-follow-on note in
`docs/superpowers/specs/2026-06-19-host-ergonomics-design.md` §5.4 / §12.

## 1. Motivation

Phase 1 of the host-ergonomics work shipped code-injected products
(`UnixHost(..., products=[MyApp(...)])`). Three places in the tree then promised
a *future* path that declares products **in lab data** via a `ProductSpec`
boundary model + a `register_product` registry:

- `docs/guide/host-products.md` (the "Future" callout)
- `src/otto/host/product.py` (module docstring)
- `docs/superpowers/specs/2026-06-19-host-ergonomics-design.md` §5.4 / §12

**That direction is wrong and is hereby retracted.** Lab JSON files live in
product repos today only as a testing convenience. The long view is that lab
definitions move to a database or a JSON file defined **outside** the product
repo, so that the lab evolves independently of product code. Declaring products
*in* lab data would re-couple the two: reverting a product's behavior would
wrongly imply the lab must change too. It never should.

The correct model: **a product repo registers its products in its own repo
code, applied when otto ingests host objects.** Lab data stays
product-agnostic. This design makes that registration first-class and
"extremely simple."

## 2. Guiding principle — two kinds of host customization

The host already carries ~8 `register_*` extension hooks (os_profile,
`command_frame`, `term`, `transfer`, `filesystem`, `loader`, `power_control`,
plus monitor parsers / snmp metrics). Each new customizable axis costs five
coordinated edits: a registry dict, a `register_/build_` pair, a spec field, a
`field_validator`, and a `to_host` line. That proliferation is the symptom of
conflating two genuinely different things:

1. **Data-selectable, per-host operator options** — e.g. `transfer=sftp`,
   `term=ssh`. These belong in lab data; the registry earns its keep by feeding
   the JSON-schema enums (`src/otto/models/jsonschema.py`) and tab-completion
   (`collect_backend_names`). **Keep these.**
2. **Integrator behavior customization** — "my product installs like *this*",
   "my host reboots like *that*". This is *code*, owned by the integrator. It
   needs **no** registry-per-field, no schema enum, no `field_validator`, and no
   lab-data string. It is expressed by overriding a method on a host subclass,
   or by registering a function from an init module.

This split mirrors the decoupling philosophy in §1: **behavior lives in code;
lab data only picks per-host operator options.** A product is pure behavior, so
its customization is kind 2.

**Product providers are the first worked example of the kind-2 model.** Future
kind-2 axes follow this template (a code-registered function or a subclass
override) rather than minting another data-style registry. The broader work of
collapsing or relocating the existing kind-1/kind-2 registries is explicitly
**out of scope** here (see §9).

## 3. Mechanism

### 3.1 The provider registry

A registry of **functions** (predicates), not classes — because a provider is
code that *decides and constructs*, not a backend selected by name.

```python
# src/otto/host/product.py
from typing import Callable, Iterable

ProductProvider = Callable[["Host"], Iterable[Product] | None]
_PRODUCT_PROVIDERS: list[ProductProvider] = []

def register_product_provider(provider: ProductProvider) -> None:
    """Register a function that decides which products a host carries.

    Call from an init module listed in ``.otto/settings.toml`` — the same
    extension hook the other host strategies use. The provider runs once per
    lab-ingested host; inspect the host's product-agnostic attributes
    (``element``, ``element_id``, ``os_type``, ``id``, ``ip``, ``resources``)
    and return the products that host should carry (or ``None``/``[]`` for
    none).
    """
    _PRODUCT_PROVIDERS.append(provider)

def apply_product_providers(host: "Host") -> None:
    """Run every registered provider against *host*, attaching results to
    ``host.products`` (deduped by ``Product.name``)."""
```

`register_product_provider` and `ProductProvider` are re-exported from
`otto.host`, alongside the existing `Product` / `FileProduct` — mirroring how
`register_transfer_backend` is surfaced.

### 3.2 The application seam

`apply_product_providers(host)` is called at the **single ingest chokepoint** —
the end of `create_host_from_dict` (`src/otto/storage/factory.py`), right before
it returns the host:

```python
    spec = spec_cls.model_validate(merged)
    host = spec.to_host(cls)
    apply_product_providers(host)   # NEW
    return host
```

Every lab-ingest path routes through this one factory — the JSON repository
today, and the future external lab database. A code-constructed host
(`UnixHost(..., products=[...])`) bypasses the factory and keeps its explicit
list, which is correct.

**Ordering is already satisfied:** the CLI calls `get_repos()` →
`apply_repo_settings()` → `repo.import_init_modules()` (which registers
providers) *before* `load_lab()` runs the factory. Providers are present when
ingest happens.

### 3.3 Semantics

- **Aggregation:** all registered providers run, in registration order; their
  results are concatenated onto `host.products`.
- **Keying:** a provider keys only on **product-agnostic** host attributes
  (`element`, `element_id`, `os_type`, `id`, `ip`, `resources`). Lab data never
  names a product. (Lab *membership* is not a runtime host attribute — `labs`
  is consumed by the repository at filter time and not carried on the host — so
  it is not a keying axis; see §9.)
- **Dedup:** a returned product whose `name` already appears on the host is
  skipped with a debug log. This guards two overlapping providers (and a
  provider colliding with constructor-injected products) from double-staging.
- **Empty:** a provider returning `None` or `[]` is a no-op.
- **Errors:** a provider that raises is a hard error at ingest — fail-loud,
  consistent with the other failure modes of `create_host_from_dict`.
- **Signature:** the provider receives the **host only**. It does not receive
  the `OttoContext`; a provider that needs global context calls
  `get_context()` itself. This keeps the contract minimal.

### 3.4 Per-host product variance

When a product's parameters vary per host (version, artifact path, flags), the
provider sources that variance **itself** — from product-repo-owned config (its
own files, env, or the future external lab database), keyed on the host's
product-agnostic attributes. otto threads nothing extra: there is no new
host-model field and no lab-data change. (Considered and rejected: an opaque
`metadata` passthrough on the host model — deferred under YAGNI until a concrete
product needs it.)

## 4. Public surface

- New: `register_product_provider`, `apply_product_providers`, and the
  `ProductProvider` type alias in `src/otto/host/product.py`.
- Re-exported from `otto.host`: `register_product_provider`, `ProductProvider`.
- **No** `build_*` (providers are functions, not classes).
- **No** host-model field, **no** lab-data field, **no** `ProductSpec`, **no**
  schema enum, **no** tab-completion entry, **no** `field_validator`.

## 5. Documentation corrections (the original trigger)

Rewrite all three locations that assert the retracted "products-in-lab-data"
direction, replacing them with the §1 decoupling philosophy and the §2
code-customization model:

- `docs/guide/host-products.md` — replace the "Future" callout with a
  "Registering products from a product repo" section: a worked
  `register_product_provider` example plus the one-line rationale (lab data
  stays product-agnostic; the lab evolves independently of product code).
- `src/otto/host/product.py` — the module docstring's "lab-data declaration
  path … is a documented future follow-on" sentence.
- `docs/superpowers/specs/2026-06-19-host-ergonomics-design.md` — the §5.4
  follow-on note and the §12 "Lab-data product declaration" deferred item:
  point them at this spec and the code-customization model.

## 6. Testing strategy

Unit tests with a registry-reset fixture (mirroring the other registries'
fixtures) and mocked/fake hosts + fake `Product` doubles:

- register → `create_host_from_dict` ingest attaches the product;
- predicate filtering: a provider keyed on `os_type` / `element` attaches only
  to matching hosts;
- multiple providers aggregate, in registration order;
- dedup-by-`name` skips the duplicate (and logs at debug);
- a provider returning `None` / `[]` is a no-op;
- a provider that raises surfaces as an ingest error;
- integration through `create_host_from_dict` (not just the helper in
  isolation).

Plus a doctest-friendly `register_product_provider` example in
`docs/guide/host-products.md`. Live-tier validation is out of scope (no live
behavior changes); the unit gate (`make test`, `ty`, `make docs`) is the bar.

## 7. Backward compatibility

Fully additive. With no registered providers, `apply_product_providers` is a
no-op and every existing host ingests exactly as before. Constructor-injected
products are unaffected.

## 8. Components & boundaries

- `product.py` — owns the registry, the `ProductProvider` type, and
  `apply_product_providers`. Self-contained; depends only on `Product` and the
  `Host` type.
- `factory.py` — gains exactly one call site; no new responsibility beyond
  "apply registered providers to a freshly-ingested host."
- `otto.host.__init__` — re-export only.

No other module changes behavior.

## 9. Out of scope / follow-on

- **Registry unification (kind-1/kind-2 rationalization).** The inventory in §2
  and the code-vs-data reframing motivate a larger workstream: collapse the
  per-axis registries toward a single generic component mechanism for the
  data-selectable axes, and move kind-2 axes to the code-customization model
  this spec pilots. That is its own brainstorm → spec → plan cycle and must not
  expand this one.
- **Opaque host `metadata` passthrough** (§3.4) — deferred under YAGNI.
- **Lab-membership keying** — `labs` is not carried on the runtime host today,
  so providers cannot key on lab membership. If a concrete need appears, expose
  it (a host attribute, or pass the lab to the provider); it naturally rides
  with the external-lab-database work below. Deferred under YAGNI.
- **External lab database** as the eventual home of lab definitions — the
  motivating end-state (§1), but not built here.

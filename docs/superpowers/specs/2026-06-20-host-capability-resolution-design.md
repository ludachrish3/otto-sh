# Host Capability Resolution (menu + selection) — Design

**Date:** 2026-06-20
**Status:** Approved (brainstorm) — ready for implementation plan
**Relates to:** `docs/superpowers/specs/2026-06-19-host-ergonomics-design.md` (host strategy registries) and `docs/superpowers/specs/2026-06-20-host-product-providers-design.md` (the code-vs-data customization split this extends).

## 1. Motivation

otto's host strategy fields split into two kinds (established in the
product-providers design): **lab-driven** facts (hardware/firmware/bed wiring,
static per host) and **product-driven** behavior (the software under test, in
code). The protocol fields `term` (session transport) and `transfer` (file
transfer) are lab-driven — a property of how you reach the physical box — but
today the lab declares a **single** chosen value, which conflates two separate
things:

- **What the host supports** (a bed/firmware fact): a box might accept both ssh
  and telnet, or scp and nc.
- **Which one a given product/test uses** (a product/test choice).

Forcing one scalar into the lab couples those. The fix: the **lab declares the
menu** of protocols a host supports; **product/test code selects** from it. This
is the first instance of a general *capability resolution* mechanism; the end
state is product repos defaulting most host options this way (see §12).

## 2. Scope

In scope: the menu + selection mechanism, applied to **`term` and `transfer`**,
plus a **general** product-preference settings seam (not hard-coded to those two
fields) and the per-instance override path.

Out of scope (follow-ons): the single-valued lab attributes
`filesystem`/`command_frame`/`loader` (they stay as-is — firmware facts, no
menu); the N→1 registry consolidation; extending product-repo defaulting to
*all* options/values (§12 names the direction).

## 3. The menu (lab data)

The lab declares the menu in the **plural** fields `valid_terms` /
`valid_transfers` (chosen name; the terser `terms`/`transfers` is an option).
Each accepts a **scalar or a list**, and the value **is the closed menu** —
only listed protocols are selectable:

- `"valid_transfers": "scp"` → menu `["scp"]` (1-element closed menu)
- `"valid_transfers": ["scp", "nc"]` → menu `["scp", "nc"]`, lab default = first (`scp`)
- field **omitted** → the **family-default menu** (§3.1)

The singular `term` / `transfer` is the **active** selection (§6); the lab does
not author it — it is resolved at build (§4). A `field_validator` validates
**each** menu entry against the registry and the host family (today's per-value
check, applied element-wise), rejects an empty list, and preserves order (first
= lab default). There is **no "open" semantics**: a selection outside the menu
is always an error.

### 3.1 Family-default menus

When `term`/`transfer` is omitted, the menu comes from the host family
(spec-class default; a profile's `defaults` may override it, per the existing
host > profile > spec precedence):

| Family | `valid_terms` default | `valid_transfers` default |
|--------|---------------------|--------------------------|
| unix | `[ssh, telnet]` | `[scp, sftp, ftp, nc]` |
| embedded | `[telnet]` | `[console]` |

Active default with no product preference = first entry (unix → ssh/scp;
embedded → telnet/console).

**Open item (flagged):** embedded hosts have no `term` field today (telnet-only,
[embedded_host.py](src/otto/host/embedded_host.py)). Giving embedded a `term`
menu of `[telnet]` makes the two families uniform but adds a 1-element menu with
no alternative. Decision recorded as: **add it for uniformity** unless reviewer
prefers leaving embedded term as fixed-telnet.

## 4. Resolution of the active protocol

At host build (`create_host_from_dict`, via the resolver in §7):

```
active = first product-preferred protocol that is in the host's menu,
         else the lab default (first in the menu)
```

Product-preference auto-resolution **never exceeds the declared menu** — a host
whose menu is `[scp]` stays on `scp` regardless of preference. The built host
carries the resolved **active** scalar plus the **menu** (§6).

## 5. Per-instance selection (the insulated override)

Changing a host's active protocol is done through the **existing override-copy
seam**, extended to `term`/`transfer`:

- `_apply_option_overrides` ([configmodule.py](src/otto/configmodule/configmodule.py))
  and `get_host` / `all_hosts` / `do_for_all_hosts` gain `term=` / `transfer=`
  parameters.
- An override returns a `dataclasses.replace` **copy** whose `__post_init__`
  rebuilds the connection/file-transfer backend (via the registry
  `create(ctx)` seam) for the chosen protocol. The shared `lab.hosts` instance
  and every other consumer are untouched — this is the **insulation**: different
  instances of one host can run different protocols, concurrency-safe by
  construction.
- The override sets the active singular `term`/`transfer`; `__post_init__`
  validates it is in `valid_terms`/`valid_transfers` and rebuilds — otherwise a
  fail-loud `ValueError` naming the menu.

This is the *only* way to change a host's active protocol. The in-place mutators
`set_term_type` / `set_transfer_type` are **removed** (§9).

## 6. Runtime host surface — active vs. menu

Distinct names cleanly separate the two concepts and resolve the earlier
drift-guard tension:

- `term: str` / `transfer: str` — the **active** selection. Backends build from
  these in `__post_init__`; the override-copy (§5) replaces them.
- `valid_terms: list[str]` / `valid_transfers: list[str]` — the **menu** (the
  lab-authored capability set; family default when omitted).

Both pairs are real fields on **both** the host and its spec, so each host field
maps directly to a spec field of the same name and the runtime↔spec drift guard
(`tests/unit/models/test_host_specs.py`) holds **without special-casing** the
active-vs-menu relationship. The menu (`valid_*`) is lab-authored; the active
(`term`/`transfer`) is resolved at build (§4) — the plan decides whether the
active is a guard-recognized resolved field or an optional in-spec pin.

## 7. CapabilityResolver (Approach A)

A small reusable helper, `host/capability.py`:

```python
class CapabilityResolver:
    """Resolve a host's active selection for one menu-style capability."""
    def resolve_active(self, menu: list[str], preference: list[str] | None) -> str: ...
    def validate_choice(self, menu: list[str], choice: str) -> str: ...  # fail-loud if not in menu
```

`term` and `transfer` stay named fields wired to one resolver instance each. A
future menu-style field opts in by instantiating the resolver — no framework,
no big-bang. The resolver is field-agnostic (it operates on `(menu,
preference, choice)`), which is what keeps the mechanism general.

## 8. Product preferences (settings, declarative & general)

A `[host_preferences]` table in a product repo's `.otto/settings.toml`,
validated through `SettingsModel` (extra='forbid'). The block is named for
**hosts generally** — not just protocols — because it is the home for product
host-level preferences now and as they expand (§12). This spec populates it with
the protocol-selection preferences, keyed by capability name with an ordered
list value:

```toml
[host_preferences]
transfer = ["sftp", "scp"]
term = ["ssh"]
```

Threaded into `create_host_from_dict` alongside `host_defaults`; multi-repo
precedence reuses the existing repo-settings reduction order (later repos overlay
earlier). The protocol entries are **capability-keyed, not
term/transfer-hardcoded**, so new menu-style capabilities need no
settings-schema change. Entries are validated against the registry; an entry not
in a given host's menu is simply skipped during intersection (it's a preference,
not a demand).

This sits beside the existing `[host_defaults]` mechanism (which defaults option
*values* like `ssh_options`); §12's end state converges product host-level
preferences and defaults under this general umbrella.

## 9. Removed: in-place setters

`UnixHost.set_term_type` / `set_transfer_type`
([unix_host.py:384-412](src/otto/host/unix_host.py#L384-L412)) are **removed** —
they mutate a shared instance (the leak this design prevents). Callers migrate
to the override-copy seam:

- `cli/host.py:149,155` (the `otto host` command's protocol switch) → resolve the
  host with the chosen protocol via the `get_host(..., term=/transfer=)`
  override instead of mutating in place.
- Tests exercising the setters (`test_host_backend_construction.py`,
  `test_term_registry.py`, `test_host.py`, `cli/conftest.py` notes) → rewritten
  against the override-copy seam.
- `rebuild_connections` docstring references → updated.

## 10. Schema & completion

`models/jsonschema.py`: `term`/`transfer` become `string | array<string>` with
the per-entry registry-derived enum preserved on both branches.
`collect_backend_names` / the completion cache are unaffected (they enumerate
backend names; the field can now hold several). Tab-completion still completes
individual protocol names.

## 11. Interface churn (no active users; churn accepted)

- New `valid_terms`/`valid_transfers` fields (spec + host), `str | list[str]`
  normalized to a menu list; `term`/`transfer` become the resolved active scalar.
- Family defaults change from scalar values to menus (§3.1).
- Existing `hosts.json` singular `transfer: "scp"` entries migrate to
  `valid_transfers: ["scp"]` (or scalar `"scp"`).
- `set_term_type` / `set_transfer_type` **removed**; `cli/host.py` migrated; ~8
  tests rewritten; docstrings updated.
- Embedded gains a `term`/`valid_terms` field, menu `[telnet]` (flagged, §3.1).
- New `[host_preferences]` settings table (new `SettingsModel` field).
- `_apply_option_overrides` + `get_host`/`all_hosts`/`do_for_all_hosts` gain
  `term`/`transfer` parameters.
- Lab entries that name a non-default protocol as a genuine bed capability stay
  in `valid_*`; where such a value is really a *product* choice, it migrates to
  that product's `[host_preferences]`.

## 12. Future direction (end state, not this spec)

Product repos should be able to default **most** host options from their own
repo, uniformly: menu **selections** (this spec: term/transfer, extensible to
other menu-style fields via the resolver) and option **values** (already
`[host_defaults]`: ssh_options, ports, etc.). The end state converges these into
a coherent "product host-defaults" story. This spec delivers the selection half
and the general capability/preference seam; broadening to all options is
sequenced separately.

Recorded as a standalone backlog item for pickup:
`todo/host-preferences-end-state.md` (ties together this spec, the existing
`[host_defaults]` option-value mechanism, and the registry-consolidation track).

## 13. Components & boundaries

- `host/capability.py` (new) — `CapabilityResolver`; field-agnostic.
- `models/host.py` — `valid_terms`/`valid_transfers` accept scalar|list;
  family-default menus; element-wise validators; resolved active
  `term`/`transfer` mirrored to the runtime host.
- `models/settings.py` — `[host_preferences]` on `SettingsModel`.
- `storage/factory.py` — resolve active via resolver + preference at build.
- `host/unix_host.py` (+ embedded) — `valid_*` menu fields; remove setters;
  `__post_init__` builds from the active `term`/`transfer` (unchanged).
- `configmodule/configmodule.py` + `context.py` — extend override seam with
  `term`/`transfer`.
- `models/jsonschema.py` — scalar-or-list schema.
- `cli/host.py` — migrate to override-copy.

## 14. Testing strategy

- `CapabilityResolver` unit tests: default resolution (lab first-in-list),
  preference intersection (in-menu wins, out-of-menu skipped), `validate_choice`
  fail-loud, single- vs multi-element menus.
- Spec validation: scalar normalizes to 1-menu; list menu; bad entry rejected;
  empty list rejected; family-default menus applied on omission.
- Factory resolution: active with/without preference; preference capped by menu.
- Override-copy insulation: `get_host(..., transfer=…)` returns a copy that
  switched + rebuilt its backend while the shared instance is unchanged;
  out-of-menu override fails loud.
- Schema: `term`/`transfer` validate as scalar and as list; enum enforced.
- CLI: `otto host` protocol selection goes through the override path.
- Full gate: `make test`, `ty check src`, `make docs`.

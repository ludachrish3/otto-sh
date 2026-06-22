# Host CLI documentation restructure — nested "Hosts" group (design)

**Status:** approved (brainstorm 2026-06-21), ready for implementation plan.
**Scope:** the User Guide host pages only (`docs/guide/host*.md`). No source code
changes; no autodoc (`docs/api/host/*`) changes.
**Builds on:** the host-ergonomics workstream (the five small capability guides —
`host-power`, `host-privilege`, `host-products`, `host-file-ops`,
`host-dynamic-cli`) and the capability-resolution / host-preferences work that
shaped `host.md`'s connection-options and term/transfer content.

## 1. Goal

The host CLI documentation has grown into a flat sprawl. Six host pages sit as
sibling entries in the User Guide toctree, interleaved with unrelated guides,
and the primary `docs/guide/host.md` is a single 462-line page that braids
together three unrelated concerns. Reorganize this into **one nested "Hosts"
group** whose pages are split along those concerns, while presenting the CLI's
two verb mechanisms as a single unified mental model and trimming the
guide/API duplication.

Two distinct "flatness" problems are in scope:

- **Macro flatness** — 6 host pages as flat toctree siblings, ungrouped.
- **Micro flatness** — `host.md` is one long scroll mixing core CLI usage,
  `hosts.json` configuration reference, and a programmatic-API tail.

## 2. Current state

User Guide host pages (`docs/guide/`):

| Page | Lines | Really about |
|------|------:|--------------|
| `host.md` | 462 | `otto host` — but mixes core verbs, connection control, `hosts.json` config reference, and a Python tail |
| `host-products.md` | 66 | product lifecycle methods (`stage`/`install`/…) |
| `host-power.md` | 41 | `power`/`reboot`/`shutdown` + reachability |
| `host-dynamic-cli.md` | 30 | how `@cli_exposed` methods become CLI verbs |
| `host-file-ops.md` | 25 | `exists`/`ls`/`mkdir`/… remote file ops |
| `host-privilege.md` | 22 | `run(sudo=True)` / `as_user` (Python-only) |

All six are flat entries in `docs/guide/index.rst`'s toctree. `cli-reference.md`
additionally carries a terse `## otto host` option/subcommand table (left as-is).

The API reference (`docs/api/host/*.rst`) is **pure autodoc** (`.. automodule::`),
so every public host method already has its signature and docstring
auto-documented. The five small guides therefore partly *duplicate* the API: the
terse method-signature tables re-state autodoc, while the genuine value-add is the
conceptual narrative.

### 2.1 Two CLI verb mechanisms (established during the brainstorm)

`otto host <id> <verb>` is produced by **two separate code paths**:

- **Hardcoded** (`src/otto/cli/host.py`): `run`, `put`, `get`, `login` — explicit
  `host_app.command(...)` registrations wrapping `host.run/put/get/interact`.
- **Dynamic** (`@cli_exposed` → `HostGroup` in `src/otto/cli/expose.py`):
  `stage`, `install`, `uninstall`, `is_installed`, `is_uninstalled`, `power`,
  `reboot`, `shutdown`, `exists`, `ls`, `mkdir`, `rm`, `cp`, `mv`, `read_file`,
  `write_file` (+ embedded), scoped per host class.

The four core verbs are hardcoded for concrete UX reasons the generic exposer
cannot currently express: `put`/`get`'s `src… dest` arg shape, `run`'s rich
`RunResult` rendering, and `login`'s interactive raw-mode PTY bridge. **Converging
the two paths is explicitly out of scope** (see §9) — this restructure presents
them as one mental model in prose only.

### 2.2 Which capabilities are CLI-reachable

Not all "capabilities" reach the CLI. Power/products/file-ops are dual CLI+Python;
**privilege (`run(sudo=True)`, `as_user`, `switch_user`) and reachability
(`is_reachable`, `wait_until_up/down`) are Python-only.** The restructured docs
must surface this rather than imply uniform CLI exposure.

## 3. Locked decisions (from the brainstorm)

1. **Structure: by concern.** Split `host.md`'s braid into *commands* (what you
   run), *connection control* (per-invocation flags), and *configuration*
   (persistent `hosts.json` reference). Group all host pages under one nested
   "Hosts" entry.
2. **Docs-only unification of the verb model.** Present `run`/`put`/`get`/`login`
   and the capability verbs as one "host verbs" family, with one honest sentence
   noting the four core verbs are built-in and the rest are auto-exposed. No code
   convergence.
3. **On-disk layout: subdirectory `docs/guide/host/`.** Cleaner namespacing
   (`host/configuration`); churn is low because external refs are label-based.
4. **Capabilities consolidate into ONE page** with five `##` sections — five
   25-line nav entries *is* the sprawl.
5. **Netcat gets its own subpage under Core commands.** Netcat is the one
   genuinely otto-custom transfer backend; it lives next to the file-transfer
   commands as a nested subpage, not in the generic configuration page.
6. **Guide/API boundary cleanup.** On the Capabilities page, replace duplicative
   method-signature tables with narrative + `{meth}`/`{class}` cross-refs into the
   existing autodoc. Guide keeps concepts; API keeps signatures.

## 4. Target structure

```text
Hosts/  (docs/guide/host/index.md)
├── Overview                         host/index.md
├── Core commands/                   host/commands/index.md   (sub-group; landing IS the commands page)
│   ├── run / put / get / login      host/commands/index.md
│   └── Netcat transfers             host/commands/netcat.md
├── Host capabilities                host/capabilities.md     (5 ## sections)
├── Connection control               host/connections.md
└── Host configuration               host/configuration.md
```

`docs/guide/index.rst`'s toctree replaces the six flat entries (`host`,
`host-dynamic-cli`, `host-privilege`, `host-products`, `host-power`,
`host-file-ops`) with a single `host/index` entry. `host/index.md` carries the
nested toctree to its children; `host/commands/index.md` carries a nested toctree
to `netcat`.

File count is 6 → 6 (index, commands/index, commands/netcat, capabilities,
connections, configuration), so the win is **structural, not fewer files**: all
host pages collapse under one sidebar entry (≤ three nav levels:
Hosts → Core commands → Netcat transfers), `host.md`'s braid splits along its
three concerns, and the five tiny capability guides consolidate into one page.

## 5. Content mapping (old → new)

### 5.1 `host/index.md` — Overview

From `host.md`: §Syntax, §Listing hosts (`--list-hosts`), §Dry run (`--dry-run`),
§Programmatic equivalents (the tested doctest + `put`/`get` Python examples).

Adds the **unified verb model** framing:

> Every `otto host` action is a verb on the host. The four core verbs — `run`,
> `put`, `get`, `login` — are built in; the rest are *capability verbs*,
> auto-exposed from `@cli_exposed` host methods and scoped to each host's class.
> `otto host <id> --help` lists exactly the verbs that host supports.

Closes with a short **"From Python"** section: the preserved doctest (must keep
passing under `make docs` doctest) and pointers to {doc}`library-usage` and the
API reference. Carries the nested toctree to the child pages.

### 5.2 `host/commands/index.md` — Core commands

From `host.md`: §Running commands (`run`), §Uploading files (`put`),
§Downloading files (`get`), §Interactive login (`login`, incl. the raw-mode
bridge, `Ctrl+]` escape, `SIGWINCH`/NAWS resize, and `--hop` support note).
Links to the Netcat subpage ("file transfers use scp by default; for the netcat
backend see Netcat transfers") and carries the nested toctree to it.

### 5.3 `host/commands/netcat.md` — Netcat transfers

All netcat-specific content, consolidated:

- The `nc` transfer backend (`--transfer nc`, `transfer: "nc"`).
- **Port-finding strategies** (`nc_port_strategy`: auto/ss/netstat/python/proc/custom)
  — moved from `host.md` §Netcat port and listener strategies.
- **Listener-check strategies** (`nc_listener_check`) — same source.
- The reversed-listener GET mechanism.
- **Netcat through hops** (SSH port-forward detail) — moved from `host.md`
  §File transfer protocols through hops (which keeps only a one-line netcat pointer).
- The `nc_options` fields (`exec_name`, `port`, the strategy/cmd overrides).

### 5.4 `host/capabilities.md` — Host capabilities

Merges all five small guides into one page with five `##` sections:
power/reboot/reachability, products & lifecycle, remote file operations,
privilege elevation, and "how methods become CLI verbs (`@cli_exposed`)". Opens
with a short table: **capability → CLI-exposed?** — surfacing that power/products/
file-ops are dual CLI+Python while privilege and reachability are Python-only.
Boundary cleanup per §6.

### 5.5 `host/connections.md` — Connection control

From `host.md`: §Reaching hosts through hops (`--hop`, hop chaining, `hop` in
`hosts.json`), §File transfer protocols through hops (with the netcat paragraph
reduced to a one-liner pointing at `host/commands/netcat.md`), §Overriding
protocol for a single session (`--term`/`--transfer`, valid values, embedded note).

### 5.6 `host/configuration.md` — Host configuration (hosts.json)

From `host.md`: §Connection options (the six `*_options` objects, the four-layer
precedence, per-key merging) — **carries the `(connection-options)=` anchor** —
plus the SSH (incl. port forwarding), Telnet, and SFTP/SCP/FTP option subsections.
The `nc_options` row in the six-object overview table becomes a one-line pointer
to `host/commands/netcat.md`. Also §Per-host toolchain — **carries the
`(per-host-toolchain)=` anchor** — and the persistent term/transfer defaults notes.

## 6. Guide/API boundary cleanup

On `host/capabilities.md`, replace the duplicative method-signature tables (e.g.
the file-ops and product-lifecycle tables that restate autodoc) with conceptual
narrative plus `{meth}`/`{class}` cross-references into the existing autodoc.
Retain the genuine concepts: the pluggable `PowerController`, the product strategy
pattern and `register_product_provider`, the sudo-wrapping/`as_user` session
behavior, and the embedded-host subset caveats. No file under `docs/api/` is
edited; the API remains the single source of exhaustive signatures.

## 7. Cross-references and anchors

Only two label-based anchors are referenced from outside the host pages, and both
must be preserved (MyST `{ref}` resolves by label regardless of file, so moving
their content is safe):

- `(connection-options)=` — referenced from `docs/cookbook/connection-options.md`
  — lands on `host/configuration.md`.
- `(per-host-toolchain)=` — referenced from `docs/guide/coverage.md` — lands on
  `host/configuration.md`.

`host.md` also has outbound `{ref}` targets — the `per-call-overrides` label
(defined in the cookbook) and the `host-preferences` label (defined in
`lab-config.md`). These are unaffected by the move; carry them to wherever that
prose lands.

The only structural reference to edit is `docs/guide/index.rst`'s toctree
(six entries → one `host/index`). A repo-wide grep for `{doc}` references to the
old bare names (`host`, `host-power`, etc.) must confirm none exist in live docs
before deleting the old files (the brainstorm found none outside
`docs/superpowers/`).

## 8. Implementation notes

- This is a **content move**, not a rewrite: migrate prose verbatim where
  possible, preserving the tested doctest and all JSON/code examples exactly.
- Rebase on the current working-tree content of `host.md` and the five small
  guides (they carry recent feature work — product providers, the unified
  `[host_preferences]` block); do not regress that material during the move.
- Use `git mv` semantics conceptually, but since content is being split across new
  files, create the new `host/` pages and delete the old flat files in one change.

## 9. Out of scope (YAGNI)

- **Code convergence** of `run`/`put`/`get`/`login` onto `@cli_exposed`
  (separate engineering spec; real arg-shape/result-rendering/interactivity
  friction — see §2.1).
- **Autodoc / API page** changes (`docs/api/host/*`).
- `cli-reference.md`'s `## otto host` table (stays as the terse flag reference).
- Restructuring any non-host guide.

## 10. Testing / verification

- `make docs` builds clean (no warnings) — in particular no broken `{doc}`/`{ref}`
  cross-references and no toctree orphans.
- The Overview's doctest passes under the docs doctest run (`make docs` /
  doctest target).
- Manual sidebar check: the "Hosts" group expands to Overview, Core commands
  (→ Netcat transfers), Host capabilities, Connection control, Host configuration.
- Spot-check the two preserved anchors resolve from the cookbook and coverage guide.

## 11. Open questions

None. (Nesting depth, capabilities consolidation, and netcat placement were
resolved during the brainstorm.)

# Match tables accept a host's fixed facts, and a guard keeps the keys in step with the host model: #1312

**Status:** design for approval, 2026-11-24. Checked once by OpenAI gpt-6-astra and Claude Fable 5.2, independently. Neither found a blocker and they did not disagree. Their findings are applied, except the two listed under "Review findings not taken".

## Why

Setting up a new project, the maintainer wrote `match = { board = "qemu", is_virtual = true }` in a `[[products]]` entry. Bootstrap refused it: `unknown match key 'board'`.

The matchable keys are a hand-written set, `MATCH_KEYS` in `src/otto/declared.py`. Nothing ties it to the host model, so it fell behind it. The lookup is a plain attribute walk on the host, and the runtime host already carries the refused facts. Only the set blocks them.

The larger worry is drift: a host field added or renamed later must not be missed again.

## Rulings (Chris, 2026-11-24)

1. **First-round keys:** `board`, `is_virtual`, `slot`, `site`, `rack`, `shelf`, `hw_version`, `sw_version`, `name`, the unix family's `docker_capable`, and the embedded family's `filesystem` and `loader`.
2. **`hop` is not matchable.**
3. **Two lists.** The matchable keys stay separate from the templating sets (`TEMPLATE_PUBLIC_FIELDS` / `TEMPLATE_PRIVATE_FIELDS` in `src/otto/inventory/snapshot.py`), each with its own guard.
4. **Purely additive.** Anything that would break an existing settings file, lab file or import goes to Chris before it is built.

## Goals

- Every key in ruling 1 is accepted in a match table and matches the host's own field.
- A field added later to a built-in host spec or to the element spec cannot be left unclassified: a test fails until someone decides whether it is matchable.
- A matchable key cannot stop resolving on a real host without a test failing.
- The documented key list cannot fall behind the set, in either direction.

## Not goals

- No change to match value rules (regex, version specifier, equality, any-of).
- No change to `metadata.<key>` / `element.metadata.<key>` paths.
- No run-time check of a project's own registered host specs. The guard is a test over otto's specs only. A load-time check would make a project's custom spec with unclassified fields start failing, which is a break (ruling 4).
- No merge of the matching and templating sets (ruling 3).

## Design

### The keys

`MATCH_KEYS` keeps its name, its type (`frozenset[str]`) and its home in `otto.declared`, and gains the twelve keys of ruling 1, for twenty in all. The unknown-key error already prints `sorted(MATCH_KEYS)`, so it follows.

Ten of the twelve are plain attributes on the runtime host, and `_resolve_key`'s existing walk reads them unchanged.

### What some keys read (for Chris to check)

These follow from the code as it stands. None is a break, but each is behaviour a user will meet:

- **`name` is never unset.** A lab entry with no `name` gets a generated label from its element, board and slot (`src/otto/host/remote_host.py`). A match on `name` reads that label.
- **`is_virtual = true` matches every container host.** Declared products also apply to `DockerContainerHost`, which no lab spec builds, and it sets `is_virtual = True`. Its `name` is `<parent>:<service>`.
- **`site` and `rack` written as digit strings are integers** on the host (`IntOrStr` in `src/otto/host/specs.py`). A regex still matches them through `str()`.
- **Bool and number values compare by `==`**, the existing rule. So `slot = true` matches slot 1, and `is_virtual = "true"` is a regex against `"True"` and never matches. The docs table gives each key's type to head this off.

### `filesystem` and `loader` (my call, for Chris to check)

On the runtime host these two are objects, not strings: `EmbeddedHost.filesystem` is an `EmbeddedFileSystem` and `EmbeddedHost.loader` is a `BinaryLoader | None`. A pattern written as the registered name would not match `str(object)`.

A match table names them the way the lab file does, by registered name: `filesystem = "littlefs"`, `loader = "llext-hex"`. Both classes carry that name as `type_name`, and registration enforces that it equals the registered name. So one small table in `otto.declared`, beside `MATCH_KEYS`, maps these two keys to the path the existing walk reads (`filesystem` to `filesystem.type_name`, `loader` to `loader.type_name`). No other key is in that table, and metadata paths are untouched.

The `MATCH_KEYS` docstring and `_resolve_key`'s docstring say "a key IS the path on the host". Both, and the docs' Matching section, gain this one exception.

Consequences, each following the existing rules:

- A host with no filesystem has `NoFileSystem`, whose name is `none`, so `filesystem = "none"` matches it, as in the lab file.
- A host with no loader has `loader = None`, and an absent value never matches.
- A unix host has neither attribute, so neither key matches it. `docker_capable` on an embedded host is the same case.
- A project's custom host class that stores `filesystem` as something without a `type_name` never matches on it.

`command_frame` and `landing_frame` have the same object shape. They are deliberately not matchable in this round.

### The guard

Four unit tests, in `tests/unit/test_declared.py` or a module beside it.

**1. Every lab-stated field is classified.** The test walks `registered_host_specs(builtins_only=True)` (the walk `tests/unit/docs/test_lab_config_field_coverage.py` already uses), by registry name so Zephyr is included, and collects every field of every spec. It also collects `ElementSpec`'s fields, each as `element.<field>`. Each must be in exactly one of:

- `MATCH_KEYS`, or
- a not-matchable set, written out in the test module with one short reason per group: credentials, the option tables, selections (`term`, `transfer`, `impairer` and their `valid_*` lists), tuning (`init_timeout`, `max_filename_len`, ...), object-valued fields left out for now (`command_frame`, `landing_frame`), and structured values a scalar cannot match (`interfaces`, `resources`, `toolchain`, `element.labs`, `element.hosts`, ...). `hop` is here, citing ruling 2. `metadata` and `element.metadata` are here too: they are reached by dotted path, not as keys.

A field name is classified once for every spec that has it, so it must mean the same thing on each.

The test also fails on a stale entry: a not-matchable name that is no longer a field of any spec, and a `MATCH_KEYS` entry that is neither a spec field, nor an element field, nor one of the two runtime-only keys (`id`, `source_lab`), which the test names explicitly.

The not-matchable set lives in the test, not in `src`: no product code reads it, and keeping it out adds no public name.

**2. An object-valued key is in the path table.** Using the existing `field_kind` (`src/otto/host/construction.py`): every `MATCH_KEYS` entry that is a spec field of kind `Convert` must be in the key-to-path table, and every entry of that table must be such a field. A future key whose host value is an object then fails here, without depending on test 3's values. If a current scalar key turns out to be a `Convert` field, the builder narrows this rule and says how in the test's docstring.

**3. Every matchable key resolves on a real host.** For each built-in registry name, the test builds a host through the factory path a lab file takes (`create_host_from_dict`, which also stamps `source_lab`), with every matchable field that family has set to a non-default value. For each key it asserts `host_matches({key: value}, host)` and one non-match with a different value. Keys the family lacks must not match. A second host per family, with only the required fields, pins what unset keys read as (`name`'s generated label, `filesystem = "none"`, `is_virtual = false`). One container host case pins `is_virtual` and `name` on `DockerContainerHost`.

**4. The docs name exactly the keys.** The Matching section of `docs/configuration/declared-products-tools.md` gets a table with one row per key, replacing the inline list. Its columns are the key, its type, which hosts have it, and what it reads when the lab entry does not set it. A test scopes to that section, reads the first column with the precedent's row-start match (so `id` is not satisfied by `element.id`), and asserts the rows equal `MATCH_KEYS` with no duplicates.

### Existing tests that change

- `test_unknown_key_raises_naming_the_key_and_the_valid_set` and `test_validate_rejects_malformed_tables` in `tests/unit/test_declared.py`, and the parametrized case at `tests/unit/models/test_settings.py:904`, use `is_virtual` as their unknown key. They switch to `hop`, a real field that stays refused.
- `test_match_keys_is_the_documented_provider_surface` pins the old eight keys. It pins the new twenty.

### Other sites

- `src/otto/init/templates.py:66` teaches `match = { "metadata.hw_version" = "rev2" }`. With `hw_version` now a key, that example points users at the metadata dict for a fact the host has. It changes to a metadata key that is not a host field.
- `docs/extend/cli-exposed-verbs.md:212` and the provider docstrings in `src/otto/host/product.py` and `dev_tool.py` list host attributes for provider *code*, which may read anything on the host, including `element.resources`. That is a different contract from match keys. They are left as they are unless a line claims to be the match key list.
- Before closing, grep once more for any other site that restates the match key list.

## Approaches considered

1. **Classified sets with a test guard (chosen).** Explicit, matches the templating precedent, and a new field forces a decision. Cost: one line per new host field.
2. **Derive the keys from the spec's scalar fields.** No list to maintain, but every new scalar field becomes matchable by default, including ones that should not be (`hop`, timeouts), and a field's type change silently changes the surface. The precedent chose "never exposed by default" for this reason.
3. **Mark matchable fields on the spec** (field metadata). One place per field, but `otto.declared` is import-light by design and must not import the host specs, and a project's custom spec would need the marker too.

## Compatibility

Additive. Every key accepted today keeps its meaning, and no valid settings file, lab file or import changes. `MATCH_KEYS` keeps its name and type and is in no API dump. Two things are observably different: the set's contents grow from eight to twenty, and the unknown-key message lists twenty names. No `!` on the commit.

## Review findings not taken

- **Classify runtime-only host fields by walking the host classes.** Not taken: those fields are session state (`products`, `inventory_ref`, ...), not fixed facts of a host. The goal is narrowed to say so: the guard covers what a lab file can state. `id` and `source_lab` stay hand-named in test 1.
- **Run the guard against an isolated stock registry**, in case a test overwrites a built-in spec. Not taken as a requirement: the docs field-coverage guard makes the same walk without it. The builder uses an isolation fixture if one already exists.

## Left to the builder

- The exact grouping and wording of the not-matchable set's reasons.
- Whether the four tests share a module with the existing `test_declared.py` cases.
- The values used to populate each family's host in test 3.

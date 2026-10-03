# Programmatic products: providers

A `[[products]]` entry is one product. When the products a host carries must
be computed — one per version in the host's metadata, a different artifact
under `--field` than under `--debug`, a set a match table cannot express — the
project registers a **provider**: a function otto runs once per host as the
lab is ingested, returning the products that host should carry.

```{literalinclude} ../../examples/getting-started/libs/gs_example/versions.py
:language: python
:start-after: "# doc: begin provider"
:end-before: "# doc: end provider"
```

List the module in `init` in `.otto/settings.toml` so the registration runs.
The pattern: key on the host's product-agnostic attributes (`os_type`,
`element.metadata`, `id`), source versions and artifact paths from the
project's own configuration, build one {class}`~otto.host.declared_product.DeclaredProduct`
(or a subclass) per product, and read {func}`otto.context.variant` where the
run's variant matters — the registry does not consult `variant` for a provider,
the provider decides. A provider also anchors its own paths, as the example's
`ROOT` does: otto anchors an entry's `artifact` to its repo, never a path a
provider builds.

A provider's name must not be one a `[[products]]` entry declares: a name is
defined in data **or** in code, and the lab refuses to load when both define
one. The rule, and what two providers returning one name do, are in
{ref}`one-definition`.

Registering a provider makes the `[project]` table required
({ref}`project-scope-required`); otto does not call a provider for a host its
repo did not declare. The dev-tool twin is
{func}`~otto.host.dev_tool.register_dev_tool_provider`.

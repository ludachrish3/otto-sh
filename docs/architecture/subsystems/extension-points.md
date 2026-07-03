# Extension points

Downstream repos extend otto from **init modules** — ordinary Python modules
named in the `init` list of `.otto/settings.toml`, imported during bootstrap
phase 2 ({doc}`../lifecycles/index`). Registration is import-time and side-effect
based: an init module calls `register_*` functions (or applies decorators),
and from then on the new component behaves exactly like a built-in — same
registries, same CLI listing and completion, same error messages
({doc}`registries`).

## The seams

| You want to add | Register with | Guide |
| --- | --- | --- |
| an `otto run` subcommand | {func}`@instruction() <otto.cli.run.instruction>` | {doc}`../../guide/run` |
| an `otto test` suite | `Test`-prefixed {class}`~otto.suite.suite.OttoSuite` subclass (auto-registers) | {doc}`../../guide/test` |
| a top-level `otto` command | {func}`otto.register_cli_command <otto.cli.registry.register_cli_command>` / {func}`@otto.cli_command <otto.cli.registry.cli_command>` | {doc}`../../guide/extending-cli` |
| a CLI verb on a host class | `@cli_exposed` on the method | {doc}`../../guide/host/capabilities` |
| a host class (new `os_type` base) | `register_host_class` | {doc}`../../guide/os-profiles` |
| an OS profile (defaults bundle) | `register_os_profile` or `[[os_profiles]]` in settings | {doc}`../../guide/os-profiles` |
| a connection (term) backend | `register_term_backend` | {doc}`../../guide/extending-backends` |
| a file-transfer backend | `register_transfer_backend` | {doc}`../../guide/extending-backends` |
| a shell dialect | `register_command_frame` | {doc}`../../guide/extending-embedded` |
| an embedded binary loader | `register_binary_loader` | {doc}`../../guide/extending-embedded` |
| an embedded filesystem type | `register_filesystem` | {doc}`../../guide/extending-embedded` |
| a power controller | `register_power_controller` | {doc}`../../guide/extending-backends` |
| products on hosts | `register_product_provider` | {doc}`../../guide/host/capabilities` |
| a host source (lab repository) | {func}`otto.storage.register_lab_repository` | {doc}`../../guide/host-database` |
| a reservation backend | `register_reservation_backend` | {doc}`../../guide/reservations` |
| per-host monitor parsers | `register_host_parsers` | {doc}`../../guide/monitor` |
| SNMP metric descriptors | `register_snmp_metric` | {doc}`../../guide/monitor` |

Options classes deserve a mention even though they aren't a registry: a
repo-wide `@options` class shared by instructions and suites is the standard
way to give a whole project consistent CLI flags ({doc}`../../guide/options`).

## What keeps third-party code honest

- **Symmetry.** Built-ins use the same `register_*` calls, so the public
  seams are exercised by otto itself on every run.
- **Conformance helpers.** For contract-shaped seams, `otto.testing`
  ships `assert_*_conforms` functions (e.g.
  {func}`~otto.testing.assert_reservation_backend_conforms`) — one pytest
  test per backend catches every contract violation in one report.
  `otto.examples` holds small, copyable reference implementations that
  otto's own suite keeps green.
- **Containment.** A broken init module becomes one framed warning, not a
  broken CLI ({doc}`../lifecycles/index`); a name collision is a loud error attributed
  to both registering modules ({doc}`registries`).
- **Schema visibility.** Data-side extensions (profiles, preferences, custom
  settings tables) surface in `otto schema export`, so editors validate them
  ({doc}`data-boundary`).

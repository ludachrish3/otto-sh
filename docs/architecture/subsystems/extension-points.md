# Extension points

Downstream repos extend otto from **init modules** — ordinary Python modules
named in the `init` list of `.otto/settings.toml`, imported during bootstrap
phase 2 ({doc}`../lifecycle`). Registration is import-time and side-effect
based: an init module calls `register_*` functions (or applies decorators),
and from then on the new component behaves exactly like a built-in — same
registries, same CLI listing and completion, same error messages
({doc}`registries`).

## The seams

| You want to add | Register with | Guide |
| --- | --- | --- |
| an `otto run` subcommand | {func}`@instruction() <otto.cli.run.instruction>` | {doc}`../../guide/cli/run/index` |
| an `otto test` suite | `Test`-prefixed {class}`~otto.suite.suite.OttoSuite` subclass (auto-registers) | {doc}`../../guide/cli/test/index` |
| a top-level `otto` command | {func}`otto.register_cli_command <otto.cli.registry.register_cli_command>` / {func}`@otto.cli_command <otto.cli.registry.cli_command>` | {doc}`../../library/extending-cli` |
| a CLI verb on a host class | `@cli_exposed` on the method | {doc}`../../library/cli-exposed-verbs` |
| a host class (new `os_type` base) | `register_host_class` | {doc}`../../library/custom-host-classes` |
| an OS profile (defaults bundle) | `register_os_profile` or `[[os_profiles]]` in settings | {doc}`../../guide/configuration/os-profiles` |
| a connection (term) backend | `register_term_backend` | {doc}`../../library/extending-backends` |
| a file-transfer backend | `register_transfer_backend` | {doc}`../../library/extending-backends` |
| a shell dialect | `register_command_frame` | {doc}`../../library/extending-embedded` |
| an embedded binary loader | `register_binary_loader` | {doc}`../../library/extending-embedded` |
| an embedded filesystem type | `register_filesystem` | {doc}`../../library/extending-embedded` |
| a power controller | `register_power_controller` | {doc}`../../library/extending-backends` |
| products on hosts | `register_product_provider` | {doc}`../../library/cli-exposed-verbs` |
| a host source (lab repository) | {func}`otto.labs.register_lab_repository` | {doc}`../../library/lab-source-backends` |
| fast completion for a host source | optional {class}`~otto.labs.protocol.SupportsHostSummaries` on the repository | {doc}`../../library/lab-source-backends` |
| a reservation backend | `register_reservation_backend` | {doc}`../../library/reservation-backends` |
| per-host monitor parsers | `register_host_parsers` | {doc}`../../library/custom-parsers` |
| SNMP metric descriptors | `register_snmp_metric` | {doc}`../../library/custom-parsers` |

Options classes deserve a mention even though they aren't a registry: a
repo-wide `@options` class shared by instructions and suites is the standard
way to give a whole project consistent CLI flags ({doc}`../../library/options-classes`).

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
  broken CLI ({doc}`../lifecycle`); a name collision is a loud error attributed
  to both registering modules ({doc}`registries`).
- **Schema visibility.** Data-side extensions (profiles, preferences, custom
  settings tables) surface in `otto schema export`, so editors validate them
  ({doc}`data-boundary`).

### Contract changes worth re-reading

Four rules tightened after these seams were first written, and a third-party
author who last read this page before then should know all four — each one
converts a mistake that used to pass quietly into one that says so:

- A {class}`~otto.host.product.Product`'s `stage`, `install`, and `uninstall`
  return a {class}`~otto.result.Result` where they used to return a
  `(status, message)` tuple, so the retcode and output of whatever did the
  work now reach the CLI's exit code untouched instead of being flattened to
  a pair. (`is_installed` is unchanged, and still returns `bool`.)
- An `@instruction()` handler must be `async def`; the decorator raises
  `TypeError` on a plain `def` rather than registering a leaf that would run
  outside the command lifecycle.
- A `@cli_command()` handler must be `async def` too, unless it is registered
  `lab_free=True` — and that exemption means "I drive the lifecycle myself",
  not "I touch no hosts".
- A registered command's returned `Result` now derives the process exit code
  (`cli/invoke.render_leaf_value`); it used to be discarded, so a command
  that returned a failing `Result` still exited `0`
  ({doc}`../lifecycle`).

## Seams and their guides

Each seam's user-facing how-to lives in the guide:

- Connection & transfer backends — {doc}`../../library/extending-backends`
- Embedded targets & command frames — {doc}`../../library/extending-embedded`
- Host classes, OS profiles & host verbs — {doc}`../../guide/configuration/os-profiles`,
  {doc}`../../guide/cli/host/capabilities/index`
- Power controllers & product providers — {doc}`../../library/extending-backends`,
  {doc}`../../guide/cli/host/capabilities/index`
- Host sources — {doc}`../../guide/configuration/host-sources`
- Reservation backends — {doc}`../../guide/cli/reservation/index`
- Monitor parsers & SNMP metrics — {doc}`../../guide/cli/monitor/index`
- Instructions, suites & options — {doc}`../../guide/cli/run/index`, {doc}`../../guide/cli/test/index`, {doc}`../../library/options-classes`
- New top-level commands — {doc}`../../library/extending-cli`

## Where the code lives

- {mod}`otto.cli.run` — the `@instruction()` decorator behind an `otto run`
  subcommand
- {mod}`otto.suite.suite` — `OttoSuite.__init_subclass__`, the
  auto-registration hook behind an `otto test` suite
- {mod}`otto.cli.registry` — `register_cli_command` / `cli_command` for a
  top-level `otto` command
- `otto.testing` — the `assert_*_conforms` conformance helpers, one per
  contract-shaped seam
- `otto.examples` — copyable reference implementations otto's own suite
  keeps green
- each remaining seam's `register_*` function lives beside the component it
  extends — see {doc}`registries` for the full inventory

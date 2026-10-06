# Public surface declaration — appendix (generated)

Generated from the static evidence ledger (scratchpad `ledger.py`). Evidence kinds: `root` (`otto.__all__`), `import` (docs code, `docs/examples/**/*.py`, `src/otto/examples`, absolute and relative), `attr` (dotted reference), `role` (Sphinx/MyST cross-reference), `setting` (`module:Name` strings), `testing` (imported by `otto.testing`), `autodoc` (listed by an `automodule` directive). *Deliberate* = any kind but `autodoc`, counting `attr`/`role`/`setting` only on user-facing pages (cookbook, cli, configuration, getting-started, examples, installation, overview, README, docstrings).

## A. Declared namespaces

| Namespace | Tier | Kind | Note |
|---|---|---|---|
| `otto` | 1 | package facade |  |
| `otto.config` | 1 | package facade |  |
| `otto.lab` | 1 | package facade | new (D1) |
| `otto.host` | 1 | package facade |  |
| `otto.labs` | 1 | package facade |  |
| `otto.models` | 1 | package facade |  |
| `otto.link` | 1 | package facade |  |
| `otto.tunnel` | 1 | package facade |  |
| `otto.docker` | 1 | package facade |  |
| `otto.reservations` | 1 | package facade |  |
| `otto.inventory` | 1 | package facade |  |
| `otto.creds` | 1 | package facade |  |
| `otto.monitor` | 1 | package facade |  |
| `otto.coverage` | 1 | package facade |  |
| `otto.suite` | 1 | package facade |  |
| `otto.project` | 1 | package facade |  |
| `otto.session` | 1 | package facade |  |
| `otto.logger` | 1 | package facade |  |
| `otto.testing` | 1 | package facade |  |
| `otto.init` | 1 | package facade |  |
| `otto.bootstrap` | 1 | single-file module (gains `__all__` in P1) |  |
| `otto.context` | 1 | single-file module (gains `__all__` in P1) | spec 2 (run state), settled: `2026-10-06-run-state-contracts-design.md` |
| `otto.instructions` | 1 | single-file module (gains `__all__` in P1) |  |
| `otto.result` | 1 | single-file module (gains `__all__` in P1) |  |
| `otto.errors` | 1 | single-file module (gains `__all__` in P1) |  |
| `otto.utils` | 1 | single-file module (gains `__all__` in P1) |  |
| `otto.registry` | 1 | single-file module (gains `__all__` in P1) | spec 3a (`2026-10-06-registry-catalog-design.md`) |
| `otto.params` | 1 | single-file module (gains `__all__` in P1) |  |
| `otto.tls` | 1 | single-file module (existing `__all__`) |  |
| `otto.host.transfer` | 2 | extension namespace | |
| `otto.host.options` | 2 | extension namespace | |
| `otto.host.command_frame` | 2 | extension namespace | |
| `otto.host.login_proxy` | 2 | extension namespace | |
| `otto.host.embedded_filesystem` | 2 | extension namespace | |
| `otto.host.product` | 2 | extension namespace | |
| `otto.monitor.parsers` | 2 | extension namespace | |
| `otto.monitor.snmp` | 2 | extension namespace | |
| `otto.monitor.log_sourced` | 2 | extension namespace | |
| `otto.cli.registry` | 2 | extension namespace | |
| `otto.host.app_shell` | 2 | extension namespace | |
| `otto.host.session_setup` | 2 | extension namespace | |
| `otto.host.dev_tool` | 2 | extension namespace | |
| `otto.examples.*` | 2 | reference implementations (each module) | copied by readers; provisional forever |

## B. User-used names with no public path today → declared home

Module references (`{mod}` links, `import otto.x`) are listed in B.2, not here.

| Defining site | Declared home | Evidence (deliberate count; first site) | Note |
|---|---|---|---|
| `otto.bootstrap:BootstrapError` | `otto.bootstrap:BootstrapError` | 2; role `docs/cookbook/extending/extending-cli.md:161` |  |
| `otto.bootstrap:bootstrap` | `otto.bootstrap:bootstrap` | 9; import `docs/cookbook/python-library.md:218` |  |
| `otto.cli.invoke:command_preamble` | *internal* (explanatory link) | 4; role `docs/cookbook/extending/extending-cli.md:170` |  |
| `otto.cli.link:link_app` | *internal* — the registration example in `dry-run-contract.md:13-20` is rewritten to target a reader's own module | 1; setting `docs/cookbook/dry-run-contract.md:16` |  |
| `otto.cli.main:entry` | *internal* (explanatory link) | 4; role `docs/cookbook/extending/extending-cli.md:153` |  |
| `otto.cli.registry:CLI_COMMANDS` | `otto.cli.registry:CLI_COMMANDS` | 2; role `docs/cookbook/extending/extending-cli.md:244` |  |
| `otto.cli.registry:CommandSpec` | `otto.cli.registry:CommandSpec` | 2; role `docs/cookbook/extending/extending-cli.md:186` |  |
| `otto.config.lab:Lab` | `otto.lab:Lab` | 8; import `docs/cookbook/extending/reservation-backends.md:518` | D1 |
| `otto.config.scope:EmptySelectionError` | `otto.lab:EmptySelectionError` | 4; role `docs/cli/run/defaults.md:235` | spec 4 §2 (hand-amended 2026-10-06) |
| `otto.config.scope:resolve_scopes` | *internal* — the page teaches `otto.lab:fleet_of_interest` | 1; import `docs/getting-started/boards-of-interest.md:47` | spec 4 §2 (hand-amended 2026-10-06) |
| `otto.config.scope:scoped_ids` | *internal* — the page teaches `otto.lab:fleet_of_interest` | 1; import `docs/getting-started/boards-of-interest.md:47` | spec 4 §2 (hand-amended 2026-10-06) |
| `otto.context:reset_context` | `otto.context:reset_context` | 3; import `docs/cookbook/python-library.md:219` | spec 2 (run state) |
| `otto.context:reset_variant` | `otto.context:reset_variant` | 1; attr `docs/cookbook/python-library.md:210` | spec 2 (run state) |
| `otto.context:set_context` | `otto.context:set_context` | 3; import `docs/cookbook/python-library.md:219` | spec 2 (run state) |
| `otto.context:set_variant` | `otto.context:set_variant` | 2; attr `docs/configuration/declared-products-tools.md:237` | spec 2 (run state) |
| `otto.context:variant` | `otto.context:variant` | 4; import `docs/examples/getting-started/libs/gs_example/versions.py:11` | spec 2 (run state) |
| `otto.coverage.capture.gitio:GitUnavailableError` | `otto.coverage:GitUnavailableError` | 1; attr `docs/cookbook/python-library.md:592` |  |
| `otto.coverage.errors:CoverageDataMismatchError` | `otto.coverage:CoverageDataMismatchError` | 1; attr `docs/cookbook/python-library.md:594` |  |
| `otto.coverage.errors:CoverageToolVersionError` | `otto.coverage:CoverageToolVersionError` | 1; attr `docs/cookbook/python-library.md:597` |  |
| `otto.coverage.reporter:run_coverage_report` | `otto.coverage:run_coverage_report` | 3; import `docs/cookbook/python-library.md:537` |  |
| `otto.coverage.tiers:TierConfig` | `otto.coverage:TierConfig` | 2; role `docs/cookbook/python-library.md:525` |  |
| `otto.coverage.tiers:resolve_get_tier` | `otto.coverage:resolve_get_tier` | 2; role `docs/cookbook/python-library.md:526` |  |
| `otto.errors:EnsureStateError` | `otto.errors:EnsureStateError` | 4; role `docs/cookbook/authoring/writing-instructions.md:400` |  |
| `otto.errors:OttoError` | `otto.errors:OttoError` | 1; attr `docs/cookbook/python-library.md:609` |  |
| `otto.examples.app_shell:Listing` | `otto.examples.app_shell:Listing` | 1; import `src/otto/examples/app_shell.py:25` |  |
| `otto.examples.app_shell:PyRepl` | `otto.examples.app_shell:PyRepl` | 3; import `docs/examples/getting-started/libs/gs_example/setup.py:7` |  |
| `otto.examples.app_shell:Row` | `otto.examples.app_shell:Row` | 1; import `src/otto/examples/app_shell.py:25` |  |
| `otto.examples.app_shell:Version` | `otto.examples.app_shell:Version` | 1; import `src/otto/examples/app_shell.py:13` |  |
| `otto.examples.lab_repository:ExampleLabRepository` | `otto.examples.lab_repository:ExampleLabRepository` | 5; import `docs/cookbook/extending/lab-source-backends.md:120` |  |
| `otto.examples.login_proxy:enter_container` | `otto.examples.login_proxy:enter_container` | 1; import `src/otto/examples/login_proxy.py:31` |  |
| `otto.examples.monitor:UptimeParser` | `otto.examples.monitor:UptimeParser` | 2; import `docs/cookbook/extending/custom-parsers.md:52` |  |
| `otto.examples.options:DeviceTestOptions` | `otto.examples.options:DeviceTestOptions` | 1; import `src/otto/examples/options.py:21` |  |
| `otto.examples.options:RepoOptions` | `otto.examples.options:RepoOptions` | 2; import `docs/cookbook/authoring/options-classes.md:92` |  |
| `otto.examples.reservations:ExampleReservationBackend` | `otto.examples.reservations:ExampleReservationBackend` | 7; import `docs/cookbook/extending/reservation-backends.md:199` |  |
| `otto.examples.reservations_cli:check_report` | `otto.examples.reservations_cli:check_report` | 2; import `docs/cookbook/extending/reservation-backends.md:520` |  |
| `otto.examples.reservations_cli:translate` | `otto.examples.reservations_cli:translate` | 2; import `docs/cookbook/extending/reservation-backends.md:520` |  |
| `otto.examples.session_setup:enter_python` | `otto.examples.session_setup:enter_python` | 1; import `src/otto/examples/session_setup.py:15` |  |
| `otto.examples.session_setup:export_app_env` | `otto.examples.session_setup:export_app_env` | 1; import `src/otto/examples/session_setup.py:15` |  |
| `otto.host.app_shell:AppShellActiveError` | `otto.host.app_shell:AppShellActiveError` | 2; role `docs/cookbook/sessions.md:127` |  |
| `otto.host.app_shell:apply_parse` | `otto.host.app_shell:apply_parse` | 1; import `src/otto/examples/app_shell.py:26` |  |
| `otto.host.app_shell:parse_one` | `otto.host.app_shell:parse_one` | 1; import `src/otto/examples/app_shell.py:14` |  |
| `otto.host.capability_grid:HostCapabilities` | `otto.host:HostCapabilities` | 3; testing `src/otto/testing/conformance_host.py:26` |  |
| `otto.host.capability_grid:SessionIdentity` | `otto.host:SessionIdentity` | 1; testing `src/otto/testing/conformance_host.py:26` |  |
| `otto.host.capability_grid:UserSupport` | `otto.host:UserSupport` | 1; testing `src/otto/testing/conformance_host.py:26` |  |
| `otto.host.command_frame:FRAME_CLASSES` | `otto.host.command_frame:FRAME_CLASSES` | 3; import `docs/architecture/subsystems/registries.md:79` |  |
| `otto.host.connections:TermContext` | `otto.host:TermContext` | 4; role `docs/cookbook/extending/extending-backends.md:120` | term-backend seam (construction contract in spec 3a; ABC in #600) |
| `otto.host.element:Element` | `otto.host:Element` | 4; import `docs/cookbook/connection-options.md:101` |  |
| `otto.host.embedded_filesystem:EmbeddedFileSystem` | `otto.host.embedded_filesystem:EmbeddedFileSystem` | 3; import `docs/cookbook/extending/extending-embedded.md:170` |  |
| `otto.host.embedded_filesystem:register_filesystem` | `otto.host.embedded_filesystem:register_filesystem` | 1; import `docs/cookbook/extending/extending-embedded.md:170` |  |
| `otto.host.errors:MountNotFoundError` | `otto.host:MountNotFoundError` | 2; role `docs/cli/docker/index.md:168` |  |
| `otto.host.errors:RawLandingError` | `otto.host:RawLandingError` | 2; role `docs/cookbook/extending/extending-backends.md:464` |  |
| `otto.host.errors:SessionSetupError` | `otto.host:SessionSetupError` | 2; role `docs/cookbook/extending/extending-backends.md:476` |  |
| `otto.host.factory:host_identity` | `otto.host:host_identity` | 1; import `src/otto/examples/lab_repository.py:43` |  |
| `otto.host.host:BaseHost` | `otto.host:BaseHost` | 35; testing `src/otto/testing/conformance_host.py:27` |  |
| `otto.host.login_proxy:LOGIN_PROXIES` | `otto.host.login_proxy:LOGIN_PROXIES` | 1; import `src/otto/examples/login_proxy.py:34` |  |
| `otto.host.login_proxy:ProxyContext` | `otto.host.login_proxy:ProxyContext` | 7; import `docs/cookbook/extending/extending-backends.md:361` |  |
| `otto.host.login_proxy:ProxyIO` | `otto.host.login_proxy:ProxyIO` | 7; import `docs/cookbook/extending/extending-backends.md:361` |  |
| `otto.host.login_proxy:resolve_chain` | `otto.host.login_proxy:resolve_chain` | 3; import `src/otto/examples/login_proxy.py:43` |  |
| `otto.host.loop_owner:HostLoopError` | `otto.host:HostLoopError` | 4; role `docs/cookbook/host-scopes.md:144` |  |
| `otto.host.mount:Mount` | `otto.host:Mount` | 4; role `docs/cli/docker/index.md:165` |  |
| `otto.host.mount:mount_for` | `otto.host:mount_for` | 2; role `docs/cli/docker/index.md:207` |  |
| `otto.host.options:FtpOptions` | `otto.host.options:FtpOptions` | 3; import `docs/cookbook/connection-options.md:211` |  |
| `otto.host.options:LocalPortForward` | `otto.host.options:LocalPortForward` | 1; import `docs/cookbook/connection-options.md:157` |  |
| `otto.host.options:NcOptions` | `otto.host.options:NcOptions` | 2; role `docs/configuration/host-options.md:592` |  |
| `otto.host.options:ScpOptions` | `otto.host.options:ScpOptions` | 3; import `docs/cookbook/connection-options.md:230` |  |
| `otto.host.options:SftpOptions` | `otto.host.options:SftpOptions` | 2; role `docs/configuration/host-options.md:358` |  |
| `otto.host.options:SnmpOptions` | `otto.host.options:SnmpOptions` | 2; role `docs/configuration/host-options.md:532` |  |
| `otto.host.options:SshOptions` | `otto.host.options:SshOptions` | 5; import `docs/cookbook/connection-options.md:103` |  |
| `otto.host.options:TelnetOptions` | `otto.host.options:TelnetOptions` | 4; import `docs/cookbook/connection-options.md:243` |  |
| `otto.host.product:ProductPlan` | `otto.host.product:ProductPlan` | 2; role `docs/cookbook/dry-run-contract.md:103` |  |
| `otto.host.product:planned_stage_dir` | `otto.host.product:planned_stage_dir` | 2; role `docs/cookbook/dry-run-contract.md:121` |  |
| `otto.host.product:put_line` | `otto.host.product:put_line` | 2; role `docs/cookbook/dry-run-contract.md:122` |  |
| `otto.host.product:register_product_kind` | `otto.host.product:register_product_kind` | 1; import `docs/configuration/declared-products-tools.md:478` |  |
| `otto.host.product:sudo_line` | `otto.host.product:sudo_line` | 2; role `docs/cookbook/dry-run-contract.md:122` |  |
| `otto.host.product:unplanned` | `otto.host.product:unplanned` | 2; role `docs/cookbook/dry-run-contract.md:123` |  |
| `otto.host.session_setup:SESSION_SETUPS` | `otto.host.session_setup:SESSION_SETUPS` | 1; import `src/otto/examples/session_setup.py:18` |  |
| `otto.host.session_setup:session_setup_from_spec` | `otto.host.session_setup:session_setup_from_spec` | 1; import `src/otto/examples/session_setup.py:26` |  |
| `otto.host.userland:Userland` | `otto.host:Userland` | 2; role `docs/cli/host/capabilities/privilege.md:12` |  |
| `otto.instructions:instruction` | `otto.instructions:instruction` | 15; import `README.md:118` |  |
| `otto.instructions:run_instruction` | `otto.instructions:run_instruction` | 3; import `docs/cookbook/authoring/writing-instructions.md:608` |  |
| `otto.inventory.protocol:SupportsStatPaths` | `otto.inventory:SupportsStatPaths` | 2; role `docs/cookbook/extending/creds-backends.md:25` |  |
| `otto.logger.management:DEFAULT_LIBRARY_LEVELS` | `otto.logger:DEFAULT_LIBRARY_LEVELS` | 2; role `docs/configuration/settings.md:271` |  |
| `otto.models.host:CredSpec` | `otto.models:CredSpec` | 4; import `docs/cookbook/extending/creds-backends.md:44` |  |
| `otto.models.inventory:FILLABLE_INVENTORY_FIELDS` | `otto.models:FILLABLE_INVENTORY_FIELDS` | 1; testing `src/otto/testing/conformance.py:49` | used by conformance checks; review at inventory promotion |
| `otto.models.inventory:INVENTORY_KEY_FIELDS` | `otto.models:INVENTORY_KEY_FIELDS` | 1; testing `src/otto/testing/conformance.py:49` | used by conformance checks; review at inventory promotion |
| `otto.models.inventory:InventoryRecord` | `otto.models:InventoryRecord` | 3; testing `src/otto/testing/conformance.py:49` | used by conformance checks; review at inventory promotion |
| `otto.models.inventory:SUPPLIES_EXEMPT_FIELDS` | `otto.models:SUPPLIES_EXEMPT_FIELDS` | 1; testing `src/otto/testing/conformance.py:49` | used by conformance checks; review at inventory promotion |
| `otto.monitor.collector:MonitorTarget` | `otto.monitor:MonitorTarget` | 3; import `docs/cookbook/extending/custom-parsers.md:16` |  |
| `otto.monitor.log_sourced:CsvMetricParser` | `otto.monitor.log_sourced:CsvMetricParser` | 3; import `docs/cli/monitor/metrics.md:61` |  |
| `otto.monitor.log_sourced:RegexLogEventParser` | `otto.monitor.log_sourced:RegexLogEventParser` | 3; import `docs/cli/monitor/metrics.md:118` |  |
| `otto.monitor.parsers:LogEvent` | `otto.monitor.parsers:LogEvent` | 2; role `docs/cli/monitor/metrics.md:167` |  |
| `otto.monitor.parsers:MetricDataPoint` | `otto.monitor.parsers:MetricDataPoint` | 4; import `docs/cookbook/extending/custom-parsers.md:17` |  |
| `otto.monitor.parsers:ParseContext` | `otto.monitor.parsers:ParseContext` | 6; import `docs/cookbook/extending/custom-parsers.md:17` |  |
| `otto.monitor.parsers:register_host_parsers` | `otto.monitor.parsers:register_host_parsers` | 5; import `docs/cookbook/extending/custom-parsers.md:53` |  |
| `otto.monitor.parsers:register_parsers` | `otto.monitor.parsers:register_parsers` | 5; import `docs/cli/monitor/metrics.md:119` |  |
| `otto.monitor.snmp:SnmpMetric` | `otto.monitor.snmp:SnmpMetric` | 2; import `docs/cookbook/extending/custom-parsers.md:151` |  |
| `otto.monitor.snmp:register_snmp_metric` | `otto.monitor.snmp:register_snmp_metric` | 1; import `docs/cookbook/extending/custom-parsers.md:151` |  |
| `otto.params:OPTIONS` | `otto.params:OPTIONS` | 1; import `src/otto/examples/options.py:38` |  |
| `otto.params:OptionsCollisionError` | `otto.params:OptionsCollisionError` | 2; role `docs/cookbook/authoring/options-classes.md:236` |  |
| `otto.params:OptionsNotAvailableError` | `otto.params:OptionsNotAvailableError` | 2; role `docs/cookbook/authoring/options-classes.md:208` |  |
| `otto.params:OptionsRegistrationError` | `otto.params:OptionsRegistrationError` | 4; role `docs/cookbook/authoring/options-classes.md:119` |  |
| `otto.params:OptionsValidationError` | `otto.params:OptionsValidationError` | 4; role `docs/cookbook/python-library.md:445` |  |
| `otto.params:options_key` | `otto.params:options_key` | 1; import `src/otto/examples/options.py:38` |  |
| `otto.params:verbs_for` | `otto.params:verbs_for` | 1; import `src/otto/examples/options.py:38` |  |
| `otto.registry:Ref` | `otto.registry:Ref` | 1; import `docs/architecture/subsystems/registries.md:80` | spec 3a |
| `otto.registry:RegistrationRefused` | `otto.registry:RegistrationRefused` | 4; role `docs/cookbook/authoring/options-classes.md:110` | spec 3a |
| `otto.registry:Registry` | `otto.registry:Registry` | 4; role `docs/cookbook/extending/extending-backends.md:27` | spec 3a |
| `otto.registry:registering_repo` | `otto.registry:registering_repo` | 1; import `docs/getting-started/boards-of-interest.md:30` | spec 3a |
| `otto.reservations.registry:RESERVATION_BACKENDS` | `otto.reservations:RESERVATION_BACKENDS` | 2; import `docs/examples/getting-started/libs/gs_example/__init__.py:47` |  |
| `otto.result:CommandNotRunError` | `otto.result:CommandNotRunError` | 2; import `docs/cookbook/dry-run-contract.md:197` |  |
| `otto.suite.expect:ExpectCollector` | `otto.suite:ExpectCollector` | 5; import `docs/cookbook/test-recipes.md:50` |  |
| `otto.suite.monitor_fixture:MonitorHandle` | `otto.suite:MonitorHandle` | 2; role `docs/cookbook/test-recipes.md:123` |  |
| `otto.suite.run:prepare_run` | `otto.suite:prepare_run` | 5; import `docs/cookbook/python-library.md:454` |  |
| `otto.tls:DEFAULT_TIMEOUT_SECONDS` | `otto.tls:DEFAULT_TIMEOUT_SECONDS` | 3; import `docs/cookbook/extending/https-clients.md:73` |  |
| `otto.tls:os_trust_context` | `otto.tls:os_trust_context` | 3; import `docs/cookbook/extending/https-clients.md:73` |  |
| `otto.tls:os_trust_session` | `otto.tls:os_trust_session` | 11; import `docs/cookbook/extending/https-clients.md:22` |  |
| `otto.tunnel.socat:SocatCarrier` | `otto.tunnel:SocatCarrier` | 1; attr `docs/cookbook/network-api.md:185` |  |
| `otto.utils:Arg` | `otto.utils:Arg` | 1; import `docs/cookbook/extending/cli-exposed-verbs.md:93` |  |
| `otto.utils:Exclude` | `otto.utils:Exclude` | 1; import `docs/cookbook/extending/cli-exposed-verbs.md:93` |  |
| `otto.utils:Opt` | `otto.utils:Opt` | 1; import `docs/cookbook/extending/cli-exposed-verbs.md:93` |  |
| `otto.utils:cli_exposed` | `otto.utils:cli_exposed` | 4; import `docs/cookbook/extending/cli-exposed-verbs.md:43` |  |
| `otto.declared:DeclaredEntry` | `otto.host.product:DeclaredEntry` | manual: callback argument of product/dev-tool kind factories (`declared-products-tools.md:489`) | today a TYPE_CHECKING-only import in product.py; P1 makes it a runtime binding (product.py already imports otto.declared at runtime, so no new edge) |
| `otto.host.dev_tool:register_dev_tool_kind` | `otto.host.dev_tool:register_dev_tool_kind` | manual: named as the twin of `register_product_kind` (`declared-products-tools.md:477`) |  |

### B.2 Module references

- `otto` — namespace
- `otto.bootstrap` — namespace
- `otto.cli.link` — internal — explanatory link to its API-reference page
- `otto.cli.registry` — namespace
- `otto.config` — namespace
- `otto.config.lab` — internal — explanatory link to its API-reference page
- `otto.config.repo` — internal — explanatory link to its API-reference page
- `otto.config.scope` — internal — explanatory link to its API-reference page
- `otto.context` — namespace
- `otto.coverage` — namespace
- `otto.coverage.reporter` — internal — explanatory link to its API-reference page
- `otto.creds` — namespace
- `otto.docker` — namespace
- `otto.docker.adapter` — internal — explanatory link to its API-reference page
- `otto.docker.compose` — internal — explanatory link to its API-reference page
- `otto.docker.deployment` — internal — explanatory link to its API-reference page
- `otto.env` — internal — explanatory link to its API-reference page
- `otto.errors` — namespace
- `otto.examples.app_shell` — namespace
- `otto.examples.lab_repository` — namespace
- `otto.examples.login_proxy` — namespace
- `otto.examples.monitor` — namespace
- `otto.examples.options` — namespace
- `otto.examples.reservations` — namespace
- `otto.examples.reservations_cli` — namespace
- `otto.examples.session_setup` — namespace
- `otto.host` — namespace
- `otto.host.command_frame` — namespace
- `otto.host.dev_tool` — namespace
- `otto.host.element` — internal — explanatory link to its API-reference page
- `otto.host.embedded_filesystem` — namespace
- `otto.host.embedded_host` — internal — explanatory link to its API-reference page
- `otto.host.factory` — internal — explanatory link to its API-reference page
- `otto.host.local_host` — internal — explanatory link to its API-reference page
- `otto.host.login_proxy` — namespace
- `otto.host.options` — namespace
- `otto.host.os_profile` — internal — explanatory link to its API-reference page
- `otto.host.power` — internal — explanatory link to its API-reference page
- `otto.host.product` — namespace
- `otto.host.session` — internal — explanatory link to its API-reference page
- `otto.host.transfer` — namespace
- `otto.host.unix_host` — internal — explanatory link to its API-reference page
- `otto.init` — namespace
- `otto.instructions` — namespace
- `otto.inventory` — namespace
- `otto.labs` — namespace
- `otto.lifecycle` — internal — explanatory link to its API-reference page
- `otto.link` — namespace
- `otto.logger` — namespace
- `otto.models.host` — internal — explanatory link to its API-reference page
- `otto.monitor.collector` — internal — explanatory link to its API-reference page
- `otto.monitor.log_sourced` — namespace
- `otto.monitor.parsers` — namespace
- `otto.monitor.snmp` — namespace
- `otto.project` — namespace
- `otto.registry` — namespace
- `otto.reservations` — namespace
- `otto.result` — namespace
- `otto.session` — namespace
- `otto.suite` — namespace
- `otto.suite.expect` — internal — explanatory link to its API-reference page
- `otto.suite.run` — internal — explanatory link to its API-reference page
- `otto.suite.selection` — internal — explanatory link to its API-reference page
- `otto.testing` — namespace
- `otto.tls` — namespace
- `otto.tunnel` — namespace
- `otto.utils` — namespace

## C. Current facade exports by evidence (the Q4 detail)

Every name here stays public-provisional at its current path unless its package is in D, or it is one of D1's fleet names, which move from `otto.config` to `otto.lab` in P1, or one of spec 4's five repo accessors, which move from `otto.config` to `otto.bootstrap` in P1 (hand-amended 2026-10-06).
*Taught* = deliberate evidence; *reference* = only autodoc/architecture mentions; *none* = no evidence at all.

| Facade | Exports | Taught | Reference only | None | Reference-only / none names |
|---|---|---|---|---|---|
| `otto` | 30 | 30 | 0 | 0 |  |
| `otto._webassets` | 3 | 0 | 1 | 2 | COVAPP, ALL°, MONITOR° |
| `otto.check` | 30 | 0 | 30 | 0 | CHECK_HOST_TIMEOUT, CheckCommandFailedError, CheckHostUnreachableError, CheckRow, CheckSection, FeatureResult, HostFingerprint, LINK_TOOLS, LINK_VERSIONS, ProvenEntry, ProvenRange, REPORT_SCHEMA, RangeLabel, ReportVerdicts, SWEEP_MIN_AGE_S, TUNNEL_TOOLS, TUNNEL_VERSIONS, UnmeasuredReason, Verdict, check_exec, count_verdicts, fingerprint_command, label_against_range, load_proven_range, parse_fingerprint, probe_fingerprint, range_labels, render_sections, report_to_json, section_counts |
| `otto.cli` | 1 | 1 | 0 | 0 |  |
| `otto.config` | 21 | 8 | 13 | 0 | DockerCompose, DockerImage, DockerSettings, MonitorSettings, ResolvedDependency, Version, get_completion_names, get_env, get_ordered_repos, is_bootstrapped, load_otto_env, load_user_settings, user_settings_path. In P1, D1's six fleet names move to `otto.lab`, and spec 4 moves the taught `get_repos` and the reference-only `get_completion_names`, `get_env`, `get_ordered_repos` and `is_bootstrapped` to `otto.bootstrap`; ten names stay |
| `otto.coverage` | 19 | 13 | 6 | 0 | CollectResult, CoverageNotInstrumentedError, CoverageReporter, CoverageStore, GcdaFetcher, ReportInputs |
| `otto.coverage.fetcher` | 1 | 0 | 1 | 0 | GcdaFetcher |
| `otto.coverage.merge` | 4 | 0 | 4 | 0 | LCOVLoader, LcovMerger, PathMapping, PathRemapper |
| `otto.coverage.store` | 1 | 0 | 1 | 0 | CoverageStore |
| `otto.creds` | 10 | 4 | 6 | 0 | CompiledCreds, JsonCredsStore, compile_creds, compile_creds_table, construct_creds_store, parse_creds_document |
| `otto.docker` | 36 | 22 | 14 | 0 | BuildOptions, HostOutput, ImageBuild, LogsTarget, build_images, compose_down, compose_up, default_docker_parent, docker_parent, docker_parents, follow_logs, get_user_compose_project, resolve_compose_logs, resolve_logs |
| `otto.env` | 15 | 0 | 0 | 15 | BackendUnavailableError°, EnvBuild°, EnvBuildError°, EnvExistsError°, EnvMeta°, EnvStatus°, META_FILENAME°, RepoState°, create_env°, env_path°, env_status°, meta_path°, read_meta°, sync_env°, write_meta° |
| `otto.host` | 70 | 32 | 37 | 1 | CommandPowerController, DevToolProvider, Expect, HopTransport, HostFilter, LocalSession, NcListenerCheck, NcPortStrategy, OsProfile, OsType, PosixFileOps, PosixPrivilege, PowerState, ProductProvider, SessionManager, ShellCommand, ShellSession, SshHopTransport, SuppressCommandOutput, TelnetSession, Toolchain, TransferProgressHandler, build_command_frame, build_host_class, build_os_profile, build_power_controller, build_term_backend, check_os_profile, get_host_class, get_os_profile, is_dry_run, make_rich_progress_handler, make_transfer_progress, power_control_from_spec, registered_dev_tool_providers, registered_product_providers, validate_host_dict, build_transfer_backend° |
| `otto.host.survey` | 4 | 0 | 0 | 4 | ProtocolVerdict°, Survey°, run_survey°, survey_report° |
| `otto.host.transfer` | 27 | 6 | 19 | 2 | ConsoleFileTransfer, FtpFileTransfer, MAX_FILE_MODE, NcFileTransfer, NcListenerCheck, NcPortStrategy, ScpFileTransfer, SftpFileTransfer, ShellFileTransfer, TftpFileTransfer, TransferProgressFactory, TransferProgressHandler, UnixFileTransfer, chmod_command, make_rich_progress_factory, make_rich_progress_handler, make_transfer_progress, parse_file_mode, validate_filename_lengths, TRANSFER_BACKENDS°, build_transfer_backend° |
| `otto.init` | 12 | 4 | 8 | 0 | AREA_NAMES, AreaVerdict, DoctorReport, FileWrite, InitInputError, ScaffoldReport, detect_areas, scaffold_prerequisites |
| `otto.inventory` | 26 | 10 | 16 | 0 | CompiledInventory, InventoryDeclaration, NetBoxInventory, RecordDifference, RefreshResult, ResolvedEntry, build_inventory, build_inventory_from_declarations, compile_inventory, construct_inventory, diff_records, document_to_records, merge_creds, parse_inventory_document, records_to_document, snapshot_cache_of |
| `otto.kmodcov` | 13 | 0 | 13 | 0 | CheckResult, ExportResult, INTERFACE, LIBRARY_DIR, LOCAL_HEADER, SHIPPED_FILES, VERSION_HEADER, check_tree, export_tree, exported_version, interface_of, modinfo_version, version_header |
| `otto.labs` | 15 | 8 | 7 | 0 | CompositeLabRepository, JsonFileLabRepository, LabSource, LoginSummary, build_lab_sources, host_summaries, list_host_ids |
| `otto.link` | 39 | 12 | 27 | 0 | AppliedPlacement, BOTH_DIRECTIONS, DirectionState, DryRunPlan, FlowDirection, IMPAIRERS, ImpairReport, Link, LinkCheckReport, LinkCommandFailedError, LinkEndpoint, LinkHostUnreachableError, LinkState, Placement, Provenance, RepairAllReport, RepairReport, ScopedState, build_impairer, canonical_key, equivalent, find_link, make_link_id, make_static_link_id, parse_percent, parse_rate, parse_time_ms |
| `otto.logger` | 4 | 2 | 2 | 0 | levels, management |
| `otto.models` | 42 | 1 | 41 | 0 | ChartSpec, ChartSpecRecord, DockerComposeSpec, DockerImageSpec, DockerSettingsSpec, ElementRecord, EmbeddedHostSpec, EventRecord, FtpOptionsSpec, HostSnapshot, HostSpec, LabSnapshot, LinkEndpointSnapshot, LinkSnapshot, LogEventRecord, MIN_INTERVAL_SECONDS, MetricPoint, MetricRecord, MonitorExport, MonitorMeta, NcOptionsSpec, OsProfileSpec, OttoEnvSettings, OttoModel, ReservationConfigSpec, ReservationEntry, ReservationFile, ScpOptionsSpec, SessionMeta, SessionRecord, SettingsModel, SftpOptionsSpec, SnmpOptionsSpec, SshOptionsSpec, TabSpecRecord, TelnetOptionsSpec, TftpOptionsSpec, ToolchainSpec, TunnelRecord, UnixHostSpec, validate_interval |
| `otto.monitor` | 19 | 4 | 15 | 0 | LiveReport, MetricCollector, MonitorInputError, MonitorServer, MonitorSession, MonitorTlsError, NoMonitorableHostsError, ReviewSourceError, is_monitorable, load_review_document, monitorable, resolve_monitor_tls, run_live, select_monitor_hosts, serve_review |
| `otto.project` | 34 | 8 | 26 | 0 | Cleanliness, CleanlinessItem, CleanlinessKind, CleanlinessReport, CleanupOptions, GetLogsOptions, HostPlan, InstallToolsOptions, PROJECT_ACTIONS, ProductPlanEntry, ProjectStatus, RepoPlan, RepoScope, StatusOptions, UninstallOptions, cleanup, combine_install_states, ensure_clean, ensure_installed, ensure_uninstalled, get_logs, install_tools, is_clean, plan_instruction, status, uninstall |
| `otto.reservations` | 32 | 12 | 20 | 0 | JsonReservationBackend, MissingResource, NullReservationBackend, ReservationGateResult, ReservationIdentity, ReservationReport, ReservationRow, ResolvedIdentity, ResourceLevel, ResourceOrigin, active_reservations, announce_expiring, build_backend, build_report, check_reservations, is_null_backend, required_resource_origins, required_resources, reset_expiry_warnings, reset_half_ported_warnings |
| `otto.session` | 18 | 10 | 8 | 0 | DemotedRepo, LoggingLevelsConflictError, ProjectSelection, RepoCheck, check_instruction_active, check_project_overlap, merge_host_preferences, merge_logging_levels |
| `otto.suite` | 6 | 5 | 1 | 0 | OttoFixturesPlugin |
| `otto.testing` | 6 | 6 | 0 | 0 |  |
| `otto.tunnel` | 27 | 8 | 19 | 0 | AddedTunnel, CARRIERS, DEFAULT_CARRIER, Direction, DiscoveredTunnel, DryRunPlan, ParsedSentinel, ProcKey, RemovedReport, Role, SENTINEL_PREFIX, Tunnel, TunnelCheckReport, TunnelDiscovery, TunnelHop, build_carrier, encode_sentinel, make_tunnel_id, parse_sentinel |

° = no evidence at all.

## D. Packages declared internal

Their `__all__` stays (the lazy-package guard needs it); they produce no golden lines, docs may not import from them, and their API-reference pages move under *Internals*.

- `otto.cli` — the CLI itself (except the tier-2 `otto.cli.registry`); docs may link its functions to explain behaviour
- `otto.check` — `otto link check` / `otto tunnel check`
- `otto.env` — `otto env` and the startup preflight
- `otto.host.survey` — `otto host <id> probe`
- `otto.kmodcov` — `otto cov` kernel-module export (the shipped C library is the user-facing part)
- `otto._webassets` — built web bundles
- `otto.coverage.fetcher` — sub-package; `GcdaFetcher` is public at `otto.coverage`
- `otto.coverage.store` — sub-package; `CoverageStore` is public at `otto.coverage`
- `otto.coverage.merge` — sub-package; LCOV merge internals, never taught
- `otto.lifecycle` — process lifecycle plumbing
- `otto.layout` — workspace layout plumbing
- `otto.declared` — declared products/tools parsing (spec 3a for `KindBuilder`, 3b for declared-entry validation; `DeclaredEntry` is declared at `otto.host.product`)

## E. Unresolved references

All eleven are false positives or stale text: `otto.code-workspace`, `otto.html` and `otto.sh` are filenames; `otto.host.transfer.unix` is quoted inside an example error message in `extending-backends.md:47` (no such module — a docs defect); `otto.cli.link.find_link` (`contributing.md:600`) names a consumer-side binding, not an API.

## F. Initial `__all__` for declared namespaces that have none today

P1 writes each list into the module as its first `__all__` (that narrows `from m import *`, and P1's footer says so). Rule: names with deliberate evidence, names homed here by B, plus `NotRunResult`. The owner may add names at review; additions are free.

| Namespace | Initial names |
|---|---|
| `otto.lab` (new package facade, D1) | Lab, load_lab, get_lab, get_host, all_hosts, do_for_all_hosts, run_on_all_hosts — created in P1; `otto.config` stops exporting them in the same commit (no aliases). Spec 4 adds EmptySelectionError, fleet_of_interest |
| `otto.bootstrap` | BootstrapError, BootstrapResult, BootstrapWarning, DependencyError, ProjectScopeError, bootstrap, get_completion_names, get_env, get_ordered_repos, get_repos, invalidate, is_bootstrapped (spec 4 §2) |
| `otto.context` | OttoContext, ProjectContextView, Variant, get_context, open_context, reset_context, reset_variant, set_context, set_variant, try_get_context, variant (spec 2 §2; RunPolicy, HostResolver and ContextBinding are declared by spec 2's commit 4, after P1) |
| `otto.instructions` | instruction, run_instruction |
| `otto.result` | CommandNotRunError, CommandResult, NotRunResult, Result, Results, ShellResult |
| `otto.errors` | EnsureStateError, OttoError |
| `otto.utils` | Arg, Exclude, Opt, Status, cli_exposed |
| `otto.registry` | Ref, RegistrationRefused, Registry, registering_repo |
| `otto.params` | OPTIONS, OptionsCollisionError, OptionsNotAvailableError, OptionsRegistrationError, OptionsValidationError, options, options_key, register_options, verbs_for |
| `otto.host.options` | FtpOptions, LocalPortForward, NcOptions, ScpOptions, SftpOptions, SnmpOptions, SshOptions, TelnetOptions |
| `otto.host.command_frame` | BashFrame, CommandFrame, FRAME_CLASSES, RawFrame, SessionMarkers, ZephyrFrame, register_command_frame |
| `otto.host.login_proxy` | Cred, LOGIN_PROXIES, ProxyContext, ProxyIO, register_login_proxy, resolve_chain |
| `otto.host.embedded_filesystem` | EmbeddedFileSystem, register_filesystem |
| `otto.host.product` | DeclaredEntry, Product, ProductPlan, ShellProduct, planned_stage_dir, put_line, register_product_kind, register_product_provider, sudo_line, unplanned |
| `otto.monitor.parsers` | DEFAULT_PARSERS, LogEvent, MetricDataPoint, MetricParser, ParseContext, register_host_parsers, register_parsers |
| `otto.monitor.snmp` | SnmpMetric, register_snmp_metric |
| `otto.monitor.log_sourced` | CsvMetricParser, RegexLogEventParser |
| `otto.cli.registry` | CLI_COMMANDS, CommandSpec, cli_command, register_cli_command |
| `otto.host.app_shell` | AppShell, AppShellActiveError, Parsed, apply_parse, parse_one |
| `otto.host.session_setup` | SESSION_SETUPS, SetupContext, register_session_setup, session_setup_from_spec |
| `otto.host.dev_tool` | DevTool, register_dev_tool_kind, register_dev_tool_provider |
| `otto.examples.app_shell` | Listing, PyRepl, Row, Version |
| `otto.examples.lab_repository` | ExampleLabRepository |
| `otto.examples.login_proxy` | enter_container |
| `otto.examples.monitor` | UptimeParser |
| `otto.examples.options` | DeviceTestOptions, RepoOptions |
| `otto.examples.reservations` | ExampleReservationBackend |
| `otto.examples.reservations_cli` | check_report, translate |
| `otto.examples.session_setup` | enter_python, export_app_env |

## G. Taught paths retired in P1

26 paths: every v1 golden deep line plus every import a reader copies (docs code, `docs/examples`, `otto.examples`), minus the declared ones. P1, the marked cutover commit, re-points every page and example to the declared path and lists each retired path in its footer. The defining module is untouched, so the old path still imports until spec 5 moves it, but it is no longer public.

| Taught path | Declared path it moves to | First source |
|---|---|---|
| `otto.config.lab:Lab` | `otto.lab:Lab` | docs/cookbook/extending/reservation-backends.md:518 (+5) |
| `otto.config.lab:load_lab` | `otto.lab:load_lab` | docs/getting-started/boards-of-interest.md:45 (+1) |
| `otto.config.repo:Repo` | `otto.config:Repo` | docs/getting-started/boards-of-interest.md:29 (+2) |
| `otto.config.scope:resolve_scopes` | `otto.lab:fleet_of_interest` (spec 4) | docs/getting-started/boards-of-interest.md:47 (+1) |
| `otto.config.scope:scoped_ids` | `otto.lab:fleet_of_interest` (spec 4) | docs/getting-started/boards-of-interest.md:47 (+1) |
| `otto.coverage.reporter:run_coverage_report` | `otto.coverage:run_coverage_report` | docs/cookbook/python-library.md:537 (+1) |
| `otto.docker.compose:get_container_host` | `otto.docker:get_container_host` | docs/cli/docker/index.md:195 (+1) |
| `otto.host.element:Element` | `otto.host:Element` | docs/cookbook/connection-options.md:101 (+4) |
| `otto.host.embedded_host:EmbeddedHost` | `otto.host:EmbeddedHost` | docs/cookbook/extending/custom-host-classes.md:50 (+1) |
| `otto.host.factory:create_host_from_dict` | `otto.host:create_host_from_dict` | docs/cookbook/python-library.md:315 (+2) |
| `otto.host.factory:host_identity` | `otto.host:host_identity` | src/otto/examples/lab_repository.py:43 |
| `otto.host.host:Host` | `otto.host:Host` | docs/examples/getting-started/libs/gs_example/products.py:15 (+1) |
| `otto.host.local_host:LocalHost` | `otto.host:LocalHost` | docs/cookbook/authoring/writing-instructions.md:518 (+1) |
| `otto.host.os_profile:register_host_class` | `otto.host:register_host_class` | docs/cookbook/extending/custom-host-classes.md:52 (+1) |
| `otto.host.os_profile:register_os_profile` | `otto.host:register_os_profile` | docs/cookbook/extending/custom-host-classes.md:11 (+2) |
| `otto.host.power:PowerController` | `otto.host:PowerController` | docs/cli/host/capabilities/power.md:32 (+1) |
| `otto.host.power:register_power_controller` | `otto.host:register_power_controller` | docs/cli/host/capabilities/power.md:32 (+1) |
| `otto.host.session:HostSession` | `otto.host:HostSession` | docs/cookbook/extending/extending-backends.md:430 (+5) |
| `otto.host.unix_host:UnixHost` | `otto.host:UnixHost` | docs/cookbook/async-patterns.md:140 (+1) |
| `otto.models.host:CredSpec` | `otto.models:CredSpec` | docs/cookbook/extending/creds-backends.md:44 (+1) |
| `otto.monitor.collector:MonitorTarget` | `otto.monitor:MonitorTarget` | docs/cookbook/extending/custom-parsers.md:16 (+1) |
| `otto.monitor.factory:build_monitor_collector` | `otto.monitor:build_monitor_collector` | docs/examples/getting-started/collect_metrics.py:8 |
| `otto.reservations.registry:RESERVATION_BACKENDS` | `otto.reservations:RESERVATION_BACKENDS` | docs/examples/getting-started/libs/gs_example/__init__.py:47 |
| `otto.suite.expect:ExpectCollector` | `otto.suite:ExpectCollector` | docs/cookbook/test-recipes.md:50 (+1) |
| `otto.suite.run:RunOptions` | `otto.suite:RunOptions` | docs/cookbook/python-library.md:454 (+1) |
| `otto.suite.run:prepare_run` | `otto.suite:prepare_run` | docs/cookbook/python-library.md:454 (+1) |

### G addendum: facade names retired by spec 3a

Spec 3a (`2026-10-06-registry-catalog-design.md` §8.1) deletes three functions that are in their facades' `__all__` today. P1 drops each from that `__all__` and lists it in its footer. None has a replacement: users register backends and otto builds them, so P1 rewrites the two teaching sentences to say that an unregistered name raises when otto builds the store, listing the registered names. This is the second exception to Q4's "no name-level narrowing", after the §4 internal list. 3a's seam commits delete the functions after P1.

| Retired name | P1 docs change | First source |
|---|---|---|
| `otto.inventory:get_inventory_backend_class` | the sentence is rewritten as above | docs/cookbook/extending/inventory-backends.md:103 |
| `otto.creds:get_creds_backend_class` | the sentence is rewritten as above | docs/cookbook/extending/creds-backends.md:99 |
| `otto.reservations:reset_half_ported_warnings` | none: it is untaught, and the diagnostic it reset is deleted | src/otto/reservations/__init__.py:130 |

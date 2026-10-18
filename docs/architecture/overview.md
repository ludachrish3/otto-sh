# Overview

otto is an asyncio test orchestrator: a CLI and a Python library that drive
*labs* of remote hosts — Unix machines over SSH/Telnet, embedded targets over
serial consoles, Docker containers — to deploy products, run commands and tests,
and collect metrics and coverage. One process, one event loop; hosts
are fanned out with asyncio, never threads.

## The nine first-party commands

Everything a user does goes through one of nine first-party commands. Each is
an ordinary entry in the CLI command registry — registered through the same
public API a third-party command uses — and each links to the page
explaining what it does once the shared machinery hands over control:

| Command | What it is |
| --- | --- |
| {doc}`otto run <subsystems/execution>` | Procedures: registered instructions with lab access |
| {doc}`otto test <subsystems/execution>` | Verdicts: your pytest tests, chosen by name or marker |
| {doc}`otto host <subsystems/hosts>` | Direct host verbs, synthesized from Python methods |
| {doc}`otto monitor <subsystems/monitoring>` | Live metrics, dashboard, and replay |
| {doc}`otto cov <subsystems/coverage/index>` | Cross-compiled gcov coverage reports |
| {doc}`otto docker <subsystems/docker-hosts>` | Images and compose stacks on lab hosts |
| {doc}`otto reservation <subsystems/reservations>` | The reservation gate, made inspectable |
| {doc}`otto schema <subsystems/data-boundary>` | The data contracts, exported for editors |
| {doc}`otto init <subsystems/bootstrap>` | Scaffold a new project; doctor an existing one |

These first-party commands stand on shared machinery, and the machinery
stands on a small set of foundations:

```{graphviz}
digraph bigpicture {
    rankdir=TB;
    node [shape=box];
    compound=true;

    subgraph cluster_commands {
        label="the nine first-party commands (otto <command>)";
        run; test; host; monitor; cov; docker; reservation; schema; init;
    }

    subgraph cluster_shared {
        label="shared lifecycle machinery";
        cli [label="CLI registry + lazy dispatch"];
        boot [label="bootstrap\n(discovery + registration)"];
        ctx [label="OttoContext + loop registries"];
    }

    subgraph cluster_subsystems {
        label="subsystems";
        hosts [label="host subsystem\nsessions · connections · transfer"];
        tests [label="test runner\n(run_tests + pytest plugins)"];
        observers [label="monitor + coverage\npipelines"];
        data [label="data boundary\nmodels · labs · settings"];
    }

    subgraph cluster_foundation {
        label="foundations";
        registry [label="Registry engine"];
        result [label="Result family"];
        logging [label="three-sink logging"];
    }

    run -> cli [ltail=cluster_commands, lhead=cluster_shared];
    cli -> hosts [ltail=cluster_shared, lhead=cluster_subsystems];
    hosts -> registry [ltail=cluster_subsystems, lhead=cluster_foundation];
}
```

Read {doc}`lifecycle` for the shared path every invocation walks —
bootstrap, dispatch, preamble, teardown — before its command takes over.

## Layer map

The package splits into four conceptual areas, and dependencies are meant to
point downward — a lower area importing from a higher one is debt. The areas
are a way to read the package; they are not the layers tach enforces, which
are finer ({doc}`quality-gates` describes them). The declared dependency
graph has no import cycle, and tach refuses one: every file is in a tach
module, every module carries a role layer, and an import may reach only its
own layer or those below it. {doc}`modules` draws the declared dependency
graph from `tach.toml`, and says which layer a new module goes in.

**Foundation** — small, dependency-light modules everything else builds on:

| Module | Responsibility |
| --- | --- |
| {mod}`otto.registry` | The generic named-registry engine behind every extension seam |
| {mod}`otto.result` | The {class}`~otto.result.Result` family: statuses, payloads, exit codes |
| {mod}`otto.utils` | `Status`, small shared helpers, CLI signature overlays |
| `otto.logger` | The root-logger funnel installer, per-library levels, {class}`~otto.logger.mode.LogMode` |
| `otto.filesystem` | Network-filesystem detection (drives WAL-vs-DELETE and rotation choices) |
| `otto.console`, `otto.params`, `otto.version` | Rich console singleton, options→CLI parameter expansion, version resolution |

**Boundary** — where external data becomes trusted runtime objects:

| Module | Responsibility |
| --- | --- |
| `otto.models` | The data-model facade: pydantic specs for lab.json, settings.toml, env vars, monitor records; the host specs live in `otto.host.specs` and are exported from `otto.host`; this package still exports `ToolchainSpec` and the eight option specs |
| `otto.labs` | The lab model and its loader (`labs/loader.py`, behind `otto.lab.Lab` and `otto.lab.load_lab`), the lab-repository (host source) protocol and the default JSON backend |
| `otto.config` | Parsing otto's inputs: the environment and each project's `settings.toml` |
| {mod}`otto.lab` | The public lab and fleet API: load a lab, reach its hosts, walk the fleet of interest |

**Domain** — the subsystems that do the actual work:

| Package | Responsibility |
| --- | --- |
| `otto.host` | Host classes, sessions, connections, transfers, privilege, power |
| `otto.suite` | `run_tests`, name resolution, the pytest plugins and their fixtures (`ctx`, `module_dir`, `test_dir`, `expect`, `monitor`), the artifact layout, `expect()` |
| `otto.monitor` | Metric collection, parsers, SNMP, the live dashboard |
| `otto.coverage` | The embedded gcov pipeline: fetch, merge, render, report |
| {mod}`otto.docker` | Image builds and compose lifecycles on parent hosts |
| {mod}`otto.reservations` | The reservation gate and its pluggable backends |

**Application** — composition and the CLI edge:

| Module | Responsibility |
| --- | --- |
| {mod}`otto.bootstrap` | Two-phase composition root: discovery ({mod}`otto.config.project`'s), then contained user-code registration; the project accessors (`get_projects`, `get_env`, ...) read it |
| {mod}`otto.context` | {class}`~otto.context.OttoContext`: the per-invocation runtime and host lifecycle scope; the public facade over `otto.run_context`, where otto's own code reads it |
| `otto.fleet` | Internal: the active lab, its hosts and the fleet walks behind {mod}`otto.lab` |
| `otto.cli` | The Typer app, the command registry, lazy dispatch, shell completion |

Two support packages sit alongside: `otto.testing` (conformance helpers
for third-party backends) and `otto.examples` (small, copyable reference
implementations, conformance-verified in otto's own suite).

## Where the boundaries are enforced

- **The CLI edge is lazy.** Importing `otto.cli` must never parse project
  settings — a malformed `settings.toml` cannot brick `otto --help`. A bare
  `import otto` resolves its public names lazily (PEP 562), so library users
  pay only for what they touch.
- **The data edge is validated.** Everything that enters from JSON, TOML, or
  the environment passes through a pydantic spec exactly once (the host specs
  in `otto.host.specs`, the lab, inventory and settings specs in
  `otto.models`); see {doc}`subsystems/data-boundary`.
- **User code is contained.** Bootstrap frames per-file failures;
  registries reject duplicate names loudly and attribute every entry to the
  module that registered it; see {doc}`subsystems/registries`.

The lifecycle page and the subsystem pages together cover each first-party
command in depth, and {doc}`principles` collects the recurring design rules
the codebase holds itself to.

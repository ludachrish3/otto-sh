# The big picture

otto is an asyncio test orchestrator: a CLI and a Python library that drive
*labs* of remote hosts — Unix machines over SSH/Telnet, embedded targets over
serial consoles, Docker containers — to deploy products, run commands and test
suites, and collect metrics and coverage. One process, one event loop; hosts
are fanned out with asyncio, never threads.

## The nine pillars

Everything a user does goes through one of nine first-party commands. Each is
an ordinary entry in the CLI command registry — registered through the same
public API a third-party command uses — and each has a lifecycle page
explaining what it does once the shared machinery hands over control:

| Pillar | What it is |
| --- | --- |
| {doc}`otto run <lifecycles/run>` | Procedures: registered instructions with lab access |
| {doc}`otto test <lifecycles/test>` | Verdicts: suites and pytest-native selection runs |
| {doc}`otto host <lifecycles/host>` | Direct host verbs, synthesized from Python methods |
| {doc}`otto monitor <lifecycles/monitor>` | Live metrics, dashboard, and replay |
| {doc}`otto cov <lifecycles/cov>` | Cross-compiled gcov coverage reports |
| {doc}`otto docker <lifecycles/docker>` | Images and compose stacks on lab hosts |
| {doc}`otto reservation <lifecycles/reservation>` | The reservation gate, made inspectable |
| {doc}`otto schema <lifecycles/schema>` | The data contracts, exported for editors |
| {doc}`otto init <lifecycles/init>` | Scaffold a new repo; doctor an existing one |

The pillars stand on shared machinery, and the machinery stands on a small
set of foundations:

```{graphviz}
digraph bigpicture {
    rankdir=TB;
    node [shape=box];
    compound=true;

    subgraph cluster_pillars {
        label="the nine pillars (otto <command>)";
        run; test; host; monitor; cov; docker; reservation; schema; init;
    }

    subgraph cluster_shared {
        label="shared lifecycle machinery";
        cli [label="CLI registry + lazy dispatch"];
        boot [label="bootstrap\n(discovery + registration)"];
        ctx [label="OttoContext + HostScope"];
    }

    subgraph cluster_subsystems {
        label="subsystems";
        hosts [label="host subsystem\nsessions · connections · transfer"];
        suites [label="suite subsystem\n+ pytest plugin"];
        observers [label="monitor + coverage\npipelines"];
        data [label="data boundary\nmodels · storage · settings"];
    }

    subgraph cluster_foundation {
        label="foundations";
        registry [label="Registry engine"];
        result [label="Result family"];
        logging [label="three-sink logging"];
    }

    run -> cli [ltail=cluster_pillars, lhead=cluster_shared];
    cli -> hosts [ltail=cluster_shared, lhead=cluster_subsystems];
    hosts -> registry [ltail=cluster_subsystems, lhead=cluster_foundation];
}
```

Read {doc}`lifecycles/index` for the shared path every invocation walks —
bootstrap, dispatch, preamble, teardown — before its pillar takes over.

## Layer map

The package splits into four layers. Dependencies point downward only —
a lower layer never imports from a higher one.

**Foundation** — small, dependency-light modules everything else builds on:

| Module | Responsibility |
| --- | --- |
| {mod}`otto.registry` | The generic named-registry engine behind every extension seam |
| {mod}`otto.result` | The {class}`~otto.result.Result` family: statuses, payloads, exit codes |
| {mod}`otto.utils` | `Status`, small shared helpers, CLI signature overlays |
| `otto.logger` | The `'otto'` logger tree, log levels, {class}`~otto.logger.mode.LogMode` |
| `otto.filesystem` | Network-filesystem detection (drives WAL-vs-DELETE and rotation choices) |
| `otto.console`, `otto.params`, `otto.version` | Rich console singleton, options→CLI parameter expansion, version resolution |

**Boundary** — where external data becomes trusted runtime objects:

| Module | Responsibility |
| --- | --- |
| `otto.models` | Pydantic specs for hosts.json, settings.toml, env vars, monitor records |
| `otto.storage` | The lab-repository (host source) protocol and the default JSON backend |
| `otto.configmodule` | Repo/lab discovery, settings parsing, fleet accessors |

**Domain** — the subsystems that do the actual work:

| Package | Responsibility |
| --- | --- |
| `otto.host` | Host classes, sessions, connections, transfers, privilege, power |
| `otto.suite` | Test-suite base class, auto-registration, the pytest plugin, `expect()` |
| `otto.monitor` | Metric collection, parsers, SNMP, the live dashboard |
| `otto.coverage` | The embedded gcov pipeline: fetch, correlate, render, report |
| {mod}`otto.docker` | Image builds and compose lifecycles on parent hosts |
| {mod}`otto.reservations` | The reservation gate and its pluggable backends |

**Application** — composition and the CLI edge:

| Module | Responsibility |
| --- | --- |
| {mod}`otto.bootstrap` | Two-phase composition root: discovery, then contained user-code registration |
| {mod}`otto.context` | {class}`~otto.context.OttoContext`: the per-invocation runtime and host lifecycle scope |
| `otto.cli` | The Typer app, the command registry, lazy dispatch, shell completion |

Two support packages sit alongside: `otto.testing` (conformance helpers
for third-party backends) and `otto.examples` (small, copyable reference
implementations, conformance-verified in otto's own suite).

## Where the boundaries are enforced

- **The CLI edge is lazy.** Importing `otto.cli` must never parse repo
  settings — a malformed `settings.toml` cannot brick `otto --help`. A bare
  `import otto` resolves its public names lazily (PEP 562), so library users
  pay only for what they touch.
- **The data edge is validated.** Everything that enters from JSON, TOML, or
  the environment passes through a pydantic spec in `otto.models` exactly
  once; see {doc}`subsystems/data-boundary`.
- **User code is contained.** Bootstrap frames per-file failures;
  registries reject duplicate names loudly and attribute every entry to the
  module that registered it; see {doc}`subsystems/registries`.

The lifecycle pages cover each pillar in depth, the subsystem pages cover the
machinery, and {doc}`principles` collects the recurring design rules the
codebase holds itself to.

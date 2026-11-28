(project-scope)=

# Project scope: the labs and hosts a project targets

A lab database can describe every host an organization owns; a given project
usually cares about a fraction of them.  The `[project]` table in
`.otto/settings.toml` is where a project declares its **fleet of interest** — the
labs it applies to, and the hosts it targets inside them.

```toml
# .otto/settings.toml

[project]
lab_patterns  = ["tech-.*", "bench1"]     # labs this project applies to
host_patterns = ["sensor-.*", "gw-\\d+"]  # hosts of interest within those labs
```

Both keys hold **Python regexes**, and both are matched with `re.fullmatch`
against a whole name — never `re.search`.  In a TOML basic string (`"..."`) a
backslash is doubled, so the regex `gw-\d+` is written `"gw-\\d+"` as above; a
literal string (`'gw-\d+'`) takes it as is.  `bench` does not admit
`bench-overflow`; write `bench.*` (wrapping any alternation first, as
`(bench|floor).*`).  Entries within a key are OR-ed: a lab is applicable when
**any** `lab_patterns` entry matches it, and a host is in the universe when its
lab is applicable **and any** `host_patterns` entry matches its id.

`lab_patterns`
: The labs this project applies to.  There is **no default**, and leaving the
  key out of a `[project]` table you did write is not "every lab" — it compiles
  to no patterns, which matches nothing.  Every-lab is spelled `[".*"]`.

`host_patterns`
: The hosts of interest within those labs.  Defaults to `[".*"]` — every host
  of every applicable lab — so a project that only needs the lab axis writes only
  `lab_patterns`.

```{warning}
Writing `[project]` **without** `lab_patterns` declares a project that applies to
no lab at all.  That is not a quiet no-op: the project is excluded, and if it is
the project driving the run, every project-layer verb aborts naming the loaded
labs and your patterns.  A `[project]` table with only `host_patterns` in it is
the shape that hits this.
```

An invalid regex fails at settings parse — the moment the project is discovered,
not on the first fleet walk — naming the offending pattern and what `re` made
of it:

```text
[project] pattern 'bench(' is not a valid regular expression: missing ), unterminated subpattern at position 5
```

(project-scope-required)=

## Required once a project provides products or dev tools

A project that registers a product or dev-tool provider (see
{doc}`../../cli/host/capabilities/index`), or declares a non-empty `[[products]]`
or `[[dev_tools]]` array ({doc}`../declared-products-tools`), **must** declare
`lab_patterns`.  The check runs at bootstrap, right after init modules have
been imported.  It does not warn: otto prints the message below, prefixed
`error:`, and exits 1.

```text
project 'sensors' registers product/dev-tool providers or declares
[[products]]/[[dev_tools]] entries but declares no [project] lab_patterns in
.otto/settings.toml. A providing project must say which labs it applies to. Add:

    [project]
    lab_patterns = [".*"]   # every lab — make the reach explicit
    #host_patterns = [".*"]

and narrow the patterns to the labs this project actually targets.
```

`lab_patterns = []` gets the same refusal: an empty list is not a narrower
declaration, it is the same "no lab" the missing key compiles to.  An empty
`host_patterns = []` is refused separately — it admits no host in any lab, so
every provider or declared entry the project registers would be dead code.

A project that registers no providers and declares no `[[products]]` or
`[[dev_tools]]` entries needs no `[project]` table at all.

(project-scope-active)=

## Which projects take part

Project configuration scopes what you see and act on, and scoping is the
default: every surface sees only the projects that take part, and only the
hosts they target ({ref}`project-scope-hosts`).  A project that does not take
part in an invocation is left out of what the surfaces below search or show.
Who takes part is the activation rule in {doc}`../../cli/projects` (an
explicit `-E` or `-I` first, then
`lab_patterns` and `host_patterns` against the loaded labs); a project with no
`[project]` table always takes part.

A lab is *selected* with `-l` / `--lab` (one option, two spellings) or the
`OTTO_LAB` environment variable.  Two tiers decide who takes part, by what the
surface knows when it runs:

- **A lab is loaded** (`otto --lab bench ...`): both patterns apply.  A project
  whose `host_patterns` match no host in the loaded lab is *host-starved* and
  does not take part.
- **No lab is loaded yet**: only the lab selection and the `-I`/`-E` switches
  are known, so only the lab axis can decide.  A host-starved project still
  takes part here, since its hosts cannot be seen before the lab loads.  That
  is the one difference between the tiers.  It applies to a lab-free command
  ({doc}`../../cli/projects`), and to the placement of declared docker
  containers while the lab is built: a host-starved project still gets
  container ids it does not deploy ({ref}`docker-container-hosts`).

With no lab selected there is nothing to infer from, so every project that
`-E` has not switched off takes part.  The listings that accept no lab
(`--list-products`, `--list-tools`, `otto run --list-instructions`, and
`otto test --list-tests` and `--list-markers`) say so with one dim line on
stderr:

```text
no lab selected, showing every project; -l <lab> narrows
```

These surfaces show or search only the projects that take part:

| Surface | What is left out |
| ------- | ---------------- |
| `otto --list-labs` | labs the project's `lab_patterns` do not match |
| `otto test`, `--list-tests`, `-n test` | the project's tests, never collected |
| `otto test --list-markers` | the project's marker panel |
| `otto run --list-instructions` | the project's instructions panel |
| `--list-products`, `--list-tools` | the project's rows |
| `otto docker` builds, image lists and use-cases | the project's images and use-case fragments |
| `otto inventory` | the project's `[inventory]` table |
| a project's root commands and instructions | refused at dispatch ({doc}`../../cli/projects`) |
| `ctx.projects`, `ctx.ordered_projects` (Python) | the project ({ref}`project-scope-python`) |

A name only a left-out project holds is refused with a line saying why that
project was not searched; {ref}`the three not-searched forms <not-searched-lines>` are listed there.

(project-scope-hosts)=

## Which hosts a run may target

The same declarations bound the hosts.  A run may target a lab host when
some project's declaration targets it (the host's lab matches the project's
`lab_patterns` and its id matches `host_patterns`) and `-E` has not switched
that project off.  The run's hosts are the union over those declarations.
`-I` adds no host: it makes a project take part, and the project's patterns
still decide its hosts.  When no project declares `[project]` at all, every
lab host may be targeted.

Two kinds of host follow a rule of their own:

- **The built-in `local` host is always targetable**, whatever any project's
  `host_patterns` say, so `otto host local` always resolves.  Whether a fleet
  walk includes it is still the walk's own `include_local` choice
  ({ref}`fleet-of-interest`).
- **A container is in scope exactly when its parent is.**  Its own id is not
  matched.  A container on the built-in `local` host follows that host, so it
  is always targetable.

Every surface that names one host reaches only these: `otto host <id>`, the
`get_host` lookups, `ctx.lab.hosts`, a link's endpoints and in-path
middleboxes (`otto link impair`, `check`), a tunnel's hosts (`otto tunnel
add`, `check`), `otto docker --parent`, the default docker parent and the
`--parent`-less fan-out of `otto docker ps` and the image verbs, and the host
listings (`--list-hosts`, `--show-lab`, the "Available hosts" hint).
`otto cov report` pairs a product with its declared definition only on these
hosts; an out-of-scope host's products are paired by path alone.  A host
reached as a **hop** is the exception: it needs only lab membership, so a jump
host no project targets still carries the connection, and `otto host <id>
--hop <jump>` may name it.
A command that opts out of scoping ({ref}`project-scope-exceptions`) reaches
any lab host.

A lab host outside the scope is refused with exit 1 before anything connects,
under `--dry-run` too, and the refusal names the fix.  When no project that
takes part targets it:

```text
host 'test3' is in lab 'bench' but outside this run's scope: no declared project targets it. To reach it, add its id to [project] host_patterns (or its lab to lab_patterns) in /home/me/proj/.otto/settings.toml.
```

When only switched-off projects target it:

```text
host 'test3' is in lab 'bench' but outside this run's scope: only switched-off projects target it (fleet-tools). Drop -E fleet-tools to reach it.
```

An id the loaded lab does not hold is the plain unknown-id error, as before
(`otto host` prints `No host with ID`), and the list of available hosts under
it holds only the hosts the run may target.

(project-scope-in-play)=

**In play** are the hosts this run's fleet walks may reach and its reservation
gate requires: the hosts above, less the built-in `local` host and the
containers on it.  A host only switched-off projects target is not in play;
a host reached only as a hop is not either.  "Targetable" and "in play" differ
only by the runner: `otto host local` resolves, but `local` never makes a
scope non-empty.  A fleet walk's own options then pick among the hosts in
play ({ref}`fleet-of-interest`).

(project-scope-effects)=
## What the declaration changes

- **Fleet walks are bounded by it.**  `ctx.all_hosts()`,
  `ctx.do_for_all_hosts(...)` and `ctx.run_on_all_hosts(...)` iterate the
  declared universe rather than the whole loaded lab.  See
  {doc}`../../cli/run/defaults` for the walk semantics, the union across projects, and
  the whole-lab fallback for projects that declare nothing.
- **Listings and `otto test` show only the projects that take part**; see
  {ref}`project-scope-active`.  `otto --list-labs` lists only the labs
  `lab_patterns` admit ({ref}`listing-labs`).
- **Monitoring and coverage walk it too.**  `otto monitor` picks its hosts
  from the same universe, and coverage collection does as well, containers
  included.  A `[coverage]` `hosts` regex ({ref}`settings-coverage`) narrows
  that set further; it never reaches a host outside the universe.
- **A project that cannot load never widens a walk.**  Its declaration still
  narrows the fleet, and a load error that would stop a run refuses the walk
  with {class}`otto.session.ProjectLoadError`; {doc}`../../cli/projects` says
  which errors stop a run and which only switch the project off.
- **It can be computed without a run.**
  {func}`otto.lab.fleet_of_interest <otto.fleet.fleet_of_interest>`
  (`from otto.lab import fleet_of_interest`) returns the ids a walk would
  take, from a loaded lab and its projects, without connecting or
  bootstrapping; {doc}`../../developing/bed-case-study/boards-of-interest`
  runs it.
- **Providers are gated by it.**  A project's product and dev-tool providers are
  not invoked at all on a host outside its universe, so nothing that project owns
  can attach to a machine it never declared.
- **Explicit targeting is bounded by it; a hop needs only lab membership.**
  `otto host <id>`, `ctx.get_host("id")`, a link or tunnel endpoint and
  `otto docker --parent` reach only a host the scope admits.  A jump host the
  project does not own still carries a hop.  See {ref}`project-scope-hosts`.

(project-scope-empty)=

## When nothing is in scope

When some project declares a scope and no host is
{ref}`in play <project-scope-in-play>`, a command that would act on the lab refuses up
front with exit 1, rather than doing nothing quietly.  This covers `otto
test`, `otto run install`, `status` and the other instructions, plugin
commands and `otto monitor --live`.  The refusal names the settings files to
widen:

```text
no host in this run belongs to any project's fleet of interest, so every fleet
walk would be empty.

    loaded labs:  unix
    declared by:  sensors
    settings:     /home/me/sensors/.otto/settings.toml

Load a lab these projects apply to (otto -l <lab>), or widen [project]
lab_patterns / host_patterns in their .otto/settings.toml.
```

The refusal's first line tells its two causes apart:

| Cause | First line |
| ----- | ---------- |
| the loaded labs hold no host any declaring project targets | `no host in this run belongs to any project's fleet of interest, so every fleet` |
| `-E` switched off every project that targets a host here | `no host is in play: every project whose [project] declaration admits a host in` |

The second names the switch instead ({ref}`projects-excluded-hosts-leave-play`),
and has no `settings:` line.

Some commands still run with nothing in scope:

| Command | With nothing in scope |
| ------- | --------------------- |
| `otto run cleanup` | runs; resets impairments and reaps tunnels lab-wide; exit 0 (below) |
| `otto link repair`, `otto tunnel list`, `otto tunnel remove` | run over the whole lab; their usual exit codes |
| `otto host local <verb>` | runs |
| `otto host <other id> <verb>` | refused by its own lookup, exit 1, with the out-of-scope text above |
| `otto cov report`, `otto docker use-cases`, `otto reservation check` | run: they contact no host, so an empty set of hosts in play is a legal answer |
| `--list-hosts`, `--show-lab` | print the refusal as a warning, list only the built-in `local` host, exit 0 |
| the unscoped commands ({ref}`project-scope-exceptions`) | run |

`otto run cleanup` exits 0 when its lab-wide steps succeed.  It resets
impairments and reaps tunnels across the whole lab as always, and declines
the steps that need a host in play, one line each.  The first line carries
the `skipped:` label every skipped result gets:

```text
skipped: debug-log sweep skipped: no host is in play
toolchain removal skipped: no host is in play
```

When the impairment reset declines a link (a qdisc otto did not create, say),
that line comes first, since it names otto state still on the wire, and it
carries the label instead:

```text
skipped: impairment reset declined: <why the link was left alone>
debug-log sweep skipped: no host is in play
toolchain removal skipped: no host is in play
```

With `--no-debug-logs` the debug-log line is not printed.

A project that does not take part is skipped with its usual warning
({doc}`../../cli/projects`).  A project forced on with `-I` whose fleet has
no host in play is skipped with:

```text
cleanup skipped for project 'sensors': its fleet has no host in play
```

(project-scope-forced-on)=

`-I` makes a project take part, but it adds no host.  A project forced on
whose `host_patterns` match no host in the loaded lab meets these outcomes,
depending on whether another project puts a host in play:

| Command | No other host in play | Another project puts a host in play |
| ------- | --------------------- | ----------------------------------- |
| `otto test`, plugin commands, `otto monitor --live` | refused, exit 1, the refusal above | run |
| `otto run install`, `status` and the other instructions | refused, exit 1, the refusal above | the project's own fleet walk refuses, exit 1, naming its patterns ({doc}`../../cli/projects`) |
| `otto run cleanup` | skips the project with the line above; exit 0 | the project's own fleet walk refuses, exit 1 |
| `otto link repair`, `otto tunnel list`, `remove` | run over the whole lab | run over the whole lab |

(project-scope-exceptions)=

## What is not scoped

Some commands and lookups must see the whole lab or every project, whatever
the declarations say:

| What | Why it sees everything |
| ---- | ---------------------- |
| `otto init`, `otto env`, `otto cache`, `otto lab` ({doc}`../../cli/lab/index`) | they diagnose or maintain every source and project, so `otto lab explain <id>` explains any lab host, one no project targets included |
| hops: a lab-declared `hop` and `otto host --hop` | a host reached as a hop needs only lab membership |
| console servers and power controllers | they are infrastructure a scoped host reaches through, not targets |
| the link self-lockout guards | a host outside the scope can still reach otto through the link ({doc}`../../cli/link/safety`) |
| what otto left on the lab: `otto link repair`, `otto tunnel list`, `remove`, `otto run cleanup`, `otto run status`'s impairment and tunnel rows, and `tunnel add`'s conflict check | an impairment or tunnel stays findable and removable after its hosts leave the scope |
| a link with an endpoint the loaded lab does not hold | `otto link list` still shows a link to a host that is not loaded, as not impairable; only an endpoint that is loaded and outside the scope hides a link |
| `otto host <id>`'s verb menu | the host's class is found lab-wide; whether the verb may run is decided separately |
| `-I` / `-E` completion and did-you-mean | they must offer every project |
| tab completion | it is a hint: it may offer an id or name the command then refuses |

A plugin command opts out the same way otto's own unscoped commands do
({ref}`extending-cli-scope`).

(project-scope-python)=

## In Python: `ctx.lab`, `ctx.projects` and `ctx.unscoped`

Every run context is scoped: the one a scoped command runs in, the one
{func}`otto.open_context <otto.context.open_context>` opens, and one you build
by hand (`OttoContext(lab=lab)`, as {doc}`../../extend/python-library` does).
Only a command that opted out ({ref}`extending-cli-scope`) runs in an
unscoped one.  The zero-argument selectors, `otto.get_lab()`,
`otto.lab.get_host` and `otto.lab.all_hosts`, read the installed context, so
they are scoped exactly when it is; with no context installed they raise
`RuntimeError`.  When no project declares `[project]`, as in a script with no
project roots, the scope is the whole lab.

So everything otto hands your code is narrowed to the run's projects and
hosts:

| Name | What gives it to you | What its `hosts` holds |
| ---- | -------------------- | ---------------------- |
| `otto.lab.Lab` | `otto.load_lab()`, `ctx.unscoped.lab`, and `ctx.lab` in an unscoped command | every lab host, `local` and containers included; read and write |
| `otto.lab.ScopedLab` | `ctx.lab` and `otto.get_lab()` in a scoped context | the hosts the run may target, in lab order; read-only |
| `otto.lab.LabView` | the type `ctx.lab` is annotated with | either of the two above |

- `ctx.lab.hosts` is a `Mapping`, not a `dict`: `dict(ctx.lab.hosts)` copies
  it.  `ctx.lab.add_host(...)` registers in the whole lab.
- `ctx.lab.links` is an immutable tuple that leaves out a link with an
  endpoint in the lab and outside the scope.  Add or remove a link through
  `ctx.unscoped.lab.links`.
- Every lookup of a host outside the scope raises the one
  {class}`otto.lab.HostOutOfScopeError <otto.config.scope.HostOutOfScopeError>`
  (defined in `otto.config.scope`), a `KeyError`: `ctx.get_host`,
  `otto.get_host`, `otto.lab.get_host` and `ctx.lab.hosts[id]` alike.  An id
  the lab does not hold raises `UnknownHostError`, also a `KeyError` and an
  `OttoError`.  Neither message is quoted the way `KeyError` quotes: `str()`
  of either is the sentence itself.
- `ctx.projects` and `ctx.ordered_projects` hold the projects that take part.
- The fleet walks (`ctx.all_hosts()` and the rest) walk the fleet of interest,
  as before.

The scope is judged from the projects, composed on first use: the first
`get_host`, read of `ctx.lab.hosts` or fleet walk on a context you built by
hand composes the projects (bootstrap and their init imports) if nothing has
yet, and raises {class}`otto.session.ProjectLoadError` where a fleet walk
would.  A context over the hostless library lab (the one a library `otto
test` session installs) never composes.

**A library run does not refuse an empty scope.**  The CLI refuses a scoped
command when no host is in play ({ref}`project-scope-empty`); `open_context`
and a hand-built context do not, since your code may be recovery.  With
nothing in play the fleet walks still refuse, but iterating `ctx.lab.hosts`
yields only the built-in `local` host.  To refuse as the CLI does, call
{meth}`ctx.require_targets() <otto.context.OttoContext.require_targets>`;
{meth}`ctx.empty_scope_refusal() <otto.context.OttoContext.empty_scope_refusal>`
returns that refusal, or `None`, without raising, and
{meth}`ctx.targetable_ids() <otto.context.OttoContext.targetable_ids>` lists
the hosts the run may target.

{attr}`ctx.unscoped <otto.context.OttoContext.unscoped>` is the whole
universe: `ctx.unscoped.lab` is the canonical `otto.lab.Lab`, and
`ctx.unscoped.projects` and `ctx.unscoped.ordered_projects` hold every parsed
project.  Read it only where the code must see past the scope: a teardown
that must find what otto left on a host that has since left the scope, for
instance.  Merge `ctx.unscoped.lab`, never a view: `ctx.lab + other` raises.
The library's recovery functions (`repair_all`, `repair_link`,
`read_link_states`, `discover_tunnels`, `remove_tunnel`,
`remove_all_tunnels`) act on the lab they are given, so pass them
`ctx.unscoped.lab` to reach out-of-scope state, as otto's own recovery
commands do.

Three things are whole-lab by design:

- {func}`otto.load_lab() <otto.labs.loader.load_lab>` returns the lab it was
  asked to build; no run's scope applies to it.
- A host's container parent, hop ids and link endpoint ids are plain
  attributes of the host and link objects.
- A custom host class inherits its hop and console-server lookups from
  {class}`~otto.host.remote_host.RemoteHost`, and they resolve through the
  whole lab, as a hop needs.  Its author does nothing for that.

## Merged labs and containers

`otto --lab lab_a+lab_b` loads two **component** labs as one merged lab
({ref}`combining-labs`).  A host is matched against the component lab it came from (the
lab name otto records on each host as it loads), never the merged name.  A
project declaring `lab_patterns = ["lab_a"]` still applies and targets the
`lab_a` hosts only.  A pattern written to match the merged name (`lab_a\+lab_b`)
matches no lab, so that project does not take part.

Docker containers created during a run inherit their parent host's lab, and a
container is in scope exactly when its parent is
({ref}`project-scope-hosts`).

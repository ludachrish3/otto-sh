# Default lab actions

Every repo with products gets a working `otto run install` — and `uninstall`,
`cleanup`, `get-logs`, `install-tools` and `status` — for free. Otto registers
those six instructions itself, before any repo's init module is imported, so a
lab that has only declared its products can already be installed, torn down,
and asked what state it is in.

Nothing about them is special-cased: they are ordinary instructions, registered
through the same `@instruction()` decorator any repo uses, whose whole body is a
call into the `otto.project` library. `otto run --list-instructions` shows them
in their own panel, attributed to otto rather than to a repo.

## The four surfaces, and the one override point

The same six behaviors are wanted in four places, and all four dispatch through
the same code:

| Surface | Looks like |
| ------- | ---------- |
| An instruction | `otto run install` |
| A script | `await otto.project.install()` |
| A suite body | the same call, inside a test |
| A test marker | `@pytest.mark.ensure("installed")` on the class or test |

```{important}
**Instructions and ensure markers are never override points.** A repo customizes
lab behavior by registering a `ProjectActions` subclass — never by defining its
own instruction named `install`. If the *instruction* could be overridden, then
`otto run install` and an `ensure("installed")` marker would run different code,
and the lab a test converges would not be the lab you installed by hand.
```

A repo that tries to claim one of the six names is refused at registration; see
[The collision error](../../../library/writing-instructions.md#the-collision-error).

## Zero effort: one repo, one command

With products registered (see
{doc}`Registering products <../../../library/cli-exposed-verbs>`) and nothing else done:

```bash
otto --lab my_lab run install
```

That walk is:

1. Otto resolves the configured repos in dependency order (bootstrap already
   computes it).
2. Each repo gets its `ProjectActions` — the subclass it registered, or otto's
   default — constructed with that repo and that repo's *view* of the live
   context.
3. Each repo's `install()` fans out across its **fleet of interest**:
   `ctx.all_hosts()`, so the built-in `local` host and Docker containers are
   excluded exactly as they are everywhere else, and so are hosts outside the
   repo's declared universe (see [The fleet of
   interest](#the-fleet-of-interest)). Hosts proceed in parallel.
4. On each host, `install(owner=<repo name>)` installs that repo's products in
   declaration order.
5. The first repo that will not install stops the walk. Installing a dependent
   on top of a dependency known to be missing produces a lab nobody can reason
   about.

The `owner=` scope in step 4 is the whole point of the layer: on a host shared
by two repos, one repo's `install` can never touch the other's products.

The other five follow the same shape:

| Instruction | Options (default) | Walk |
| ----------- | ----------------- | ---- |
| `install` | `--ensure` (off), `--recover-partial` (on, meaningful with `--ensure`) | dependencies first, fail-fast |
| `uninstall` | `--product-logs` (on), `--debug-logs` (on) | dependents first, best-effort |
| `cleanup` | `--product-logs` (on), `--debug-logs` (on), `--reset-impairments` (on), `--remove-tunnels` (on) | dependents first, best-effort |
| `get-logs` | `--product-logs` (on), `--debug-logs` (on), `--require-product-logs` (off) | order immaterial, best-effort |
| `install-tools` | `--dev` (on), `--toolchain` (off) | dependencies first, fail-fast |
| `status` | `--full` (off) | reads only; changes nothing |

Every one of those is a `--flag / --no-flag` pair, as usual.

`cleanup` is strictly more than `uninstall`: each repo also gives up its own dev
tools, the host-global toolchain tools come off, and the lab's own leftovers go
with them — netem impairments are reset and every otto tunnel is reaped. See
[What `cleanup` takes off the lab](#what-cleanup-takes-off-the-lab) for what
those last two do and do not touch. `--ensure` turns `install` into a converge —
the lab's current state is read and only the missing work is done — which is
what an `ensure` marker's steps do before a test.

(fleet-of-interest)=

## The fleet of interest

Step 3 above says the walk fans out across "the fleet". The fleet is not
automatically the whole loaded lab: it is the set of hosts the repos in this
run declared an interest in.

A repo declares that in the `[project]` table of its `.otto/settings.toml` —
`lab_patterns` and `host_patterns`, both matched with `re.fullmatch` (see
{ref}`project-scope` for the schema):

```toml
[project]
lab_patterns  = ["bench.*"]
host_patterns = ["sensor-.*"]
```

From there, which hosts a walk reaches is decided by **which object the call
goes through**, never by an argument the call site has to remember:

| Walk goes through | Base set |
| ----------------- | -------- |
| `self.ctx` inside a `ProjectActions` (the repo's view) | that repo's universe |
| the plain context (`otto.context.get_context()`, host-global steps) | the **union** of every declaring repo's universe |
| either, when **no** repo in the run declared `[project]` | the whole loaded lab — today's behavior, unchanged |

That last row is why a product-less project and every repo written before
`[project]` existed are untouched by any of this. The fallback is all-or-
nothing on purpose: it applies when *nothing* declared, not per repo, so a
product-less repo joining a run cannot quietly restore the whole lab for
everyone.

Two things are deliberately **outside** the fleet, exactly as before:
the built-in `local` host (pass `include_local=True`) and Docker containers
(`include_containers=True`). Both flags are applied *after* scoping, so they
still work under a declaration.

### Narrowing further: `pattern=`

`pattern=` picks a **subset** of that base set, never a superset, and it is a
full match:

```python
import re

await self.ctx.do_for_all_hosts(_dispatch_install, pattern=re.compile("sensor-.*"))
```

A pattern that matches none of the hosts the walk may reach raises
{class}`~otto.config.scope.EmptySelectionError` rather than doing nothing:

```text
pattern 'sensor' fullmatches none of the 6 host(s) this run may walk,
so the selection is empty and nothing would be contacted.

Host patterns are FULL matches, never substring searches. To match by prefix,
append a wildcard — 'sensor.*' — wrapping any alternation first, as
'(sensor).*'.
```

A silently empty sweep is the one failure worse than a crash: it reports
success over a lab nothing happened on. When the pattern *did* match and
`include_containers` / `include_local` then removed every match, the same class
is raised with the other of its two messages — that one names the flag and
tells you **not** to widen the regex, because the regex was already right.

### Explicit targeting is never scoped

`otto host <id> <verb>`, `ctx.get_host("id")` and the host-id listing
`otto host` prints reach any host in the loaded lab, declaration or not.
A repo that has to hop through a machine it does not own must still be able to
name it, and a scoping typo must never brick the one command that could
diagnose it.

### When a declaration and the loaded lab disagree

The consequence depends on **whose** declaration it is, and the asymmetry is
deliberate — one project's scoping must not veto another project's run.

- **The driving project** — the first `OTTO_SUT_DIRS` entry, whose run this is
  — applying to none of the loaded labs, or applying but targeting no host in
  them, **aborts** at every project-layer verb. The error names the loaded
  labs, the declared patterns, and the `settings.toml` to edit; the two cases
  get different messages, because "load a different lab" and "widen
  `host_patterns`" are different fixes. The abort happens at the verb, not at
  startup, so `otto host <id> <verb>` still works while you fix it.
- **A dependency** whose declaration admits no host here is **skipped**,
  loudly — one `WARNING` per verb. Either shape can be the reason, and each
  gets the text its own fix needs. No loaded lab applies to it:

  ```text
  repo 'sensors' is not applicable to the loaded lab(s) [floor] (lab_patterns:
  bench.*) — skipping it for install
  ```

  …or a loaded lab does apply and no host in it matches — where naming the labs
  would blame the one thing that is already right:

  ```text
  repo 'sensors' applies to lab(s) [bench] but its [project] host_patterns
  (gw-.*) match no host there — skipping it for install
  ```

  `otto run status` shows it as a row of its own and leaves it out of the
  tri-state fold, so a skipped dependency can neither vanish from the report
  nor drag the lab to PARTIAL — and the row says which of the two it was:

  ```text
  acme     installed
  sensors  not applicable (labs: floor)
  gateway  no matching hosts (host_patterns: gw-.*)
  ```

- **Every repo's declaration excluding every host** fails the fleet walk itself
  with the same class of error, naming the loaded labs and each declaring repo.
  Under the whole-lab fallback the same emptiness stays silent: an empty walk
  over an undeclared fleet means the *lab* is empty, which is a lab problem and
  has always been quiet.

`otto run status --full` prints the resolved answer for every repo — the labs
it applies to, and the hosts it targets:

```text
fleet of interest
acme     labs: bench1        hosts: sensor-1, sensor-2
sensors  labs: bench1, bench2  hosts: sensor-1
```

Every repo otto resolved gets a row, declared or not — a repo with no
`[project]` table shows every loaded lab and every host, which is what the
fallback means. Those are the sets as they stood when the run resolved them:
display data, not a walk's answer, since a walk re-derives membership live so a
container that joins later is scoped rather than frozen out.

Context creation also logs one line at INFO, and only when something actually
narrowed: `fleet of interest: 6 of 214 lab hosts (2 repo(s), 1 excluded)`.

## What `cleanup` takes off the lab

The last two steps of `cleanup` are lab infrastructure, and each goes through
the library that owns it rather than issuing `tc` or `kill` itself:

| Step | Call | Scope |
| ---- | ---- | ----- |
| `--reset-impairments` | `otto.link.manage.repair_all(lab)` | every static link in the lab — only the impairable ones can carry otto's netem |
| `--remove-tunnels` | `otto.tunnel.manage.remove_all_tunnels(lab)` | every otto tunnel, found by scanning |

Two consequences worth knowing before you rely on either.

**A qdisc otto did not create is left alone.** `repair_all` refuses to clear a
foreign root qdisc — the `tc` configuration a colleague put on a shared host is
not otto's to delete — and it refuses a management or hop-transit interface for
the self-lockout reasons `link impair` refuses them. Those refusals are
reported: `cleanup` comes back `Skipped` naming each declined link, which is
`is_ok` (a decline is not a teardown failure and must not abort the rest of a
best-effort cleanup) but is deliberately *not* `Success` — something may still
be on that netdev, and otto has just declined to take it off. A link otto tried
and failed to repair is a failure, as usual.

**A link that could never have been impaired is not a decline at all.**
`repair_all` files those in the same bucket — every implicit hop edge is one,
since they carry no named interface — and `cleanup` drops them before
reporting, asking the same pure `impairment_refusal` predicate `otto link list`
prints its reasons from. Otherwise `Success` would be unreachable on every real
lab (an N-host lab resolves at least N implicit ids), each message would carry N
lines nobody can act on, and a genuine foreign-qdisc refusal would be
indistinguishable from that standing noise.

**The tunnel reap is owner-agnostic and verified.** It finds tunnels by
scanning every `has_bash` host for otto's process tag, so a tunnel left behind
by a crashed run comes down with the rest — and it re-scans after killing. A
process still present in that second scan, or a host the scan could not reach,
fails the cleanup: those are the two ways a tunnel outlives its own reap.

**Order: the tunnel reap is last of all.** A tunnel can *be* the access path to
a host, so reaping it earlier would sever the connection the repo walk, the log
sweep and the toolchain removal still need. Resetting impairments sits
immediately before it for the mirrored reason — clearing delay and loss off a
link only improves the path everything above ran over.

## Many repos: composition and order

Composition is by *iteration over resolved repos in dependency order*, never by
cross-repo subclassing — you cannot subclass a class that may be absent, and an
**optional** dependency may well be.

- **Build-up walks dependencies first.** `install` and `install-tools` take the
  order bootstrap computed. An optional dependency that is present is simply in
  the walk; an absent one simply is not.
- **Teardown walks it reversed.** `uninstall` and `cleanup` bring the dependent
  down before the thing it depends on.
- **Building is fail-fast, tearing down is best-effort.** A repo that will not
  come down must not strand the ones behind it, so every repo is attempted and
  the first failure is what gets reported — named:
  `uninstall failed in repo 'widget': …`.
- **Host-global steps happen once, at the ends.** The debug sweep runs after
  every repo has torn down (teardown-time activity is what those logs exist to
  capture); the toolchain removal follows it, so no log retrieval depends on
  tooling that step is deleting. `cleanup` then finishes with the lab's own
  infrastructure — impairments, and the tunnel reap last of all.

Ordering beyond dependency order is not configurable, and the orchestrator
itself is not overrideable: a repo customizes by overriding its own actions.

## Where the logs land

Retrieval writes into a documented tree, keyed by host id:

```text
<output-dir>/logs/<host-id>/product/…
<output-dir>/logs/<host-id>/debug/…
```

`<output-dir>` is the active command's output directory (see
[Logging and artifacts](index.md#logging-and-artifacts)) unless a caller passes
an explicit `dest=`. The shape is a tested contract, not an implementation
detail — an override that retrieves logs its own way should still land them
there wherever a host attribution exists.

Product logs are owner-scoped and hauled per repo; debug logs are the host's
and are swept once. `uninstall` and `cleanup` gather product logs **before**
each repo's teardown and sweep debug logs **after** the last one. Retrieving
zero logs is success — `--require-product-logs` is how a run whose whole
purpose was the logs turns an empty haul into a failure, and it is asked only
of hosts the repo actually has products on.

## Reading `status`

`otto run status` prints each counted repo's state and exits on the lab-level
aggregate, so a script can branch without parsing the table:

| Exit | Aggregate | Meaning |
| ---- | --------- | ------- |
| `0` | INSTALLED | every counted repo is installed |
| `1` | UNINSTALLED | every counted repo is uninstalled |
| `2` | PARTIAL | anything in between — `otto run install --ensure` recovers it |

Three codes rather than a boolean, for the same reason the state is a tri-state:
a half-installed lab and a clean one need different handling, and reporting them
alike is how remnants get installed over.

A repo with nothing to say about its install state — no products anywhere, no
registered actions, a docs-only repo — is **not counted**: it is absent from the
table rather than listed with a made-up state, and it cannot drag the aggregate
to PARTIAL forever.

A repo whose declaration **admits no host** in this run is left out for a
different reason and shown differently — `not applicable (labs: …)` or
`no matching hosts (host_patterns: …)` on a row of its own. It is never asked,
because its fleet is empty by declaration, so it has no state to fold;
see [When a declaration and the loaded lab
disagree](#when-a-declaration-and-the-loaded-lab-disagree).

`await otto.project.is_uninstalled()` is that first row as a boolean, for a
script that only wants to know whether the lab is empty. There is deliberately
**no** `is_installed()` at this layer: `False` on it would cover PARTIAL and
UNINSTALLED alike, which is exactly the ambiguity the tri-state exists to
resolve — so every other question reads `status()`.

### `--full`: the lab's other axis

`status` answers about *products*. `otto run status --full` adds the second
axis — everything [`cleanup` takes off the lab](#what-cleanup-takes-off-the-lab)
— one row per thing it would act on:

```text
acme     installed
widgets  uninstalled
products & dev tools  acme     clean
                      widgets  dirty
toolchain tools       test1    clean
                      test2    unknown — the toolchain probe did not answer: …
impairments           core     dirty — a->b
                      edge     clean
tunnels               lab      unknown — the scan could not reach test2
lab is dirty — otto run cleanup takes it off
```

Three things about that are contracts rather than styling:

- **The exit code does not move.** It still means install state and nothing
  else — the run above exits `2` because the *install* aggregate is PARTIAL,
  and a fully installed lab with dev tools left on it and a tunnel up still
  exits `0`. Scripts branch on that code; folding a second axis into it would
  change the answer to a question nobody re-asked. The cleanliness aggregate is
  the last line instead.
- **`unknown` is a cell, not a crash.** `is_clean()` *raises* on a state nobody
  could read, deliberately — a converge must not clean on a non-fact. A display
  has the opposite duty: an unreachable host must not hide the twelve hosts
  that answered. Both read the same probes, so the rows and the boolean cannot
  drift; `--full` is not `is_clean()` with the exception swallowed.
- **It costs device work, which is why it is a flag.** Bare `otto run status`
  counts products and nothing else. `--full` adds a netem read per impairable
  link and a process scan per `has_bash` host — the same reads the `clean`
  ensure step makes. Links otto refuses to impair get no row at all — every implicit hop
  edge, and also the management or hop-transit interfaces a scan turns down.
  A refused link is never read, so there is no state to show and a "clean" row
  would be a claim about something nobody looked at; `cleanup` names the
  scan-found refusals in its own `Skipped` message, which is where an operator
  can act on one.

The same report is a library call: `await otto.project.cleanliness()` returns
one `CleanlinessItem` per row, grouped in `cleanup`'s own step order, plus the
`overall` aggregate — in which a **dirty** row outranks an unreadable one,
because an answer already in hand is not discarded for a scan that fell short.


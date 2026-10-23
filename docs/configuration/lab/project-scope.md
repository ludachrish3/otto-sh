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
against a whole name — never `re.search`.  `bench` does not admit
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

(project-scope-effects)=
## What the declaration changes

- **Fleet walks are bounded by it.**  `ctx.all_hosts()`,
  `ctx.do_for_all_hosts(...)` and `ctx.run_on_all_hosts(...)` iterate the
  declared universe rather than the whole loaded lab.  See
  {doc}`../../cli/run/defaults` for the walk semantics, the union across projects, and
  the whole-lab fallback for projects that declare nothing.
- **Monitoring and coverage walk it too.**  `otto monitor` picks its hosts
  from the same universe, and coverage collection does as well, containers
  included.  A `[coverage]` `hosts` regex ({ref}`settings-coverage`) narrows
  that set further; it never reaches a host outside the universe.
- **A project that cannot load never widens a walk.**  A fleet walk resolves every
  parsed project's declaration, a project the dependency pass skipped included.  A
  load error that would stop a run (an unparseable `settings.toml`, an
  active project's failure, or the failure of a project an active project
  requires) refuses the walk with {class}`otto.session.ProjectLoadError`, the
  same error {func}`otto.open_context <otto.context.open_context>` raises.  A
  switched-off or lab-inactive project's error only demotes it, unless an
  active project requires it, and its declaration still narrows the fleet.
  See {doc}`../../cli/run/defaults` for the walk semantics.
- **It can be computed without a run.**
  {func}`otto.lab.fleet_of_interest <otto.fleet.fleet_of_interest>` returns the
  ids a walk would take, from a loaded lab and its projects, without connecting or
  bootstrapping; {doc}`../../developing/bed-case-study/boards-of-interest` runs it.
- **Providers are gated by it.**  A project's product and dev-tool providers are
  not invoked at all on a host outside its universe, so nothing that project owns
  can attach to a machine it never declared.
- **Explicit targeting is not bounded by it.**  `otto host <id> <verb>`,
  `ctx.get_host("id")` and the `otto host` id listing reach any host in the
  loaded lab — including a jump host the project does not own.

## Merged labs and containers

A host is matched against the **component** lab it came from, never a
composite display name.  Under `otto --lab lab_a+lab_b` a project declaring
`lab_patterns = ["lab_a"]` still applies — its hosts are stamped `lab_a` — and
it targets the `lab_a` hosts only.  A pattern written to match the merged name
(`lab_a\+lab_b`) matches nothing.

Docker containers created during a run inherit their parent host's lab, so a
container joins the same universes its parent is in.

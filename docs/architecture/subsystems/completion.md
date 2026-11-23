# Shell completion and the shim

A warm `otto <TAB>` has to run bootstrap and load Typer (which vendors
click) and rich before it can print a single candidate — none of which the
answer itself needs. `otto._shim` is a second, much smaller entry point
that answers a bash TAB straight from the completion cache, standard
library only, whenever the cache's own bookkeeping says it still describes
the world. Anything it cannot answer that way runs today's path unchanged;
see
{doc}`the CLI page <../../cli/index>` and
{doc}`the cache page <../../cli/cache/index>` for the user-facing
behavior this subsystem serves.

## Two paths, one answer

The shim is a second implementation of Typer's own bash completion, so it must
match byte for byte, not "close enough". The contract is enforced by generating
a corpus of command lines against the bootstrapped app (the real dispatch tree,
not a warm-load stub) and asserting the shim's answer equals Typer's for every
case it accepts, in `tests/unit/shim/test_differential.py`. The corpus is
generated from the serialised tree itself — every command, every option, every
fragment and complete-word shape, under four `OTTO_LAB` environments — and
compared in eight interleaved slices (one parametrized test each, so xdist
spreads what was a single 4-minute test across workers; every slice walks
every command group). The tests assert three things. Zero mismatches over
every case the shim answers. A floor of `MIN_ANSWERED_PER_CHUNK` answered
cases per slice, so a change that collapses coverage into hand-overs fails
loud rather than passing quietly. And a counted histogram of
the hand-over reasons: each must fall in a named class (a `live` completer, a
`tunnel add --hosts` fragment past its first comma, a value attached to a flag
that takes none, stacked short flags), and the two
"unknown option"/"unknown command" classes are counted EXACTLY against the
hand-written lines that produce them. That last one is load-bearing: a generated
shape that Typer never reaches hands over on both sides and looks like coverage
otherwise — an earlier version of the generator walked host verbs with no host
id, and most of its hand-overs were that artefact rather than any rule of the
shim's. Every function in the shim that mirrors Typer or click names what it
mirrors, so a change to one side is a deliberate change to both.

## The entry

`shim` is one of the completion cache's two sections, written in the same
rebuild as `names`. What the sections are, how they stay fresh, and who
rebuilds them is on {doc}`completion-cache`. The shim reads these fields of
its payload (built by `otto.config.completion_tree.build_shim_payload`):

- `keys` — the `names` key set as `[path, mtime_ns, size]` triples
  (`stat_triple`), directories included, so the shim sees a new or removed
  file by its directory's mtime without running any glob;
- `inventory` — the inventory's freshness as a stat check can verify it:
  `none`, `stat` with the triples of the files a file-derived backend read,
  or `opaque`, which always hands over (`inventory_block`);
- `tables` — the project roots whose tables in the test-names cache a
  test-name or `-m` TAB reads, in order;
- `tree` — described next.

The payload holds no TTL. The shim enforces the taint and the `ttl_seconds`
the writer stored with the `shim` entry and with the `names` entry it also
serves from, as every reader of a section does ({doc}`completion-cache`,
"Freshness").

At a test-name or `-m` site the shim answers from those projects' tables in
the test-names cache (the cache file's `__collected_tests__` namespace),
with the names and markers pytest last recorded. It checks each table's
`env` and age, but stats nothing the table tracks; the check of the test
files runs in a background process, at most once per check window. How the
tables are written, checked and refreshed is on {doc}`completion-cache`
("The test-names cache").

When a check fails because the cache itself is stale, the hand-over carries
`stale=True` and the TAB that finds it rebuilds the cache
({doc}`completion-cache`, "Who reads, who refreshes").

## The tree

The tree is the bootstrapped command graph, serialised once per cache write.
A `Node` is `{name, params, commands, group}`; `group` is the *parser's*
shape, not "has subcommands" — it records whether the underlying class is a
`TyperGroup` or a leaf `TyperCommand`, which is what actually decides how
click parses interspersed options and positionals. A `Param` carries its
flags, whether it takes a value, `multiple`, `nargs`, its list-segment
separator, and a `Source` describing how to answer it (`static`, `payload`,
`tests`, `markers`, `observed`, `echo`, `none`, or `live`). `otto test`'s `NAMES` is a
variadic positional with a `tests` source and no separator, so each word
completes one test name. Host verbs get their own
layer: the root node carries a `host_classes` map from each host class name
to that class's verb nodes, since a verb shared across classes can have a
different signature per class; the resolver picks the right map once it
knows the typed host id's class. A `payload` source may also be
`host_scoped`: the resolver keeps the typed host id and the host group's
`--term`, and the source reads that host's bucket of `logins_by_host`
(`--user` on the host verbs), filtered by the source's `flavour` and the
term. The filter is written once, in `otto.cli.completers.filter_logins`,
and mirrored in the shim.

Every completer in the CLI declares its `Source` with the
`completion_source` decorator (`otto.cli.completers`); the tree serialiser
just reads that declaration off the completer function. A completer with no
declaration is serialised `live` — always a hand-over — and a unit pin
enumerates every completer in the CLI and fails by name if one lacks a
declaration, so a newly added completer cannot silently regress its TAB into
the slow path.

## The resolver

`otto._shim_complete.resolve` walks the argument words before the fragment
against the tree, mirroring click's own two-parser split: a `TyperGroup`
stops consuming options at the first non-option word
(`allow_interspersed_args = False`) and hands the rest to subcommand
resolution, while a leaf command interleaves options and positionals freely.
The walk understands the option forms click accepts exactly (`--long`,
`--long=value`, `-svalue`), `--`, a pending value-taking option, positional
consumption and subcommand descent; a stacked short-flag group and any long
option that is not an exact match hand over rather than guess. Anything else
the walk does not model also raises `Handover` — an unknown option, an
unknown subcommand, a `live` source, an option taking more than one word,
and a value attached to a flag that takes none (`--debug=x`), which click
answers by raising `BadOptionUsage` and abandoning the parse of that whole
command — and the caller falls back to `otto.cli.main.entry()`, which is
always right.

## The window

A successful validation pass of the `names` key set leaves a marker file
beside the cache (`otto.config.cache_maintenance.MARKER_FILENAMES`); a
marker fresher than the
cache, not dated after the current time, and under `SHIM_WINDOW_SECONDS` (60)
old lets the next TAB skip the stat pass entirely. A marker dated in the
future — the clock stepped back — is not trusted: it would pass the
fresher-than-the-cache check against every rewrite, and its window would last
the size of the step plus the minute. Because the marker must be at least as
new as the cache's own mtime, rewriting the cache invalidates it
automatically — nothing has to remember to delete it. The marker
lifecycle under the cache commands is on {doc}`../../cli/cache/index`.

The second marker, `tests`, is not a window over a stat pass: it records when
the test-names cache was last checked, and a test-name TAB starts the next check
once it is `CHECK_WINDOW_SECONDS` (ten minutes) old ({doc}`completion-cache`,
"The collect child").

**Known inequalities**, beyond the window itself:

- `otto test --list-markers <TAB>` — on the *full* path, the eager,
  value-only `list_markers_callback` (`otto.cli.test`) renders its Rich
  panels straight into the completion stream, because that callback has no
  way to see that parsing is resilient. This is a pre-existing bug in the
  full path, being fixed separately; the shim never calls the callback, so
  it answers this site cleanly.
- Typer's vendored click's `get_help_option_names` returns
  `list(set(...))`, so the *order* of `-h`/`--help` candidates is
  per-process (hash-seed dependent). The cached tree freezes the order from
  whichever process wrote it, so the
  shim's order can differ from a given full-path process's — the *set* is
  always identical.

## What a TAB costs, and how it is measured

A TAB's file-operation cost is decided by the path that answers it, not by
the kind of its parameter. There are two families of path:

- **The shim answers** from the cache once it has validated it. The
  validation is either the `names` stat pass or the test-names tables, and
  what the source kind does after that (`payload`, `static`, `none`,
  `observed`, `tests`, `markers`) is in-memory work over data the validation
  already loaded.
- **The full CLI answers** after a hand-over. Its cost is whatever the
  completer does once it runs.

The import budget (`scripts/import_budget.py`; what it counts and how its
ceilings work is on {doc}`../startup-performance`) measures one warm surface
for each of these cost profiles. Each is gated in every Python minor's
ceilings file. `unix+templated` is two labs [combined with
`+`](../../cli/index.md#combining-labs-with-): the generated project's json `unix`
lab and the `templated` lab its jsonnet source renders, so one answer holds a host
from each.

| Surface | The TAB | What it measures |
|---|---|---|
| `completion_project_warm` | `otto <TAB>` | the shim's command menu after the `names` stat pass; it stands for every command-name, option-name and host-verb menu |
| `completion_host_warm` | `otto -l unix+templated host <TAB>` | a `payload` site the shim answers, with a jsonnet lab source in the `names` key set |
| `completion_test_warm` | `otto test T<TAB>` | a test-name site the shim answers from the test-names tables, starting no collect child |
| `completion_docker_observed_warm` | `otto docker logs --parent test1 <TAB>` | an `observed` site the shim answers from `__docker_observed__` |
| `completion_project_handover` | `otto tunnel remove <TAB>` | a `live` site: the hand-over and the completer behind it |
| `completion_hosts_past_sep_handover` | `otto tunnel add --hosts` past its first comma (the measured TAB types `test1,`) | a list past its separator: the hand-over, host discovery and the narrowing to test1's segment |
| `completion_remote_path_hit` | `otto host budget-ssh get <TAB>` | a remote path whose listing is cached: bootstrap, the reservation gate (no `[reservations]`), the lab and the cached listing |
| `completion_remote_path_hit_reserved` | the same TAB | the same hit with `[reservations]` declared and the reservation answer cached: a second lab load and one sidecar read, and no backend built |

**A count is kept only from the path it names.** Each surface's measured
run must prove which path answered and what it answered, or its count
neither gates nor is recorded. A shim surface proves that the full CLI never
loaded (`SHIM_DENY`) and that the run took the `names` stat leg rather than
the marker window's skip; a hand-over surface proves that its completer's
module loaded. Every TAB surface but `completion_project_handover` checks
its answer, and every one checks that `completion_cache.json` (and the
remote-path sidecar) did not change across the run, so a TAB that found the
cache stale and rebuilt it cannot pass as the warm path. Each check is a
`Surface` field in the script (`expect_not_loaded`, `expect_loaded`,
`expect_answer`, `expect_answer_exact`, `stat_leg`, `cache_unchanged`).
`completion_host_warm`'s rendered host (`expect_answer_templated`) is asked
only of an interpreter that loads a jsonnet binding; on one with none (CPython
3.15 on aarch64 today) the result notes the dropped line in `proofs_skipped`,
and the count is gated all the same.

**Kinds that share a measured path.** A `static`, `none` or `markers` site
costs what its profile's representative costs, so it has no surface of its
own: `tests/unit/shim/test_subvariant_cost.py` records one whole TAB per
kind in a fresh child, through the one recorder
(`tests/_fixtures/recorded_tab.py`), and asserts that its file operations
equal the representative's. That recorder counts `os.stat` only; an
`os.lstat` or a `pathlib` stat added to one of those kinds' paths is counted
only by the measured representatives, under strace.

**Unmeasured on purpose.**

- **The remote-path listing miss.** It is the one TAB that contacts a host,
  and its cost depends on that host and its hops.
- **A reservation-cache miss** at a remote-path site. Its cost is the
  reservation backend's.

**One limitation, declared.** The remote-path hits read a listing their
writer stamped with the real clock, and the TAB trusts a listing for
`LISTING_TTL_SECONDS` (`otto.config.remote_completion_cache`, 45 s). A machine
loaded enough to take longer than that between the writer and the end of the
measured run measures a miss. The miss fails closed: the proofs catch the
live listing, so no count is kept. The verdict then adds that the seeded
listing expired, with the seconds that passed, so the failure reads as
overload, not a regression. Measured runs take about a second, more than 25
times inside the TTL.

### Adding a completion site

`tests/unit/import_budget/test_completion_coverage.py` enumerates every
value-taking parameter of the serialised tree, through every host class's
view (`tests/_fixtures/completion_sites.py`), and fails, naming the site,
when no gated surface measures it. Two maps next to `SURFACES` decide which
surface that is:

- `SITE_PROFILES` maps a site the shim answers by its source kind. A new site
  of an existing kind needs nothing. A new kind needs an entry, and a
  surface of its own if no existing validation reads its data. `echo` has no
  site today, so it has no entry; its first site fails the test until it is
  mapped.
- `HANDOVER_SITES` maps each site the shim hands over by its identity
  (command path, host class, parameter), one entry per host class. A site
  hands over when its source is `live`, when it is a list that goes `live`
  past its separator, or when it takes more than one word. A new one needs
  an entry, pointing at the surface whose path it shares, or at a new
  surface when none does.

The test also fails on a listed site that no longer exists or no longer
hands over. `VARIANT_SURFACES` names the measured completion surfaces no site
maps to (`completion_project_warm`, `completion_remote_path_hit_reserved`),
so a surface that nothing reaches fails too. Adding a surface is in
[Contributing](../../contributing.md#the-import-budget).

### Decided twice

The shim answers from each completer's declaration with its own standard-library
code, so some decisions are made once in the completers and again in the
shim. Where both sides answer the same site, the differential test (above)
compares them. The list records where they are; nothing in it is scheduled
to change, and a change to one side is a change to both:

- **Completer bodies and the shim's interpreter of the same declaration:**
  - `otto.cli.completers.filter_logins` and `_host_logins`;
  - `otto.cli.completers.selected_lab_names` and `selected_labs`;
  - `otto.cli.completers.lab_scoped_host_ids` and `_lab_host_set`;
  - `otto.cli.docker._default_parent_for_tab` and `_default_parent`;
  - `otto.cli.docker._image_ref_completer` and `_observed_values`;
  - `otto.config.completion_cache._fresh_lists` and `_fresh_sub_entry`;
  - `otto.cli.main._log_level_completer` and `_static_values`;
  - `otto.link.derive.link_attaches` and `_link_attaches`.
- **Two sources for the links site:** the shim answers `otto link` from the
  cached `links` key, and `otto.cli.link._link_completer` recomputes the
  links from the projects, ignoring that key.
- **The past-the-comma hand-over:** the shim hands over because the `hosts`
  source is `live_past_sep`, and `otto.cli.tunnel._hosts_completer` checks
  for the comma again to decide whether to narrow.
- **Separated-list completion:** `otto.utils.complete_separated_list` and the
  shim's copy of it.
- **Caches the shim never reads:** the remote-path and `tunnel remove` sites
  each keep one.
- **Walks of the context chain:** `selected_lab_names` and
  `host_group_request` in `otto.cli.completers`, and `_collect_chain_params`
  in `otto.cli.remote_completion`, each walk the parent contexts for the
  parameters they need. The shim's `Resolution` carries the same values
  (`root_lab_values`, `host_id`, `term`) from its own parse of `COMP_WORDS`.
- **Host-class verbs:** the serialised per-class views the shim reads, and
  `HostGroup.scoped_names` (`otto.cli.expose`), which the full CLI calls.
- **Staleness:** the shim's stat pass and the Python readers' digests.

The names without a module are in `otto._shim_complete`.

## Where the code lives

- `otto._shim` — console-script entry point; decides `--version`, a bash
  TAB, or a hand-off to `otto.cli.main.entry`.
- `otto._shim_complete` — the resolver: locate the cache, validate it, parse
  `COMP_WORDS`, answer. Standard library only.
- `otto.config.completion_tree` — the tree serialiser (`serialize_tree`,
  `build_shim_payload`, `inventory_block`); slow (write) path only.
- `otto.config.cache_sections` — the `shim` section's registration, its
  digest composed from `names` ({doc}`completion-cache`).
- `otto.config.cache_maintenance` — the marker filenames and the `names`
  window constant, and the `clear`/`prune` walk that removes them.
- `tests/unit/shim/test_differential.py` — the equality proof.
- `scripts/import_budget.py` — the TAB surfaces and the site map
  ("What a TAB costs, and how it is measured", above).

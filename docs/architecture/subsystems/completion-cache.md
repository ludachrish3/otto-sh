# The completion cache

The completion cache is one JSON file per workspace. It holds what shell
completion and the root help screen need to know about your repos: command
and instruction names, `otto test`'s registered flags, host ids, the
serialised command tree the console-script shim answers a bash TAB from,
and, for each repo, a table of the test names and markers pytest collected.
This page is the one place that explains what the file holds, how otto
decides whether it can trust it, who reads it, and who writes it. The shim
that answers from it is on {doc}`completion`, the user-facing commands that
inspect and delete it are on {doc}`../../cli/cache/index`, and what a user
sees of the test names is on {doc}`../../cli/test/selection`.

## Why there is a cache

A TAB keystroke and `otto --help` both need the names your repos register.
Without a cache, getting those names means running
{func}`~otto.bootstrap.bootstrap`, which imports every repo's init modules.
That is user code. It may be slow, it may print, and it may be broken. None
of that is acceptable while the user waits for a
candidate list, and anything printed into a completing shell corrupts the
list the shell is parsing.

The other constraint is the filesystem. On a network filesystem every path
syscall costs a round trip to the server, so wall time is roughly the round
trip times the number of `open`, `stat` and directory-listing calls
({doc}`../startup-performance` has the cost model and the measurements). The
cache therefore has two jobs: answer without running user code, and decide
whether it is still valid by touching as few paths as possible.

## Where it lives and what it's keyed on

The file is `$OTTO_HOME/<workspace key>/completion_cache.json`, with
`OTTO_HOME` defaulting to `~/.otto`. The workspace key comes from the set of
SUT directories in `OTTO_SUT_DIRS` ({func}`otto.config.home.workspace_key`):
a short hash plus a readable slug. It does not depend on the directory otto
was run from, so one workspace has exactly one cache.

A few terms recur below:

- A file's **stat triple** is `path|mtime_ns|size`, the answer to one `stat`
  call.
- A section's **key set** (its **key paths**) is the list of files and
  directories whose stat triples are hashed into that section's digest. An
  edit to any of them moves the digest, which makes the section stale.
- The **fast path** answers from the cache without running
  {func}`~otto.bootstrap.bootstrap`. The **full path** bootstraps and lets
  Typer answer, which is always right, only slower.
- The console-script shim **hands over** a TAB when it cannot answer it from
  the cache: it passes the TAB to the full path in the same process.
- A completion **site** is the value a TAB is completing: a test-name site
  is a TAB at `otto test`'s `NAMES` positional, and an `-m` site is a TAB
  inside the value of `-m`.
- A repo's table in the **test-names cache** is what pytest last collected
  from each of its test files ([below](#the-test-names-cache)).

- **One file.** Every reader validates what it needs from a single `open`.
  That is the cheapest a cache on a network filesystem can be.
- **One schema stamp.** The top-level `"schema"` must equal
  `SCHEMA_VERSION` in `otto.config.completion_cache` (the shim keeps its own
  copy, pinned equal by a test). A file from any other schema is a miss, and
  the next write keeps only its reserved namespaces (below).
- **Atomic writes.** Every write goes to a temporary file beside the cache
  and is moved into place with `os.replace`. A reader on any machine sees the
  old file or the new one, never a mix. No reader takes a lock.

## The sections

The file stores a `"sections"` map. Each section has its own digest, its own
write time and its own taint flag, so a reader validates only the section it
needs:

```text
{"schema": N,
 "sections": {"names": {"fingerprint", "generated_at", "tainted", "payload"},
              "shim":  {...}},
 "__collected_tests__": {...},
 "__dynamic_tunnels__": {...},
 "__docker_observed__": {...}}
```

The sections are registered in `otto.config.cache_sections.SECTIONS`. A
section is rebuilt from what bootstrap registered, so nothing in one comes
from a test file.

**`names`** holds everything whose source is bounded by the files that can
register something. Its payload keys are `instructions`, `test_options`,
`hosts`, `hosts_by_lab`, `host_drops`, `docker_hosts`, `docker_use_cases`,
`docker_images`, `docker_services_by_use_case`, `repos`, `term_backends`, `transfer_backends`, `usernames`, `commands`, `labs`,
`host_classes_by_id`, `projects`, `links`, `logins_by_host` and
`docker_default_parent_by_lab` (each lab to the parent the docker verbs default to by the one rule;
a lab the rule refuses is absent, and the container ids in `hosts` and `hosts_by_lab` sit under that parent only).

- `test_options` is the `test` verb's merged flags, the options classes
  registered for `test` serialised as plain data, so the Typer fast path in
  `entry()` offers them on `otto test --<TAB>` without importing an options
  module. A
  verb whose flags cannot be merged (a collision) caches `[]`; `otto test`
  itself is where the collision is reported.
- Its readers are {func}`otto.cli.main.entry`, which installs it for the
  completers and for the root help screen, the shim, which reads its values
  when a TAB completes one of them, and `otto cache info`, which reads
  `host_drops` and the host lists.
- Its key set is small: each repo's `.otto/settings.toml`, the files of its
  init modules and its lab files, plus every directory those were found in.
  Test files and pytest configs are not in it: a test file cannot register
  anything, and nothing in this payload reads one.

**`shim`** holds what the console-script shim needs to answer a TAB without
importing otto's CLI: the serialised command `tree`, the stat triples of the
`names` key set (`keys`), an `inventory` block, the effective `ttl_seconds`,
and `tables`, the SUT directories whose tables a test-name or `-m` TAB
reads. Its only reader is `otto._shim_complete`.
{doc}`completion` describes each field from the shim's side. It has no key
set of its own: it describes the same registrations as `names`, so it is
stale exactly when `names` is, and its digest is composed from `names`'s
(below).

**Test names have no section.** A section is rebuilt by a process that has
just bootstrapped, which imports no test file, and its digest covers a
fixed key set. Test names come only from pytest, and a key set that covered
them would be the whole test tree, which every `names` reader would then
pay for. They live in each repo's table instead
([The test-names cache](#the-test-names-cache)), which validates itself.

## Freshness

This is how a section is validated; a repo's table has rules of its own
([below](#the-test-names-cache)). A section is served only when all of these
hold:

- it is not tainted;
- its `generated_at` is within the TTL;
- its stored `fingerprint` equals a digest recomputed now.

**The per-section digest** is a sha256 over the section's key paths, sorted
and deduplicated. Each path contributes its stat triple, `path|mtime_ns|size`,
or `missing:<path>` when it does not exist, so creating the file moves the
digest. File contents are never read (`hash_file` in
`otto.config.completion_cache`). Every key set also includes the directories
the enumeration entered. A directory's mtime moves when a file in it is
created, removed or renamed, so a new init module or lab file is detected
without listing the directory again.

**The shared tail** is appended to every section's digest after its key
paths. It carries the two inputs no path list can:

- one `unresolved:<name>` line per init module that resolves under no `libs`
  entry;
- the freshness text of the process inventory, the one host inventory otto
  resolves for the whole workspace: a stat of the inventory file for a `json`
  inventory, the snapshot hash for a cached remote one.

**The shim's digest is composed:** it is `sha256("names:<digest>\n")`
(`Section.derived_from`), so it moves if and only if the `names` digest
moves. Both are computed through one memo, so checking both sections hashes
the `names` key set once.

**The TTL** bounds staleness that no stat can see. It is `CACHE_TTL_SECONDS`
(a day) normally. It drops to `UNFINGERPRINTED_CACHE_TTL_SECONDS` (five
minutes) when any repo has one of these:

- a `[[lab.sources]]` entry on a non-`json` backend;
- any `[reservations]` table, since `--holder` names come only from custom
  backends;
- an init module that resolves under no `libs` entry.

Each of these feeds completion data that the digest cannot track, so the TTL
is the only thing that bounds it. There is one TTL for the whole file, but it
is compared with each section's own `generated_at`.

Nothing is written at all when the inventory cannot report its freshness, or
when asking it raised. A digest built from such an answer is not stable, so
every write would add an entry that is never served.

**Taint.** A section written while {func}`~otto.bootstrap.bootstrap` reported
errors is stored with `"tainted": true` and never served. The reason is that
the failure is stable. The broken file keeps the same stat triple until
someone edits it, so its digest never moves, and serving the partial names
would hide the missing commands until the TTL expired. Storing the entry
anyway records the digests it was built from. A rewrite that would only
repeat an identical tainted entry is skipped, so an unchanged broken
workspace does not rewrite the file on every TAB.

**The shim's stored triples.** The shim does not recompute digests; hashing
needs `hashlib` over every path, and it runs with the standard library alone.
It compares the stored `keys` triples with `os.stat` directly, checks the
`inventory` block (`none`, `stat` with its own triples, or `opaque`, which
always hands over), and checks `ttl_seconds`. A successful stat pass is
remembered for up to a minute by the `names` marker file beside the cache;
the rules are in {doc}`completion`, section "The window".

**Why stat-based rather than content-based.** Hashing contents costs an open
and a full read of every key file, on every check. That is the cost the cache
exists to avoid, and on a network filesystem it is a round trip per file
before the first byte arrives. A stat triple is one call. The price is that
an edit which keeps both the size and the mtime cannot be detected, which is
rare for source files edited by people. The test-names cache makes the same
trade. How stat-based validation behaves when several machines share one
`$OTTO_HOME` is covered in {doc}`../startup-performance`
("Shared-home safety").

## Reserved namespaces

Three top-level keys sit beside `"sections"`. A rebuild writes none of them,
and each has writers of its own:

- **`__collected_tests__`** holds each repo's table in the test-names
  cache, keyed by its SUT directory. Only a pytest collection writes it: an
  `otto test` run, `--list-tests`, `otto -n test`, `--list-markers` on a
  cold table, and the collect child a test-name or `-m` TAB starts. What a
  table holds and how it stays right is the next section.
- **`__dynamic_tunnels__`** holds the tunnel ids `otto tunnel` last
  discovered, for `otto tunnel remove <TAB>`. It is written by
  `otto tunnel list` and `otto tunnel remove` and expires after two minutes,
  because tunnels come and go without any file changing. It is keyed by a
  narrow *tunnel-scope digest* (`_tunnel_scope_digest`): the settings, the
  lab files and the inventory term, but no init modules, no pytest config
  and no test file. Tunnel ids are discovered from the workspace's lab and
  the live process/argv state, never from a test, so a wider digest would
  cost a command that never reads a test file a stat per test file. The
  short TTL does the rest of the freshness work.
- **`__docker_observed__`** holds what each docker host's daemon last said,
  for the docker completers (container names and ids, image references).
  Its own `schema_version` (1) sits beside a `hosts` map; each host has up to
  two independently stamped sub-entries, `containers` (`names`, `ids`) and
  `images` (`refs`, `ids`), each with an `observed_at`. Containers expire
  after 15 minutes and images after 24 hours, because containers come and go
  and an image reference is still worth offering a day later; each list is
  capped at 200 entries so a warm TAB stays one file read. The writers are
  the verbs that ask a daemon, and each records one kind: `ps`, `compose ps`,
  `compose up` and `compose down` record containers (15 min); `images`,
  `build` and `compose build` record images (a day). Each replaces
  its host's sub-entry whole, and an answered but empty probe is recorded as
  empty, so the hint vanishes after a `compose down`; a probe that failed, was
  declined, or answered with no row otto could read records nothing. A TAB never writes or deletes: an expired
  sub-entry is simply absent from what it reads.

A docker TAB at `CONTAINER` or `--tag` reads one host's entry here: the host
`--parent` names, or without it the parent the verb itself would use — the
selected lab's entry in `docker_default_parent_by_lab` (the rule is under
{ref}`Which host <docker-which-host>`). Where the map has no entry the rule
refuses, and TAB offers nothing, as the verb would refuse. With several labs
selected (`-l east,west`), TAB offers names only when every selected lab
resolves to the same parent; one refusing lab, or two different parents, offers
nothing, so TAB never offers what the verb would reject. With no lab
selected, TAB answers the map's only distinct value: two labs that map to the
same host still answer it, two different hosts answer nothing.

That guarantee rests on one premise: a host id carries one `docker_priority`
across the labs a selection merges. A host that two lab sources rank
differently is ranked per lab in the cache but by the later source in the
session (`_summaries_by_lab` keeps the first summary; the merged lab keeps the
later component's object), so "every selected lab agrees" holds only when the
inventories agree on the host.

`write_sections` carries all three namespaces across every rewrite, including a
rewrite that drops sections from an older schema. They come from work a
rebuild must never do (a pytest collection, a scan of the lab for tunnels, a daemon's answer),
so dropping them would make the next test-name TAB pay for a collection
again.

## The test-names cache

Each repo's table (`otto.config.collected_tests`, with its own
`schema_version`, 5) holds, for each test file and conftest, what pytest
collected there the last time it read the file: its tests as
`(classes, base name)` pairs, parametrizations collapsed; the markers
applied in it; the error that stopped it collecting, if one did; and the
file's stat from just before pytest read it. Beside the file records it
holds the stat of every directory pytest entered under the test
directories, every file a record's tests depend on (below), the markers
pytest registered, `generated_at`, and `env`. `env` is what decides
collection for the whole repo: the pytest config files at the repo's root
(`pyproject.toml`, `pytest.ini` and the others pytest reads, each watched
whether or not it exists), the repo's `.otto/settings.toml`, the stat of
the site-packages directories pytest and otto are installed in (its mtime
moves when any package is installed or removed), and the Python version,
`sys.prefix` and the pytest and otto versions. The stored layout is in the
`otto.config.collected_tests` module docstring.

**Only pytest writes a table.** Every record in a table comes from one
collection path, `otto.suite.run._run_pytest_session`: it runs the pytest
session, merges what the session read into the stored table
(`updated_table`) and writes the table when that changed it. The one write
without a session is a refresh that found only deleted files, which drops
their records. otto never reads a test file itself, and a cache rebuild
never touches a table.

**The table validates itself.** A table needs no digest. `classify`
(`otto.config.collected_tests`) checks it with one `stat` per file,
directory and dependency it stores, plus the few `env` paths, and never
lists a directory:

- a file whose stat matches its record is fresh, one whose stat moved is
  changed, and one that is gone is deleted;
- a directory whose stat moved has gained or lost an entry, so the next
  collection enters it whole, and pytest, not otto, decides which of its
  files are tests (`python_files`, `collect_ignore`, `norecursedirs` and
  every plugin hook apply as in any run);
- a conftest that changed, appeared or went away makes every file under
  its directory changed;
- a record whose dependency moved is changed;
- a table that is missing, whose `env` differs, or whose whole tree was
  last collected more than a day ago (`CACHE_TTL_SECONDS`, against
  `generated_at`) is **cold**. It vouches for nothing, and only a
  collection of the whole tree brings it back.

A collection of only some files keeps `generated_at` as it was. So a
collection of the whole tree happens at least once a day whatever else
happens, and it is the safety net for what no stat can see.

**Dependencies.** A test file can gain or lose tests when another file
changes: a base class in another test module or a library, an imported
test function, what a star-import brings in, a module imported whole whose
flag decides whether a test exists. Each record names those files, as its
pytest session imported them ({doc}`execution`, "Handing off to pytest",
has how the session finds them), and the table stores each one's stat
once. Tracked: the repo's own files, its libs, an editable install's
source, any directory on `PYTHONPATH`, and a sourceless library through its
`.pyc`. Not tracked file by file: installed packages under `site-packages`
or `dist-packages`, and the standard library, since `env` already covers
both. A dependency that a collection
sees for the first time is stored with no stat, because its stat from
before that read is not known. The test files that use it stay changed
until the next collection reads them and stamps it: one extra collection
of those files, at the next run or check, while their names stay right.
What the tables still can't see, and what a user does about it, has one
home: {doc}`../../cli/test/selection`, "What the cache can't follow".

**One refresh.** A reader of test names that is not a run (the collect
child, `--list-markers`) brings the tables up to date with
`otto.suite.run._refresh_tables`, which does for each repo what a run does
for a repo it must search. A cold table is seeded by one whole-tree
`--collect-only` session, and a warm one collects only what is not fresh.
A table whose only news is a deleted file drops that record with no session.
`--list-markers` has it seed a cold table only. A run, a listing and a dry
run need no separate seed: on a cold table their own session covers the
whole tree, and the table is written from what it collected.

**The check marker.** Every write of a table touches the `tests` marker
beside the cache file (`completion_cache.tests.ok`), because its writer
classified the whole table before it wrote. The collect child touches it
too when it found everything current and wrote nothing. The marker's mtime
is when the tables were last checked.

### The collect child

A completer never runs pytest itself: its standard output belongs to the
shell. It starts the venv's `otto` again with `_OTTO_DUMP_TEST_NAMES=1`,
and that child brings every repo's table up to date and prints nothing
(`collect_child_main`, through `_refresh_tables`). The child first takes
`.completion_collect.lock` beside the cache file. A second child finds it
held and leaves at once, and a lock older than 45 seconds was left by a
child that died, and is taken over.
Only then does the child arm its 15-second deadline and bootstrap, so the
repos' init modules and the collection both run under the deadline. Any
failure (a timeout, a broken init module, a directory whose listing a
refresh could not finish) writes `.completion_collect.failed`, and for a
minute after that no TAB starts another child or waits for one. The child
runs in the workspace home, never in the shell's directory.

It is used in two ways:

- **The seed blocks once.** A TAB that finds a repo's table cold waits for
  the child, then answers from what the child wrote. It waits at most two
  seconds past the child's deadline, and then kills it. A repo the child
  could not seed offers no names until a run, a listing or a later TAB
  seeds it. The wait happens once per cold table, not once per TAB.
- **The refresh is detached.** Once a TAB has printed its answer, it may
  start the child in a session of its own, with `/dev/null` for every
  stream, and never wait for it. The child re-reads only what moved, and
  the TABs after it offer what it found.

Every test-name or `-m` TAB answers the same way, whichever path answers
it: the shim in bash, or the completers' `completion_view` on the full path
(zsh, fish, or a bash TAB the shim handed over). It reads the tables and
answers with the names every record held when pytest last read it. It stats
nothing a table tracks: only each table's `env` paths, a handful whatever
the corpus size. A cold table sends the shim's TAB over (stale) to the full
path, which seeds it. When the `tests` marker is missing or older than the
**check window** (`CHECK_WINDOW_SECONDS`, ten minutes), the TAB touches the
marker and starts the child once its answer is written, unless the lock is
held or the cooldown applies. So a test that was added, edited or deleted
reaches a TAB one check late: at most the window plus one TAB after the
change. A finished run, `--list-tests` or dry run, or a child, that updated
a table has touched the marker, so a TAB right after one of them starts
nothing. `--list-markers` does so only when it seeded a cold table, and an
interrupted run may leave some repos unread.

A run never depends on the child. It classifies every table before it
decides anything, and collects what moved in its own session, whatever the
child has or hasn't done yet.

### Bytecode stays out of the test directories

Every pytest session otto starts, the collect child's included, writes the
bytecode of what it imports under the workspace home's `pycache/`
directory, not into a `__pycache__` beside the source; a user's
`PYTHONPYCACHEPREFIX` is used instead when it is set. A collection turns
pytest's cache plugin off (`-p no:cacheprovider`), and a run keeps pytest's
cache in the home's `pytest-cache/`. So a session adds no entry to a test
directory, and a directory's stat moves only when a file in it is added,
removed or renamed. Otherwise a `__pycache__` or a `.pytest_cache`
appearing in a test directory would move a stored stat and cost one more
collection (issue #456). This covers otto's pytest sessions only. The repos'
init modules, which otto imports at startup outside any session, still
compile beside themselves unless `PYTHONPYCACHEPREFIX` is set
({doc}`../startup-performance`).

## Who reads, who refreshes

Only completion and the root help screen (`otto`, `otto --help`, `otto -h`;
`ROOT_HELP_ARGV` in `otto.cli.main`) read the sections, and only they check
and rebuild them. (`otto cache info` also reads them, to report on them.)
The test-names cache is read by test-name and `-m` completion and by
`otto test`'s runs, listings and dry run, which also write them. Every other
command, `otto host … exec` included, bootstraps and dispatches without
reading, validating or writing either. Checking validity costs a stat per
tracked path, and a command that never reads the cache has no reason to pay
that.

```{graphviz}
digraph cache_paths {
    rankdir=LR;
    node [shape=box];

    tab [label="bash TAB"];
    shim [label="otto._shim\nnames: stored triples\ntest names: env stats only"];
    answer [label="answered from shim\n(no otto CLI import)", style=dashed];
    names [label="entry(): names section\nvalid → set_completion_names"];
    help [label="otto --help (root)"];
    rebuild [label="bootstrap +\none stat per path:\nvalidate names and shim,\nrebuild on a miss"];
    tables [label="test-names cache\n(__collected_tests__)"];
    child [label="collect child\n(seed: waited for;\nrefresh: detached)"];
    run [label="otto test run,\nlisting, dry run"];
    ordinary [label="any other command"];
    dispatch [label="bootstrap + dispatch\n(cache never touched)"];

    tab -> shim;
    shim -> answer [label=" valid"];
    shim -> names [label=" hand over,\n cache fine"];
    shim -> rebuild [label=" hand over,\n cache stale"];
    help -> names;
    names -> rebuild [label=" names miss"];
    answer -> child [label=" check due", style=dashed];
    child -> tables [label=" classify,\n collect, write"];
    run -> tables [label=" classify,\n collect, write"];
    ordinary -> dispatch;
}
```

| Invocation | Answered from | Validates and rebuilds |
| --- | --- | --- |
| `otto --version` | nothing | nothing |
| bash TAB answered by the shim | `shim` and `names`; at a test-name or `-m` site also each repo's table | compares the stored `names` triples (unless the marker vouches for them) and, at a test-name or `-m` site, each table's `env`; never rebuilds; starts the detached collect child once the check window has lapsed |
| bash TAB handed over because the cache is stale (`Handover.stale`), a cold table included | no section: it skips the `names` fast path and bootstraps | validates both sections and rebuilds on a miss; skipped with no repos, no home or an uncacheable inventory |
| bash TAB handed over for any other reason, or a zsh/fish TAB | `names` | on a `names` miss, bootstraps, validates both sections and rebuilds on a miss |
| test-name or `-m` TAB that reached the full path | `names`, then each repo's table | like any handed-over TAB for the sections; checks each table's `env` as the shim does; a cold one waits once for the collect child, and once the check window has lapsed the child starts detached after the answer |
| root `otto --help` | `names` | on a `names` miss, bootstraps, validates both sections and rebuilds on a miss |
| an `otto test` run | each repo's table | never a section; classifies every table before any session, and writes each table from its own sessions |
| `otto test --list-tests`, `otto -n test` | each repo's table | never a section; collect as a run would, with `--collect-only`, and write each table from those sessions |
| `otto test --list-markers` | each repo's table | never a section; seeds a cold table with one whole-tree collection (`_refresh_tables`) and reads a warm one as it is |
| the collect child | each repo's table | never a section; seeds a cold table, collects what moved in a warm one, writes the tables and touches the `tests` marker |
| any other command | nothing | nothing |
| `otto tunnel list`/`remove` | nothing | never; writes `__dynamic_tunnels__` only (keyed by the narrow tunnel-scope digest, which reads no test file) |
| a docker verb that asks a daemon (`ps`, `images`, `build`, `compose ps`/`build`/`up`/`down`) | nothing | never a section; writes the host's `__docker_observed__` sub-entries only |
| a docker TAB (`CONTAINER`, `--tag`) | `names` for the default parent; `__docker_observed__` for names and references | reads only; never contacts a host, never writes or deletes |

**A stale TAB repairs the cache.** When the shim hands a TAB over, it says
why. `Handover.stale` is true when the cache itself is at fault: no cache
file, a schema mismatch, a missing section, an expired TTL, a stored `names`
path that is gone, has appeared or has changed, or, at a test-name or `-m`
site, a repo whose table is missing, expired or has another `env`.
`otto._shim.main` passes the flag on as `entry(cache_stale=True)`.
{func}`~otto.cli.main.entry` then skips the `names` fast path, bootstraps,
checks and rebuilds the sections, and lets Typer answer the TAB; at a
test-name or `-m` site the completer then seeds a cold table through the
collect child. That TAB is slow once; the next one is answered by the shim.
Every other hand-over reason leaves the flag false, so those TABs keep their
normal cost and do not bootstrap for the cache's sake; the parsing reasons
are listed in {doc}`completion`, section "The resolver". Two of the false
ones are about the cache and are false on purpose. A tainted entry must not
count as stale, because rebuilding cannot help until the broken file is
fixed. An opaque inventory cannot be checked by stat at all, so a rebuild
would not make the next TAB answerable either.

**A missing or stale `names` section also rebuilds.** Completion and root help
read `names` first. On a miss they bootstrap. After bootstrap, `entry()`
first asks `cache_is_writable`, which stats no key path. With no repos,
no home or an uncacheable inventory, no entry could be stored, and nothing
below runs. Otherwise `cache_is_stale` validates both sections in one
read, and `entry()` rewrites both on any miss, in one atomic update. The
digests it computed are handed to the writer, so no key set is hashed twice.

**A rebuild stats each path once.** The validity check and the rebuild ask
about the same `names` key paths twice: once for the digests and once for
the shim's stored triples. `entry()` wraps the check and the rebuild in
`otto.config.corpus_snapshot`, a scope held in a context variable. Inside
it, `stat` answers each path the first time and replays the answer after
that; outside a scope it is the plain call, and nothing the scope stores
outlives the `with` block.

**The rebuild imports no test file.** `otto test` is a single command with
no subcommand per class, so the tree the rebuild serialises never reads a
suite registry, and no section holds a test name. Nothing in a rebuild reads
a test file, imports pytest or touches the test-names cache; the `otto test`
flags it stores come from the options registry, which bootstrap has already
filled from init modules. That is also why an edit to a test file can never
change a cached flag. The schema history is in the comments above
`SCHEMA_VERSION` in `otto.config.completion_cache`: 21 dropped the `suites`
payload when `otto test` became a single command, 22 added `test_options`,
and 23 removed the `tests` section, whose static scan of the test files was
the last thing in a rebuild that read one. 26 added
`docker_default_parent_by_lab`.

## What it costs, and the guards that hold it

- **A warm TAB or root help** costs the `names` key-set stats (none while
  the marker vouches for them) plus one open of the cache file. A bash
  test-name or `-m` TAB adds only a stat of each table's `env` paths,
  whatever the size of the test tree.
- **A rebuild** costs the `names` key-set stats and the bootstrap. It reads
  no test file and imports no pytest. It happens once per change to the
  workspace.
- **Checking a repo's table** costs one stat per file, directory and
  dependency it tracks. `otto test`'s runs, listings and dry run pay it, and
  so do the collect child and every full-path test-name TAB; a bash TAB
  never does.
- **A collection** costs pytest's own start plus roughly a hundred file
  operations per test file it imports. The tables keep a run's session to
  the files that hold its names and the files that changed.
- **Everything else** costs nothing: `otto --version` and ordinary commands
  do no completion-cache I/O.

The import-budget guard (`scripts/import_budget.py`, run by `make profile`)
pins these on a generated repo shaped like a real one:

- The scaling pins measure the same command at 50 test files in 5
  directories and at 200 in 20, and allow at most five more `workspace`
  file operations and at most fifteen more in the whole-process `file_ops`,
  which also carries the import system's run-to-run wobble:
  `test_help_io_does_not_scale_with_corpus_size`,
  `test_completion_io_does_not_scale_with_corpus_size`,
  `test_completion_handover_io_does_not_scale_with_corpus_size`,
  `test_dispatch_io_does_not_scale_with_corpus_size`,
  `test_a_test_name_tab_does_not_scale_with_corpus_size` (a bash test-name
  TAB the shim answers) and `test_cold_rebuild_does_not_scale_with_corpus_size`
  (a cold rebuild; its `workspace` count only).
- The `test_repo` surface gates a repeated `otto test TestTop0`: its seed is
  the same command, so the measured run finds a current table and imports
  only the file that holds the name.

The scaling pins assert a shape, not a count, so they hold whatever the
ceilings are. The counters are described in {doc}`../startup-performance`
("What holds these numbers in place").

## Deliberate non-goals

- **Rebuilding only the stale sections** (issue #446). A rebuild rewrites
  both sections. `shim` is derived from `names`, so neither is ever stale
  without the other.
- **Seeding in a detached process** (issue #447). A stale section is
  rebuilt, and a cold table is seeded, while the TAB that found it
  waits: both are what that TAB's answer is made of, and a detached seed
  would answer it with nothing. The refresh of a warm table is detached
  ([The collect child](#the-collect-child)), because a warm table already
  has an answer, one check late at worst. The process lifecycle #447 said a
  detached rebuild would need (spawning, a lock with stale-lock recovery, a
  cooldown after a failure, a deadline) exists now for that refresh; moving
  the section rebuild onto it would remove no work.
- **zsh and fish.** Only bash TABs reach the shim. Other shells use the
  `names` fast path in `entry()` and rebuild only when `names` misses; at a
  test-name or `-m` site they answer from the test-names cache as the shim does.
- **Root help and the test-names cache.** `otto --help` validates `names`
  alone and never reads or writes the test-names cache: a help screen has no
  test names on it. The next test-name TAB, run or listing seeds a cold table.

## Where the code lives

- `otto.config.completion_cache` — the file: location, schema, TTLs,
  `hash_file`, `read_sections` / `write_sections`,
  `cache_is_writable` / `cache_is_stale`, the collectors, the reserved
  namespaces, and the collect child's lock, cooldown and spawn.
- `otto.config.cache_sections` — the `Section` registry (`names`, `shim`),
  the key-set functions, the shared tail and the composed digest.
- `otto.config.collected_tests` — the test-names cache: `classify`,
  `updated_table`, `completion_view` and `collect_child_main`.
- `otto.suite.run` — `_run_pytest_session`, the one collection path that
  writes a table, the run's decision of which files to collect, and
  `_refresh_tables`, the refresh the collect child and `--list-markers` share.
- `otto.config.corpus_snapshot` — the one-stat-per-path scope used during a
  rebuild.
- `otto.config.completion_tree` — `build_shim_payload`: the serialised tree,
  stored triples, inventory block and table list.
- `otto.config.cache_maintenance` — the marker files, and the walk behind
  `otto cache clear` and `prune`.
- {mod}`otto.config.home` — the workspace key and home.
- {func}`otto.cli.main.entry` — who reads and who rebuilds.
- `otto._shim` / `otto._shim_complete` — the bash TAB reader, the
  `Handover.stale` flag, the check window and the detached spawn.
- {mod}`otto.cli.test` — `test_verb_params`, the merged `otto test` flags
  that `test_options` serialises, and the test-name and `-m` completers.

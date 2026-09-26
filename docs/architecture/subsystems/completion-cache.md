# The completion cache

The completion cache is one JSON file per workspace. It holds what shell
completion and the root help screen need to know about your repos: command,
instruction and suite names, host ids, test names, and the serialised command
tree the console-script shim answers a bash TAB from. This page is the one
place that explains what the file holds, how otto decides whether it can
trust it, who reads it, and who rebuilds it. The shim that answers from it is
on {doc}`completion`, and the user-facing commands that inspect and delete it
are on {doc}`../../cli/cache/index`.

## Why there is a cache

A TAB keystroke and `otto --help` both need the names your repos register.
Without a cache, getting those names means running {func}`~otto.bootstrap.bootstrap`,
which imports every repo's init modules, and loading every repo's test files
to register suites. That is user code. It may be slow, it may print, and it
may be broken. None of that is acceptable while the user waits for a
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
- A completion **site** is the value a TAB is completing: a `--tests` site
  is a TAB inside the value of `--tests`.

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
              "tests": {...},
              "shim":  {...}},
 "__collected_tests__": {...},
 "__dynamic_tunnels__": {...}}
```

The sections are registered in `otto.config.cache_sections.SECTIONS`.

**`names`** holds everything whose source is bounded by the files that can
register something. Its payload keys are `instructions`, `suites`, `hosts`,
`hosts_by_lab`, `host_drops`, `docker_hosts`, `docker_use_cases`,
`term_backends`, `transfer_backends`, `usernames`, `commands`, `labs`,
`host_classes_by_id`, `projects`, `links` and `logins_by_host`.

- Its readers are {func}`otto.cli.main.entry`, which installs it for the
  completers and for the root help screen, the shim, which reads its values
  when a TAB completes one of them, and `otto cache info`, which reads
  `host_drops` and the host lists.
- Its key set is small: each repo's `.otto/settings.toml`, the files of its
  init modules, its pytest config files, its **top-level** test files, and
  its lab files, plus every directory those were found in.

**`tests`** holds the static `--tests` name *floor* and the `-m` marker
floor (`tests` and `markers`), both from one `ast` scan of the test sources.
A floor is the set of names that a static scan can see. It is always
available, and a real pytest collection can only add to it (see the
reserved namespaces below). No test code runs to produce it.

- Its readers are the shim, at a `--tests` or `-m` site, and the `--tests`
  and `-m` completers on the full path.
- Its key set is the whole test corpus: every file matching the repo's
  pytest `python_files` under each tests directory, recursively, plus every
  `conftest.py` there and on the path up to the SUT root, plus the pytest
  config files and `settings.toml`, plus every directory walked.

**`shim`** holds what the console-script shim needs to answer a TAB without
importing otto's CLI: the serialised command `tree`, the stat triples of the
`names` and `tests` key sets (`keys`), an `inventory` block, the effective
`ttl_seconds`, and `tests_digest`, the fingerprint the collected test names
are stored under. That fingerprint is `compute_fingerprint`, not the `tests`
section's digest; see [Reserved namespaces](#reserved-namespaces). Its only
reader is `otto._shim_complete`.
{doc}`completion` describes each field from the shim's side.

Why `names` and `tests` are separate: a names reader should never pay for the
corpus. `otto ho<TAB>` and `otto --help` validate `names`, whose key set grows
with the number of init files and top-level test files, not with the number
of tests. Only a TAB at a `--tests` or `-m` site validates `tests`, whose key
set is the whole corpus walk, because nothing smaller can say which tests
exist.

Why `shim` has no key set of its own: it serves both kinds of TAB, so it is
stale exactly when `names` or `tests` is. Its digest is composed from theirs
(below), and the stat triples it stores are theirs. Giving it a key set of its
own would mean walking the corpus a second time to learn nothing new.

## Freshness

A section is served only when all of these hold:

- it is not tainted;
- its `generated_at` is within the TTL;
- its stored `fingerprint` equals a digest recomputed now.

**The per-section digest** is a sha256 over the section's key paths, sorted
and deduplicated. Each path contributes its stat triple, `path|mtime_ns|size`,
or `missing:<path>` when it does not exist, so creating the file moves the
digest. File contents are never read (`hash_file` in
`otto.config.completion_cache`). Every key set also includes the directories
the enumeration entered. A directory's mtime moves when a file in it is
created, removed or renamed, so a new top-level test file or lab file is
detected without listing the directory again.

**The shared tail** is appended to every section's digest after its key
paths. It carries the two inputs no path list can:

- one `unresolved:<name>` line per init module that resolves under no `libs`
  entry;
- the freshness text of the process inventory, the one host inventory otto
  resolves for the whole workspace: a stat of the inventory file for a `json`
  inventory, the snapshot hash for a cached remote one.

**The shim's digest is composed:** it is `sha256("names:<digest>\ntests:<digest>\n")`
(`Section.derived_from`). It moves if and only if one of its children's
digests moves. Both child digests are computed through one memo, so checking
all three sections hashes each key set once. A path in both key sets (a tests
dir is) is hashed once for each section that owns it; inside the rebuild's
snapshot scope those hashes share one `stat` call.

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
would hide the missing commands and suites until the TTL expired. Storing the
entry anyway records the digests it was built from. A rewrite that would only
repeat an identical tainted entry is skipped, so an unchanged broken
workspace does not rewrite the file on every TAB. The errors that taint the
write include test files that failed while the rebuild loaded suites (see
below).

**The shim's stored triples.** The shim does not recompute digests; hashing
needs `hashlib` over every path, and it runs with the standard library alone.
It compares the stored `keys` triples with `os.stat` directly, checks the
`inventory` block (`none`, `stat` with its own triples, or `opaque`, which
always hands over), and checks `ttl_seconds`. A successful stat pass is
remembered for up to a minute by a marker file beside the cache; the rules
are in {doc}`completion`, section "The window".

**Why stat-based rather than content-based.** Hashing contents costs an open
and a full read of every key file, on every check. That is the cost the cache
exists to avoid, and on a network filesystem it is a round trip per file
before the first byte arrives. A stat triple is one call. The price is that
an edit which keeps both the size and the mtime cannot be detected, which is
rare for source files edited by people. How the stat-based digests behave
when several machines share one `$OTTO_HOME` is covered in
{doc}`../startup-performance` ("Shared-home safety").

## Reserved namespaces

Two top-level keys sit beside `"sections"`. Neither is written by a rebuild.

- **`__collected_tests__`** holds the `--tests` and `-m` names that a real
  pytest collection found. That includes parametrized and generated tests,
  which the static floor cannot see. It is written by an unfiltered
  `otto test --list-tests`, or by the bounded subprocess the `--tests`
  completer starts on a cold TAB (`maybe_warm_collected_tests`). It expires
  after a day.
- **`__dynamic_tunnels__`** holds the tunnel ids `otto tunnel` last
  discovered, for `otto tunnel remove <TAB>`. It is written by
  `otto tunnel list` and `otto tunnel remove` and expires after two minutes,
  because tunnels come and go without any file changing.

`__collected_tests__` is keyed by `compute_fingerprint`, one digest over the
workspace's settings, init-module, pytest-config, test-source and lab files
plus the inventory. That is what makes the entry correct: a pytest collection
depends on test sources, conftests and pytest configuration, and all of them
are in that digest. The same fingerprint is stored in the shim payload as
`tests_digest`, so the shim can find the collected names without computing it.

`__dynamic_tunnels__` is keyed by a narrower *tunnel-scope digest*
(`_tunnel_scope_digest`): settings, lab files and the inventory term, in the
same order `compute_fingerprint` hashes them, but no init modules, no pytest
config and no test sources. Tunnel ids are discovered from the workspace's
lab and the live process/argv state, never from a test — so hashing the
corpus to key them bought nothing but cost, exactly the corpus-proportional
price an ordinary command must not pay. The short TTL still does the rest of
the freshness work the narrower digest does not.

`write_sections` carries both namespaces across every rewrite, including a
rewrite that drops sections from an older schema. They come from work a
rebuild must never do (a pytest collection, a scan of the lab for tunnels),
so dropping them would make the next `--tests` TAB pay for a collection
again.

## Who reads, who refreshes

Only completion and the root help screen (`otto`, `otto --help`, `otto -h`;
`ROOT_HELP_ARGV` in `otto.cli.main`) read the sections, and only they check
and rebuild them. (`otto cache info` also reads them, to report on them.)
Every other command, `otto host … exec` included, bootstraps and dispatches
without reading, validating or writing a section.
Checking validity costs one stat per key path, which is one stat per test
file for the `tests` section. A command that never reads the cache has no
reason to pay that.

```{graphviz}
digraph cache_paths {
    rankdir=LR;
    node [shape=box];

    tab [label="bash TAB"];
    shim [label="otto._shim\nstat pass on stored triples"];
    answer [label="answered from shim\n(no otto CLI import)", style=dashed];
    names [label="entry(): names section\nvalid → set_completion_names"];
    help [label="otto --help (root)"];
    rebuild [label="bootstrap +\ncorpus_snapshot scope:\nvalidate all three,\nrebuild on a miss"];
    ordinary [label="any other command"];
    dispatch [label="bootstrap + dispatch\n(cache never touched)"];

    tab -> shim;
    shim -> answer [label=" valid"];
    shim -> names [label=" hand over,\n cache fine"];
    shim -> rebuild [label=" hand over,\n cache stale"];
    help -> names;
    names -> rebuild [label=" names miss"];
    ordinary -> dispatch;
}
```

| Invocation | Answered from | Validates and rebuilds |
| --- | --- | --- |
| `otto --version` | nothing | nothing |
| bash TAB answered by the shim | `shim` and `names`; at a `--tests`/`-m` site also `tests` and `__collected_tests__` | compares the stored stat triples only; never rebuilds |
| bash TAB handed over because the cache is stale (`Handover.stale`) | no section: it skips the `names` fast path and bootstraps | validates all three sections and rebuilds on a miss; skipped with no repos, no home or an uncacheable inventory |
| bash TAB handed over for any other reason, or a zsh/fish TAB | `names` | on a `names` miss, bootstraps, validates all three and rebuilds on a miss |
| `--tests`/`-m` TAB that reached the full path | `names`, then `tests` and `__collected_tests__` | like any handed-over TAB, rebuilds when `names` misses; a stale `tests` section alone is answered by a live `ast` scan, with no rebuild; a cold collected set may start the warm-up subprocess, which writes `__collected_tests__` |
| root `otto --help` | `names` | on a `names` miss, bootstraps, validates all three and rebuilds on a miss |
| any other command | nothing | nothing |
| unfiltered `otto test --list-tests` | nothing | never; writes `__collected_tests__` only (keyed by `compute_fingerprint` — a corpus walk, but this command already paid for the collection it caches) |
| `otto tunnel list`/`remove` | nothing | never; writes `__dynamic_tunnels__` only (keyed by the narrower tunnel-scope digest — no corpus walk) |

**A stale TAB repairs the cache.** When the shim hands a TAB over, it says
why. `Handover.stale` is true when the cache itself is at fault: no cache
file, a schema mismatch, a missing section, an expired TTL, or a stored path
that is gone, has appeared or has changed. `otto._shim.main` passes the flag
on as `entry(cache_stale=True)`. {func}`~otto.cli.main.entry` then skips the
`names` fast path, bootstraps, checks and rebuilds the cache, and lets Typer
answer the TAB. That TAB is slow once; the next one is answered by the shim.
Every other hand-over reason leaves the flag false, so those TABs keep their
normal cost and do not bootstrap for the cache's sake; the parsing reasons
are listed in {doc}`completion`, section "The resolver". Two of the false
ones are about the cache and are false on purpose. A tainted entry must not
count as stale, because rebuilding cannot help until the broken file is
fixed. An opaque inventory cannot be checked by stat at all, so a rebuild
would not make the next TAB answerable either.

**A missing or stale `names` section also rebuilds.** Completion and root help
read `names` first. On a miss they bootstrap. After bootstrap, `entry()`
first asks `cache_is_writable`, which does no corpus I/O. With no repos,
no home or an uncacheable inventory, no entry could be stored, and nothing
below runs. Otherwise `cache_is_stale` validates all three sections in one
read, and `entry()` rewrites all three on any miss, in one atomic update. The
digests it computed are handed to the writer, so no key set is hashed twice.

**One rebuild is one corpus walk.** The validity check and the rebuild ask
about the test corpus from several places:

- the `names` and `tests` digests;
- the static `ast` scan;
- the shim's stored triples;
- `compute_fingerprint`.

`entry()` wraps the check and the rebuild in `otto.config.corpus_snapshot`,
a scope held in a context variable. Inside the scope, `walk`, `stat` and
`glob` each answer a question the first time and replay the answer after
that. The result is one directory listing per directory and one stat per path
for the whole rebuild; the scan still opens each file once to parse it. Each
consumer keeps its own filtering, so every key set is exactly what it would
be without the scope. Outside a scope the functions are plain calls, and
nothing the scope stores outlives the `with` block; the walker's own
details are in the `otto.config.corpus_snapshot` module docstring.

**The rebuild loads the test files.** Collecting `suites` reads the `SUITES`
registry. Test files are no longer imported by
{func}`~otto.bootstrap.bootstrap`; they are imported by
{func}`~otto.bootstrap.load_test_suites` ({doc}`../lifecycle`), the loader
behind `SUITES`. `entry()` calls it itself, first thing inside the snapshot
scope, before anything there stats the tree, and only once
`cache_is_writable` holds. Without that check, a workspace whose inventory
cannot be cached would import every test file on every root help and TAB,
because each of those takes the full path. Importing a test file can create
`__pycache__` beside it, which moves the mtime of a directory the cache keys
on. Loaded after the stat was memoized, that move would make the entry written
stale on arrival, and the next root help or TAB would rebuild again. The only
answer the load itself memoizes is each tests dir's `test_*.py` glob, which an
import cannot change. A test file that fails to
load appends a framed error to the bootstrap result. Taint is decided when the
entry is written, so that error still taints the write, and `entry()` prints
its `warning:` line once, after the rebuild.

One side effect is known and tracked as issue #456. A `--tests` TAB that
warms the collected set runs pytest collection in a subprocess, and in a SUT
with no pytest config that collection can create `.pytest_cache` inside a
tests directory. The new directory moves a stored mtime, so the next TAB
finds the cache stale and rebuilds once more. This happens once, not in a
loop.

## What it costs, and the guards that hold it

- **A warm check** costs the key-set stats plus one open of the cache file:
  `names` scales with init files and top-level test files, and `tests` with
  the corpus.
- **A rebuild** costs about one stat and one open per test file and one
  listing and one stat per directory, plus the bootstrap and the suite load
  the collection needs. It happens once per change to the workspace.
- **Everything else** costs nothing: `otto --version` and ordinary commands
  do no completion-cache I/O.

The import-budget guard (`scripts/import_budget.py`, run by `make profile`)
pins these rates on a generated repo shaped like a real one:

- `test_cold_rebuild_walks_the_corpus_once` measures a cold rebuild at 50
  and at 200 nested test files. The added files may cost at most one stat and
  one open each, and the added directories one listing and one stat each. A
  second walker anywhere in the rebuild doubles the per-file rate and fails
  the test, which names the counter.
- The warm scaling pins assert that corpus size adds nothing (at most five,
  for noise) to `open`, `scandir` and `stat_workspace`:
  `test_help_io_does_not_scale_with_corpus_size`,
  `test_completion_io_does_not_scale_with_corpus_size`,
  `test_completion_handover_io_does_not_scale_with_corpus_size` and
  `test_dispatch_io_does_not_scale_with_corpus_size`.

Stat calls produce no Python audit event, so the guard counts them with
strace. The counters and their gates are described in
{doc}`../startup-performance` ("What holds these numbers in place").

## Deliberate non-goals

- **Rebuilding only the stale sections** (issue #446). A rebuild rewrites all
  three sections. With the single corpus walk, a selective rebuild would save
  mostly collector CPU, not I/O.
- **Rebuilding in a detached process** (issue #447). This would take the
  rebuild off the TAB entirely, but it needs a process lifecycle of its own:
  spawning, locking, recovering stale locks, reporting failures, and isolating
  tests. It removes no work. It stays the escalation path if a real corpus
  shows that one walk is still too slow for an interactive TAB.
- **zsh and fish.** Only bash TABs reach the shim. Other shells use the
  `names` fast path in `entry()` and rebuild only when `names` misses.
- **Root help repairing `tests`.** `otto --help` validates `names` alone. When
  `names` is valid it answers without bootstrapping, even if `tests` or `shim`
  is stale. The next `--tests` TAB repairs them.

## Where the code lives

- `otto.config.completion_cache` — the file: location, schema, TTLs,
  `hash_file`, `compute_fingerprint`, `read_sections` / `write_sections`,
  `cache_is_writable` / `cache_is_stale`, the collectors, and the reserved
  namespaces.
- `otto.config.cache_sections` — the `Section` registry (`names`, `tests`,
  `shim`), the key-set functions, the shared tail and the composed digest.
- `otto.config.corpus_snapshot` — the one-walk scope used during a rebuild.
- `otto.config.completion_tree` — `build_shim_payload`: the serialised tree,
  stored triples and inventory block.
- `otto.config.cache_maintenance` — the marker files, and the walk behind
  `otto cache clear` and `prune`.
- {mod}`otto.config.home` — the workspace key and home.
- {func}`otto.cli.main.entry` — who reads and who rebuilds.
- `otto._shim` / `otto._shim_complete` — the bash TAB reader and the
  `Handover.stale` flag.
- {func}`otto.bootstrap.load_test_suites` — the suite load a rebuild triggers.

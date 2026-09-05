# Shim-answered shell completion — a self-describing cache entry served without the CLI framework

**Date:** 2026-09-04
**Status:** Approved design, pre-implementation
**Scope:** The warm path of bash tab completion. A new `shim` section in the
completion cache carries a completion tree and a stat-checkable validation
block, and `otto._shim` answers a TAB from it with the standard library alone.
Every case it cannot answer takes today's path unchanged.

## 1. Motivation and decisions

A warm `otto <TAB>` on the dev VM costs 0.20 s, imports 285 non-stdlib modules
and makes 2,499 file syscalls. None of that is user code or lab data: the cache
read itself is a few milliseconds. The cost is `discover()` (0.16 to 0.21 s,
because loading the environment settings imports the pydantic host stack) and
the Typer, click and rich import graph. On the NFS-mounted deployment that
reported this (2026-09-03) each of those syscalls is a network round trip, and
a TAB feels like "hundreds of milliseconds". The shim that already answers
`otto --version` in 0.02 s shows the floor: a plain JSON read costs 0.01 s and
278 syscalls.

Bare pytest collection was measured as an alternative for `--tests` and
rejected: 0.41 s for a one-test repo, 10.4 s for the 9,133-test otto corpus,
against 0.002 s and 0.44 s for the static AST scan and one millisecond to stat
the corpus's 490 key paths. Pytest's own `cache/nodeids` file is an
ever-growing union with no freshness rule, so it is not a collection cache.

Decisions taken during brainstorming (Chris, 2026-09-04):

1. **Scope: every site whose candidates the cache holds**, `--tests` included.
   Remote paths, tunnel ids and the first `--tests` TAB that has to warm the
   pytest-collected set keep today's path.
2. **Freshness: the same contract as today, plus a 60-second trust window.**
   The entry stores its key paths with stat triples; the shim stats every one
   on a TAB, then trusts that pass for 60 s via a marker file that a cache
   rewrite invalidates.
3. **Bash only.** Every other shell hands over.
4. **Approach A, a tree-driven shim**, on its own. Slimming the framework path
   (approach C) was rejected: its expensive half is a second `settings.toml`
   parser, its cheap half (importing only the dispatch target's subapp)
   already exists, and the sites left on the framework path are dominated by
   an SSH listing, a pytest subprocess or a full bootstrap.
5. **Third-party commands keep completing from the cache**, names and flags,
   as today; option values from a plugin's own completer are never produced
   at TAB time by any path, because that would run plugin code.
6. **Host verbs are per host, honestly.** The class is a static function of
   the lab entry's `os_type`, so the tree carries verbs per class and the
   payload maps each host id to its class; a host of unknown class gets the
   union, never a guess.
7. **Reference for equality is bootstrapped Typer**, the output dispatch
   actually accepts, not today's warm stubs (which omit plugin host classes
   and any instruction whose signature the stub schema cannot round-trip).
8. **`-m` marker completion is in scope.** `otto test --markers/-m` has no
   completer today. It gains one with the same two layers as `--tests`: a
   static floor (the markers declared in the repo's pytest config, otto's
   built-in markers, and every `@pytest.mark.<name>` the AST scan already
   walks past) plus the markers the pytest-collected items carry. Same key
   set, same collected record, one more payload key on each.

## 2. Architecture

Four components.

1. **The tree serialiser** (`otto.config.completion_tree`, slow path). After
   bootstrap, walks the real click command tree and emits the completion tree
   (§3.3). Runs in the same pass that writes the other sections.
2. **The entry** (§3): a `shim` section in the existing cache file, beside
   `names` and `tests`, holding the validation block and the tree. The shim
   reads the `names` and `tests` payloads as they are.
3. **The resolver** (`otto._shim_complete`, imported by `otto._shim`; stdlib
   only). Locates the entry, validates it, parses `COMP_WORDS` against the
   tree, prints bash candidates. Anything else hands over to
   `otto.cli.main.entry()` in-process.
4. **The proof**: an in-process differential test against bootstrapped
   Typer, unit pins per rule, one end-to-end test, and an import-budget
   surface with the framework on its deny list.

Data flow on a TAB:

```
bash → env _OTTO_COMPLETE=complete_bash COMP_WORDS=… COMP_CWORD=… otto
  otto._shim.main
    not bash completion? → entry()
    otto._shim_complete.answer(env)
      locate entry (OTTO_HOME, OTTO_SUT_DIRS → workspace key)   miss → entry()
      verdict: schema, section, taint, TTL                       miss → entry()
      marker within 60 s, else stat pass (names; tests if needed) miss → entry()
      parse COMP_WORDS against the tree                          unknown/live → entry()
      candidates from payload/static/tests sources
      print, exit 0
```

The slow path is unchanged except that it writes one more section and three
more payload keys. A hand-over is today's path exactly: `entry()` sees the
same environment and the same argv, and nothing has been written to stdout.

## 3. The entry

### 3.1 Placement and schema

`shim` is a registered `Section` like `names` and `tests` (same writer, same
`generated_at`, same `tainted` flag, same schema stamp). `SCHEMA_VERSION` is
bumped, so every existing cache misses exactly once and is rewritten. The
section's key paths are the **union** of the `names` and `tests` key sets, so
the writer rewrites it whenever either sibling is rewritten: a tests-only edit
must refresh `keys.tests`, or the shim's stat pass over it would keep failing
until the names side happened to change. This costs nothing extra on the write
pass, which already digests both siblings for the merged-view check. The shim
never computes a digest; it compares stat triples.

### 3.2 The validation block

```json
"shim": {
  "ttl_seconds": 86400,
  "keys": {
    "names": [["/repo/.otto/settings.toml", 1725400000123456789, 412],
              ["/repo/lab", 1725400000000000000, 4096],
              ["/repo/.otto/init.py", null, null]],
    "tests": []
  },
  "inventory": {"kind": "stat",
                "files": [["/home/me/.otto/inventory.json",
                           1725400000000000000, 2048]]},
  "tests_digest": "…",
  "tree": {}
}
```

- **`keys.names` / `keys.tests`** mirror the `names` and `tests` section key
  sets at write time: one `[path, mtime_ns, size]` per file, and one per
  **directory the key-path enumeration visited** (every tests dir and every
  subdirectory the corpus walk entered, every lab directory a source expands,
  every directory a glob was expanded in). A missing file is stored as
  `[path, null, null]` and must still be missing. Rationale: a file edited in
  place moves its own triple; a file added, removed or renamed moves its
  directory's mtime; so the shim sees a new lab file or test file without
  re-implementing any glob. The enumeration functions gain a way to report the
  directories they visited (a `visited: set[Path]` sink threaded through
  `expand_lab_paths`, `Repo.iter_test_files` and `iter_test_sources`), and the same
  report feeds both the section digest and the stored triples, so the two key
  sets cannot drift.
- **`inventory`** is the inventory's freshness as the shim can check it:
  `{"kind": "none"}` when none is declared; `{"kind": "stat", "files":
  [[path, mtime_ns, size], …]}` for a backend whose fingerprint is
  stat-derived (the json backend); `{"kind": "opaque"}` for one whose
  fingerprint is a snapshot hash. `opaque` hands over. A LIST of triples, not
  one: `stat_paths()` returns a list, and `CredsOverlay.stat_paths` returns
  the inner backend's paths PLUS the creds file, so the declared-and-overlaid
  case is two files. Same `[path, mtime_ns, size]` shape and the same
  missing-path rule as `keys`, so one stat pass validates both. A broken
  declaration never reaches here: its digest is ephemeral and no entry is
  written.
- **`ttl_seconds`** is the TTL the writer applied (`_cache_ttl_seconds`): a
  day, or five minutes when an init module resolves under no `libs` entry or a
  lab source is not file-backed. The shim compares `generated_at` against it.
- **`tests_digest`** is `compute_fingerprint(repos)` at write time, the key
  the pytest-collected set (`__collected_tests__`) is stored under. Once
  `keys.names` and `keys.tests` both validate by stat, the stored digest is
  still current and the shim reads the collected entry under it, applying the
  entry's own schema and TTL checks (`read_collected_tests`). A cold or failed
  collected entry hands over, so the first `--tests` TAB warms it exactly as
  today.
- **Markers ride the same two layers.** The `tests` section payload gains
  `markers`: the union of `Repo.configured_markers()` (the pyproject
  `[tool.pytest.ini_options].markers` names), the `OTTO_MARKERS` names, and
  the names of every `@pytest.mark.<name>` decorator or `pytestmark`
  assignment the static scan encounters in the files it already parses. The
  collected record gains `markers`, the union of `m.name for m in
  item.iter_markers()` over the collected items, and
  `COLLECTED_SCHEMA_VERSION` is bumped. Both are keyed and validated exactly
  as the test names beside them; a `markers` site is a `tests` site for the
  validator.

### 3.3 The tree

```
Node:  {"name": str, "params": [Param, …], "commands": {name: Node, …}}
Param: {"flags": [str, …],          # [] for a positional
        "name": str,                 # click's param name (for the "already given" rule)
        "takes_value": bool,         # false for flags and boolean pairs
        "multiple": bool,            # repeatable option
        "nargs": int,                # -1 for a variadic positional
        "sep": "+" | "," | null,     # list-segment grammar, else null
        "source": Source}
Source: {"kind": "static", "values": [str, …]}
      | {"kind": "payload", "key": str, "lab_scoped": bool, "always": [str, …]}
      | {"kind": "tests"}
      | {"kind": "markers"}
      | {"kind": "echo"}            # click's File type: it echoes the fragment
      | {"kind": "none"}
      | {"kind": "live"}
```

Rules:

- `commands` is ordered as `list_commands` yields it on the bootstrapped path.
- Hidden parameters are not serialised (click never offers them).
- A boolean pair (`--field/--debug`) is one Param with both flags in
  `flags`, `takes_value` false.
- **Host verbs.** `host` is a Node whose `params` hold the group's own
  parameters (the `host_id` positional first) and whose `commands` is the
  union verb set. The tree additionally carries
  `"host_classes": {class_name: {verb: Node, …}}`; the resolver descends into
  the typed host's class node when `host_classes_by_id` knows the host, else
  into the union. A verb's parameters live under the class because a verb
  shared across classes can carry a different signature per class. A
  `remote_path` parameter is `live`.
- **Third-party groups, instruction and suite stubs** are ordinary Nodes,
  serialised from the click objects after bootstrap (not from the stub
  schema), so no command is omitted for an unsupported annotation.
- **Classification is exhaustive and fails loud.** The serialiser maps each
  completer callable to its Source through one registry (§3.4). A parameter
  carrying a completer the registry does not know is serialised `live`, and a
  unit pin enumerates every `autocompletion=` in the CLI and requires each to
  be registered, so a new completer cannot silently go live.

### 3.4 Completer classification

One row per completer that exists today. "Filter" is what the completer does
to its candidates; the resolver reproduces it exactly.

| Completer | Source | Scoping | Filter and order |
|---|---|---|---|
| `_lab_completer` (`--lab`) | payload `labs`, sep `+` | none | `complete_separated_list(sorted(names), frag, "+")` |
| `_project_completer` (`-I`, `-E`) | payload `projects` (new) | none | prefix, discovery order |
| `_username_completer` (`--as-user`) | payload `usernames` | none | sorted, prefix |
| `_host_id_completer` (`host` positional) | payload `hosts` / `hosts_by_lab`, `always` = builtin ids | lab | sorted, prefix |
| `_hosts_completer` (`tunnel add --hosts`) | as host ids, sep `,` | lab | first segment as host ids; a fragment containing `,` is **live** |
| `_docker_host_completer` (`--on`) | payload `docker_hosts` | lab: intersect with the lab's host set | sorted, prefix |
| `_use_case_completer` | payload `docker_use_cases` | none | sorted, prefix |
| `_term_completer` | payload `term_backends` | none | sorted, prefix |
| `_transfer_completer` | payload `transfer_backends` entries whose `host_families` contains `"unix"` | none | sorted, prefix |
| `_link_completer` | payload `links` (new: `{id, hosts}`) | lab: offered when **any** endpoint host is in the lab's host set (the `collect_link_ids` rule) | sorted, prefix |
| `_tests_completer` (`--tests`) | `tests` floor ∪ collected set, sep `,` | none | `complete_separated_list(sorted(names), frag, ",")` |
| `_markers_completer` (`--markers`/`-m`, new) | `markers` floor ∪ collected markers | none | the expression rule below |
| `_tunnel_id_completer` | live | | |
| `_remote_completer` closures (`remote_path`) | live | | |
| click `Choice` / enum params | static | | prefix |
| `typer._click.types.File` params | echo | | its `shell_complete` echoes the fragment |
| path-typed params (`TyperPath`) | none | | `TyperPath.shell_complete` returns `[]` (measured): Typer offers nothing and bash falls back to its own filename completion (`complete -o default`) |
| everything else with a value | none | | |

Lab scoping: the selected labs are the `-l`/`--lab` values on the line (each
split on `+`, repeats accumulated) or, when none is given, `OTTO_LAB` split the
same way (empty means unset). With labs selected, the host set is the union of
their `hosts_by_lab` buckets plus `always`; with none, it is `hosts`.

The expression rule (`-m`): a marker expression is words separated by
whitespace and parentheses (`smoke and not (slow or flaky)`). The fragment's
head is everything up to and including the last whitespace or `(`; the tail
is the identifier being typed. Candidates are the marker names starting with
the tail, each emitted as head plus name, sorted; the keywords `and`, `or`,
`not` are never offered. A fragment with no head completes a bare name. The
function lives in `otto.utils` beside `complete_separated_list`, is the
completer's only logic, and the resolver mirrors it.

## 4. The resolver

### 4.1 Locate

`OTTO_HOME` (empty counts as unset) or `~/.otto`; `OTTO_SUT_DIRS` split on `,`
or `os.pathsep`, each expanded and resolved, sorted, deduplicated; key =
`sha256("\n".join(paths))[:8] + "-" + normalize(basenames joined by "-")[:40]`,
with `normalize` = lowercase and runs of `[-_.]` collapsed to `-`, and
`no-repos` for the empty set. A unit pin holds this equal to
`otto.config.home.workspace_key` over a corpus with symlinks, relative paths,
duplicates and the empty set. No SUT dirs hands over.

### 4.2 Validate

In order, any failure hands over:

1. The cache file reads and parses; `schema` matches; `sections.shim` is a
   dict with a dict `payload`; `tainted` is false; `generated_at` is numeric
   and within `ttl_seconds`; `sections.names` and, for a `tests` site,
   `sections.tests` carry dict payloads.
2. The marker for the key set (`completion_cache.names.ok`,
   `completion_cache.tests.ok`, beside the cache file) exists, its mtime is not
   older than the cache file's mtime, and now minus its mtime is under 60 s.
   Then the pass is skipped.
3. Otherwise the stat pass: for every triple, `os.stat` must give the stored
   `mtime_ns` and `size`, and a stored missing path must still fail to stat;
   the inventory line by its kind. A pass that succeeds touches the marker
   (`os.utime` on an existing file, else create); a touch that fails is
   ignored.

The `tests` key set is checked only when the resolved site is `tests`, after
the `names` pass. Both passes must succeed before `tests_digest` is trusted.

### 4.3 Parse

`COMP_WORDS` is split as click splits it (`shlex`, posix, whitespace split,
no comments, an unterminated quote or escape keeps the partial token). The
words after the program name and before `COMP_CWORD` are the arguments, the
word at `COMP_CWORD` (or `""`) is the fragment. The walk keeps a current Node,
a pending value-taking Param, the set of positionals filled, and the set of
options already given.

- `--` ends option parsing; later tokens are positional.
- A token starting with `-` (and longer than `-`) is an option of the current
  Node, matched as `--long`, `--long=value`, `-s`, `-svalue`, or a boolean
  pair's either flag. A value-taking option without `=` consumes the next
  token unconditionally (click does too); with no next token it becomes the
  pending Param. An option not on the current Node hands over.
- A positional token fills the current Node's unfilled positionals in order
  (a variadic one absorbs the rest); once they are filled it must name a
  subcommand, and the walk descends. On `host`, the descent target for a verb
  is the class Node when `host_classes_by_id` knows the typed id, else the
  union Node. An unknown subcommand hands over.
- The fragment, in this order: the pending Param's Source; else, if it starts
  with `-` and `--` was not seen, the current Node's option flags, excluding
  non-`multiple` options already given, prefix-filtered; else the next
  unfilled positional's Source; else the current Node's subcommand names,
  prefix-filtered, in tree order.

Any exception anywhere hands over.

### 4.4 Answer

Candidates are printed one per line, joined with `\n`, followed by the
trailing newline Typer's `echo` adds, on stdout; exit 0. An empty candidate
list prints the empty line (bash's `-o default` then completes filenames, as
today). Nothing is written to stdout before the decision to answer.

### 4.5 What hands over

Any shell but bash; no SUT dirs; every validation failure of §4.2; an
`opaque` inventory; an unknown option or subcommand; a `live` Source; a
`tunnel --hosts` fragment past its first `,`; a cold pytest-collected set on a
`tests` site; any exception.

## 5. Full-path changes

Independently useful and testable through Typer before the shim exists:

- `HostSummary.os_type: str | None`, filled by the json backend from the
  resolved entry (`host_data.get("os_type", "unix")` is the factory's rule;
  the summary records the same). A backend that does not set it leaves
  `None`.
- Names payload keys: `host_classes_by_id` (`{id: class_name}`, computed after
  bootstrap through `build_os_profile(os_type).base`; hosts with `None`
  omitted), `projects` (discovered repo names, in discovery order), `links`
  (`[{"id", "hosts": [a, b]}]` from the same enumeration `collect_link_ids`
  uses). All three join `DELEGATED_NAMES_KEYS`.
- `HostGroup._class_for` during resilient parsing looks the typed id up in
  the cached `host_classes_by_id` instead of returning `None`, so Typer's
  completion scopes verbs honestly at no lab cost. Dispatch is unchanged.
- `--markers`/`-m` on `otto test` gains `_markers_completer`: the `tests`
  section's `markers` floor (live static scan on a miss, like `--tests`)
  united with the collected record's `markers`, through the expression rule
  of §3.4. The static scan collects marker names in the same AST pass that
  collects test names; `Repo.configured_markers()` and `OTTO_MARKERS` are
  read as they are today for `--list-markers`.

## 6. Proof and gates

- **Differential test** (`tests/unit/shim/test_differential.py`): for a
  fixture repo whose init module registers a host class, a CLI group with a
  nested subcommand, and an instruction with options, bootstrap once and build
  the click group; write the cache; generate the corpus by walking the tree:
  for every Node, `""`, `-`, `--`, each option flag with `""`, `=`, and a
  partial value, each positional with `""` and a partial, subcommand prefixes;
  each with and without `-l <lab>` and with `OTTO_LAB`; plus hand-written
  cases for quotes, spaces, `--`, repeated options, attached short values,
  list heads. For every case, run the resolver in-process and Typer's
  `BashComplete.complete()` against the bootstrapped group, and require
  identical output where the resolver answers. Cases where it hands over are
  reported, and the set of hand-over reasons must match the expected set for
  the corpus.
- **Unit pins**, each red-proven by mutation: workspace key equality; the
  validator against the section digest (touch a keyed file, both stale;
  create a file in a watched directory, shim stale; a missing key path
  appears, stale; a stale entry never answers); marker semantics (inside the
  window, not after a rewrite, one per key set, touch failure ignored);
  taint, TTL, opaque inventory, unknown option, unknown subcommand, `live`,
  cold collected set hand over; every parse rule in §4.3; the classification
  registry covers every `autocompletion=` in the CLI.
- **End to end** (`tests/e2e/config/test_shim_completion.py`): cold TAB
  writes the entry; warm TAB answers the same candidates, leaves the cache
  file's mtime untouched and the marker present; a touched lab file makes the
  next TAB hand over and rewrite.
- **Budget.** `completion_repo_warm` gains `typer`, `click`, `rich`,
  `pydantic`, `pydantic_settings` on its deny list, its module snapshot
  becomes the shim's, and its I/O golden is re-measured. Today's expectations
  move to a new `completion_repo_handover` surface (a `live` site) so the
  fallback path stays watched.

## 7. Rollout

- Schema bump; no configuration; no user action.
- `CACHE_FILE_NAMES` gains the two markers so `clear` and `prune` remove
  them.
- `otto cache info`'s workspace block gains `shim: served (validated 12s ago)`
  or `shim: handing over — <reason>` for the current workspace, computed by
  the same validator.
- Docs: `docs/guide/cli/index.md` "Shell completion" (how a TAB is answered,
  the window, what hands over), `docs/guide/cli/cache/index.md` (the section,
  the markers, the `shim` line), and `docs/architecture/subsystems/` gains a
  page on the completion tree.

## 8. Non-goals

zsh, fish and PowerShell; approach C; tunnel ids and remote paths from the
cache; reading pytest's `cache/nodeids`; any change to dispatch.

## 9. Plan shape

Five stages, each gated: (1) the full-path changes of §5; (2) the serialiser,
the section and the schema bump; (3) the resolver as a pure function with the
differential test; (4) the shim wiring, validator, markers, end-to-end test
and budget surfaces; (5) `cache info` and docs.

## 10. Risks

- **Click semantics coverage.** The parse mirrors a subset; the generated
  corpus is the net, and any construct it cannot express hands over rather
  than guesses.
- **NFS attribute caching** can delay a directory or file mtime change by the
  client's attribute-cache timeout (seconds). The window already accepts
  staleness of that order; documented on the CLI page.
- **The window** is the one intended inequality with Typer and is stated as
  such in the docs.

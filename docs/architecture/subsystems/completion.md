# Shell completion and the shim

A warm `otto <TAB>` has to run bootstrap and load Typer (which vendors
click) and rich before it can print a single candidate — none of which the
answer itself needs. `otto._shim` is a second, much smaller entry point
that answers a bash TAB straight from the completion cache, standard
library only, whenever the cache's own bookkeeping says it still describes
the world. Anything it cannot answer that way runs today's path unchanged;
see
{doc}`the CLI page <../../guide/cli/index>` and
{doc}`the cache page <../../guide/cli/cache/index>` for the user-facing
behavior this subsystem serves.

## Two paths, one answer

The shim is a second implementation of Typer's own bash completion, so it must
match byte for byte, not "close enough". The contract is enforced by generating
a corpus of command lines against the bootstrapped app (the real dispatch tree,
not a warm-load stub) and asserting the shim's answer equals Typer's for every
case it accepts, in `tests/unit/shim/test_differential.py`. The corpus is
generated from the serialised tree itself — every command, every option, every
fragment and complete-word shape, under four `OTTO_LAB` environments — and the
test asserts three things. Zero mismatches over every case the shim answers. A
floor of `MIN_ANSWERED` answered cases, so a change that collapses coverage into
hand-overs fails loud rather than passing quietly. And a counted histogram of
the hand-over reasons: each must fall in a named class (a `live` completer, a
`tunnel add --hosts` fragment past its first comma, a value attached to a flag
that takes none, a cold collected set, stacked short flags), and the two
"unknown option"/"unknown command" classes are counted EXACTLY against the
hand-written lines that produce them. That last one is load-bearing: a generated
shape that Typer never reaches hands over on both sides and looks like coverage
otherwise — an earlier version of the generator walked host verbs with no host
id, and most of its hand-overs were that artefact rather than any rule of the
shim's. Every function in the shim that mirrors Typer or click names what it
mirrors, so a change to one side is a deliberate change to both.

## The entry

`shim` is a third registered `Section` in `otto.config.cache_sections`,
alongside `names` and `tests`, written by the same slow pass under the same
schema and `tainted` flag. Its payload, built by
`otto.config.completion_tree.build_shim_payload`, carries: `keys`, the
`names` and `tests` key sets as `[path, mtime_ns, size]` triples
(`stat_triple`) plus one triple per **directory** the key-path enumeration
visited — a file that appears, is removed, or is renamed moves its
directory's mtime, so the shim sees a new lab file or test file without
re-running any glob itself; `inventory`, the process inventory's freshness as
a stat check can verify it — `none` when none is declared, `stat` for a
file-derived backend (the json backend), or `opaque` for a backend that
cannot report which files it read at all (`inventory_block`), which always
hands over; `ttl_seconds`, the same TTL the writer applied to the merged
view (a day, or five minutes when any repo has a non-file-backed init
module, a non-file-backed lab source, or a non-empty `[reservations]`
table); `tests_digest`, the fingerprint the pytest-collected test set is
stored under, so a warm `--tests`/`-m` set can still be found once the stat
passes succeed; and `tree`, described next.

## The tree

The tree is the bootstrapped command graph, serialised once per cache write.
A `Node` is `{name, params, commands, group}`; `group` is the *parser's*
shape, not "has subcommands" — it records whether the underlying class is a
`TyperGroup` or a leaf `TyperCommand`, which is what actually decides how
click parses interspersed options and positionals. A `Param` carries its
flags, whether it takes a value, `multiple`, `nargs`, its list-segment
separator, and a `Source` describing how to answer it (`static`, `payload`,
`tests`, `markers`, `echo`, `none`, or `live`). Host verbs get their own
layer: the root node carries a `host_classes` map from each host class name
to that class's verb nodes, since a verb shared across classes can have a
different signature per class; the resolver picks the right map once it
knows the typed host id's class.

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

A successful validation pass leaves a marker file beside the cache
(`otto.config.cache_maintenance.MARKER_FILENAMES`); a marker fresher than the
cache and under `SHIM_WINDOW_SECONDS` (60) old lets the next TAB skip the
stat pass entirely. Because the marker must be at least as new as the
cache's own mtime, rewriting the cache invalidates every marker beside it
automatically — nothing has to remember to delete them. The marker lifecycle
under the cache commands is on {doc}`../../guide/cli/cache/index`.

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

## Where the code lives

- `otto._shim` — console-script entry point; decides `--version`, a bash
  TAB, or a hand-off to `otto.cli.main.entry`.
- `otto._shim_complete` — the resolver: locate the cache, validate it, parse
  `COMP_WORDS`, answer. Standard library only.
- `otto.config.completion_tree` — the tree serialiser (`serialize_tree`,
  `build_shim_payload`, `inventory_block`); slow (write) path only.
- `otto.config.cache_sections` — the `shim` section's registration and key
  paths, alongside `names` and `tests`.
- `otto.config.cache_maintenance` — the marker filenames and window
  constant, and the `clear`/`prune` walk that removes them.
- `tests/unit/shim/test_differential.py` — the equality proof.
- `scripts/import_budget.py` — `completion_repo_warm` pins the warm TAB to
  exactly three modules (`otto`, `otto._shim`, `otto._shim_complete`) and
  pins `open_home 2` and `scandir 0` on its I/O golden;
  `completion_repo_handover` pins the cost of a TAB the resolver hands over.

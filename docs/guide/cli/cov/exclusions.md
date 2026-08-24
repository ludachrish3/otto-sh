# Exclusion rules

An exclusion rule removes lines and branches from the **measured** data.
The excluded records are deleted from the merged store before any
percentage is computed, so an excluded line leaves the denominator
everywhere at once: the file page, the directory rollups, per-ticket
coverage, `store.json`, and the report's headline number.

Filtering happens after every data source has been folded in — `.gcda`
captures, harvested tiers, `capture.json` e2e captures and the committed
manual store — so one set of rules covers all of them, whatever produced
the tracefile.

## The standard markers

lcov's `geninfo` honors the standard markers natively, stripping them
before otto parses anything:

- `LCOV_EXCL_LINE` — exclude one line.
- `LCOV_EXCL_START` / `LCOV_EXCL_STOP` — exclude a block.
- `LCOV_EXCL_BR_LINE`, `LCOV_EXCL_BR_START` / `LCOV_EXCL_BR_STOP` —
  branch-only variants (the line still counts, only its branches are
  excluded).

otto now **also enforces these itself**, as two always-on rules. That
matters for harvested data: a `.info` file produced by someone else's CI
was never seen by your `geninfo`, so before, its marked lines stayed in
the numbers. They no longer do.

In the row-coloring precedence (see {ref}`coverage-colors`), excluded
**always wins**, even over a covered, stale, or aging line.

## Declaring rules

Rules live in `[coverage.exclusions]` as an array of tables, each with a
`kind`:

```toml
[[coverage.exclusions.rules]]
kind = "marker"
name = "MYPROJ_NO_COV"   # family: _LINE / _START / _STOP

[[coverage.exclusions.rules]]
kind = "preprocessor"
macros = ["DEBUG_LOG", "TRACE_VERBOSE"]

[[coverage.exclusions.rules]]
kind = "preprocessor"
pattern = '#if 0'

[[coverage.exclusions.rules]]
kind = "path"
patterns = ["vendor/**", "**/*_generated.c"]

[[coverage.exclusions.rules]]
kind = "regex"
pattern = '\bassert\('
stat = "branch"
```

Rules **union** — there is no precedence between them to reason about.

Patterns are Python `re` syntax. Write them as TOML **literal** strings
(single quotes), as above, so backslashes need no doubling. Every regex
is compiled when settings load: a bad one fails immediately, naming the
rule's index and the regex error, before any coverage work starts.

### `stat` — lines or branches

Every kind accepts `stat`, which defaults to `"line"`:

- `stat = "line"` deletes the line record outright. Its branches go with
  it.
- `stat = "branch"` keeps the line and clears only its branches.

The branch form exists for lines that are genuinely covered but carry an
arm nothing can take. `assert(x)` is the standard case: the line runs, but
the failure arm never does, so the file is permanently short on branch
coverage with no way to say so.

A line excluded at `stat = "line"` subsumes any branch-level exclusion of
the same line — it is not "a line whose branches were excluded", it is
simply gone.

## `marker`

`name` is the family **base**, and otto derives its members the way lcov
derives its own:

| | `stat = "line"` | `stat = "branch"` |
| --- | --- | --- |
| single line | `{base}_LINE` | `{base}_BR_LINE` |
| block open | `{base}_START` | `{base}_BR_START` |
| block close | `{base}_STOP` | `{base}_BR_STOP` |

So `name = "MYPROJ_NO_COV"` recognizes `MYPROJ_NO_COV_LINE`,
`MYPROJ_NO_COV_START` and `MYPROJ_NO_COV_STOP`. Custom markers get block
form for free, and the branch family falls out of the same base rather
than needing a second declaration.

The built-in set is not a special case: `LCOV_EXCL` is exactly this rule
at each `stat`.

```{note}
This is a change in spelling as well as in effect. The old
`[coverage.exclusions] markers` list matched its string **bare**, so a
project writing `// MYPROJ_NO_COV` must now write
`// MYPROJ_NO_COV_LINE`. There is one convention now — lcov's — rather
than two.
```

Two bases can derive the same token: `FOO` at `stat = "branch"` yields
`FOO_BR_LINE`, and so does `FOO_BR` at `stat = "line"`. One string cannot
stand for two different stats, so loading rejects any two families whose
derived tokens overlap, naming both.

The built-in families are in that check, not exempt from it. The trap is
`LCOV_EXCL_BR` at `stat = "line"`, which derives exactly the tokens the
built-in *branch* family already owns.

## `preprocessor`

Excludes the arm whose own directive matches, from that directive to the
next one at the same nesting depth. Nested constructs inside a matched arm
go with it. Two spellings, and a rule must use exactly one:

**`pattern`** is a regex matched against the normalized logical directive
line — continuations joined, the gap between `#` and the keyword
collapsed — so `#  ifdef  X` and a backslash-continued condition both
present as `#ifdef X …`. Surgical, and the right tool for `#if 0`.

**`macros`** is a list of names. The condition is tokenized and each
identifier's **negation parity** tracked; the arm is excluded when a
listed macro appears positively:

```c
#if defined(A) && defined(DEBUG_LOG)    // positive       -> excluded
#if defined(A) && !defined(DEBUG_LOG)   // negated        -> kept
#ifndef DEBUG_LOG                       // negated        -> kept
#if DEBUG_LOG > 2                       // positive       -> excluded
#if !(A && !defined(DEBUG_LOG))         // double negated -> excluded
#elif defined(DEBUG_LOG)                // positive       -> excluded
#else                                   // no condition   -> never matches
```

### Why otto does not evaluate the condition

otto never needs to know which arm the preprocessor actually selected,
because a dead arm has no coverage records at all — the compiler emitted
no code for it, so deleting it removes nothing. gcov already resolved
liveness; the rule only has to name an arm.

That one observation is what keeps this feature free of a macro
environment, of `compile_commands.json`, and of having to agree with your
build system about anything.

## `path`

Globs, matched against each file's path relative to the `[coverage]` repo
root. Matching files are dropped whole and never even opened.

```toml
[[coverage.exclusions.rules]]
kind = "path"
patterns = ["vendor/**", "**/*_generated.c"]
```

A `**` segment crosses directory separators — any number of them,
including none. A `*` matches within one path segment and `?` matches one
character, neither crossing a `/`. Patterns are anchored at both ends, so
`vendor/**` names everything under `vendor/` and `**/*_generated.c` names
that suffix at any depth, the repo root included.

A glob that **starts with `/`** is matched against the file's absolute
path instead. That is the only way to name a file outside the repo root:
a relative glob cannot match one, since there is no relative path to match
it against.

## `regex`

Matched against each raw source line — the general-purpose fallback when
no other kind fits.

## Limitations

Three things this design does not do. None of them are reported at
runtime, so they are worth knowing before you write a rule.

**A mis-scoped rule is silent.** There is no per-rule accounting and no
warning when a rule matches nothing, so a typo'd macro name or a glob
that anchors wrong simply does nothing. Verify a new rule by checking
that the numbers moved: the excluded count on the file or directory page
is the quickest read.

**`||` over-matches.** `#if defined(PROD) || defined(DEBUG_LOG)` excludes
the arm even when `PROD` is what made it live, because separating those
cases needs real evaluation. Use a `pattern` rule when you need to be
exact about which condition you are naming.

**An `#else` arm cannot be named.** It carries no expression for `macros`
to match and no distinguishing text for `pattern` to match. This only
bites when *both* arms carry records, which needs a store merging builds
made with different flags — reachable, since `.info` files from different
hosts do get merged. Work around it with a source marker or a `path`
rule.

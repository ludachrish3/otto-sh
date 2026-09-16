# Coverage report search: the ⌘K palette, a report-wide text index, and function jump

**Status:** approved design, not yet planned. Stage 1 of 2 (stage 2, symbols
and references, is sketched in §11 and gets its own spec).
**Breaks:** nothing. `OTTO_COV_DATA_FORMAT` stays 2 (additive chunks);
`STORE_FORMAT_VERSION` stays 7 (additive key, tolerated absent on load).
**Bundle:** the covapp ceiling stays 2 000 kB; §9 pays for the palette by
dropping a 219 kB icon set the report never draws.

## 1. Motivation and provenance

The coverage report has no way to find code. A developer looking at a
directory page who wants "every call to `mutex_lock` that is still
uncovered" opens files one by one or leaves for their editor, and the editor
does not know what is covered. Verified against source during the brainstorm
of 2026-09-15:

1. Source text reaches the browser only inside per-file chunks
   (`cov_data/files/<mangled>.js`, `_build_file_chunk` in
   `src/otto/coverage/renderer/spa_data.py`), loaded lazily on navigation.
   Nothing report-wide is searchable today.
2. The only search UI is `TicketSearch.tsx` (ticket ids, `/`) and the runs
   page's free-text filter over label/host/ticket/board. The shortcuts dialog
   lists three bindings: `?`, `/`, `Escape`.
3. `PALETTE_BINDING` (`web/src/ui/shortcuts.ts:80`) already defines ⌘K on
   mac and Ctrl+K elsewhere, with `matchesBinding`/`formatBinding` and the
   reserved-key rules. The monitor's `CommandMenu.tsx` and `SearchTrigger.tsx`
   consume it. The covapp does not.
4. `lcov` tracefiles carry `FN:` and `FNDA:` records (compiler-emitted
   function start lines and per-function hit counts). `lcov_loader.py`
   documents them in its docstring and parses only `SF`/`DA`/`BRDA`.
5. The UI-rework spec (2026-07-24) lists "report-side keyboard navigation
   (menu item exists; bindings TBD)" as open item 4. This spec fills it.

Chris's rulings from the brainstorm: coverage-aware filtering is wanted but
must be a toggle (people also just jump around the code); no optional extras,
every feature is always present in `otto-sh`; dependencies are welcome where
they replace code we would otherwise write; real repos are around 1 MB of
C/C++.

## 2. Goal and scope

One palette, opened by ⌘K / Ctrl+K (and a search trigger in the app bar) on
every page of the report, that searches:

- **Text**, substring or regex, across every source file in the report,
  with an off-by-default "uncovered only" toggle.
- **Functions** by name (`@` prefix), jumping to the definition, using the
  compiler's own `FN:` records.

Enter navigates to the file page with the target line scrolled into view and
every match in that file highlighted. The query travels in the hash (`?q=`)
so a result is a shareable link.

### Non-goals (documented, deliberately out)

- Symbols other than functions, clickable identifiers, and find-references.
  Stage 2 (§11).
- Fuzzy matching. Substring with smart-case is what `grep`/`rg` users expect;
  fuzzy is for the `@` function list only if stage 2 finds it wanted.
- Sharding the text index. At 1 MB of source one chunk is right; §6.4 records
  the lever.
- Demangling C++ `FN:` names. They are shown as `lcov` emitted them; §6.3
  names the `geninfo --demangle-cpp` knob for a follow-up.
- Bare-key stepping bindings (`n`/`N`). Stepping happens inside the palette
  (§3.4), so no new bare keys join `/`.

## 3. Palette UX

### 3.1 Opening and closing

- ⌘K / Ctrl+K toggles the palette on every page. The listener is local to
  the covapp (the `TicketSearch.tsx` pattern: `matchesBinding(e,
  PALETTE_BINDING)`, `document.addEventListener("keydown")`), not
  `useGlobalShortcuts`, which is zustand-coupled to the monitor.
- The app bar gains a search trigger to the left of `TicketSearch`,
  rendered as the monitor's `SearchTrigger` (button dressed as an input,
  ⌘K keycap). `SearchTrigger` currently reads `useUiStore`; it takes an
  `onOpen` prop instead so both apps use it (the monitor passes
  `openPalette`).
- `Escape` closes. A click outside closes. Reopening restores the last query
  and mode for the session (component state, not persisted).
- The ⋮ menu's "Keyboard shortcuts" dialog gains one row: `⌘K` / `Ctrl+K`,
  "Search code and functions". `ShortcutsDialog.tsx`'s header comment says
  the list is everything covapp binds; it stays true.

### 3.2 Input and modes

- One text field. Leading `@` switches to **function mode**; anything else
  is **text mode**.
- Text mode has two chips, both mouse-toggled, both persisted:
  - **Regex** (off by default). Off: the query is a literal substring. On:
    the query is a JavaScript `RegExp` with the `g` flag (and `i` per
    smart-case). An invalid pattern shows the `SyntaxError` message inline
    under the field and lists no results.
  - **Uncovered only** (off by default). On: only lines whose search state
    is `u` (§6.2) count as matches.
  Persistence: `localStorage["otto-cov:palette:regex"]` and
  `["otto-cov:palette:uncovered"]`, NOT stamp-namespaced. These are
  preferences about how the user searches, not facts about one report,
  unlike `?ctx=`/`?ticket=` which name report-specific ids.
- **Smart-case** in both text sub-modes: a query with no uppercase letter is
  case-insensitive; any uppercase letter makes it case-sensitive (the
  `rg`/`fzf` convention). Regex mode applies it as the `i` flag.
- Queries shorter than 2 characters in text mode list nothing and show
  "Type at least 2 characters". Function mode lists everything for a bare
  `@` (a browsable index).
- Input is debounced 50 ms.

### 3.3 Results

Text mode:

- Grouped by file, in the report tree's order (§6.2 fixes the emit order),
  except that the **current file page's group is listed first** when the
  palette is opened from a file page. This is how "search this file" works
  without a scope toggle.
- Each row: line number, the line's text with the matched span emphasised,
  and a state dot using the existing `state_colors`/uncovered tint. A line
  with several matches is one row.
- Capped at **500 matching lines**. The footer always reads "N lines in M
  files" and, when capped, "showing the first 500 — refine the query". The
  count is total, never the shown count (the `TicketSearch` overflow rule).
- **Ticket scoping:** when a ticket is pinned (`?ticket=`), only files the
  ticket owns lines in are searched, mirroring how the tree hides
  non-participating files. The file set is `TicketChunk.files[].path` from
  `loadTicketChunk`, the same source `scopeTreeToTicket` in `tickets.ts`
  reads, so the palette and the tree cannot disagree. The footer says "in
  ticket PROJ-204". A
  run-context focus (`?ctx=`) does not restrict the file set, because the
  tree does not either. "Uncovered" always means uncovered in every tier of
  the whole report (§6.2); the chip's tooltip says so.

Function mode:

- Rows: function name, display path, start line, and a covered/uncovered
  pill from `FNDA` (hit in any tier, or not). Substring smart-case over the
  name. Sorted by name, then path. Capped at 200 with the same footer rule.

### 3.4 Keyboard inside the palette

| Key | Action |
| --- | --- |
| ↑ / ↓ | Move the selection across rows, over group boundaries |
| Enter | Navigate to the selected row and close |
| ⌘Enter / Ctrl+Enter | Navigate to the selected row and keep the palette open (stepping) |
| Escape | Close |

Navigation target: `#/coverage/<display path>?lines=<n>&q=<encoded query>`
for text rows (`&re=1` and `&unc=1` appended when those chips are on, so the
link reproduces the search), `#/coverage/<path>?lines=<start>` for function
rows. `?lines=` reuses `FilePage.parseLinesRange` and its scroll-to-row
effect unchanged.

### 3.5 Empty and error states

- Index not yet loaded: the field is enabled immediately; the results area
  shows "Loading search index…" until the chunks resolve (§6.1). Typing
  during the load is allowed; results appear when it lands.
- Stamp mismatch or load failure: the results area shows the same message
  `GuardScreen` uses for "report changed on disk", and the palette stays
  usable to close. It does not replace the page.
- No matches: "No matches for `query`" plus, when "Uncovered only" is on,
  "(uncovered only)" so the toggle is the first suspect.

## 4. File page: `?q=` highlighting

- `FilePage` reads `q`, `re`, `unc` from the hash query (the `focus.tsx`
  `parseHashQuery` helper; `useHashLocation` already strips the query before
  route matching, so a new param is safe). It recomputes the match set for
  the current file with the same engine the palette uses (§5) and paints
  every matched span.
- Painting uses the **CSS Custom Highlight API** (`CSS.highlights`,
  `Highlight`, `Range`) over the text nodes of each `.cv-src` cell: no DOM
  mutation, so Shiki's spans are untouched and the ranges are rebuilt when
  `q` or the chunk changes. Where `CSS.highlights` is undefined the row
  keeps the existing `data-highlighted` tint only.
- The app bar shows a dismissible pill "`query` · N matches" while `q` is
  set; its ✕ removes `q`, `re`, `unc` from the hash via `setHashQuery`.
  `?lines=` is left alone.
- `q` is NOT mirrored to localStorage. It is a link-state, not a focus.

## 5. Search engine (TypeScript, no library)

`web/src/covapp/search.ts`, pure functions, no React:

```ts
export interface SearchOptions { regex: boolean; uncoveredOnly: boolean }
export interface SearchFile { chunk: string; path: string; text: string; states: string }
export interface LineMatch { line: number; spans: [number, number][]; text: string }
export interface FileMatches { chunk: string; path: string; lines: LineMatch[] }
export function compileQuery(q: string, opts: SearchOptions): RegExp | { error: string };
export function searchFiles(files: SearchFile[], re: RegExp, opts: SearchOptions,
                            cap: number, firstChunk?: string): { groups: FileMatches[]; total: number; capped: boolean };
export function searchFunctions(fns: FunctionEntry[], q: string, cap: number): { rows: FunctionEntry[]; total: number };
```

- `compileQuery` escapes the literal in substring mode, applies smart-case,
  and always returns a `RegExp` with `g` so both modes run one code path.
  Zero-length matches (e.g. `a*`) advance `lastIndex` by one to guarantee
  termination.
- `searchFiles` scans `text` with `re.exec` per line. Line splitting is on
  `"\n"` exactly as `FilePage` does (`chunk.source.split("\n")`), so line
  numbers agree by construction. `states[i]` gates line `i+1` when
  `uncoveredOnly`. `total` counts every matching line even past `cap`.
- Budget: 1 MB of C in one synchronous pass is under 20 ms in V8; the
  vitest scaling test (§10) pins a generous ceiling so a quadratic regression
  fails, not a slow laptop.

## 6. Data contract

Two new lazily loaded chunk kinds, siblings of the file and ticket chunks:
same stamp guard, same `document.createElement("script")` loader, same
pending/cache maps in `data.ts`.

### 6.1 Loading

Both chunks load on the first palette open (either kind of query needs at
least one), in parallel, and are cached for the page's life. Nothing loads
them at boot: a report opened to look at one file pays nothing for search.

### 6.2 `cov_data/search.js` → `window.__OTTO_COV_SEARCH__({...})`

```json
{"stamp": "<stamp>",
 "files": [{"chunk": "<mangled>", "path": "<display path>",
            "text": "<full source, errors=replace>",
            "states": "<one char per line>"}]}
```

- `files` is in **tree order**: the same depth-first, name-sorted walk that
  builds `IndexPayload.tree`, so grouped results and the tree agree.
- `states` has exactly `text.count("\n") + 1` characters. Character `i`
  describes line `i + 1`:

  | char | meaning | derived from |
  | --- | --- | --- |
  | `-` | no line record (not executable) | absent from `fr.lines` and not excluded |
  | `c` | covered | `LineRecord.hits.is_hit()` and `state is None` |
  | `u` | uncovered | record present, not hit, `state is None` |
  | `x` | excluded | in `fr.excluded_lines` |
  | `s` | stale | `state == "stale"` |
  | `a` | aging | `state == "aging"` |

  Records numbered past EOF (the existing "keys may exceed EOF" case) are
  dropped from `states`. Excluded wins over any record on the same line,
  matching the reporter's filter stage, which already deleted the record.
- `text` duplicates the file chunk's `source`. At 1 MB that is the right
  trade: one script for the whole corpus versus N.

### 6.3 `cov_data/symbols.js` → `window.__OTTO_COV_SYMBOLS__({...})`

```json
{"stamp": "<stamp>",
 "functions": [{"name": "checked_add", "chunk": "<mangled>",
                "path": "<display path>", "line": 4, "end": 9,
                "hits": {"system": 4, "unit": 12}}]}
```

- Sorted by `name`, then `path`, then `line`.
- `end` is the `FN:` end line when the tracefile carries one (lcov ≥ 2.0
  emits `FN:<start>,<end>,<name>`), else `null`.
- `hits` is the per-tier `FNDA` total, shaped like `LineJson.hits`.
- Names are as emitted. C++ builds without `--demangle-cpp` yield mangled
  names; that knob belongs to the capture pipeline and is a follow-up.
- Stage 2 extends this chunk with `refs` and a `kind` per symbol (§11);
  the name `symbols.js` is chosen now so stage 2 adds keys rather than
  renaming a file.

### 6.4 Sizing rule and the NFS ruling

The report is rendered to, and served from, NFS (Chris, 2026-09-15). As in
the UI-rework spec §9, **file and round-trip counts are the metric**, not
bytes: a real report has 100–200 source files and about 1 MB of text. This
design's I/O accounting, which stage 2 must preserve:

- Rendering adds exactly two files per report (`search.js`, `symbols.js`)
  and zero source reads (§8 shares one read per file with the file chunk).
- Serving the palette costs one request for the whole corpus. Loading the
  100–200 per-file chunks instead would be 100–200 NFS-backed requests,
  which is why the text index is precomputed rather than assembled in the
  browser.
- Stage 2 adds keys to existing chunks (`symbols.js`, the file chunks);
  it must not introduce a per-file chunk kind.

The renderer logs a warning when `search.js` exceeds 16 MB, naming the
size and this section. That is the trigger for sharding by top-level
directory (an index listing shards, loaded in order, results streamed);
nothing is built for it now, and at the sizes above it never will be.

### 6.5 Contract table additions (`tests/_fixtures/covapp_contract.json`)

```
chunk_callbacks   += search: "__OTTO_COV_SEARCH__", symbols: "__OTTO_COV_SYMBOLS__"
cov_data_layout   += search: "search.js", symbols: "symbols.js"
search_chunk_keys  = [files, stamp]
search_file_keys   = [chunk, path, states, text]
search_state_chars = {"-": "none", "c": "covered", "u": "uncovered", "x": "excluded", "s": "stale", "a": "aging"}
symbols_chunk_keys = [functions, stamp]
function_json_keys = [chunk, end, hits, line, name, path]
```

Asserted from both sides exactly as today: Python off real emitted payloads,
TypeScript via `Record<keyof X, true>` maps, plus `window` declarations for
the two callbacks in `types.ts`.

## 7. Store model: functions from `FN:`/`FNDA:`

`src/otto/coverage/store/model.py`:

```python
@dataclass
class FunctionRecord:
    name: str
    start_line: int
    end_line: int | None = None
    hits: LineHits = field(default_factory=LineHits)
```

- `FileRecord.functions: dict[str, FunctionRecord]` keyed by name.
  `merge()` adds hits like lines do and keeps the first-seen lines.
- `to_dict()` gains `"functions": [...]` (sorted by start line);
  `CoverageStore.load` reads it with `.get("functions", [])`, so a v7 store
  written before this change loads with no functions. `STORE_FORMAT_VERSION`
  does not move: nothing existing changes meaning.
- `lcov_loader.py` parses `FN:<start>[,<end>],<name>` and
  `FNDA:<count>,<name>`. A C++ name can itself contain commas
  (`foo<int, char>`), so the split is positional from the left, never
  `split(",")`: take `start` up to the first comma; if the remainder matches
  `^\d+,` take that as `end`; everything after is the name, commas included.
  `FNDA` splits once at the first comma. Each `FNDA` count is added to
  `functions[name].hits` for the load's tier. An `FNDA` for a
  name with no prior `FN` creates the record with `start_line=0` and a
  warning naming the file, rather than dropping the count.
- `tests/_fixtures/_report_fixture.py` builds `FileRecord`s directly; it
  gains `FunctionRecord`s for `checked_add` (hit) and `main` (hit) in
  `main.c` and one never-hit function in `utils.c`, so the fixture report
  shows both pills and the docs screenshot has something to show.

## 8. Emission (`spa_data.py`)

- `_build_search_payload(store, prefix, stamp)` and
  `_build_symbols_payload(store, prefix, stamp)` beside the existing
  builders; `emit_chunks` writes both files after the file chunks and before
  `index.js`. Reading each source twice (file chunk and search chunk) is
  avoided by building the file chunks and the search file list from one
  read per file.
- The tree walk order is shared with `build_index_payload` by extracting the
  existing walk into one generator both call, so §6.2's ordering promise is
  the same code, not a parallel implementation.

## 9. Bundle headroom

Measured from the shipped sourcemap (2026-09-15, `covapp.js` 1 990 kB):

| bytes | share | source |
| --- | --- | --- |
| 869 kB | 44 % | `@shikijs/langs` (cpp 501 kB + cpp-macro 284 kB + c 72 kB) |
| 219 kB | 11 % | `@untitledui/file-icons`, reached only through `components/application/empty-state`'s `FileIcon` |
| 178 kB | 9 % | react-dom |
| 133 kB | 7 % | react-aria |
| 67 kB | 3 % | `web/src/covapp` itself |

The covapp reaches `empty-state.tsx` from `GuardScreen.tsx`; the monitor
reaches it from `shell/EmptyState.tsx`. The icon set enters through the
vendored `EmptyState.FileTypeIcon` slot, which no file in `web/src` renders
(verified 2026-09-15). `empty-state.tsx` drops that slot and with it the
`@untitledui/file-icons` import; the `FeaturedIcon` and `Illustration` slots
are untouched. This is one commit, first on the branch, and frees about
210 kB in both bundles. The
palette, engine, and highlight code are expected under 30 kB; the ceiling
stays at 2 000 kB and the build still fails on growth past it.

Shiki's grammars are the other 44 % and are data, not code. Replacing them
with render-time highlighting is a stage-2 decision (§11), because it needs
the same parser stage 2 brings.

## 10. Testing

Each row names the mutation that turns it red.

| Layer | Test | Red on |
| --- | --- | --- |
| Python unit | `test_lcov_loader.py`: `FN:4,checked_add` and `FN:4,9,checked_add` both load; `FNDA:12,checked_add` adds per tier; two `.info` loads merge hits; orphan `FNDA` warns and keeps the count | dropping either `FN` shape, summing into the wrong tier, losing an orphan count |
| Python unit | `test_model.py`: `FileRecord` round-trips `functions` through `to_dict`/`load`; a store dict without the key loads | a load that `KeyError`s on old stores |
| Python unit | `test_spa_data.py`: `states` length equals line count for a file with records past EOF; excluded beats a record; tree order equals index order for a two-directory store; `symbols.js` sorted by name/path/line; the 16 MB warning fires on a monkeypatched threshold | any off-by-one in `states`, a second walk drifting from the tree |
| Python unit | `test_covapp_contract.py`: new keys read off real payloads | an emitter key not in the table |
| Python unit | `test_spa_renderer.py`: both chunk files exist after `render()` | forgetting to call the new builders |
| vitest | `search.test.ts`: smart-case both ways; regex on/off; invalid regex → `error`; zero-length pattern terminates; `cap` and `total` diverge past the cap; `uncoveredOnly` gates on `states`; `firstChunk` moves the group to the front; 1 MB synthetic corpus completes under a pinned ceiling | each branch of the engine |
| vitest | `contract.test.ts`: `Record<keyof SearchChunk, true>` etc. | interface drift |
| vitest | `data.test.ts`: search/symbols loaders share the stamp guard and dedupe in-flight loads | a copy-paste loader that skips the guard |
| vitest | `Palette.test.tsx`: ⌘K opens/closes; `@` switches mode; chips persist to the two localStorage keys; Enter navigates to `?lines=&q=`; Ctrl+Enter keeps it open; ticket pin narrows the file set; footer counts total not shown | each UX rule in §3 |
| vitest | `FilePage.test.tsx`: `?q=` paints ranges when `CSS.highlights` is stubbed and falls back when absent; the pill's ✕ clears `q`/`re`/`unc` and not `lines` | highlight without fallback, clearing too much |
| vitest | `AppShell.test.tsx` (where the dialog's rows are asserted today): the ⌘K row is listed | the dialog lying |
| Playwright | `test_spa_search.py`: Ctrl+K on the root page, type `checked`, Enter lands on `product/main.c` with row 4 highlighted and the pill showing; `@main` jumps; the CSP lane loads both new chunks with the autouse pageerror guard clean | a real-browser or CSP regression |
| Playwright | `test_spa_search.py`: the palette on `utils.c` lists `utils.c` first | §3.3 ordering |
| docs | `scripts/capture_docs_media.py` gains `coverage-search.png`; `make docs-captures-check` | screenshot drift |

The report-browser conftest's bundle-staleness gate and `_pageerror_guard`
apply unchanged. The empty-state change is covered by the existing monitor
and covapp e2e suites; `knip` catches the now-unused `@untitledui/file-icons`
dependency, which is removed from `package.json` in the same commit.

## 11. Stage 2 (reserved, own spec)

Recorded so stage 1's names and shapes do not need renaming later.

1. **Parser.** `tree-sitter`, `tree-sitter-c`, `tree-sitter-cpp` as hard
   dependencies (no extra). Wheels verified 2026-09-15: core has cp310–cp314
   Linux wheels, no abi3; both grammars are abi3. The 3.15 nightly canary
   will build the core from source, so that lane needs a C compiler present.
   Both grammar wheels ship `HIGHLIGHTS_QUERY` and `TAGS_QUERY`; the tags
   query is the definition/reference extraction GitHub's code navigation
   uses (`definition.function`, `definition.type`, `reference.call`, …).
2. **`symbols.js` grows** `kind` per entry, non-function definitions, and
   `refs: [[chunk, line, kind]]`. Sharding by symbol-name hash is the lever.
3. **File chunks gain `tokens`**: identifier occurrences resolved to symbol
   ids, rendered as Shiki decorations (`shiki/core` supports `decorations`
   in the installed 4.4.3) so a click delegate on `.cv-src` resolves them.
4. **Render-time highlighting.** With a parse already in hand,
   `HIGHLIGHTS_QUERY` can emit highlight scopes into the file chunk and the
   client drops Shiki: −870 kB grammars, −~130 kB engine. Measured
   2026-09-15: naive JSON token streams are 2.5–3.8× source size, so this
   needs a compact per-line encoding (~3 bytes per token) to stay near 1×.
   Doc promise "syntax highlighting runs a pure-JS regex engine" changes to
   "pre-rendered at report time". Decision deferred to the stage-2
   brainstorm; stage 1 does not depend on it either way.
5. **Precise producer door.** The capture metadata already records each
   toolchain's sysroot; a libclang/`compile_commands.json` producer could
   feed the same `symbols.js` shape with true semantic resolution.

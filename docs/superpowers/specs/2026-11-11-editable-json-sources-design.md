# Every historical source is editable — one source per URL (#958)

**Status:** draft, decisions settled with Chris 2026-11-11 · **Issue:** #958 · **Breaking:** yes (`!`)

## Intent

A `.json` monitor export is a past live session made portable. It goes to two
kinds of reader: people, who open it in the dashboard's historical view, and
other tools and ML pipelines, which read the file directly for statistics and
training. Editing its events is how someone annotates or corrects that record
before passing it on: marking an incident noticed afterwards, fixing a label,
ending a span left open. Export produces the copy that goes out.

Today only live mode and a `.db` review can edit events. A served `.json` is
read-only (the server's `_mutation_guard` answers 403), and a file imported in
the browser is read-only too (`detachServerSource` sets `editable=false`).

The direction (Chris, 2026-11-07 and 2026-11-11): every historical source
takes the same event edits; only how they are saved differs.

## The model: one URL, one source

Each URL (each running `otto monitor`) serves exactly one source at a time,
and every tab open on it sees that source and its current data.

| | Live | Historical `.db` | Historical `.json` |
|---|---|---|---|
| Started by | `otto monitor --live [--db F]` | `otto monitor F.db` | `otto monitor F.json`, or an Import on a `.json` URL |
| Where edits go | the running session (and its `--db`, if given) | written into the file as you go | the server's in-memory copy of the document; the file is never written |
| Every tab sees the same data; a reload keeps it | yes | yes | yes |
| Import another file | **not offered** | **not offered** | yes, after a prompt; the URL switches to that `.json` |
| Unsaved-changes marker | none | none | from the first edit until an export |
| Edits can be lost by | — | — | switching source (prompts first); stopping `otto monitor` (warns in the terminal) |

- **Live and historical are separate.** A live URL never switches to a file.
  Export stays on it: it is how a live session becomes a `.json`. To look at a
  file while a live session runs, start a second `otto monitor F.json`.
- **`--db` on a live run is unchanged:** a missing file is created for the
  run; an existing one gains this run as a new session, with everything the
  run collects.
- **Only a `.json` URL switches** (Chris, 2026-11-11). A live URL and a `.db`
  URL never change source: both refuse Import, at the server as well as in
  the UI. To look at another file, start a second `otto monitor`. The browser
  cannot open a database, so Import accepts `.json` only.
- **Switching is a server change, not a tab change.** An Import on a
  `.json` URL sends the file to the server, which makes it the URL's
  source for every tab. This replaces #873's per-tab detach, in which the
  importing tab quietly diverged from the server and from its other tabs.

## Edits

- **Same edits everywhere.** Create (Mark now is live-only, as today; Add
  event and sweep in review), edit, end and delete work on every source from
  the same UI, through the same `web/src/data/eventApi.ts` helpers and the
  same server routes. No component branches on the source.
- **One write rule.** A `.json` edit goes through the server's existing event
  routes and `event_ops` (the label and span rule `check_event_write`,
  `normalizeEventStyle`, the `legacyColor`/`legacyDash` preservation of #857,
  the #941 compare-and-write). The `.json` path differs from the `.db` path
  only in its persistence step: it patches the in-memory document
  (`_patch_document_event` / `_drop_document_event`) without an archive write.
- **Ids.** A new `.json` event gets the next id in its session from a
  per-session high-water mark (shared allocation logic, see Contracts), initialised at load to the session's highest
  id and never lowered, so a deleted id is never handed out again while the
  source is open. This is what #929's revert guard (`event_ops.id_handed_out`)
  needs: undoing a delete after a create must not collide. The mark is not
  written into the export; undo history does not survive a reload or a
  switch, so a re-imported file may start again from its highest id.
  *(Claude's call.)* An event the file holds without an id stays view-only,
  as today (Chris, 2026-11-11).
- **Edits are data.** An event added or changed in review carries no marker
  of having been edited; downstream readers treat it exactly like one
  recorded live.
- **Undo/redo** works on every source unchanged: #929 applies every inverse
  through the same helpers.

## Saving a `.json`: export

- **The tab builds the export** in every mode, from the data it is showing,
  with the one builder live export uses today (`documentFromSessions`,
  `web/src/data/exportDoc.ts`). Every tab holds the same data as the server
  (each edit is pushed to it), so Export works whenever a tab is open, in
  live and every historical view, even after `otto monitor` has stopped.
  (Chris, 2026-11-11.)
- **Export asks for a name.** Export, or **Ctrl+S**, opens a small dialog
  with the file name pre-filled and editable; confirming downloads it,
  cancelling does nothing. Ctrl+S suppresses the browser's own "save page".
  The pre-filled name is the source's name, with `-edited` before the
  extension when there are unsaved writes (never added twice), or the live
  default. (Chris, 2026-11-11.) Where the file lands then follows the
  browser's own download setting.
- **Confirming counts as saved.** The tab then tells the server how many
  writes the file covered (a POST, never a GET: the `SameSite=Lax` auth
  cookie, `server.py:264`); the server lowers the unsaved count by exactly
  that, so a write that landed meanwhile stays unsaved, and every tab's
  marker follows. If the server is gone, the download still happens and only
  the acknowledgement is lost.
- Export writes the format Import reads, so open → edit → export gives an
  edited copy, round-tripping every edit: id, label, times, colour, dash and
  source. A legacy colour or dash survives untouched unless edited.
- **Unsaved state lives on the server**, because every tab must agree on it.
  It covers the whole document (every session in it) and is a count of
  writes since load or the last export: any write raises it, export resets
  it to zero. An edit later undone still counts; there is no diff against a
  baseline. *(Claude's call.)* Every tab shows the "unsaved changes" marker
  while it is non-zero.
- **Stopping `otto monitor`** with unsaved `.json` edits prints a warning
  giving that count. It does not block the stop. *(Claude's call.)*

## Switching source

- **Every switch asks first** (Chris: "as soon as a user changes to a JSON
  file, they are prompted"). The prompt says what is being left: unsaved
  edits that will be lost, or a file with none that closes. Other tabs,
  which see no prompt, get a notice that the source changed.
- After a switch, every tab on the URL shows the new source (see below).

## Every open tab updates at once

Review mode gets the push stream live mode already uses (`/api/stream`):
event changes, a source switch and the unsaved count reach every open tab
immediately (Chris, 2026-11-11). This covers `.db` review too, so two tabs on
one `.db` no longer drift apart until a reload. A tab still showing an old
source cannot write into the new one: the server refuses a write addressed
to a source it no longer serves, and the tab moves onto the current source.

## Contracts the reviews settled (Astra + Fable, 2026-11-11)

Both reviewers agreed on these; they bind the plan.

- **One replaceable source holder.** Today the served document, its cached
  body and `archive_path` are closure values of `_build_app`
  (`server.py:300-325`). They move into one holder (document, body,
  archive path, unsaved count, id high-water marks, source token), replaced
  only under `collector.mutation_lock()`. Every write checks its source token
  *inside* that lock, before resolving any session or event, so a write that
  crosses a switch is refused rather than patched into the new source (or
  tripping an assert).
- **A source token on every write and on the switch confirmation.** Session
  ids alone cannot tell sources apart: re-importing an edited copy keeps
  every one. A switch confirmation carries the token and unsaved count it was
  shown with; if either changed, the server refuses and the tab re-prompts.
- **One client transition.** `detachServerSource` (`source.ts:44`) becomes the
  one transition every source change runs through (an import response, a
  switch frame, a resync that finds a new source): bump the generation,
  clear undo history explicitly (today only `setEditable(false)` clears it),
  close the editor, sweep, popover and open span, and take the new source's
  name and state from the server.
- **Document-level stream frames.** Per-session fragments require `session`
  (`models/monitor.py:722`) and `stream.ts:21` drops frames without one, so
  a switch and the unsaved count travel in a new document-level frame kind.
  Event changes publish from the document patch/drop seam, through the one
  `_publish`, with its stamp a parameter. A conflict repair publishes but does
  not count as an unsaved write.
- **Resync restores everything.** A reconnect re-fetches `/api/mode` (now
  carrying the source token, editable, and the unsaved count) along with the
  snapshot, so a tab that missed a switch frame still lands on the current
  source.
- **Ordering.** Review responses and pushes carry the server's stamp so a
  late HTTP reply cannot overwrite a newer pushed change, and resync replays
  them; the client advances the chart horizon from a stamp only in live mode.
  *(Claude's call, between Fable's "one stamp, the consumer decides the
  horizon" and Astra's "a separate revision": one stamp is one decision, and
  the horizon rule moves to its single consumer.)*
- **Export is built by the tab; the acknowledgement is a POST.** One builder
  (`documentFromSessions`) for every mode; the POST carries the source token
  and the unsaved count the file covered, and the server acknowledges only
  that count. *(Settled with Chris: replaces the earlier server-built
  export, so Export survives a stopped server.)*
- **One validator, CLI and upload.** `load_review_document`
  (`review.py:30`) gains the editable-document checks: duplicate session ids
  are refused; event ids must be positive integers the browser can hold
  exactly. An event whose id is missing or repeated within its session stays
  view-only, the same rule as Chris's for id-less events. *(Claude's call:
  repeated ids follow the id-less rule rather than refusing the file.)* A
  file that fails leaves the current source and its unsaved state untouched.
- **Ids are per session**, matching how the API addresses events (session +
  id) and the schema, with the allocation rule shared with `MetricStore`'s
  existing "counter stays ahead" logic (`store.py:69`) rather than a third
  allocator. *(Claude's call, Astra over Fable: a document-global counter
  would be a rule the API does not need.)*
- **One mutation sequence, two backends.** `.db` and `.json` share the
  mutate/revert sequence; each backend supplies only its read and its write.
- **Upload.** A raw JSON body (no multipart dependency), a byte limit, one
  upload at a time under the mutation lock, parsed on the event loop (no new
  thread hand-off). Live refuses an upload at the server as well as hiding
  Import; today's drop handler is unconditional (`ImportExport.tsx:36`).
- **No whole-document re-serialisation per edit.** The cached body becomes
  lazy, rebuilt when next requested, instead of `document_json` on every
  patch (`server.py:359`).
- **Scope of "every tab".** Tabs on this `otto monitor` see each other at
  once. A second `otto monitor F.db` process on the same file is still seen
  only through a refused write (#941), as today.
- **Docs.** `dashboard.md` (sources and how they save; export is the save),
  `review.md`, and the docstrings and comments that say `.json` is read-only
  (`review.py:53`, `server.py:398`, `server.py:626`).

## Non-goals

- A richer session list for multi-session `.db` files (filed separately).
- Writing edits back into the original `.json` file.
- Opening a `.db` from the browser.
- Any change to live mode beyond refusing Import and the export name dialog.

## Acceptance

- On live, `.db` and `.json` (CLI-served and imported) sources, create, edit,
  end and delete work and show in every tab.
- Editing a `.json`, exporting, then importing the export round-trips every
  edit (id, label, times, colour, dash, source); legacy style survives.
- The unsaved marker appears after the first `.json` edit, in every tab, and
  clears after export. Importing another file while unsaved prompts first.
- Live and `.db` URLs refuse Import, in the UI and at the server.
- Export and Ctrl+S open the name dialog with the pre-filled name; the
  download carries the confirmed name; Export works after `otto monitor` has
  stopped.
- `.db` and live editing behaviour is unchanged.
- `docs/cli/monitor/dashboard.md` documents which sources save how. The
  commit is `!`.

## Left to the builder

Route shapes and the upload byte limit; the name dialog's look (Untitled UI
first); the token's form; the marker's
placement and wording (Untitled UI first; "N changes since export" reads
well); the toast other tabs show after a switch; how the shutdown warning
reaches the terminal.

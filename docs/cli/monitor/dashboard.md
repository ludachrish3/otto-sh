# Web dashboard

In both modes, `otto monitor` serves a web server: it binds an OS-assigned
free port and logs the dashboard URL at startup (`Server running at
http://<ip>:<port>`, one URL per non-loopback interface).

On load, the dashboard shell first opens `GET /api/stream` (Server-Sent
Events) on that same server, so no change published from then on is missed.
Once the stream is open, it asks the server one question — `GET /api/mode`
— then follows up with `GET /api/monitor_sessions` and renders the result,
exactly as if you'd used Import yourself: no click needed. Live and review
servers answer with the same `format:1` shape — a live monitor session is
one whose `end` is still open, like a crashed session found on disk — so the
topology map populates immediately either way (as do the fleet grid and
charts once you switch to Hosts). From then on the stream *grows* the loaded
session in place, appending each fragment as it arrives.
Served by a bare static file server with no `/api/*` routes at all, the
dashboard still works: when the stream cannot open, the boot asks nothing
more and falls back to the same empty Import screen, never a broken page.
Import waits until the boot knows which it is. Once an otto server's stream
has opened, the server is there: a request that fails is retried through the
stream's reconnect, and the page says it is waiting for the server's data
rather than offering Import. Once `/api/mode` has answered, its source
decides: a live run that has recorded nothing yet shows that waiting screen,
without Import, and keeps retrying until its first data arrives, with the
**Reconnecting…** banner up while each retry backs off.

You can also feed it a monitor export document yourself — drag a file onto
the window, or use the **⋯** overflow menu's *Import* — where Import is
offered: on a `.json` review it switches the source the server serves, and
with no server behind the page it loads the file into the tab (see [Sources
and saving](#sources-and-saving)). An imported document renders exactly
like a boot-fetched monitor session:

- **Fleet grid.** Element-grouped host tiles, each with a status dot, an
  element-level health-rollup bar, and a labeled headline metric; a down
  tile shows its outage duration instead.

  ![The review dashboard's fleet grid: element-grouped host tiles with a
  status dot, a health-rollup bar per element, and a labeled headline
  metric](../../_static/generated/dashboard-review.png)

- **Health, scoped to the viewed range.** Every status, rollup, and
  headline reflects whichever time window the review bar is currently
  showing — narrow the range and a host that's healthy across the full
  session can show down (or vice versa) if that's what the narrower
  window actually contains.
- **Per-subject charts.** Drilling into a host (or an element) stacks its
  metrics as synced chart panels — panning or zooming one follows the
  rest of the stack, so a spike is easy to correlate across series. See
  [Chart gestures](#chart-gestures) below for how to drive them.

  ![A host's subject page: the series tree and its chip filters beside the
  top of the synced chart stack, every chart sharing one time axis and the
  session's event markers](../../_static/generated/dashboard-review-charts.png)

- **Series and source filtering.** A per-subject series tree toggles
  individual metrics on and off; chip filters narrow by metric group or
  by data source (a series' own host vs. an external management host —
  externally-sourced series carry a provenance badge).

  ![An element's subject page narrowed to CPU % by its chip filter: one
  line per member host on a single chart, with the element's hosts listed
  in the series tree](../../_static/generated/dashboard-element.png)

- **Events.** A reverse-chronological slide-over lists every event in the
  loaded document; clicking a row re-scopes the review bar's range to
  that event's span (padded ±15 minutes) — or, if that padded range falls
  entirely outside the session, shows a notice and stays open rather than
  closing on a silent no-op. An editable session (see [Marking
  events](dashboard.md#marking-events)) adds marking controls to this same
  panel.

  ![The Events slide-over: each event's color, label, and start time, newest
  first, with a span's duration after its
  start](../../_static/generated/dashboard-events.png)

- **Multiple sessions.** A document spanning more than one session (a
  config change captured mid-run, or a `--db` archive several `--live --db`
  runs appended into, for example) exposes a session picker; each entry's
  tooltip is that session's `--label`/`--note`, and each session renders
  under the lab configuration it was captured under, so drift between
  sessions never bleeds into the wrong one's view.
- **Export.** The **⋯** menu's *Export* (**⌘S** / **Ctrl S**) downloads the
  data the tab shows as a `.json` document, edits included, under a name
  you confirm; see [Saving a `.json`: export](#saving-a-json-export).

## Sources and saving

Each URL — each running `otto monitor` — serves one source at a time, and
every tab open on it shows that source and the same data. Every source the
server serves takes the same event edits (see [Marking
events](#marking-events)); they differ only in where an edit goes and how
you keep it:

| | Live | `.db` review | `.json` review |
|---|---|---|---|
| Started by | `otto monitor --live` | `otto monitor F.db` | `otto monitor F.json`, or an Import on a `.json` URL |
| An edit goes to | the running session, and its `--db` file when given | the `.db` file, at once | the server's copy of the document; the file itself is never written |
| Every tab shows it, and a reload keeps it | yes | yes | yes, until `otto monitor` stops |
| How you keep it | run with `--db` (see [Persisting data](live.md#persisting-data--sessions)), or Export before it stops | nothing to do | Export (**⌘S** / **Ctrl S**) |
| Import another file | not offered | not offered | yes, after a prompt; every tab moves to that file |
| Unsaved-changes marker | never | never | from the first edit until an export |
| Edits are lost by | stopping a run started without `--db` | — | switching source (asks first); stopping `otto monitor` (warns in its terminal) |

An edit made in one tab shows in every other tab on the same URL at once,
with no reload; this holds for a `.db` review as much as a `.json` one. An
edit is data, nothing more: an event added or changed in review carries no
mark of having been edited, so whoever reads the exported file sees it
exactly like one recorded live. An event the file holds without an id (or
with an id another event in its session repeats) stays view-only: it is
listed and drawn, but nothing can edit, end or delete it.

### Saving a `.json`: export

**Export** — the **⋯** menu's *Export*, the command palette's, or **⌘S** /
**Ctrl S**, which replaces the browser's own "save page" — opens a small
dialog with the file name filled in and its stem selected, so typing
replaces the name and keeps `.json`; **Enter** exports. The name offered is
the source's name with `.json` as its extension (`metrics.db` becomes
`metrics.json`), and `-edited` before it when the source has unsaved
changes (`metrics.json` becomes `metrics-edited.json`, and
`metrics-edited.json` stays as it is); in live mode it is
`otto-monitor-export.json`. Confirming downloads the file where your
browser saves downloads; **Cancel** or **Esc** closes the dialog and saves
nothing. The export holds every edit in every field (id, label, times,
colour, dash, and origin: manual or test), so importing it again gives back
exactly what you saw; an older capture's non-hex colour or unknown dash is
kept as it was unless you changed it.

**Confirming counts as saved.** On a `.json` review the review bar shows a
badge such as **3 changes since export** (**1 change since export** for
one) in every tab, from the first edit until an export. It counts writes,
not differences: an edit you then undo still counts, and so does the undo.
Once you confirm an export the badge clears in every tab; a change that
lands from another tab while the export is on its way stays counted. A
`.db` review never shows it: its edits are already in the file. Neither
does a live run: with `--db` its edits are in the archive; without it
nothing is kept on disk, so Export before stopping it.

**Stopping `otto monitor` with unsaved changes** goes ahead, and prints a
warning in its terminal naming the file it served and the count:

```text
Warning: 3 unsaved changes to metrics.json since the last export are lost.
```

(`1 unsaved change … is lost.` for one.) Export from any open tab first to
keep them. Export before starting `otto monitor` again, too: a restarted
server serves the file as it is on disk, without the edits, at a new URL
with a new [access key](serving.md#access-key). A tab still open on the old
URL never reaches it: it keeps what it shows behind the **Reconnecting…**
banner (Export still works there), and the restart's own printed URL is
the one to open.

### Switching source

Only a `.json` URL switches source. On one, every Import (the **⋯** menu,
**⌘I** / **Ctrl I**, the palette, or a file dropped on the window) first
asks: the dialog **Open new.json?** says either "Opening new.json closes
metrics.json." or, with unsaved changes, "Opening new.json discards 3
unsaved changes to metrics.json. Export first to keep them." **Cancel** has
the focus, so **Enter** never discards anything; **Open** asks the server to
serve the file. If another tab edits the source while the dialog is open,
pressing **Open** is refused and the dialog opens again with the current
count; if another tab switches the source, the dialog closes. A file that
is not a monitor export, or that repeats a session id, is refused ("Import
failed: could not open new.json: …"), and the current source and its
unsaved changes stay as they were.

A switch moves every tab on the URL to the new file. Every other tab shows
the toast "Source changed to new.json", and every tab's [undo
history](#undo-and-redo) starts again empty, since nothing in it applies to
the new file.

A live URL and a `.db` URL never switch: neither offers Import, and a file
dropped on one is ignored. To look at a file while a live run goes on, start
a second `otto monitor F.json`; it prints its own URL.

With no server behind the page (a bare static file server, see above),
Import loads the file into the tab only: it is view-only, with no marking
controls and no stream, and nothing is shared with other tabs. Reload the
page to return to the empty Import screen.

### While the server is gone

If `otto monitor` stops, or the connection to it drops, while a tab is open,
the tab keeps what it shows and the [**Reconnecting…**
banner](#live-status-pause-and-reconnect) appears, in review as in live
mode. Updates from other tabs stop arriving until the connection is back.
Export still works: the tab builds the file from the data it holds, so you
can still save a `.json` review's edits after the server has stopped, but
the badge stays, since no server hears the export. An Import on a `.json`
URL needs the server, and fails until it is back.

## Topology view

![The topology map of a dense lab laid out by data-plane structure:
element nodes with their host counts and health bars, the local node on
the left, three tunnels drawn dashed over the links they ride — ok in
gray, degraded in orange, uncertain faded — and the Key panel
bottom-left](../../_static/generated/dashboard-topology.png)

The topology map is the dashboard's landing view (`/`) — `/topology` remains
a working alias, so existing bookmarks and links keep resolving — with an
intra-element drill-down at `/topology/<element>`. It lays the lab out by its
data-plane structure rather than the management hop chain — see
{doc}`../../architecture/subsystems/network` for the underlay/overlay model it
draws from. The inter-element map aggregates each element into one node;
opening an element expands it into its individual hosts, alongside the
`local` node for otto's own management path. The fleet grid — the other
view, reachable from the same switcher — lives at `/hosts`; see [Web
dashboard](dashboard.md#web-dashboard).

The bottom-left **Key** panel documents the canvas's two axes — link class
and health status. There are three link classes:

- **static** — from the lab config: a declared link, a hop-derived one, or
  the `local` management star.
- **tunnel** — a live `otto tunnel`, drawn dashed with a wide casing sleeve
  (the only class drawn with one) so it reads as wrapped around a path
  rather than as a peer of the static links.
- **reports for** — metrics sourced from a management host rather than the
  subject itself.

**Tunnels are a live overlay**, not a snapshot. Each tunnel is drawn along
the links its hop path actually traverses: a consecutive pair of hops rides
its underlay link's exact geometry where one joins that pair, and gets a
plain routed segment between the two nodes where none does. Status styles
the whole tunnel uniformly, never per-segment — **ok** is the shipped
dashed-plus-casing stroke, **degraded** is a warning-accent variant of the
same geometry, and **uncertain** ghosts it down to a faint opacity. Clicking
any segment of a tunnel — riding or bare — selects the whole tunnel,
highlights every other segment of its path, and opens the tunnel block in
the link inspector: status, carriers (`present/expected`), protocol,
service port, age, and the ordered hop path.

Tunnel discovery runs on the collector's own collection interval and scans
the *whole lab*, independent of which hosts are actually monitored — a
tunnel between two otherwise-unpolled hosts still appears, on the same
cadence as every other metric tick.

## Chart gestures

Every per-subject chart (described under *Per-subject charts* above)
shares one gesture set, synced across the whole stack the same way panning
and zooming already are:

- **Drag** across a chart's plot area to zoom-select that range — release
  and the whole stack re-windows to exactly what you dragged.
- **Ctrl-drag** pans the current window instead of zooming, keeping its
  width fixed. This is **Ctrl** on every platform, not the app's usual
  per-platform modifier (⌘ on Mac, elsewhere Ctrl — see [Marking
  events](dashboard.md#marking-events)).
- **`+`/`-` buttons**, one pair per chart, step the zoom in or out around
  the window's current center.
- The **mouse wheel scrolls the page**, not the chart — it is never
  hijacked for zooming.

"Sweep span on chart" (see [Marking events](dashboard.md#marking-events)) reuses
the same drag gesture for a different purpose: while armed, the next drag
opens the event editor pre-filled with the dragged range instead of
zooming.

## Marking events

Every source the server serves — a live run, or a `.db` or `.json` review
(see [Reviewing a capture](review.md#reviewing-a-capture)) — is **editable**:
you can mark events directly from the dashboard, and [Sources and
saving](#sources-and-saving) says where each one keeps them. Only a file
loaded into a tab with no server behind it has no marking controls.

**From the app bar.** The **Mark now…** button (**⌘E** / **Ctrl E**) opens
a small popover for a label and stamps a point event at the current time.
The live window follows the server's clock, so the mark shows at once, even
when no metric has arrived for longer than the window. An event dated in the
future stays off the window until the server's time reaches it.
Its dropdown adds:

- **Start span…** / **End span** — opens the same label popover to open a
  span, then closes it (server clock) once you choose **End span**; only
  one span can be open at a time. The dashboard tracks the span it opened,
  and **End span** / **Stop** go inactive as soon as that span ends or is
  deleted, from any source: **End now** on its row, the editor, or another
  client.
- **Sweep span on chart** — arms a one-shot drag on the *next* chart you
  drag across: instead of zoom-selecting, that drag becomes the span's
  start and end and opens the full event editor pre-filled with it. Esc
  cancels the armed gesture before you drag.
- **Add event…** — opens the same editor with a blank draft, for typing
  exact times instead of dragging or marking "now".

The **Events** slide-over carries the same flows inline, so you never have
to leave it: a live session's compose row has **Mark**/**Start**/**Stop**
buttons; a review session (there's no "now" to stamp against) gets a
single **Add event…** button instead. On an editable session the
slide-over opens with focus on the compose row's first control (live: the
label field; review: **Add event…**). Each row also grows **Edit** and
**End now** controls once the session is editable — **End now** stamps an
open span's end without opening the editor. Both are absent entirely (not
just disabled) on a file loaded with no server behind it, since there's
nothing on a server for them to persist to, and on a [view-only
event](#sources-and-saving).

**The event editor**, opened from any of the flows above, has a **Label**,
second-granularity **Start**/**End** date-time fields (clearing **End**
turns a span back into a point event), a row of named color swatches
(**Grey**, the default, **Violet**, **Green** and **Red**; the automatic test
marks use Grey for a start, Green for a pass and Red for a failure), and a
**Dash** style select (`solid`, `dot`, `dash`, `longdash`, `dashdot`,
`longdashdot`) applied to the event's chart overlay — a point event's marker
line and the border drawn around a span. It opens with focus in **Label**,
so you can type straight away. Each swatch shows its name in a
tooltip when you hover it or reach it with the keyboard: Tab enters the row
on the selected swatch, the arrow keys move along it, and Space or Enter
picks one. The selected colour's name also stays beside the label, as in
**Color · Grey**. An event whose colour is not one of the four keeps it as
an extra swatch named for its value, such as **Custom — #00abcd**, and you
can switch to a preset and back to it.

**Custom…**, after the swatches (the next Tab stop), picks any other colour.
It opens a popover with a hue wheel around a saturation and brightness
square, and a **Hex** field for an exact value: six hexadecimal digits, with
or without the `#`. While the field holds anything else, it keeps what you
typed, says "Enter six hexadecimal digits, for example #00abcd", and
**Use colour** stays disabled; moving the wheel or the square replaces the
text with that colour's hex. Two previews, **Light chart** and **Dark
chart**, draw the event's marker, dash and span fill in the chosen
colour over each theme's chart background. **Use colour** puts the
colour in the editor, selecting a preset swatch if it matches one;
**Cancel**, Esc or a click outside discards it, and Esc closes only the
popover. Nothing is sent until you press **Save**.

Saving an edit sends only the fields you changed, so changing just the label
never rewrites the colour, the dates or anything else; a Save that changed
nothing just closes the editor. An event colour is always a hex value. An
event from an older capture whose colour is not hex (a name such as `red`) is
drawn in Grey. Its colour is kept as it was, in the archive and in an export,
until you pick a new one: the editor says so under the swatches, and its
**Use Grey** button picks Grey for you. A dash that is not one of the listed
styles is drawn as the default dash and kept; the editor shows it as custom
(for example **Custom — wavy**), and choosing any dash, the default included,
replaces it. **Save** stays disabled until **Start** is complete and **End**
is either complete or empty, and, when you add an event or change the label,
until the label is filled in; the reason shows under the field. Editing or
deleting an existing event reuses the same panel; **Delete** opens a dialog
that names the event ("Delete event?") and removes it only when you press
**Delete** in the dialog. **Cancel**, **Esc** or a click outside the dialog
closes just the dialog and leaves the editor open. Every change made here can
be taken back; see [Undo and redo](#undo-and-redo).

When you review a `.db` or `.json` source, an edit, **End now** or a delete
applies to the event as the server holds it now (the archive's row, or the
server's copy of the document), not as this tab loaded it. Because the
editor sends only the fields you changed, a field you did not change keeps
what another tab, or another `otto monitor` on the same `.db` archive,
saved meanwhile. If the event changes between the server reading it and writing
your change, nothing is written and the tab shows the event as it was saved.
The editor says "This event was changed elsewhere. It now shows the saved
version." and stays open on that version, so you can make your change again;
if the event was deleted meanwhile, the editor closes and a toast says so.
**End now** in the Events panel reports the refusal as a warning at the top
of the page.

**Crowded labels.** Event labels stack in up to three rows at the top of
each chart. Where more events crowd one spot, the earliest ones keep their
labels and the last visible label ends in a count such as **(+2)**. Click
that label, or Tab to it and press Enter, to list the hidden events; on an
editable session each one opens the event editor (events without an id
are listed only).

**During a test run.** Neither the `monitor` fixture (see [Monitoring from a
test](../../setup/test-recipes.md#monitoring-from-a-test)) nor the run-wide
`otto test --monitor` serves a dashboard. Events from `monitor.event(...)`, and
the automatic per-test start/pass/fail marks of `--monitor`, are recorded into
the archive; see them on the timeline by [reviewing the
capture](review.md#reviewing-a-capture).

## Undo and redo

The dashboard remembers the event changes you make in this tab, so a mistaken
mark, edit, end or delete can be taken back. It works on every editable
source: a live run, and a `.db` or `.json` review. A file loaded into a tab
with no server behind it takes no changes, so it has nothing to undo and the
commands are disabled there.

- **Shortcuts.** **Ctrl Z** undoes the latest change and **Ctrl Y** redoes
  it; on a Mac they are **⌘Z** and **⌘Y**, and **⌘⇧Z** (**Ctrl Shift Z**)
  redoes too. The command palette offers the same two commands, named for the
  change ("Undo delete “test fail”"), and disabled while there is nothing to
  undo.
- **Depth.** The last 10 changes can be undone, newest first, and redone in
  the other direction. The 11th change forgets the oldest. A new change after
  an undo discards what was left to redo.
- **The toast.** Every successful undo and redo shows a short toast such as
  **Undid: delete “test fail”**, with a button for the opposite step (**Redo**
  or **Undo**). It stays longer than an ordinary toast, waits while you hover
  or focus it, and is announced by screen readers, even while the Events panel
  is open. **F6** moves keyboard focus to the toast from anywhere, the Events
  panel included; Tab then reaches its button.
- **What is recorded.** Marks and spans started from the app bar or the Events
  panel, events added in the editor or by sweeping the chart, saving an edit,
  **End span** and **End now**, and deletes. Undoing a delete brings back the
  same event, with the same id, label, times, colour, dash and source; the
  exceptions are an event whose label is blank or whose end is not after its
  start, both possible only in an older capture: no change may store them, so
  undoing such an event's delete is refused and a toast says so, and the step
  is dropped. An undo of an edit puts back only the fields that edit sent; if
  anything else about the event changed meanwhile, the undo is skipped (next
  item). View actions (zoom, range, pause) are not part of the history, and
  neither is anything done by another tab, another client or a test run.
- **A conflict is skipped.** If the event was changed elsewhere since your
  change, undo will not overwrite it: a toast says "Can't undo: “test fail”
  changed elsewhere", the tab shows the event as it now stands, and that step
  is dropped, so the next press reaches the step before it. If the archive is
  busy, or the request fails, a toast says "Couldn't undo — try again" and
  the step is kept for you to retry.
- **One change at a time.** The tab sends one event change at a time. While
  one is in flight, the other event controls and the Undo and Redo commands
  are disabled, and their shortcuts do nothing. Nothing is queued: press again
  once the change finishes. The toast's own **Undo** or **Redo** button,
  pressed meanwhile, says "Wait for the current change to finish".
- **When it does not apply.** **Ctrl Z** inside a text field, date field or
  menu, and while the event editor, the mark popover, the palette or the
  delete dialog is open, keeps its ordinary meaning; the Events panel is not
  one of those, so undo works from it.
- **When it is forgotten.** The history lives in the tab only: each tab has
  its own, even on the same URL. A reload or a new tab starts empty. A change
  of source empties it too: in every tab on the URL when an Import switches
  a `.json` review, whichever tab made it (see [Switching
  source](#switching-source)), and in the tab itself when it loads a file
  with no server behind it.

## Live status, pause, and reconnect

While `--live`, a healthy session shows no connection chrome at all — the
app bar stays quiet as long as the stream is open and receiving fragments.
If the SSE connection drops, a slim amber **Reconnecting…** banner appears
directly under the app bar for as long as the retry-with-backoff loop is
unresolved, and disappears the moment the stream reconnects. The same banner
shows while reviewing a source the server serves (`otto monitor <source>`),
whose stream carries other tabs' changes; a file loaded into a tab with no
server behind it has no stream and never shows it. **Pause/Resume**
is the icon button in the app bar's right-hand cluster (its `aria-label`
reads "Pause" or "Resume" to match); review/historical context — including a
client-side Import with no backing server — is carried entirely by the
review bar's **HISTORICAL** badge, never by the app bar.

**Pause is a view control, not a data control.** Clicking **Pause** freezes
the visible time window; it does not stop ingestion — fragments keep
applying to the loaded session in the background, so clicking **Resume**
catches up immediately with no gap to backfill. "Paused" means live mode
with a pinned range, so manually picking a custom range (a chart drag-zoom,
for example) pauses too, and toggling pause from either one resumes
following the tail.

**Reconnect re-fetches; it never replays.** When the SSE connection drops,
the client backs off and retries, and immediately before reopening the
stream it re-fetches the whole `/api/monitor_sessions` payload rather than
replaying whatever fragments it missed while disconnected.

**A silent host dims.** Health (see [Health, scoped to the viewed
range](dashboard.md#web-dashboard)) is derived from the gap since a host's last
sample: a host goes **down** once that gap exceeds `HEALTH_K` (3) times its
collection cadence. In live mode that evaluation runs against a moving
"now" rather than a fixed range boundary, driven by a clock that ticks at
the collection interval.


# Web dashboard

In both modes, `otto monitor` serves a web server: it binds an OS-assigned
free port and logs the dashboard URL at startup (`Server running at
http://<ip>:<port>`, one URL per non-loopback interface).

On load, the dashboard shell asks that same server one question — `GET
/api/mode` — then, regardless of the answer, follows up with `GET
/api/monitor_sessions` and renders the result, exactly as if you'd used
Import yourself: no click needed. Live and review servers answer with the
same `format:1` shape — a live monitor session is one whose `end` is still
open, like a crashed session found on disk — so the topology map populates
immediately either way (as do the fleet grid and charts once you switch to
Hosts). In live mode, once that initial load succeeds the shell also opens
`GET /api/stream` (Server-Sent Events) and *grows* the loaded session in
place by appending each fragment as it arrives.
Served by a bare static file server with no `/api/*` routes at all, the
dashboard still works: any failure of that boot fetch — connection refused,
a non-JSON body, whatever a plain server hands back — falls back to the
same empty Import screen, never a broken page.

Feed it a monitor export document yourself at any time — drag a file onto
the window, or use the **⋯** overflow menu's *Import* — and it renders
that document entirely in the browser, exactly like a boot-fetched monitor
session:

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
- **Export.** The **⋯** menu re-downloads whatever document is currently
  loaded, unchanged.

Importing a file while connected to a server (live, or an editable review
served by `otto monitor`) disconnects from that server and shows the file
read-only: the stream stops and the marking controls go away. There is no
in-app way back; reload the page to return to the server's data.

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

Any **editable** session — a live run, or a review opened from a `.db`
session archive (see [Reviewing a capture](review.md#reviewing-a-capture)) —
can mark events directly from the dashboard; a `.json` export has no
marking controls at all.

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
open span's end without opening the editor. Both disappear entirely (not
just disabled) on a read-only source, since there's nothing on the server
for them to persist to.

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

Saving an edit sends the colour only if you picked one, so changing just the
label never rewrites it. An event
colour is always a hex value. An event from an older capture whose colour is
not hex (a name such as `red`) is drawn in Grey. Its colour is kept as it
was, in the archive and in an export, until you pick a new one: the editor
says so under the swatches, and its **Use Grey** button picks Grey for you.
The dash works the same way: saving sends it only if you choose one. A
dash that is not one of the listed styles is drawn as the default dash and
kept; the editor shows it as custom (for example **Custom — wavy**), and
choosing any dash, the default included, replaces it.
**Save** stays disabled until
**Start** is complete and **End** is either complete or empty; the reason
shows under the field. Editing or deleting an existing event reuses the
same panel; **Delete** opens a dialog that names the event ("Delete
event?") and removes it only when you press **Delete** in the dialog.
**Cancel**, **Esc** or a click outside the dialog closes just the dialog
and leaves the editor open. Every change made here can be taken back; see
[Undo and redo](#undo-and-redo).

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
session: a live run, or a review opened from a `.db` archive. A review opened
from a `.json` file has nothing to undo, and the commands are disabled there.

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
  same event, with the same id, label, times, colour, dash and source. An undo
  of an edit puts back only the fields that edit sent; if anything else about
  the event changed meanwhile, the undo is skipped (next item). View actions (zoom, range, pause) are not part of the history, and
  neither is anything done by another tab, another client or a test run.
- **A conflict is skipped.** If the event was changed elsewhere since your
  change, undo will not overwrite it: a toast says "Can't undo: “test fail”
  changed elsewhere" and that step is dropped, so the next press reaches the
  step before it. If the archive is busy, or the request fails, a toast says
  "Couldn't undo — try again" and the step is kept for you to retry.
- **One change at a time.** The tab sends one event change at a time. While
  one is in flight, the other event controls and the Undo and Redo commands
  are disabled, and their shortcuts do nothing. Nothing is queued: press again
  once the change finishes. The toast's own **Undo** or **Redo** button,
  pressed meanwhile, says "Wait for the current change to finish".
- **When it does not apply.** **Ctrl Z** inside a text field, date field or
  menu, and while the event editor, the mark popover, the palette or the
  delete dialog is open, keeps its ordinary meaning; the Events panel is not
  one of those, so undo works from it.
- **When it is forgotten.** The history lives in the tab only. A reload or a
  new tab starts empty, and so does importing or opening another file, or the
  tab becoming read-only.

## Live status, pause, and reconnect

While `--live`, a healthy session shows no connection chrome at all — the
app bar stays quiet as long as the stream is open and receiving fragments.
If the SSE connection drops, a slim amber **Reconnecting…** banner appears
directly under the app bar for as long as the retry-with-backoff loop is
unresolved, and disappears the moment the stream reconnects. **Pause/Resume**
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


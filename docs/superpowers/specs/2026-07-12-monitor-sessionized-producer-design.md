# Monitor Sessionized Producer (Plan 5a) — Design

**Date:** 2026-07-12 · **Status:** approved (brainstorm with Chris, this date)
**Parents:** UX spec `2026-07-05-monitor-untitled-ui-redesign-design.md` §12 (entry paths), §14 (backend contract); contract spec `2026-07-10-monitor-export-format-and-dummy-data-phase-design.md` §7 (backend catch-up — this plan implements it), §2/§3 (what a snapshot carries).

## Goal

Give the monitor a real data path: live runs become **sessions** persisted in a
sessionized SQLite archive, a **producer** turns them into `format: 1`
documents, and `otto monitor <source>` serves review mode with the document
**auto-loaded** into the shell. Closes the guide's "only fixtures are
importable" gap — real labs can finally review their runs in the new UI.

**Plan 5 decomposition (decided):** 5a = this spec (sessionization + producer +
CLI split + boot hydration). 5b = live mode proper (SSE into the session-shaped
store, Live●/disconnected/pause chrome, unreachable dimming, live perf items).
5c = marking (Mark-now, click/drag spans, edit-in-slide-over). 5b/5c get their
own specs; nothing here should block on them.

## Decisions (from brainstorm)

1. **Explicit live opt-in via a flag** (amended from UX §12's literal
   `otto monitor live` subcommand proposal — the section itself marked the
   CLI shape "to confirm"): `otto monitor --live` is the only
   hardware-touching path; `otto monitor <source>` is review mode; bare
   `otto monitor` prints usage naming both, exit 2. A flag avoids the
   subcommand-vs-filename token ambiguity (a file named `live`) and Typer's
   fragile callback+subcommand coexistence, while keeping the spec's intent:
   live is never accidental. `--live` plus a source is an error (mutually
   exclusive). Breaking, chosen knowingly.
2. **Legacy read support DROPPED** (chosen over read-forever and
   migrate-in-place): the flat pre-session SQLite schema and the flat
   `/api/export/json` JSON become unreadable. No synthesis, no migration
   code. Fail-loud errors name the break.
3. **Sessions are framed at the edges**: collector/store stay session-blind
   (one process run = exactly one live session); the CLI/server layer owns
   the frame and the lab snapshot; the DB layer owns multi-session
   persistence; the producer reads frame + store.

## CLI

One command, no subcommands (decided — see Decisions #1):

- `otto monitor --live [--hosts REGEX] [--interval/-i S] [--db PATH]
  [--label TEXT] [--note TEXT]` — reservation gate unchanged; `--label`/
  `--note` stamp the session (idiom precedent: coverage's `--ticket`/
  `--note`). Downstream split (decided): `label` is the session picker's
  display name (existing behavior); `note` renders as the picker entry's
  tooltip (`title` attribute — new in this plan) and travels in the stored
  document as the run's fuller context. `--db` optional as today (no persistence without it).
- `otto monitor <source>` — positional path, `.json` (format:1) or `.db`
  (schema v2). No gate, no collection loop, serves review mode.
- `otto monitor --live <source>` → error: mutually exclusive (one run is
  either collecting or reviewing, never both).
- Bare `otto monitor` → usage text naming both forms, exit code 2.
- The old `--file` option is removed (superseded by the positional).

## Modes and boot hydration

- **`GET /api/mode`** → `{"mode": "live" | "review", "source": <basename |
  null>}` — UX §14's launch-mode flag, minimal shape.
- The shell performs its **first boot fetch**: `GET /api/mode`, failing SOFT —
  unreachable/404/non-JSON → behave exactly as today (empty shell + Import).
  This keeps static serving (`python3 -m http.server` demos, docs capture)
  and the offline Playwright pin working unchanged.
- **Review mode:** shell fetches **`GET /api/document`** (the full format:1
  `MonitorExport`, every session in the source) and hydrates the review
  store through the SAME code path Import uses — same validation, same
  warnings surface, same session picker. UX §12 Path 2 semantics: the
  monitor IS the review tool; **no Exit control**. Import stays available
  and replaces the document, exactly as today.
- **Live mode (this phase):** `/api/document` → 404; the shell behaves as
  today (the existing `menu-export` exports the *loaded* document client-side
  and is not touched). The interim win: `GET /api/export/json` returns a
  real format:1 snapshot of the running session — fetch the URL, then
  Import the file; a live-mode export affordance in the chrome is 5b's
  work. Streaming is 5b.

## Sessionization

- **`src/otto/monitor/session.py` — `SessionFrame`**: `id` (UTC timestamp
  slug, e.g. `2026-07-12T14-30-05Z`, unique per DB), `label: str | None`,
  `note: str | None`, `start` (stamped at launch), `end` (stamped at clean
  shutdown; **crash leaves it null** and readers fall back to the last
  sample's timestamp — sessions stay readable after a kill).
- **Lab snapshot at launch** (CLI layer, not collector): hosts
  (element/board/slot/hop/ip/os fields) from the active lab config; elements
  via the existing derivation; links from `otto.link`'s foundation —
  **implicit-from-hop + declared only**, `impair` middlebox config included,
  dynamic/tunnel links excluded (contract §2/§3.2, unchanged).
- **SQLite schema v2** (`db.py`): `sessions(id, label, note, start, end,
  lab_json, meta_json)`; `session_id` column added to `metrics`, `events`,
  `log_events`; `metrics.source` added (contract §7). Deliberate
  simplification vs §7's sketch: no separate `configs(...)` table — the
  per-session `lab_json`/`meta_json` columns carry the snapshot inline,
  which is what the shell's client-side config-drift indicator already
  consumes from the document. A schema-version stamp identifies v2; opening
  anything else — legacy flat DBs included — **fails loud**, naming the
  format and the no-migration policy.
- **Multi-session archives:** reusing a v2 `--db` across runs appends a new
  session; prior sessions are never touched. The shell's session picker
  already handles multi-session documents.

## Producer

- **`src/otto/monitor/export.py`** (pure): `build_export(...) →
  MonitorExport`.
  - Live path: reshape `(SessionFrame, MetricCollector store contents)` —
    `MetricRecord`/`EventRecord`/`LogEventRecord` rows (shapes are already
    near-identical), `get_meta_model()` → `SessionMeta`, frame + snapshot →
    `SessionRecord`.
  - Review path: read every session from a v2 DB.
  - Format:1 `.json` sources skip the producer — validated against the
    `MonitorExport` schema, then served verbatim.
- **`/api/export/json` emits format:1** (contract §7's stated intent — a
  breaking change to that endpoint's payload); `/api/document` serves the
  same body. Validation through the existing pydantic models means the
  schema drift guards police the producer for free.

## Deletions (deliberate, breaking)

- `history.py` legacy serialization: `to_json`/`from_json`/`from_sqlite`/
  `load_*`, and flat no-session DB writes.
- `MetricCollector.from_json`/`from_sqlite` classmethods — review mode no
  longer builds a collector; it serves a document.
- `test_harness.py`'s legacy pins (old export round-trip, `--file` replay)
  are **rewritten, not deleted**: new pins for `/api/mode`, `/api/document`,
  and format:1 `/api/export/json`. The harness remains the wire-contract
  truth, one era newer.
- Untouched this phase: `/api/data`, `/api/stream`, event CRUD endpoints
  (5b/5c decide), and the kept legacy web data layer (`store.ts`,
  `api/client.ts`, `api/sse.ts` — fate rides 5b).
- Docs: `docs/guide/monitor.md`'s "no producer exists" paragraph replaced
  with real instructions (`otto monitor --live --db lab.db`, then
  `otto monitor lab.db`).

**Breaking-changes summary (for the changelog):** live collection now requires the explicit `--live` flag; bare `otto monitor` no
longer starts live; `--file` removed; legacy flat `.db`/`.json` captures
unreadable; `/api/export/json` payload is now format:1.

## Testing

- **Unit:** `SessionFrame` stamping incl. crash-tolerant null `end`;
  producer reshape against a live-populated store (golden `MonitorExport`
  comparison, schema-validated); v2 schema round-trip; multi-session
  append; refuse-legacy / refuse-unknown-version fail-loud paths; lab
  snapshot derivation (hop→implicit links, declared passthrough, dynamic
  exclusion, impair passthrough).
- **Harness (hostless):** pins for `/api/mode` (both modes), `/api/document`
  (review: full doc; live: 404), format:1 `/api/export/json`.
- **Playwright (dashboard lane):** review-mode boot over a two-session v2
  DB → shell boots hydrated (no Import), session picker shows both
  sessions, historical chrome; live-mode boot pins today's empty-shell
  behavior (mode fetch soft-fail covered by the existing offline spec).
- **CLI e2e:** bare usage (exit 2), `live` reservation-gated, source
  positional dispatch, `--label`/`--note` land in the session row.
- **Live-bed proof (final task):** `otto monitor --live --db` against the
  peer lab VMs — a few minutes of collection, clean shutdown, then
  `otto monitor <db>` and export: document parses, loads in the shell,
  sessions/lab/links real. Read-only collection, no VM power operations,
  light load.
- **Gates:** the established sweep — `make coverage-hostless`, nox lint+ty,
  `make web` + air-gap, vitest (the boot-fetch change touches web),
  `make dashboard`, import-budget, `make schema` if models move.

## Out of scope

Live streaming into the shell, live chrome/dimming, pause (5b). Marking UI
and session-aware event endpoints (5c). Dynamic-link overlay, NetEm, link
event timeline (gated on their own backends, per the contract spec). Legacy
data conversion tooling (revisit only if demand appears).

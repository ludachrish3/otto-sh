# Monitor event marking (Plan 5c) — final review follow-ups

Recorded from the 2026-07-18 final whole-branch review of Plan 5c (event
marking). None of these are merge-blocking.

1. **Literal/enum-typed dash + color in the exported schema.** `dash` and
   `color` currently round-trip as loose `string` on the wire
   (`web/src/api/export.gen.ts`'s generated `Dash1`/`Color1` types), so
   `web/src/shell/EventEditor.tsx` keeps its own hand-copied
   `DASH_STYLES`/`EVENT_COLOR_SWATCHES` tuples in sync with
   `otto/models/monitor.py`'s `VALID_DASH_STYLES` by hand rather than by the
   type system. Narrowing the pydantic model fields to a `Literal`/enum
   would let `scripts/gen_web_types.sh` emit a real union type and delete
   the hand-kept-in-sync comment in `EventEditor.tsx`.
2. **Shared UPDATE column tuple between `db.py` and `archive_edit.py`.**
   `EVENT_INSERT_SQL`/`event_insert_params()` are already shared (Task 4's
   dedup directive), but the two UPDATE statements (live's `MetricDB` vs the
   review archive editor) still each spell out their own column list. They
   differ in `WHERE` scoping (live trusts its bound frame; the archive
   editor scopes by `session_id`), but the column tuple itself could still
   be one shared constant the way the INSERT one is.
3. **Stale `openSpan` invalidation.** `web/src/ui/uiStore.ts`'s `openSpan`
   (the span the AppBar's "End span" targets) isn't invalidated if that
   event gets ended or deleted through another surface (the Events panel's
   per-row **End now**, or a `.db` archive edited out-of-band). Today this
   just produces a loud 409/404 warning on the next "End span" attempt
   rather than corrupting anything, but the stale reference itself is never
   cleared.
4. **`MarkControl` has no double-submit guard.** A fast double-click (or a
   double-tap on a touch device) on **Mark**/**Start** can fire two
   `createEvent` calls before the first response lands, creating two
   events from one click. Plan-consistent (the brief didn't call for one),
   but worth a debounce or disable-while-pending guard in a follow-up pass.
5. **Spec wording drift: "End-now appears in the editor."** The spec's
   §UI surfaces section describes **End now** as an editor affordance;
   Task 10 shipped it as a per-row button in the Events panel's list
   instead (`web/src/shell/EventsPanel.tsx`'s `event-endnow-<id>` control),
   never added to `EventEditor.tsx`. Reviewed and accepted as the shipped
   behavior — the spec sentence is simply stale and could use a wording
   fix in a docs-only follow-up.

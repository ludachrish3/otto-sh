# Event undo and redo in the monitor dashboard (#929)

**Status:** draft r4. It folds in the r1 builder check and three rounds of Astra and Fable design review. Fable called r3 sound to plan from, and r4 takes both reviews' remaining fixes. · **Issue:** #929 (P1, size L) · **Date:** 2026-11-07

## Intent

This is a quick "oops" for the user's own recent event changes in the monitor dashboard: a mark in the wrong place, a bad label edit, a span ended too early, a delete they regret.

Chris, 2026-11-07:
- It fixes up recent actions that turned out to be mistakes, seconds to minutes later.
- It works in the same tab, with no reload in between.
- The dashboard is mostly used by one person editing events. Undo covers only that tab's own actions, never anyone else's work.
- It is not a history, audit or collaboration feature.

## Goals

- **Depth:** up to 10 actions can be undone in reverse order and redone forward. The 11th action evicts the oldest.
- **Shortcuts:**
  - Ctrl+Z undoes and Ctrl+Y redoes. On macOS they are ⌘Z and ⌘Y, plus ⌘⇧Z for redo.
  - They work from the chart and the panels, the Events panel included.
  - They appear in the command palette as "Undo <action>" and "Redo <action>".
- **Redo is cleared** by a new action taken after an undo.
- **Toast:** every undo and redo shows a short toast, for example `Undid: delete “test fail” · Redo`. It is keyboard-reachable and announced to screen readers.
- **Undoing a delete restores the SAME event:** id, label, times, colour, dash and source.
- **Modes:** it works in live mode and in editable `.db` review. Read-only `.json` review has nothing to undo, and the commands are disabled there.
- **#958 follow-on:** once #958 (editable `.json` review) has its in-tab event helpers, undo works there with no change to this design.

## Non-goals

- History that survives a reload, crosses tabs, or is shared between users.
- Undoing what another tab, client or test run did.
- View actions: zoom, range, window presets, series visibility and Pause. There is one history stack, for events only.
- Merging conflicting changes.

## Anti-goals (failures even if it "works")

- An undo that overwrites a change made elsewhere.
- An inverse that reaches a different source after a file import (#873), or a different metric session than the one it was recorded in.
- A restored delete that comes back with a new id, or as `manual` when it was `auto` or `user_code`.
- Ctrl+Z eating the browser's text undo inside an input.
- A second toast component, shortcut table, gate store, write sequence or comparison rule. Also inverses hand-written per component (AGENTS.md, ONE DECISION, ONE PLACE).

## Recorded actions

| Action (UI) | Request | Recorded as |
|---|---|---|
| Mark now, Start span, Add event (editor Save of a draft), sweep | POST create | create: the created record (the response) |
| Editor Save of an existing event | PATCH: label, timestamp and end_timestamp, plus colour/dash only when touched (`styleChanges`) | update: the fields SENT, their values before, and the record after (the response) |
| End now / End span | POST `/end` | update: `end_timestamp` was `null`, plus the record after |
| Delete, after the #869 confirmation | DELETE | delete: the record the SERVER deleted (see §1) |

- **An update's inverse covers only the fields the request sent.** It never diffs two full records, which would also undo a field someone else changed in between.
- **"Before" is what the tab showed** when the user sent the request.
- **Redo re-applies the recorded values.** It never recomputes "now".
- **Snapshots are in wire form,** made through `storedEventStyle` (`web/src/data/eventStyle.ts`). A legacy colour or unknown dash is recorded as it is stored.

## Decided design

### 1. Server: one revert route, compare-then-write (Chris chose this over a tab-only check, 2026-11-07)

**The route:** `POST /api/session/{session_id}/event/{event_id}/revert`.
- The body is `{ expect: Record | null, write: Record | null }`.
- `Record` holds the six wire fields: label, timestamp, end_timestamp, color, dash and source.

| expect | write | Meaning | Used for |
|---|---|---|---|
| record | record | Put the event back to `write` | undo or redo of an update |
| record | `null` | Delete it | undo of a create, redo of a delete |
| `null` | record | Restore it under `event_id` | undo of a delete, redo of a create |

**One comparison rule.** A new `event_ops.same_event(a, b)` takes two `EventRecord`s and is the only "is this the expected state" test.
- `timestamp` and `end_timestamp` are compared as aware datetimes, never as text. The wire `Z`, the stored `+00:00` and the browser's `.000Z` all differ textually.
- `color` is compared after `EventRecord`'s lenient normalisation, and `label`, `dash` and `source` as strings.
- Live `MonitorEvent.to_dict()` and archive rows both validate into `EventRecord`, as `_event_response` already does.

**The update and delete legs reuse the existing write sequences.** No sibling sequence restates them.
- **Live:** they go through `collector.mutate_event`, the one change-or-delete entry point under `_mutation_lock()`, with a resolver.
  - The resolver compares `current` to `expect` with `same_event`, and raises `EventChanged` on a mismatch.
  - Otherwise it returns `write`, or `None` for a delete.
- **Review:** `archive_edit.revert_event` checks `expect` inside the same flocked connection, before the statement, and then runs the same row update or delete that `archive_edit.update_event` and `archive_edit.delete_event` use. The comparison is against the ACTUAL row, scoped by `session_id`.
  - Reading that row goes through one extracted `event_row_record(row) -> EventRecord` in `db.py`, beside `EVENT_INSERT_SQL`/`event_insert_params`. The events SELECT column list and the row-to-`EventRecord` conversion are spelled today in `db.py` (~:540) and `export.py` (~:275); both move onto it, and the new check uses it. That makes one conversion, not three.
- **Source on the update leg:** `mutate_event` keeps `current.source` (`replace(..., source=current.source)`). `source` is compared through `expect`, never written.

**Only restore adds a write path.**
- **Live:** `collector.restore_event`, under `_mutation_lock()`.
- **Review:** `archive_edit.restore_event`.
- Both go through one explicit-id insert statement and parameter builder in `db.py`, beside `EVENT_INSERT_SQL` / `event_insert_params`.
- **The live store side** reuses `MetricStore.note_imported_event`, which keeps the id and advances the counter.
- **High-water guard:** an id above it is refused as `changed`.
  - **Live mode** uses the store counter.
  - **`.db` review** uses SQLite's `sqlite_sequence` row for `events`. The tables are `AUTOINCREMENT`, so ids are never reused.
- **Fix the counter while here.** `MetricStore.add_event` advances `_next_event_id` by one even when the DB rowid is far ahead. It should advance past the assigned id, through the same helper `note_imported_event` uses.
  - This deliberately reverses the "id quirk preserved" contract: `store.py`'s docstring and `test_store.py::test_event_id_quirk_preserved` are rewritten to "the counter stays ahead of every assigned id".

**DELETE returns the deleted record.**
- The plain DELETE route answers 200 with the record it actually deleted, instead of 204. That record is captured under the lock or transaction:
  - `collector.delete_event` returns the `MonitorEvent` that `mutate_event` hands back. Today it returns a `bool`.
  - `archive_edit.delete_event` returns the row it removed, through `event_row_record`.
- The tab records THAT record. A stale copy in the tab can never be "restored" over a different event.

**Validation.**
- **Live mode** is strict, through `MonitorEvent` construction. Live events never hold legacy styles.
- **Review mode** is lenient, like `EventRecord`: a legacy colour or dash is written back unchanged (#857). That is what lets an edit that replaced `"red"` be undone.
- **`source`** is accepted only here. `EventCreateBody` still has no `source`.

**Responses.**
- **200:** written. The body is the resulting record, or `null` after a delete.
- **409 `{code: "changed", current: Record | null}`:** the comparison failed, the event is missing (never 404 on this route), or the id is above the high-water mark. `current` is the event as it is now, or `null` when it is absent.
- **409 `{code: "busy"}`:** `ArchiveLockedError`.
- **404 / 403:** the usual `_mutation_guard`.

**Propagation.**
- **Live:** the route is `@_observed`, like every live mutation, and publishes the matching added, updated or removed SSE change.
- **Review** has no SSE. The route patches the served document (`_patch_document_event` / `_drop_document_event`), and the tab applies the response.

**Docs:** list the route in `server.py`'s module docstring with the other event routes, and note the DELETE status change there.

### 2. Tab: one history store, one recording point

**The store.** `web/src/data/undoHistory.ts` is a zustand store, so the palette can subscribe.
- It holds two bounded stacks of 10.
- Each entry is `{ kind, sessionId, eventId, label, sent, before, after, trackedSpan }`.
- Its actions are record, undo, redo, clear and markTracked.

**Recording** is a parameter of the one shared sequence, `mutate()` in `web/src/data/eventApi.ts`.
- A successful ordinary mutation records its entry and clears the redo stack.
- `revertEvent`, the new helper for the route, runs through the same `mutate()` with recording off. On success it moves the entry between the stacks.

**Outcomes.** The request helper in `eventApi.ts`, which discards the status today, returns the status, the code and `current`.
- **`changed`:**
  - If `current` equals the entry's target state, an earlier attempt already committed and its response was lost.
    - The entry moves as if it had succeeded.
    - `current` is applied through the SAME success-application path a 200 uses, deletion included. Review mode has no SSE to repair the display otherwise.
  - The comparison is a TS mirror of `event_ops.same_event`. A mirror test holds the two equal, the way `tests/unit/models/test_browser_format_mirrors.py` holds `OBSERVED_AT_HEADER`. Text compare is unsafe because store records arrive with both `Z` and `+00:00` spellings.
  - Otherwise a toast says, for example, "Can't undo: “test fail” changed elsewhere", and the entry is dropped.
- **`busy`, or a network failure:** the entry is kept, and a toast says "Couldn't undo — try again".
- **The toast is undo's own surface,** since undo has no control to attach an error to. Ordinary mutation failures still go to the warnings banner through `reportMutationFailure`.

**The server is the only conflict check.** The tab never pre-checks against its own store.

**#958 hook.** Undo touches data only through `eventApi`'s helpers. #958's in-tab backend implements `revertEvent` as well.

### 3. One event mutation at a time per tab

- **One rule:** `useMutationGate.run` refuses when ANY event mutation is pending.
  - That means ordinary gates (editor Save and Delete, mark, End now, compose) and the `history` key alike.
  - A new `anyMutationPending()` over the existing gate store decides it. No second flag.
- **Every gate's exposed `pending` reads that same predicate**, so buttons disable with it. Only the spinner (`pendingAction`) stays per-key.
- **Consequences:**
  - The server applies mutations in the order the tab records them, so the stacks can't be recorded out of order.
  - A new action can't finish while an undo's stack move is pending.
  - An editor opened during an undo sees `pending` at its opening. #932's existing inherited-request rule then reseeds it from the store when the undo settles, and closes it if the event is gone. No new editor logic is needed.
- **The cost:** a Mark now pressed while a Save is in flight is refused rather than run concurrently. That's acceptable for a single-user dashboard.
- **Contracts this deliberately reverses,** rewritten rather than treated as regressions:
  - `useMutationGate.ts` (~:2 and ~:22-25, "one in-flight mutation per gate key"; "never makes another event's editor look busy");
  - `EventEditor.tsx`'s gate comment (~:112);
  - `eventeditor.test.tsx` "a Save still pending for one event does not make another event's editor busy (#932)", and the colour-while-pending test after it;
  - `mutationgate.test.ts` "another key is not shut by it".

  The tests become "any pending mutation shuts every gate's `pending`; `pendingAction` stays per key".
- **Unchanged:** #932's per-opening settlement (`settleOnOpening`) and its inherited reseed (`inherited: fresh && gate.pending`), which now reads the global predicate. That is what the editor consequence above relies on.
  - A draft editor opened during an unrelated pending mutation reseeds from its own draft when that mutation settles. This is harmless, because its fields are disabled meanwhile.
- **Loading indicators read per-key `pendingAction`, never `pending`.** Example: `EventsPanel.tsx` (~:182) uses `pending` for its End now spinner today, and would otherwise spin on every row.

### 4. Late echoes never roll the store back (live mode)

- Today `fragment.ts` replaces an event with whatever record arrives last, so a delayed SSE echo can undo a newer HTTP response in the tab's view. With undo, that would visibly un-revert an event and drop the tracked-span re-adoption.
- **The rule:** the shared event-application path (`applyFragment`) keeps a stamp map per session, keyed by event id, holding the server stamp of the last change applied.
  - **Stamps:** `observed_at`, strictly increasing via `event_ops.utc_now`. `_publish` stamps every SSE fragment, and `_observed` stamps every live response header. The comparator is the existing `observedAtMicros` (`exportDoc.ts`).
  - **An older change is ignored,** in either direction: a removal older than a restore, and a create echo older than the delete that undid it.
  - **Deleted ids keep their stamp,** as tombstones. That's bounded, because edits happen at human rate.
  - **A snapshot (bootstrap or resync) seeds every id it carries,** and sets the session's floor, from that session's `observed_at` (`SessionRecord.observed_at`). Any later change older than the snapshot is ignored, including one for an id the snapshot doesn't have. That covers a delayed HTTP response arriving after a resync showed the event deleted.
  - **One mutation's own echo:** the HTTP header stamp is read after the publish stamp. The echo of a mutation is therefore always older than its own response, and is dropped. Its content is identical, so nothing is lost.
- One rule serves every mutation, not just undo. It extends the snapshot-vs-fragment ordering #868 already does with `observedAtMicros`.
- Review mode has no SSE, so the rule is inert there.

### 5. Shortcuts, focus and overlays

**Bindings.**
- Add `undo` (`mod+z`) and `redo` (`mod+y`, with `mod+shift+z` as a second binding) to the single binding table, `web/src/ui/shortcuts.ts`, and to the registry, `commands.ts`.
- `Binding` gains `shift` and allows a second binding per command. Today `matchesBindingFor` rejects shift on mod chords.

**When undo and redo are inert** (the delete confirmation lives only inside the event editor, so the editor's state covers it; `ConfirmDeleteEvent` has no other caller):
- **Text editing**, decided from the DOM:
  - input, textarea, select, and contenteditable (any value but `false`);
  - `[role="spinbutton"]`, which covers date segments;
  - `[role="menu"]` and `[role="listbox"]`.
- **An editing overlay is open**, decided from state, never from DOM ancestry:
  - the event editor (`uiStore.eventEditor`);
  - the mark popover;
  - the palette.
- **The Events panel is not an editing overlay.** It is where users end and delete events, so undo stays live inside it. Its DOM `role="dialog"` must therefore not suppress.

**One predicate.** Extend the predicate in `shortcuts.ts` that `shouldSuppressSlash` now uses. Its overlay inputs come from state.
- "/" keeps its current behaviour.
- A command opts in through a per-command flag. The command loop never special-cases undo ids.

**Palette labels** are dynamic: "Undo delete “test fail”", or a disabled "Undo" when there's nothing to undo, the tab is read-only, or a mutation is pending.

### 6. Lifetime

- The history lives only in the tab's memory. A reload or a new tab starts empty.
- **A source switch clears both stacks.** `detachServerSource` (`web/src/data/source.ts`) calls the clear directly, beside the resets it already makes.
- The tab dropping to read-only clears them too.
- Each entry carries its `sessionId`. Selecting another session in a `.db` review keeps the history, and an undo addresses the entry's own session.

### 7. The tracked span (#868), live mode

- **Two policies, stated as such:**
  - **An explicit Start span REPLACES** the tracked span, as today: `startSpan` (`web/src/shell/marking.ts`) keeps `setOpenSpan`. Start span is offered even while a span is tracked (`commands.ts`, `MarkControl.tsx`), and End span then ends the newest.
  - **A history re-adoption YIELDS.** `data/openSpan.ts` gains `adoptOpenSpan(sessionId, eventId)`, which tracks the span only when none is tracked AND the stored event is present and still open. It uses the same "present and open" predicate as the existing reconciler, shared and not restated.
- **Recording.**
  - A Start span's create passes `track: true` through `createEvent`'s options into the shared recording, so its entry records `trackedSpan: true`. Mark now sends the same request and records `false`.
  - Every update and delete captures whether its event was the tracked span, in the shared recording sequence, before the request applies. That includes an editor Save that sets an end, which clears tracking today.
- **After an undo or redo** that leaves a `trackedSpan` event open, the history calls `adoptOpenSpan`. It never replaces a newer tracked span.
- §4's ordering rule stops a late echo of the original End from clearing the re-adopted span.

### 8. One toast for the whole web app

- **Promote covapp's toast** (`web/src/covapp/Toast.tsx`, one slot that a new message replaces) to `web/src/ui/Toast.tsx`. Back it with a small zustand store and a `showToast(message, action?)`, so the history and `command.run` can raise it. covapp moves onto it.
- **New behaviour:**
  - an optional, keyboard-reachable action button (`Redo` / `Undo`);
  - the auto-dismiss timer pauses on hover or focus, and runs longer when there's an action.
- **Unchanged:** `role="status"` stays, and the vendored Untitled UI tokens provide the styling. `web/src/components/**` is not hand-edited.
- **Docs:** `web/README.md` records the widened `ui/**` set.

## Calls I made (Chris to check)

1. **Conflicts:** a conflict is skipped with a toast and dropped, never merged. A busy archive keeps the entry for a retry.
2. **Updates:** an update's inverse covers only the fields the request sent. "Before" is what the tab showed when the user sent it.
3. **One mutation at a time per tab:** a second action while one is in flight is refused, not queued.
4. **Overlays:** undo is inert while the event editor, mark popover, palette or delete confirmation is open, and live in the Events panel.
5. **DELETE returns the deleted record (200)** instead of 204. The response shape changes, and no external consumer is known.
6. **The store's id counter is fixed** to stay ahead of DB rowids. This reverses a deliberately preserved quirk.
7. **Late echoes are ignored** per event, by server stamp. This fixes a pre-existing display rollback for every mutation.
8. **Toast:** covapp's toast is promoted rather than built anew, and covapp gains pause-on-focus.
9. **The tracked span:** an explicit Start span keeps replacing the tracked span, as today. Undo's re-adoption yields to any span already tracked, and to an event that has since ended or vanished.
10. **Delete dialog copy:** "This can't be undone." is reworded.

## Left to the builder

- The exact entry type, the file layout inside `web/src/data/`, the toast copy for each kind of action, and the toast timings within the rule above.
- **Copy:** `ConfirmDeleteEvent`'s "This can't be undone." changes, because a delete can now be undone. A test asserts the dialog no longer calls deletion irreversible.
- **Tests,** covering at least:
  - each action type's undo and redo, in live mode and in `.db` review;
  - the 10-entry bound, and redo cleared by a new action;
  - `changed` from a remote edit (dropped);
  - a committed revert whose response is lost, then retried (it moves, it isn't dropped);
  - `busy` (kept);
  - a stale-tab delete then restore, which restores the SERVER's record, in both modes;
  - a reused `--db` whose rowids run ahead of the counter;
  - a legacy colour round-tripping through undo in `.db` review;
  - timestamps in `Z`, `+00:00` and `.000Z` forms comparing equal;
  - one-mutation-at-a-time refusal;
  - an editor opened during an undo, which reseeds or closes;
  - a delayed echo ignored, in both directions, and a stale HTTP response arriving after a resync;
  - a lost-response recovery, which refreshes the displayed event as well as moving the stack;
  - a newer remote end or delete before the undo's response, after which the span is NOT re-adopted;
  - Start span, then an editor Save that ends it, then Undo, after which End span targets it again;
  - Start span → Undo → Redo, after which End span targets the restored event;
  - the focus and overlay rules: date segment, menu, delete confirmation, and the Events panel staying live;
  - a source switch clearing the history.
- **Docs:**
  - an undo section in `docs/cli/monitor/dashboard.md`;
  - the route, and the DELETE change, in `server.py`'s module docstring.

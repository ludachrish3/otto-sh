// web/src/data/sourceApi.ts
// The served source as the server names it: the headers that carry its
// token, its server stamps and its switch confirmation, the check of its
// state (`/api/mode`), and the acknowledgement of an export.
// src/otto/monitor/server.py declares the same header names, and
// tests/unit/models/test_browser_format_mirrors.py holds each pair equal.
import type { MonitorSourceState, SourceSavedBody } from "../api/export.gen";
import { errorMessage, networkErrorMessage } from "./eventApiError";

/** The source's token: answered on every `/api/monitor_sessions`, sent on
 * every event write. */
export const SOURCE_TOKEN_HEADER = "X-Otto-Source";

/** The server's clock: the stamp on every mutation response, and the
 * capture stamp of every `/api/monitor_sessions` (data/snapshot.ts). */
export const OBSERVED_AT_HEADER = "X-Otto-Observed-At";

/** A switch confirmation's unsaved count (decimal). */
export const UNSAVED_HEADER = "X-Otto-Unsaved";

/** An uploaded source's file name (percent-encoded UTF-8). */
export const SOURCE_NAME_HEADER = "X-Otto-Source-Name";

const KINDS: readonly unknown[] = ["live", "db", "json"];

const isCount = (v: unknown): boolean => v === undefined || (Number.isInteger(v) && Number(v) >= 0);

/** Whether `v` is a source state as `/api/mode` answers it. */
export function isSourceState(v: unknown): v is MonitorSourceState {
  if (typeof v !== "object" || v === null) return false;
  const rec = v as Record<string, unknown>;
  return (
    (rec["mode"] === "live" || rec["mode"] === "review") &&
    KINDS.includes(rec["kind"]) &&
    (typeof rec["source"] === "string" || rec["source"] === null) &&
    typeof rec["token"] === "string" &&
    typeof rec["editable"] === "boolean" &&
    isCount(rec["unsaved"]) &&
    isCount(rec["revision"]) &&
    typeof rec["observed_at"] === "string"
  );
}

/** The source a `source_changed` refusal's body names, or null when the body
 * is no such refusal or its state does not read. */
export function refusedSourceState(body: unknown): MonitorSourceState | null {
  if (typeof body !== "object" || body === null) return null;
  const rec = body as Record<string, unknown>;
  return rec["code"] === "source_changed" && isSourceState(rec["source_state"])
    ? rec["source_state"]
    : null;
}

/** How a switch request ended:
 *  - `ok`: the server serves the upload now, as `state`;
 *  - `changed`: refused as `source_changed`, the confirmation naming a
 *    source (or a count) the server no longer serves; `state` is the one it
 *    serves;
 *  - `error`: any other refusal (403, 413, 422), or no answer at all. */
export type UploadResult =
  | { kind: "ok"; state: MonitorSourceState }
  | { kind: "changed"; state: MonitorSourceState }
  | { kind: "error"; message: string };

/** Asks the server to serve `text` as the `.json` source `name`, confirming
 * the switch against the source as the prompt showed it: its token and its
 * unsaved count, which the server compares exactly. Never rejects. */
export async function uploadSource(
  text: string,
  name: string,
  shown: { token: string; unsaved: number },
): Promise<UploadResult> {
  let res: Response;
  try {
    res = await fetch("/api/source", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        [SOURCE_TOKEN_HEADER]: shown.token,
        [UNSAVED_HEADER]: String(shown.unsaved),
        [SOURCE_NAME_HEADER]: encodeURIComponent(name),
      },
      body: text,
    });
  } catch (err) {
    return { kind: "error", message: networkErrorMessage(err) };
  }
  let body: unknown = null;
  try {
    body = await res.json();
  } catch {
    // an unreadable body: judged by the status alone
  }
  if (res.ok) {
    return isSourceState(body)
      ? { kind: "ok", state: body }
      : { kind: "error", message: "the server's answer names no source" };
  }
  const state = refusedSourceState(body);
  if (state !== null) return { kind: "changed", state };
  return { kind: "error", message: errorMessage(body, res.status) };
}

/** Tells the server an export holds every write of source `body.token` up
 * to `body.revision`, which lowers its unsaved count; the source frame the
 * server then publishes carries the new count to every tab. Best effort:
 * every failure is swallowed, a refusal for a source the server no longer
 * serves (409 `source_changed`) included, since the download has happened
 * and a missed acknowledgement only leaves the marker up. No warning and no
 * store change. */
export async function acknowledgeExport(body: SourceSavedBody): Promise<void> {
  try {
    await fetch("/api/source/saved", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  } catch {
    // swallowed: see above
  }
}

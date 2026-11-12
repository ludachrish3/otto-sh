// The ONE fetch of `/api/mode` and the ONE fetch of `/api/monitor_sessions`
// (data/source.ts's syncFromServer and admitSource both read through here).
//
// Soft-fail contract (binding): the built dist/ is also served by dumb
// static file servers with no /api/* routes at all (the docs-capture
// script, ad-hoc demo serving), and the offline Playwright pin blocks every
// non-local request outright. Both depend on the shell staying exactly as it
// is without a server whenever a fetch cannot succeed. So ANY transport
// failure — a rejected fetch, a non-200 response, a body that is not
// JSON/text, a mode body that is not a source state — answers null, and
// nothing is written. A 200 snapshot body is returned as text unparsed: ITS
// validation failures are not swallowed, they surface through the store's
// `importError`, the same as a bad file chosen through Import.
//
// Both also answer null once `gen` is no longer the current source (a file
// was imported, or the tab moved onto another source meanwhile), checked
// after every await. A caller still re-checks in its own continuation: a
// helper's check does not cover the step after it.
import type { MonitorSourceState } from "../api/export.gen";
import { isSourceState, OBSERVED_AT_HEADER, SOURCE_TOKEN_HEADER } from "./sourceApi";
import { isCurrentSource } from "./sourceGeneration";

/** A snapshot body with the token of the source it was read from and the
 * server's capture stamp (null when the server sent none). */
export interface Snapshot {
  text: string;
  token: string | null;
  captured: string | null;
}

/** The served source's state, or null (see the soft-fail contract above). */
export async function fetchMode(gen: number): Promise<MonitorSourceState | null> {
  let res: Response;
  try {
    res = await fetch("/api/mode");
  } catch {
    return null;
  }
  if (!res.ok || !isCurrentSource(gen)) return null;
  let body: unknown;
  try {
    body = await res.json();
  } catch {
    return null;
  }
  return isSourceState(body) && isCurrentSource(gen) ? body : null;
}

/** The served document, or null (see the soft-fail contract above). */
export async function fetchSnapshot(gen: number): Promise<Snapshot | null> {
  let res: Response;
  try {
    res = await fetch("/api/monitor_sessions");
  } catch {
    return null;
  }
  if (!res.ok || !isCurrentSource(gen)) return null;
  let text: string;
  try {
    text = await res.text();
  } catch {
    return null;
  }
  if (!isCurrentSource(gen)) return null;
  return {
    text,
    token: res.headers.get(SOURCE_TOKEN_HEADER),
    captured: res.headers.get(OBSERVED_AT_HEADER),
  };
}

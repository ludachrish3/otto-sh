// The shell's ONLY boot fetch. On mount, ask a same-origin otto monitor
// server whether it's running in review mode with a document already
// loaded (`otto monitor <source>`, the positional review path — a saved
// .json or .db export; note `--db` is a LIVE-mode flag, not this) and, if
// so, hydrate the review store exactly as the Import front door would.
//
// Soft-fail contract (binding): the built dist/ is also served by dumb
// static file servers with no /api/* routes at all (the docs-capture
// script, ad-hoc demo serving), and the offline Playwright pin blocks
// every non-local request outright. Both depend on this module leaving the
// shell exactly as it behaves without it whenever the fetch can't succeed.
// So ANY transport failure — a rejected fetch, a non-200 response, a body
// that isn't JSON, or JSON that isn't shaped like the mode payload — is
// swallowed and returns silently, before `importText` is ever called.
// `importText` only ever sees a 200 `/api/document` body; ITS validation
// failures are not swallowed — they surface through the store's existing
// `importError`, the same as a bad file chosen through Import.
import { useReviewStore } from "./reviewStore";

interface ModePayload {
  mode: "live" | "review";
  source: string | null;
}

function isModePayload(value: unknown): value is ModePayload {
  if (typeof value !== "object" || value === null) return false;
  const rec = value as Record<string, unknown>;
  return (
    (rec.mode === "live" || rec.mode === "review") &&
    (typeof rec.source === "string" || rec.source === null)
  );
}

export async function bootstrapFromServer(): Promise<void> {
  let modeRes: Response;
  try {
    modeRes = await fetch("/api/mode");
  } catch {
    return;
  }
  if (!modeRes.ok) return;
  let modeBody: unknown;
  try {
    modeBody = await modeRes.json();
  } catch {
    return;
  }
  if (!isModePayload(modeBody) || modeBody.mode !== "review") return;

  let docRes: Response;
  try {
    docRes = await fetch("/api/document");
  } catch {
    return;
  }
  if (!docRes.ok) return;
  let bodyText: string;
  try {
    bodyText = await docRes.text();
  } catch {
    return;
  }
  useReviewStore.getState().actions.importText(bodyText, modeBody.source ?? "server");
}

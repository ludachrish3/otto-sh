// Read-side companion to focus.tsx's hash-query writers: a hook that
// re-renders on `hashchange` (which both `window.location.hash = ...` and
// focus.tsx's synthetic dispatch fire) plus pure helpers over a hash string.
// FilePage derives `?lines=`/`?q=` from this instead of parsing once per
// chunk load, so a navigation that changes only the query on the SAME file
// (the palette's Ctrl+Enter stepping) still moves the highlight.
import { useSyncExternalStore } from "react";

function subscribe(onChange: () => void): () => void {
  window.addEventListener("hashchange", onChange);
  return () => window.removeEventListener("hashchange", onChange);
}

function snapshot(): string {
  return window.location.hash;
}

export function useHash(): string {
  return useSyncExternalStore(subscribe, snapshot, snapshot);
}

function stripHashMark(hash: string): string {
  return hash.startsWith("#") ? hash.slice(1) : hash;
}

export function hashPathOf(hash: string): string {
  const raw = stripHashMark(hash);
  const at = raw.indexOf("?");
  return at === -1 ? raw : raw.slice(0, at);
}

export function hashQueryOf(hash: string): URLSearchParams {
  const raw = stripHashMark(hash);
  const at = raw.indexOf("?");
  return new URLSearchParams(at === -1 ? "" : raw.slice(at + 1));
}

const COVERAGE_PREFIX = "/coverage/";

/** Tree segments for a `#/coverage/...` path; `[]` at the root; `null` for
 * any other route or a segment that does not decode. */
export function coverageSegmentsOf(path: string): string[] | null {
  if (path === "/coverage") return [];
  if (!path.startsWith(COVERAGE_PREFIX)) return null;
  try {
    return path.slice(COVERAGE_PREFIX.length).split("/").filter(Boolean).map(decodeURIComponent);
  } catch {
    return null;
  }
}

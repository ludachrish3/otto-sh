// Boot layer for the covapp classic-script data lane (see
// docs/superpowers/plans/2026-07-25-coverage-spa.md, Task 2). Two classic
// scripts feed this module: covapp.html loads `cov_data/index.js`
// (`window.__OTTO_COV__ = {...}`) BEFORE the app bundle so getIndex()/
// dataGuard() never race a script tag; per-file chunks arrive later, on
// navigation, via a `<script src="./cov_data/files/<chunk>.js">` this
// module injects itself — the chunk calls `window.__OTTO_COV_FILE__({...})`
// once it executes, which resolves whichever loadFileChunk() call is
// waiting on that chunk id.

import type { FileChunk, IndexPayload } from "./types";
import { EXPECTED_DATA_FORMAT } from "./types";

export type DataGuardResult = "ok" | "missing" | "format";

/** Thrown by `loadFileChunk` when an arriving chunk's `stamp` doesn't match
 * the index's — the report on disk changed (regenerated) since the index
 * loaded, so this chunk cannot be trusted alongside it. */
export class StampMismatchError extends Error {
  constructor(chunk: string) {
    super(`coverage chunk "${chunk}" stamp does not match the loaded report index`);
    this.name = "StampMismatchError";
  }
}

// Minimal structural check, not exhaustive validation: enough to rule out
// "the script never ran" / "something unrelated assigned window.__OTTO_COV__"
// without hard-coding every key (tier_order is legitimately [] for a
// data-less store — Global Constraints — so no key is assumed non-empty).
function isIndexPayloadShape(value: unknown): value is IndexPayload {
  if (typeof value !== "object" || value === null) return false;
  const rec = value as Record<string, unknown>;
  return (
    typeof rec.format === "number" &&
    typeof rec.stamp === "string" &&
    Array.isArray(rec.tier_order) &&
    Array.isArray(rec.runs) &&
    typeof rec.tree === "object" &&
    rec.tree !== null
  );
}

/** The validated `window.__OTTO_COV__` payload, or `null` if it's absent or
 * doesn't look like an IndexPayload at all (format mismatches still return
 * the payload here — that's `dataGuard()`'s job, not getIndex()'s). */
export function getIndex(): IndexPayload | null {
  const raw = window.__OTTO_COV__;
  return isIndexPayloadShape(raw) ? raw : null;
}

/** `"missing"` when the index script never ran (or produced something
 * unusable); `"format"` when it ran but its `format` doesn't match this
 * build's `EXPECTED_DATA_FORMAT`; `"ok"` otherwise. App.tsx renders
 * GuardScreen instead of the router whenever this isn't `"ok"`. */
export function dataGuard(): DataGuardResult {
  const index = getIndex();
  if (index === null) return "missing";
  if (index.format !== EXPECTED_DATA_FORMAT) return "format";
  return "ok";
}

interface Waiter {
  resolve: (chunk: FileChunk) => void;
  reject: (err: Error) => void;
}

// Module-level mutable state (test seam: _resetForTests below).
let pending = new Map<string, Waiter[]>();
let cache = new Map<string, FileChunk>();
let callbackRegistered = false;

function registerCallback(): void {
  if (callbackRegistered) return;
  callbackRegistered = true;
  window.__OTTO_COV_FILE__ = (chunk: FileChunk) => {
    // Chunks self-identify via their own `chunk` field — look up the
    // waiters by THAT, not by whatever loadFileChunk() call happens to be
    // most recent, so concurrent loads of different chunks stay correct.
    const waiters = pending.get(chunk.chunk);
    if (!waiters) return; // stray/late callback nobody is waiting on — drop it
    pending.delete(chunk.chunk);

    const index = getIndex();
    if (index === null || chunk.stamp !== index.stamp) {
      const err = new StampMismatchError(chunk.chunk);
      for (const waiter of waiters) waiter.reject(err);
      return;
    }

    cache.set(chunk.chunk, chunk);
    for (const waiter of waiters) waiter.resolve(chunk);
  };
}
registerCallback();

/** Load one file's chunk, injecting its classic `<script>` on first request,
 * resolving via the `window.__OTTO_COV_FILE__` callback the chunk itself
 * invokes once it executes. Cached per chunk id; concurrent calls for the
 * same not-yet-cached chunk share one script injection and all resolve/
 * reject together. Rejects with `StampMismatchError` if the arriving
 * chunk's stamp doesn't match the loaded index's, or a plain `Error` if the
 * script itself fails to load. */
export function loadFileChunk(chunk: string): Promise<FileChunk> {
  const cached = cache.get(chunk);
  if (cached) return Promise.resolve(cached);

  return new Promise<FileChunk>((resolve, reject) => {
    const waiters = pending.get(chunk);
    if (waiters) {
      // Already in flight — join the existing wait, don't inject a second script.
      waiters.push({ resolve, reject });
      return;
    }
    pending.set(chunk, [{ resolve, reject }]);

    const script = document.createElement("script");
    script.src = `./cov_data/files/${encodeURIComponent(chunk)}.js`;
    script.onerror = () => {
      const list = pending.get(chunk) ?? [];
      pending.delete(chunk);
      const err = new Error(`failed to load coverage chunk "${chunk}"`);
      for (const waiter of list) waiter.reject(err);
    };
    document.head.appendChild(script);
  });
}

/** Test seam: clear pending waiters + the resolved-chunk cache between
 * tests. Does not un-register the `window.__OTTO_COV_FILE__` callback —
 * that registration is idempotent and harmless to leave in place. */
export function _resetForTests(): void {
  pending = new Map();
  cache = new Map();
}

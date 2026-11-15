// A `/api/mode` answer (MonitorSourceState) and a `/api/monitor_sessions`
// answer for tests that stub the server, the fetch stub that serves them
// (`stubServer`), and what a stubbed fetch received (`lastRequestBody`).

import { vi } from "vitest";

import type { MonitorSourceState } from "../api/export.gen";
import { OBSERVED_AT_HEADER, SOURCE_TOKEN_HEADER } from "../data/sourceApi";

/** A live server's source state; `overrides` picks the mode, source and the rest. */
export function sourceState(overrides: Partial<MonitorSourceState> = {}): MonitorSourceState {
  const mode = overrides.mode ?? "live";
  return {
    mode,
    kind: mode === "live" ? "live" : "db",
    source: null,
    token: "test-token",
    editable: true,
    unsaved: 0,
    revision: 0,
    observed_at: "2026-07-18T12:00:00+00:00",
    ...overrides,
  };
}

/** A snapshot response as the server answers it: the body, the token of the
 * source it was read from (`sourceState`'s by default) and, when given, the
 * capture stamp. */
export function snapshotResponse(
  text: string,
  token = "test-token",
  captured: string | null = null,
): Response {
  const headers: Record<string, string> = { [SOURCE_TOKEN_HEADER]: token };
  if (captured !== null) headers[OBSERVED_AT_HEADER] = captured;
  return new Response(text, { status: 200, headers });
}

/** A server stamp `n` microseconds into the test's second: an ordered
 * sequence of `observed_at` values. */
export function stamp(n: number): string {
  return `2026-07-18T12:00:00.${String(n).padStart(6, "0")}+00:00`;
}

/** Fakes the timers the stream schedules, its one-frame flush and its
 * reconnect backoff (data/stream.ts), so a test steps them explicitly and
 * never sleeps. The caller restores real timers (`vi.useRealTimers`). */
export function fakeStreamTimers(): void {
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
}

/** Runs the stream's next scheduled timer under `fakeStreamTimers`: the
 * one-frame flush of the frames emitted since the last flush, or, with
 * nothing buffered, the reconnect a dropped connection waits for. */
export async function runStreamTimer(): Promise<void> {
  await vi.advanceTimersToNextTimerAsync();
}

/** A JSON response as the server answers it. */
export function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

/** The JSON body of the last request the stubbed fetch received. */
export function lastRequestBody(): unknown {
  const calls = vi.mocked(fetch).mock.calls;
  const init = calls.at(-1)?.[1] as RequestInit;
  return JSON.parse(init.body as string);
}

/** One route of `stubServer`: answers a fetch of its URL, given its init. */
export type Route = (init?: RequestInit) => Promise<Response>;

/** Stubs fetch with a route table, keyed by URL; a route may be replaced
 * mid-test, and a fetch of any other URL throws. `onFetch` sees each URL
 * fetched, in order. Returns the fetch mock. */
export function stubServer(routes: Record<string, Route>, onFetch?: (url: string) => void) {
  const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    onFetch?.(url);
    const route = routes[url];
    if (!route) throw new Error(`unexpected fetch ${url}`);
    return route(init);
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

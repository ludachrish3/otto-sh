// TypeScript side of the per-ticket Python<->TypeScript contract.
//
// The data-format version, `types.ts`'s ticket interfaces, `TicketsPage`'s
// sentinel-id literals and the `window` chunk-callback names are all
// hand-maintained mirrors of what
// `src/otto/coverage/renderer/spa_data.py` emits, with no compiler tying
// the two languages together. Both sides assert against one shared table,
// `tests/_fixtures/covapp_ticket_contract.json`; the Python half is
// `tests/unit/cov/test_covapp_ticket_contract.py`, which reads its keys off
// real emitted payloads. A drift on either side fails exactly one suite and
// names the language that moved. Same shape as the `formatOutage` fixture
// parity precedent in `src/data/time.test.ts`.
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import { SENTINEL_TICKET_IDS } from "./pages/TicketsPage";
import type { TicketChunk, TicketSummary, TicketTotals } from "./types";
import { EXPECTED_DATA_FORMAT } from "./types";

const here = dirname(fileURLToPath(import.meta.url));
const contract = JSON.parse(
  readFileSync(join(here, "../../../tests/_fixtures/covapp_ticket_contract.json"), "utf-8"),
) as {
  data_format: number;
  sentinel_ticket_ids: string[];
  /** Spelled out rather than `Record<string, string>`: the callback NAMES
   * are the whole API between the emitted classic scripts and this bundle,
   * so a contract file that dropped one should fail HERE, at the type, not
   * as an `undefined` key silently asserted against `typeof … === "function"`. */
  chunk_callbacks: { index: string; file: string; ticket: string };
  ticket_summary_keys: string[];
  ticket_totals_keys: string[];
  ticket_chunk_keys: string[];
  ticket_chunk_file_keys: string[];
};

// `Record<keyof X, true>` is the compiler half of this guard: adding a field
// to the interface without listing it here fails to type-check (missing
// key), and listing one the interface does not have fails too (excess key).
// The runtime assertions below then tie that exhaustive list to the shared
// contract, so all three of interface, contract and Python emitter agree.
const TICKET_SUMMARY_KEYS: Record<keyof TicketSummary, true> = {
  id: true,
  url: true,
  owned: true,
  covered: true,
  uncovered: true,
  per_tier: true,
  asserted: true,
  chunk: true,
};

const TICKET_TOTALS_KEYS: Record<keyof TicketTotals, true> = {
  owned: true,
  covered: true,
  uncovered: true,
  per_tier: true,
  asserted: true,
};

const TICKET_CHUNK_KEYS: Record<keyof TicketChunk, true> = {
  stamp: true,
  id: true,
  files: true,
};

const TICKET_CHUNK_FILE_KEYS: Record<keyof TicketChunk["files"][number], true> = {
  path: true,
  owned: true,
  covered: true,
  missing: true,
  per_tier: true,
  asserted: true,
  asserted_only: true,
};

describe("covapp ticket contract (shared with the Python emitter)", () => {
  it.each([
    ["TicketSummary", TICKET_SUMMARY_KEYS, contract.ticket_summary_keys],
    ["TicketTotals", TICKET_TOTALS_KEYS, contract.ticket_totals_keys],
    ["TicketChunk", TICKET_CHUNK_KEYS, contract.ticket_chunk_keys],
    ["TicketChunk.files[]", TICKET_CHUNK_FILE_KEYS, contract.ticket_chunk_file_keys],
  ])("%s declares exactly the contract's keys", (_name, declared, expected) => {
    expect(Object.keys(declared).sort()).toEqual([...expected].sort());
  });

  it("the expected data format matches the Python emitter's", () => {
    // dataGuard() renders GuardScreen instead of the report when a payload's
    // `format` differs from this build's, so a one-sided bump ships either a
    // report that renders nothing or a bundle that rejects every existing
    // report. Prose said "bump both together or never"; this enforces it.
    expect(
      EXPECTED_DATA_FORMAT,
      "types.ts disagrees with tests/_fixtures/covapp_ticket_contract.json. A format " +
        "bump must move ALL THREE together: OTTO_COV_DATA_FORMAT (spa_data.py), " +
        "EXPECTED_DATA_FORMAT (types.ts), and data_format in the fixture",
    ).toBe(contract.data_format);
  });

  it("sentinel ticket ids match the Python constants", () => {
    expect([...SENTINEL_TICKET_IDS].sort()).toEqual([...contract.sentinel_ticket_ids].sort());
  });

  it("registers the contract's chunk callbacks on window", async () => {
    // data.ts registers its callbacks at module scope, so importing it is
    // what installs them — the callback NAME is the whole API between the
    // emitted classic scripts and this bundle.
    await import("./data");

    const w = window as unknown as Record<string, unknown>;
    expect(typeof w[contract.chunk_callbacks.file]).toBe("function");
    expect(typeof w[contract.chunk_callbacks.ticket]).toBe("function");
  });
});

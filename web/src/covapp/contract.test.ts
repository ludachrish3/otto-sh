// TypeScript side of the covapp Python<->TypeScript contract.
//
// The data-format version, `types.ts`'s ticket interfaces, `TicketsPage`'s
// sentinel-id literals and the `window` chunk-callback names are all
// hand-maintained mirrors of what
// `src/otto/coverage/renderer/spa_data.py` emits, with no compiler tying
// the two languages together. Both sides assert against one shared table,
// `tests/_fixtures/covapp_contract.json`; the Python half is
// `tests/unit/cov/test_covapp_contract.py`, which reads its keys off
// real emitted payloads. A drift on either side fails exactly one suite and
// names the language that moved. Same shape as the `formatOutage` fixture
// parity precedent in `src/data/time.test.ts`.
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it, vi } from "vitest";

import { SENTINEL_TICKET_IDS } from "./pages/TicketsPage";
import { makeIndex } from "./testUtils";
import type {
  BranchJson,
  CoverageState,
  DirNode,
  FileChunk,
  FileNode,
  IndexPayload,
  LineJson,
  LineStatBucket,
  OverrideJson,
  RunContrib,
  RunJson,
  StatBucket,
  Stats,
  Tester,
  Thresholds,
  TicketChunk,
  TicketSummary,
  TicketTotals,
} from "./types";
import { EXPECTED_DATA_FORMAT } from "./types";

const here = dirname(fileURLToPath(import.meta.url));
const contract = JSON.parse(
  readFileSync(join(here, "../../../tests/_fixtures/covapp_contract.json"), "utf-8"),
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
  coverage_states: string[];
  index_payload_keys: string[];
  run_json_keys: string[];
  override_json_keys: string[];
  run_contrib_keys: string[];
  stats_keys: string[];
  line_stat_bucket_keys: string[];
  stat_bucket_keys: string[];
  dir_node_keys: string[];
  file_node_keys: string[];
  thresholds_keys: string[];
  file_chunk_keys: string[];
  line_json_keys: string[];
  tester_keys: string[];
  branch_json_keys: string[];
  stats_flags_keys: string[];
  line_states: string[];
  /** Spelled out for the same reason as `chunk_callbacks`: these are the
   * paths `data.ts` and `covapp.html` fetch by hand. */
  cov_data_layout: { index: string; files_dir: string; tickets_dir: string };
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

describe("covapp ticket + callback contract (shared with the Python emitter)", () => {
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
      "types.ts disagrees with tests/_fixtures/covapp_contract.json. A format " +
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

  it("reads the index payload from the contract's window property", async () => {
    // The index is the one callback name NOT installed as a function: the
    // emitted script ASSIGNS `window.__OTTO_COV__ = {...}` and data.ts reads
    // that property. Driven through getIndex() rather than grepping the
    // source, because `window.__OTTO_COV__` appears in three doc comments in
    // that file — a substring guard is satisfied by the comments alone and
    // stays green while the real read is renamed.
    const { getIndex } = await import("./data");
    const w = window as unknown as Record<string, unknown>;
    const saved = w[contract.chunk_callbacks.index];
    try {
      delete w[contract.chunk_callbacks.index];
      expect(getIndex()).toBeNull();
      const payload = makeIndex();
      w[contract.chunk_callbacks.index] = payload;
      expect(getIndex()).toBe(payload);
    } finally {
      if (saved === undefined) delete w[contract.chunk_callbacks.index];
      else w[contract.chunk_callbacks.index] = saved;
    }
  });
});

// ── The rest of the payload surface ─────────────────────────────────────────
//
// Same `Record<keyof X, true>` idiom as the ticket keys above, and it is TWO
// guards, not one. The compiler half: adding a field to the interface without
// listing it here fails to type-check, and listing one the interface lacks
// fails too — caught by `tsc` (`make check-ts`), NOT by vitest, which only
// transpiles. The runtime half: the assertions below tie that exhaustive list
// to what spa_data.py actually emits. Neither half alone is enough.

const COVERAGE_STATES: Record<CoverageState, true> = {
  uncovered: true,
  excluded: true,
  stale: true,
  aging: true,
};

const INDEX_PAYLOAD_KEYS: Record<keyof IndexPayload, true> = {
  format: true,
  stamp: true,
  generated_at: true,
  otto_version: true,
  project_name: true,
  tier_order: true,
  tier_labels: true,
  tier_colors: true,
  state_colors: true,
  thresholds: true,
  stat_types: true,
  runs: true,
  overrides: true,
  run_contrib: true,
  total_lines: true,
  tree: true,
  tickets: true,
  tickets_totals: true,
};

const RUN_JSON_KEYS: Record<keyof RunJson, true> = {
  id: true,
  tier: true,
  label: true,
  board: true,
  host: true,
  labs: true,
  captured_at: true,
  tester: true,
  ticket: true,
  note: true,
  base_commit: true,
  dirty_remap: true,
  aging: true,
};

const OVERRIDE_JSON_KEYS: Record<keyof OverrideJson, true> = {
  id: true,
  tier: true,
  key: true,
  reason: true,
  as_of: true,
};

const RUN_CONTRIB_KEYS: Record<keyof RunContrib, true> = {
  lines: true,
  revoked: true,
  files: true,
};

const STATS_KEYS: Record<keyof Stats, true> = {
  lines: true,
  branches: true,
  flags: true,
  ctx_lines: true,
};

const STAT_BUCKET_KEYS: Record<keyof StatBucket, true> = {
  total: true,
  hit: true,
  per_tier: true,
};

const LINE_STAT_BUCKET_KEYS: Record<keyof LineStatBucket, true> = {
  ...STAT_BUCKET_KEYS,
  asserted_per_tier: true,
  asserted_only: true,
};

const DIR_NODE_KEYS: Record<keyof DirNode, true> = {
  name: true,
  dirs: true,
  files: true,
  stats: true,
};

const FILE_NODE_KEYS: Record<keyof FileNode, true> = {
  name: true,
  path: true,
  chunk: true,
  stats: true,
};

const THRESHOLDS_KEYS: Record<keyof Thresholds, true> = {
  high: true,
  medium: true,
};

const TESTER_KEYS: Record<keyof Required<Tester>, true> = {
  name: true,
  email: true,
};

const BRANCH_JSON_KEYS: Record<keyof BranchJson, true> = {
  block: true,
  branch: true,
  hits: true,
  reachable: true,
};

// `Stats["flags"]` is a nested object literal, not a named interface, so
// `keyof` reaches it through the parent type.
const STATS_FLAGS_KEYS: Record<keyof Stats["flags"], true> = {
  stale: true,
  aging: true,
  excluded: true,
};

// LineJson.state is a SECOND, NARROWER vocabulary than CoverageState —
// `"stale" | "aging" | null` — so `coverage_states` does not cover it.
const LINE_STATES: Record<NonNullable<LineJson["state"]>, true> = {
  stale: true,
  aging: true,
};

const FILE_CHUNK_KEYS: Record<keyof FileChunk, true> = {
  stamp: true,
  chunk: true,
  path: true,
  source: true,
  lines: true,
  excluded: true,
};

// Four of these are optional (`run`, `stale_run`, `ticket`, `asserted`) —
// `keyof` includes optional keys already, so no `Required<>` is needed. The
// Python half emits a line carrying every one, so both sides describe the
// FULL set rather than the minimum. Neither half notices OPTIONALITY moving,
// which is a real (low-impact) gap: consumers read these with `?.`/`??`.
const LINE_JSON_KEYS: Record<keyof LineJson, true> = {
  hits: true,
  branches: true,
  state: true,
  run: true,
  stale_run: true,
  ticket: true,
  asserted: true,
};

describe("covapp payload contract (shared with the Python emitter)", () => {
  it.each([
    ["CoverageState", COVERAGE_STATES, contract.coverage_states],
    ["IndexPayload", INDEX_PAYLOAD_KEYS, contract.index_payload_keys],
    ["RunJson", RUN_JSON_KEYS, contract.run_json_keys],
    ["OverrideJson", OVERRIDE_JSON_KEYS, contract.override_json_keys],
    ["RunContrib", RUN_CONTRIB_KEYS, contract.run_contrib_keys],
    ["Stats", STATS_KEYS, contract.stats_keys],
    ["StatBucket", STAT_BUCKET_KEYS, contract.stat_bucket_keys],
    ["LineStatBucket", LINE_STAT_BUCKET_KEYS, contract.line_stat_bucket_keys],
    ["DirNode", DIR_NODE_KEYS, contract.dir_node_keys],
    ["FileNode", FILE_NODE_KEYS, contract.file_node_keys],
    ["Thresholds", THRESHOLDS_KEYS, contract.thresholds_keys],
    ["FileChunk", FILE_CHUNK_KEYS, contract.file_chunk_keys],
    ["LineJson", LINE_JSON_KEYS, contract.line_json_keys],
    ["Tester", TESTER_KEYS, contract.tester_keys],
    ["BranchJson", BRANCH_JSON_KEYS, contract.branch_json_keys],
    ["Stats.flags", STATS_FLAGS_KEYS, contract.stats_flags_keys],
    ["LineJson.state", LINE_STATES, contract.line_states],
  ])("%s declares exactly the contract's keys", (_name, declared, expected) => {
    expect(Object.keys(declared).sort()).toEqual([...expected].sort());
  });

  it.each([
    ["loadFileChunk", "files_dir"],
    ["loadTicketChunk", "tickets_dir"],
  ] as const)("%s requests the cov_data path the emitter writes", async (fn, key) => {
    // Asserted on the URL actually REQUESTED, not on the source text: both
    // directory names appear in data.ts's own doc comments, so a substring
    // guard passes while the real `script.src` is renamed and every chunk in
    // a shipped report 404s.
    const data = await import("./data");
    const injected: string[] = [];
    const spy = vi.spyOn(document.head, "appendChild").mockImplementation(((
      node: HTMLScriptElement,
    ) => {
      if (node.src) injected.push(node.src);
      return node;
    }) as typeof document.head.appendChild);
    try {
      void (data[fn] as (c: string) => Promise<unknown>)("chunk-id");
    } finally {
      spy.mockRestore();
    }

    expect(injected).toHaveLength(1);
    expect(new URL(injected[0] as string).pathname).toContain(
      `/cov_data/${contract.cov_data_layout[key]}/`,
    );
  });

  it("loads the index from the cov_data path the emitter writes", () => {
    // covapp.html names it exactly once, in the <script src>, so a substring
    // check there is genuine — unlike the two above.
    const html = readFileSync(join(here, "../../covapp.html"), "utf-8");
    expect(html).toContain(`cov_data/${contract.cov_data_layout.index}`);
  });
});

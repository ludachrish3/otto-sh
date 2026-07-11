import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { beforeEach, describe, expect, it } from "vitest";

import { useReviewStore } from "../data/reviewStore";

const __dir = dirname(fileURLToPath(import.meta.url));
const DRIFT = readFileSync(join(__dir, "../../fixtures/drift.json"), "utf-8");
const MINIMAL = readFileSync(join(__dir, "../../fixtures/minimal.json"), "utf-8");

function reset() {
  useReviewStore.setState({
    sessions: [],
    rawDocument: null,
    sourceName: null,
    warnings: [],
    importError: null,
    activeSessionId: null,
    range: null,
  });
}

describe("reviewStore", () => {
  beforeEach(reset);

  it("importText loads sessions and activates the first", () => {
    const ok = useReviewStore.getState().actions.importText(DRIFT, "drift.json");
    expect(ok).toBe(true);
    const s = useReviewStore.getState();
    expect(s.sessions).toHaveLength(3);
    expect(s.activeSessionId).toBe(s.sessions[0].id);
    expect(s.sourceName).toBe("drift.json");
    expect(s.importError).toBeNull();
  });

  it("importText reports errors without clobbering loaded data", () => {
    useReviewStore.getState().actions.importText(MINIMAL, "minimal.json");
    const ok = useReviewStore.getState().actions.importText("{}", "bad.json");
    expect(ok).toBe(false);
    const s = useReviewStore.getState();
    expect(s.importError).toMatch(/format|unversioned/i);
    expect(s.sessions).toHaveLength(1); // minimal still loaded
    expect(s.sourceName).toBe("minimal.json");
  });

  it("selectSession switches and resets the range", () => {
    useReviewStore.getState().actions.importText(DRIFT, "drift.json");
    const s2 = useReviewStore.getState().sessions[1].id;
    useReviewStore.getState().actions.setRange({ from: 1, to: 2 });
    useReviewStore.getState().actions.selectSession(s2);
    expect(useReviewStore.getState().activeSessionId).toBe(s2);
    expect(useReviewStore.getState().range).toBeNull();
  });

  it("resetView restores first session + full range", () => {
    useReviewStore.getState().actions.importText(DRIFT, "drift.json");
    const first = useReviewStore.getState().sessions[0].id;
    useReviewStore.getState().actions.selectSession(useReviewStore.getState().sessions[2].id);
    useReviewStore.getState().actions.setRange({ from: 1, to: 2 });
    useReviewStore.getState().actions.resetView();
    expect(useReviewStore.getState().activeSessionId).toBe(first);
    expect(useReviewStore.getState().range).toBeNull();
  });
});

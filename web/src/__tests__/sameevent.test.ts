// web/src/__tests__/sameevent.test.ts
// The TS mirror of event_ops.same_event: runs the SAME cases file the
// Python test (tests/unit/monitor/test_event_ops.py) runs.
import { describe, expect, it } from "vitest";
import cases from "../../../tests/_fixtures/same_event_cases.json" with { type: "json" };
import type { EventRecord } from "../api/export.gen";
import { sameEvent } from "../data/sameEvent";

interface Case {
  name: string;
  a: EventRecord | null;
  b: EventRecord | null;
  equal: boolean;
}

describe("sameEvent", () => {
  it.each(cases as Case[])("$name", ({ a, b, equal }) => {
    expect(sameEvent(a, b)).toBe(equal);
    expect(sameEvent(b, a)).toBe(equal);
  });
});

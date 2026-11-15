import { describe, expect, it } from "vitest";

import { isSourceState } from "../data/sourceApi";
import { sourceState } from "./_sourceState";

// The server always sends `unsaved` and `revision`, as non-negative integers
// (#1035): a state without them, or with any other value, does not read.
describe("isSourceState counts", () => {
  it("reads a state with integer counts", () => {
    expect(isSourceState(sourceState({ unsaved: 2, revision: 5 }))).toBe(true);
  });

  it.each(["unsaved", "revision"])("refuses a state without %s", (key) => {
    const { [key as "unsaved"]: _dropped, ...rest } = sourceState();
    expect(isSourceState(rest)).toBe(false);
  });

  it.each([
    ["true", true],
    ["a fraction", 1.5],
    ["a string", "1"],
    ["negative", -1],
    ["null", null],
  ])("refuses a count that is %s", (_, value) => {
    expect(isSourceState({ ...sourceState(), unsaved: value })).toBe(false);
    expect(isSourceState({ ...sourceState(), revision: value })).toBe(false);
  });
});

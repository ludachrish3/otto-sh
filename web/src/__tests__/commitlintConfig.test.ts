// @vitest-environment node
//
// The repo-root commitlint.config.mjs, run through the real commitlint
// (`@commitlint/load` + `@commitlint/lint`, the documented API). It lives in
// web/'s lane because that is the one CI lane with node_modules; the
// .githooks/commit-msg hook (pinned structurally by
// tests/unit/test_commit_msg_hook.py) feeds the same config to the same linter.
// Git hands the hook the raw message file, comments and scissors section
// included; commitlint's `--edit` drops comment lines and everything below the
// scissors line. That is the hook's concern, not this file's.

import { resolve } from "node:path";

import lint from "@commitlint/lint";
import load from "@commitlint/load";
import { describe, expect, it } from "vitest";

const CONFIG = resolve(import.meta.dirname, "../../../commitlint.config.mjs");
const RULE = "breaking-change-exclamation-mark";

async function verdict(message: string) {
  const loaded = await load({}, { file: CONFIG });
  return lint(message, loaded.rules, {
    parserOpts: loaded.parserPreset?.parserOpts ?? {},
  });
}

describe("commitlint.config.mjs", () => {
  it("enables only the breaking-change-exclamation-mark rule", async () => {
    const loaded = await load({}, { file: CONFIG });
    expect(Object.keys(loaded.rules)).toEqual([RULE]);
  });

  it("refuses a `!` header with no footer, naming the rule", async () => {
    const out = await verdict("feat(api)!: x");
    expect(out.valid).toBe(false);
    expect(out.errors.map((e) => e.name)).toEqual([RULE]);
  });

  it("accepts a `!` header with a BREAKING CHANGE footer", async () => {
    const out = await verdict("feat(api)!: x\n\nbody\n\nBREAKING CHANGE: use y.");
    expect(out.valid).toBe(true);
  });

  it("accepts a plain fix", async () => {
    expect((await verdict("fix: x")).valid).toBe(true);
  });

  it("refuses a BREAKING CHANGE footer with no `!`", async () => {
    const out = await verdict("feat: x\n\nBREAKING CHANGE: use y.");
    expect(out.valid).toBe(false);
    expect(out.errors.map((e) => e.name)).toEqual([RULE]);
  });

  it.each(["fixup! feat(api)!: x", "squash! feat(api)!: x"])("accepts %s", async (subject) => {
    expect((await verdict(subject)).valid).toBe(true);
  });

  it("accepts the release bump commit", async () => {
    expect((await verdict("chore(release): bump version 0.20.0 -> 0.21.0")).valid).toBe(true);
  });
});

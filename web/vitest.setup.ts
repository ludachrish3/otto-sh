// Console warnings elevated to test failures (Chris, 2026-07-19): React and
// react-aria surface real accessibility/correctness defects as console.warn/
// console.error at render time (e.g. react-aria's "A `textValue` prop is
// required for <Tag> elements with non-plain text children"), and a warning
// that only scrolls past in CI output is a warning nobody fixes. Any
// console.warn/console.error emitted while a test runs now fails THAT test,
// naming the offending output.
//
// Capture-then-fail (in afterEach), not throw-at-call-site: throwing from
// inside console.error mid-render detonates React's own error handling and
// reports a confusing secondary stack instead of the warning itself.
//
// A test that intentionally exercises a warning path can opt out explicitly:
//   import { allowConsoleOutput } from "../vitest.setup";  // path as needed
//   allowConsoleOutput();  // inside the test body, scoped to that test
import { afterEach, beforeEach } from "vitest";

// React 19 requires this flag for act() (which Testing Library wraps every
// render/event in). Testing Library's own auto-setup would set it, but that
// registration needs vitest's `globals: true`, which this project doesn't
// use — so every act() call warned "testing environment is not configured
// to support act(...)" (67 times across the suite before this guard).
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

// Accepted warnings — each entry is a REVIEWED exception, not a mute button.
// Add one only with a comment saying why the warning cannot be fixed at the
// source and why it is harmless.
const ACCEPTED: RegExp[] = [
  // SeriesPanel's chips (see its header comment): the vendored Tag's fixed
  // prop destructure can neither forward `data-testid` nor accept a
  // `textValue` override, and its children must carry the testid <span>, so
  // react-aria's dev-only "A `textValue` prop is required" advisory is
  // unavoidable short of forking the vendored component. Harmless here:
  // textValue only feeds TagGroup type-to-select, which the chips don't use.
  /A `textValue` prop is required for <Tag> elements/,
];

// Interception spans beforeEach->afterEach, so output from beforeAll/
// afterAll hooks escapes the guard — accepted; no current hook logs.
const original = { warn: console.warn, error: console.error };
let captured: string[] = [];
let allowed = false;

/** Opt the CURRENT test out of the console guard (resets automatically). */
export function allowConsoleOutput(): void {
  allowed = true;
}

beforeEach(() => {
  captured = [];
  allowed = false;
  console.warn = (...args: unknown[]) => {
    const message = args.map(String).join(" ");
    if (!ACCEPTED.some((rx) => rx.test(message))) captured.push(`console.warn: ${message}`);
    original.warn(...args);
  };
  console.error = (...args: unknown[]) => {
    const message = args.map(String).join(" ");
    if (!ACCEPTED.some((rx) => rx.test(message))) captured.push(`console.error: ${message}`);
    original.error(...args);
  };
});

afterEach(() => {
  console.warn = original.warn;
  console.error = original.error;
  if (!allowed && captured.length > 0) {
    const output = captured.join("\n  ");
    captured = [];
    throw new Error(
      `test emitted console warnings/errors (fix them, or call allowConsoleOutput() if the test exercises a warning path on purpose):\n  ${output}`,
    );
  }
  captured = [];
});

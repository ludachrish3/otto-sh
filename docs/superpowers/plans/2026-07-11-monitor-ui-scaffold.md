# Monitor UI Scaffold (Plan 2) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the monitor's app shell with the redesigned foundation — Tailwind v4 + React-Aria primitives (Untitled-UI-style, open-code), hash routing, theme v2, the client-side **Import** front door, and the historical **review chrome** (HISTORICAL tag · session picker · range picker · Reset) — all driven by the Plan-1 fixtures, no backend changes.

**Architecture:** New `web/src/data/` layer (export-document parser + `reviewStore`) typed by the generated `export.gen.ts`; new `web/src/ui/` primitives on `react-aria-components`; new `web/src/shell/` + `web/src/pages/` replacing `components/*` + `dashboard.css`. The legacy pure data layer (`store.ts`, `api/sse.ts`, `plotly.ts`, `grouping.ts`, `retirement.ts`, `events.ts`, `logevents.ts`) and its vitest suites **stay untouched** for the later live-hookup and chart phases. The Playwright browser lane pivots from the DOM-parity contract to a `data-testid` behavior contract in the same plan, so `make dashboard` is green again by the end.

**Tech Stack:** Tailwind CSS v4 (`@tailwindcss/vite`), `react-aria-components`, `wouter` (hash routing), `@fontsource-variable/inter` (vendored font — air-gap), zustand 5, vitest + @testing-library/react, pytest-playwright.

**Specs:** UX source of truth `docs/superpowers/specs/2026-07-05-monitor-untitled-ui-redesign-design.md` (§6 shell, §7 chrome, §12 review mode, §13 states); data contract `docs/superpowers/specs/2026-07-10-monitor-export-format-and-dummy-data-phase-design.md`.

**Plan series / merge strategy:** Plan 2 of the series (Plan 1 = export contract + fixtures, merged as `aa99ee1`). Plans 2–4 ride **this one branch** (`worktree-monitor-ui-scaffold`), following the Phase-2-React-port precedent: every plan leaves all gates green on the branch, but the monitor has **no charts until Plan 3** — merging to main mid-series would ship a chartless monitor, so the merge point is Chris's call at a parity milestone.

## Global Constraints

- **Zero changes under `src/otto/`** — this plan is `web/` + `tests/e2e/monitor/dashboard/` only. No schema regen, no docs gate needed, no collision with the library-extraction branch.
- **HOSTLESS ONLY** — nothing may touch lab VMs (10.10.200.x). All gates here (`make dashboard`, `web-check`, `coverage-hostless`) are hostless.
- **Air-gap is a hard constraint**: every asset bundles into `src/otto/monitor/static/dist/` (fonts included — no CDN, no Google Fonts). `scripts/check_airgap.sh` greps built js/css/html for absolute http(s) URLs; the runtime twin is the offline Playwright test rebuilt in Task 6.
- **The legacy data layer is KEPT, not deleted** (UX spec §15): `web/src/store.ts`, `api/client.ts`, `api/sse.ts`, `plotly.ts`, `plotly-gl2d.d.ts`, `grouping.ts`, `retirement.ts`, `events.ts`, `logevents.ts`, the `plotly.js-gl2d-dist-min` dep, and their 7 pure vitest suites. They become unreferenced by the app (vite tree-shakes them out of the bundle) but keep compiling and keep their tests green. Their fate belongs to Plans 3+ / live hookup.
- **`tests/e2e/monitor/dashboard/test_harness.py` is untouchable** — UI-agnostic server wire pins (hostless marker, not browser). It must pass unmodified throughout.
- **Between Tasks 4 and 6 the browser-marked Playwright specs are known-broken on this branch** (old DOM contract gone, new specs not yet written). Do not run `make dashboard` in that window; per-task verification is vitest + `npm run build`. Task 6 restores the lane; Task 7 proves it.
- **Web tooling:** biome (not eslint/prettier) — run `cd web && npm run check:fix` then `npm run check` before every commit that touches web/. TS is `strict` with `noUnusedLocals`; vitest coverage thresholds (65/53/66/66-ish ratchet in `vite.config.ts`) may need recalibration after the component wipe — adjust with a one-line justification comment, never delete the thresholds.
- **New npm deps installed with exact pins**: `npm install -E <pkg>` (repo pins exact versions). Record installed versions in the task report.
- **`data-testid` is the new Playwright contract** — every id referenced by Task 6's specs is listed in its task; components must carry them exactly. Styling classes are NEVER a test contract.
- **Python edits (Task 6 only)**: ruff format + check; conventional commits with `Assisted-by: Claude Fable 5` trailer embedded in `-m`; never `git add -u`.
- **Fresh worktree setup**: `uv sync` + `make web-install`.

---

### Task 1: Build foundation — Tailwind v4, fonts, deps

**Files:**
- Modify: `web/package.json` + `web/package-lock.json` (via npm install)
- Modify: `web/vite.config.ts` (tailwind plugin)
- Create: `web/src/app.css`
- Test: `npm run build` (old app still intact and green — this task is purely additive)

**Interfaces:**
- Produces: Tailwind v4 utilities + a `dark` class variant on `<html>`; `Inter Variable` as `--font-sans`; deps `react-aria-components`, `wouter` available for Tasks 2–5. `web/src/app.css` exists but is **not imported yet** (Task 4 wires it; importing it now would double-style the legacy UI).

- [ ] **Step 1: Install deps (exact pins)**

```bash
cd web
npm install -E tailwindcss @tailwindcss/vite react-aria-components wouter @fontsource-variable/inter
```

- [ ] **Step 2: Wire the vite plugin**

In `web/vite.config.ts`, add to imports and plugins:

```ts
import tailwindcss from "@tailwindcss/vite";
```

and change `plugins: [react()]` to `plugins: [react(), tailwindcss()]`.

(Leave `vite.covreport.config.ts` alone — covreport must not gain Tailwind.)

- [ ] **Step 3: Create `web/src/app.css`**

```css
/* The redesigned monitor's single stylesheet: Tailwind v4 + theme tokens.
   Replaces dashboard.css when the new shell lands (Task 4). Theme is a
   `dark` class on <html> (theme.ts v2), not media-query-only, so the
   in-app toggle can override the OS preference. */
@import "tailwindcss";
@import "@fontsource-variable/inter";

@custom-variant dark (&:where(.dark, .dark *));

@theme {
  --font-sans: "Inter Variable", system-ui, sans-serif;
  /* Brand: violet, matching the existing event-default #7c5cff family. */
  --color-brand-50: #f3f1ff;
  --color-brand-300: #b3a5ff;
  --color-brand-500: #7c5cff;
  --color-brand-600: #6a4be6;
  --color-brand-700: #5940bf;
  /* Status colors (UX spec §7: live green / historical blue / warn amber). */
  --color-status-live: #2f9e6e;
  --color-status-historical: #4c8dff;
  --color-status-warn: #e8a13c;
}

body {
  @apply bg-white font-sans text-gray-900 antialiased dark:bg-gray-950 dark:text-gray-100;
}
```

- [ ] **Step 4: Verify the old app still builds and tests pass**

```bash
cd web && npm run build && npm run test && npm run check
```

Expected: all green — nothing imports `app.css` yet, so the legacy UI is untouched. Check `git status` shows only the four intended files.

- [ ] **Step 5: Commit**

```bash
git add web/package.json web/package-lock.json web/vite.config.ts web/src/app.css
git commit -m "build(web): tailwind v4 + react-aria + wouter + vendored Inter

Additive foundation for the monitor UI redesign (plan 2026-07-11); app.css
not yet imported, legacy UI untouched.

Assisted-by: Claude Fable 5"
```

---

### Task 2: UI primitives (`web/src/ui/`)

**Files:**
- Create: `web/src/ui/Button.tsx`, `web/src/ui/Menu.tsx`, `web/src/ui/Select.tsx`, `web/src/ui/Badge.tsx`, `web/src/ui/ToggleGroup.tsx`, `web/src/ui/TextInput.tsx`
- Test: `web/src/__tests__/ui.test.tsx`

**Interfaces:**
- Produces (used by Tasks 4–5): `Button({variant?: "primary"|"secondary"|"ghost", ...aria ButtonProps})`; `OverflowMenu({items: MenuAction[]})` where `MenuAction = {id: string, label: string, onAction: () => void, isDisabled?: boolean, testId?: string}`; `Select<T>({label, items, selectedKey, onSelectionChange, testId})`; `Badge({tone?: "historical"|"neutral", children})`; `ToggleGroup({options: {id, label}[], selectedId, onSelect, testId})`; `TextInput({label, type?, value, onChange, testId})`.
- All primitives accept and forward `data-testid` via a `testId` prop.

- [ ] **Step 1: Write the failing test**

Create `web/src/__tests__/ui.test.tsx`:

```tsx
// Behavior smoke tests for the ui/ primitives: they render accessible
// roles, forward test ids, and fire their callbacks. Styling is not a
// contract and is deliberately unasserted.
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { Badge } from "../ui/Badge";
import { Button } from "../ui/Button";
import { OverflowMenu } from "../ui/Menu";
import { Select } from "../ui/Select";
import { TextInput } from "../ui/TextInput";
import { ToggleGroup } from "../ui/ToggleGroup";

describe("Button", () => {
  it("renders a button role and fires onPress", () => {
    const onPress = vi.fn();
    render(
      <Button onPress={onPress} testId="btn">
        Go
      </Button>,
    );
    fireEvent.click(screen.getByTestId("btn"));
    expect(onPress).toHaveBeenCalledOnce();
    expect(screen.getByRole("button", { name: "Go" })).toBeTruthy();
  });
});

describe("OverflowMenu", () => {
  it("opens on trigger click and fires the item action", () => {
    const onAction = vi.fn();
    render(
      <OverflowMenu
        items={[{ id: "import", label: "Import…", onAction, testId: "menu-import" }]}
      />,
    );
    fireEvent.click(screen.getByTestId("overflow-menu"));
    fireEvent.click(screen.getByTestId("menu-import"));
    expect(onAction).toHaveBeenCalledOnce();
  });

  it("renders disabled items as disabled", () => {
    render(
      <OverflowMenu
        items={[
          { id: "x", label: "X", onAction: () => {}, isDisabled: true, testId: "menu-x" },
        ]}
      />,
    );
    fireEvent.click(screen.getByTestId("overflow-menu"));
    expect(screen.getByTestId("menu-x").getAttribute("aria-disabled")).toBe("true");
  });
});

describe("Select", () => {
  it("shows items and reports selection", () => {
    const onSelectionChange = vi.fn();
    render(
      <Select
        label="Session"
        items={[
          { id: "a", label: "baseline" },
          { id: "b", label: "rewired" },
        ]}
        selectedKey="a"
        onSelectionChange={onSelectionChange}
        testId="session-picker"
      />,
    );
    fireEvent.click(screen.getByTestId("session-picker"));
    fireEvent.click(screen.getByText("rewired"));
    expect(onSelectionChange).toHaveBeenCalledWith("b");
  });
});

describe("ToggleGroup", () => {
  it("marks the selected option and reports clicks", () => {
    const onSelect = vi.fn();
    render(
      <ToggleGroup
        options={[
          { id: "full", label: "Full" },
          { id: "15m", label: "15m" },
        ]}
        selectedId="full"
        onSelect={onSelect}
        testId="range-presets"
      />,
    );
    const full = screen.getByRole("radio", { name: "Full" });
    expect(full.getAttribute("aria-checked")).toBe("true");
    fireEvent.click(screen.getByRole("radio", { name: "15m" }));
    expect(onSelect).toHaveBeenCalledWith("15m");
  });
});

describe("Badge / TextInput", () => {
  it("render content and forward test ids", () => {
    const onChange = vi.fn();
    render(
      <>
        <Badge tone="historical" testId="tag">
          HISTORICAL
        </Badge>
        <TextInput label="From" value="2026-07-01T08:00" onChange={onChange} testId="from" />
      </>,
    );
    expect(screen.getByTestId("tag").textContent).toBe("HISTORICAL");
    fireEvent.change(screen.getByTestId("from"), { target: { value: "2026-07-01T09:00" } });
    expect(onChange).toHaveBeenCalledWith("2026-07-01T09:00");
  });
});
```

- [ ] **Step 2: Run to verify failure**

Run: `cd web && npx vitest run src/__tests__/ui.test.tsx`
Expected: FAIL — modules under `../ui/` don't exist.

- [ ] **Step 3: Implement the primitives**

`web/src/ui/Button.tsx`:

```tsx
import { Button as AriaButton, type ButtonProps } from "react-aria-components";

const VARIANT_CLASSES = {
  primary:
    "bg-brand-600 text-white hover:bg-brand-700 pressed:bg-brand-700 " +
    "dark:bg-brand-500 dark:hover:bg-brand-600",
  secondary:
    "border border-gray-300 bg-white text-gray-700 hover:bg-gray-50 " +
    "dark:border-gray-700 dark:bg-gray-900 dark:text-gray-200 dark:hover:bg-gray-800",
  ghost:
    "text-gray-500 hover:bg-gray-100 hover:text-gray-700 " +
    "dark:text-gray-400 dark:hover:bg-gray-800 dark:hover:text-gray-200",
} as const;

export interface UiButtonProps extends Omit<ButtonProps, "className"> {
  variant?: keyof typeof VARIANT_CLASSES;
  testId?: string;
}

/** The one button. Variants cover every current chrome use; no size axis yet (YAGNI). */
export function Button({ variant = "secondary", testId, ...props }: UiButtonProps) {
  return (
    <AriaButton
      {...props}
      data-testid={testId}
      className={
        "inline-flex cursor-pointer items-center gap-1.5 rounded-lg px-3 py-1.5 text-sm " +
        "font-medium outline-offset-2 transition-colors disabled:cursor-not-allowed " +
        `disabled:opacity-50 ${VARIANT_CLASSES[variant]}`
      }
    />
  );
}
```

`web/src/ui/Menu.tsx`:

```tsx
import {
  Menu as AriaMenu,
  Button as AriaButton,
  MenuItem,
  MenuTrigger,
  Popover,
} from "react-aria-components";

export interface MenuAction {
  id: string;
  label: string;
  onAction: () => void;
  isDisabled?: boolean;
  testId?: string;
}

/** The chrome's "⋯" overflow menu (UX spec §7): infrequent actions live here. */
export function OverflowMenu({ items }: { items: MenuAction[] }) {
  return (
    <MenuTrigger>
      <AriaButton
        aria-label="More actions"
        data-testid="overflow-menu"
        className="cursor-pointer rounded-lg px-2 py-1 text-lg leading-none text-gray-500
          hover:bg-gray-100 hover:text-gray-700 dark:text-gray-400 dark:hover:bg-gray-800"
      >
        ⋯
      </AriaButton>
      <Popover
        className="min-w-44 rounded-lg border border-gray-200 bg-white p-1 shadow-lg
          dark:border-gray-700 dark:bg-gray-900"
      >
        <AriaMenu className="outline-none">
          {items.map((item) => (
            <MenuItem
              key={item.id}
              id={item.id}
              isDisabled={item.isDisabled}
              onAction={item.onAction}
              data-testid={item.testId}
              className="cursor-pointer rounded-md px-3 py-1.5 text-sm text-gray-700
                outline-none focus:bg-gray-100 disabled:cursor-not-allowed disabled:opacity-50
                dark:text-gray-200 dark:focus:bg-gray-800"
            >
              {item.label}
            </MenuItem>
          ))}
        </AriaMenu>
      </Popover>
    </MenuTrigger>
  );
}
```

`web/src/ui/Select.tsx`:

```tsx
import {
  Select as AriaSelect,
  Button as AriaButton,
  Key,
  ListBox,
  ListBoxItem,
  Popover,
  SelectValue,
} from "react-aria-components";

export interface SelectItem {
  id: string;
  label: string;
}

export function Select({
  label,
  items,
  selectedKey,
  onSelectionChange,
  testId,
}: {
  label: string;
  items: SelectItem[];
  selectedKey: string;
  onSelectionChange: (key: string) => void;
  testId?: string;
}) {
  return (
    <AriaSelect
      aria-label={label}
      selectedKey={selectedKey}
      onSelectionChange={(key: Key) => onSelectionChange(String(key))}
      className="inline-flex"
    >
      <AriaButton
        data-testid={testId}
        className="inline-flex cursor-pointer items-center gap-1.5 rounded-lg border
          border-gray-300 bg-white px-3 py-1.5 text-sm text-gray-700 hover:bg-gray-50
          dark:border-gray-700 dark:bg-gray-900 dark:text-gray-200 dark:hover:bg-gray-800"
      >
        <SelectValue />
        <span aria-hidden className="text-gray-400">
          ▾
        </span>
      </AriaButton>
      <Popover
        className="min-w-44 rounded-lg border border-gray-200 bg-white p-1 shadow-lg
          dark:border-gray-700 dark:bg-gray-900"
      >
        <ListBox className="outline-none">
          {items.map((item) => (
            <ListBoxItem
              key={item.id}
              id={item.id}
              textValue={item.label}
              className="cursor-pointer rounded-md px-3 py-1.5 text-sm text-gray-700
                outline-none focus:bg-gray-100 selected:font-semibold dark:text-gray-200
                dark:focus:bg-gray-800"
            >
              {item.label}
            </ListBoxItem>
          ))}
        </ListBox>
      </Popover>
    </AriaSelect>
  );
}
```

`web/src/ui/Badge.tsx`:

```tsx
import type { ReactNode } from "react";

const TONE_CLASSES = {
  historical:
    "bg-status-historical/15 text-status-historical dark:bg-status-historical/25",
  neutral: "bg-gray-200 text-gray-600 dark:bg-gray-800 dark:text-gray-300",
} as const;

export function Badge({
  tone = "neutral",
  testId,
  children,
}: {
  tone?: keyof typeof TONE_CLASSES;
  testId?: string;
  children: ReactNode;
}) {
  return (
    <span
      data-testid={testId}
      className={`inline-flex items-center rounded-md px-2 py-0.5 text-xs font-semibold
        tracking-wide ${TONE_CLASSES[tone]}`}
    >
      {children}
    </span>
  );
}
```

`web/src/ui/ToggleGroup.tsx`:

```tsx
import { Radio, RadioGroup } from "react-aria-components";

/** Segmented single-choice control (range presets). RadioGroup gives the
 * roving-selection semantics; visual is a joined pill row. */
export function ToggleGroup({
  options,
  selectedId,
  onSelect,
  testId,
  label,
}: {
  options: { id: string; label: string }[];
  selectedId: string;
  onSelect: (id: string) => void;
  testId?: string;
  label?: string;
}) {
  return (
    <RadioGroup
      aria-label={label ?? "options"}
      value={selectedId}
      onChange={onSelect}
      data-testid={testId}
      className="inline-flex overflow-hidden rounded-lg border border-gray-300
        dark:border-gray-700"
    >
      {options.map((opt) => (
        <Radio
          key={opt.id}
          value={opt.id}
          className="cursor-pointer border-r border-gray-300 px-3 py-1.5 text-sm
            text-gray-600 last:border-r-0 selected:bg-brand-600 selected:text-white
            hover:bg-gray-50 dark:border-gray-700 dark:text-gray-300
            dark:selected:bg-brand-500 dark:hover:bg-gray-800"
        >
          {opt.label}
        </Radio>
      ))}
    </RadioGroup>
  );
}
```

`web/src/ui/TextInput.tsx`:

```tsx
import { Input, Label, TextField } from "react-aria-components";

export function TextInput({
  label,
  type = "text",
  value,
  onChange,
  testId,
}: {
  label: string;
  type?: string;
  value: string;
  onChange: (value: string) => void;
  testId?: string;
}) {
  return (
    <TextField value={value} onChange={onChange} className="inline-flex items-center gap-1.5">
      <Label className="text-xs text-gray-500 dark:text-gray-400">{label}</Label>
      <Input
        type={type}
        data-testid={testId}
        className="rounded-lg border border-gray-300 bg-white px-2 py-1 text-sm
          text-gray-700 dark:border-gray-700 dark:bg-gray-900 dark:text-gray-200"
      />
    </TextField>
  );
}
```

- [ ] **Step 4: Run tests, lint**

```bash
cd web && npx vitest run src/__tests__/ui.test.tsx && npm run check:fix && npm run check && npm run typecheck
```

Expected: all green. If a react-aria-components API detail differs from the code above (prop names evolve), adapt minimally and note the exact change in your report — the test file is the contract.

- [ ] **Step 5: Commit**

```bash
git add web/src/ui web/src/__tests__/ui.test.tsx
git commit -m "feat(web): react-aria UI primitives for the monitor redesign

Button/Menu/Select/Badge/ToggleGroup/TextInput, Untitled-UI-style styling,
data-testid contract throughout.

Assisted-by: Claude Fable 5"
```

---

### Task 3: Review data layer (`web/src/data/`)

**Files:**
- Create: `web/src/data/exportDoc.ts`, `web/src/data/reviewStore.ts`, `web/src/data/time.ts`
- Test: `web/src/__tests__/exportdoc.test.ts`, `web/src/__tests__/reviewstore.test.ts`

**Interfaces:**
- Consumes: types from `web/src/api/export.gen.ts` (`MonitorHistoricalExportDocument`, `SessionRecord`, `HostSnapshot`, `LinkSnapshot`, `ElementRecord`, `MetricRecord`, …) and the committed fixtures `web/fixtures/*.json` (test inputs).
- Produces (used by Tasks 4–5):
  - `parseExportDocument(text: string): ParseResult` — throws `ExportParseError` on non-JSON / missing-`format` / wrong-format; returns normalized sessions + warnings.
  - `NormalizedSession` — per-session: `id, label, note, startMs, endMs, lab, meta, metrics, events, logEvents, chartMap, elements: DerivedElement[], hostIds: Set<string>, elementIds: Set<string>`.
  - `DerivedElement {id, type, explicit, description, hostIds, singleton}`.
  - `sessionBounds(s): TimeRange`; `presetRange(bounds, minutes|null): TimeRange|null`; `clampRange`; `metricsForSubject(s, subjectId, range|null): MetricRecord[]`; `subjectKind(s, id): "host"|"element"|null`.
  - `useReviewStore` (zustand): state `{sessions, rawDocument, sourceName, warnings, importError, activeSessionId, range}` + actions `{importText(text, sourceName), selectSession(id), setRange(range|null), resetView(), clearImportError()}`. `selectSession` and `importText` reset `range` to `null` (= full).
  - `web/src/data/time.ts`: `parseTs(iso: string): number` (ms), `msToLocalInput(ms): string` ("YYYY-MM-DDTHH:mm" local), `localInputToMs(v: string): number | null`, `formatSpan(fromMs, toMs): string`.

- [ ] **Step 1: Write the failing tests**

Create `web/src/__tests__/exportdoc.test.ts`:

```ts
// The import-path contract, tested against the REAL committed fixtures —
// the same files the Playwright specs and manual dev use. Fixture JSON is
// imported directly (vite/vitest resolve JSON imports).
import { describe, expect, it } from "vitest";

import driftDoc from "../../fixtures/drift.json";
import kitchenDoc from "../../fixtures/kitchen-sink.json";
import minimalDoc from "../../fixtures/minimal.json";
import { ExportParseError, parseExportDocument, presetRange, sessionBounds } from "../data/exportDoc";
import { metricsForSubject, subjectKind } from "../data/exportDoc";

const parse = (doc: unknown) => parseExportDocument(JSON.stringify(doc));

describe("parseExportDocument", () => {
  it("rejects non-JSON", () => {
    expect(() => parseExportDocument("not json")).toThrow(ExportParseError);
  });

  it("rejects a legacy document without a format field", () => {
    expect(() => parse({ metrics: [], events: [] })).toThrow(/unversioned|format/i);
  });

  it("rejects unknown format versions", () => {
    expect(() => parse({ format: 2, sessions: [] })).toThrow(/format 2/);
  });

  it("parses all three committed fixtures without warnings", () => {
    for (const doc of [kitchenDoc, minimalDoc, driftDoc]) {
      const result = parse(doc);
      expect(result.warnings).toEqual([]);
      expect(result.sessions.length).toBeGreaterThan(0);
    }
  });

  it("normalizes omitted optional sections to empty defaults", () => {
    const result = parse({
      format: 1,
      sessions: [{ id: "bare", start: "2026-07-01T08:00:00Z" }],
    });
    const s = result.sessions[0];
    expect(s.lab.hosts).toEqual([]);
    expect(s.metrics).toEqual([]);
    expect(s.elements).toEqual([]);
    expect(s.endMs).toBe(s.startMs); // open session with no samples
  });

  it("warns on duplicate session ids", () => {
    const dup = { id: "s", start: "2026-07-01T08:00:00Z" };
    const result = parse({ format: 1, sessions: [dup, dup] });
    expect(result.warnings.some((w) => w.includes("duplicate session id"))).toBe(true);
  });
});

describe("element derivation (kitchen-sink)", () => {
  const session = parse(kitchenDoc).sessions[0];
  const byId = new Map(session.elements.map((e) => [e.id, e]));

  it("derives grouped elements from hosts", () => {
    expect(byId.get("chassis-a")?.hostIds).toHaveLength(3);
    expect(byId.get("chassis-a")?.type).toBe("physical"); // members carry slots
    expect(byId.get("workers")?.type).toBe("logical"); // explicit entry
    expect(byId.get("workers")?.explicit).toBe(true);
  });

  it("includes the explicit zero-host element (empty chassis)", () => {
    expect(byId.get("spare-chassis")?.hostIds).toEqual([]);
    expect(byId.get("spare-chassis")?.type).toBe("physical");
  });

  it("marks single-host elements singleton", () => {
    expect(byId.get("db-01")?.singleton).toBe(true);
    expect(byId.get("chassis-a")?.singleton).toBe(false);
  });

  it("resolves subject kinds host-first", () => {
    expect(subjectKind(session, "chassis-a_lc1")).toBe("host");
    expect(subjectKind(session, "chassis-a")).toBe("element");
    expect(subjectKind(session, "nope")).toBeNull();
  });
});

describe("ranges", () => {
  const session = parse(kitchenDoc).sessions[0];
  const bounds = sessionBounds(session);

  it("bounds span the session", () => {
    expect(bounds.to - bounds.from).toBe(2 * 3600 * 1000);
  });

  it("presets compute from the bounds end", () => {
    const last15 = presetRange(bounds, 15);
    expect(last15).not.toBeNull();
    expect(last15?.to).toBe(bounds.to);
    expect(last15 ? last15.to - last15.from : 0).toBe(15 * 60 * 1000);
    expect(presetRange(bounds, null)).toBeNull(); // Full = no range filter
  });

  it("metricsForSubject filters by subject and range", () => {
    const all = metricsForSubject(session, "workers_w2", null);
    const last15 = metricsForSubject(session, "workers_w2", presetRange(bounds, 15));
    expect(all.length).toBeGreaterThan(last15.length);
    expect(last15.length).toBeGreaterThan(0);
    // element-targeted series resolve through the element id
    expect(metricsForSubject(session, "chassis-a", null).length).toBeGreaterThan(0);
  });
});
```

Create `web/src/__tests__/reviewstore.test.ts`:

```ts
import { readFileSync } from "node:fs";
import { beforeEach, describe, expect, it } from "vitest";

import { useReviewStore } from "../data/reviewStore";

const DRIFT = readFileSync(new URL("../../fixtures/drift.json", import.meta.url), "utf-8");
const MINIMAL = readFileSync(new URL("../../fixtures/minimal.json", import.meta.url), "utf-8");

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
```

- [ ] **Step 2: Run to verify failure**

Run: `cd web && npx vitest run src/__tests__/exportdoc.test.ts src/__tests__/reviewstore.test.ts`
Expected: FAIL — `../data/exportDoc` does not exist.

- [ ] **Step 3: Implement**

`web/src/data/time.ts`:

```ts
/** Time helpers for the review UI. All internal times are epoch ms. */

export function parseTs(iso: string): number {
  return Date.parse(iso);
}

/** ms → the value a <input type="datetime-local"> wants, in LOCAL time. */
export function msToLocalInput(ms: number): string {
  const d = new Date(ms);
  const pad = (n: number) => String(n).padStart(2, "0");
  return (
    `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}` +
    `T${pad(d.getHours())}:${pad(d.getMinutes())}`
  );
}

export function localInputToMs(value: string): number | null {
  const ms = Date.parse(value);
  return Number.isNaN(ms) ? null : ms;
}

export function formatSpan(fromMs: number, toMs: number): string {
  const mins = Math.round((toMs - fromMs) / 60_000);
  if (mins < 60) return `${mins}m`;
  const hours = mins / 60;
  return Number.isInteger(hours) ? `${hours}h` : `${hours.toFixed(1)}h`;
}
```

`web/src/data/exportDoc.ts`:

```ts
// Client-side reader of the versioned monitor export document (spec
// 2026-07-10 §3; wire types generated in api/export.gen.ts). This is the
// Import front door's parser: it validates the format marker, normalizes
// the lenient optionals ONCE at the boundary (everything downstream sees
// dense arrays), derives elements from hosts, and surfaces non-fatal
// oddities as warnings (spec ship-and-note: duplicate ids warn, not fail).
import type {
  ElementRecord,
  HostSnapshot,
  LinkSnapshot,
  MetricRecord,
  MonitorHistoricalExportDocument,
  SessionRecord,
} from "../api/export.gen";
import { parseTs } from "./time";

export class ExportParseError extends Error {}

export interface TimeRange {
  from: number;
  to: number;
}

export interface DerivedElement {
  id: string;
  type: "physical" | "logical";
  explicit: boolean;
  description: string | null;
  hostIds: string[];
  singleton: boolean;
}

export interface NormalizedSession {
  id: string;
  label: string | null;
  note: string | null;
  startMs: number;
  endMs: number;
  lab: {
    hosts: HostSnapshot[];
    links: LinkSnapshot[];
    explicitElements: ElementRecord[];
  };
  meta: NonNullable<SessionRecord["meta"]>;
  metrics: MetricRecord[];
  events: NonNullable<SessionRecord["events"]>;
  logEvents: NonNullable<SessionRecord["log_events"]>;
  chartMap: Record<string, string>;
  elements: DerivedElement[];
  hostIds: Set<string>;
  elementIds: Set<string>;
}

export interface ParseResult {
  document: MonitorHistoricalExportDocument;
  sessions: NormalizedSession[];
  warnings: string[];
}

export function deriveElements(
  hosts: HostSnapshot[],
  explicit: ElementRecord[],
): DerivedElement[] {
  const byId = new Map<string, DerivedElement>();
  for (const rec of explicit) {
    byId.set(rec.id, {
      id: rec.id,
      type: rec.type ?? "logical",
      explicit: true,
      description: rec.description ?? null,
      hostIds: [],
      singleton: false,
    });
  }
  for (const host of hosts) {
    const existing = byId.get(host.element);
    if (existing) {
      existing.hostIds.push(host.id);
    } else {
      byId.set(host.element, {
        id: host.element,
        type: "logical",
        explicit: false,
        description: null,
        hostIds: [host.id],
        singleton: false,
      });
    }
  }
  for (const el of byId.values()) {
    // Type inference only where not explicitly declared: slots => physical.
    if (!el.explicit) {
      const hostsOf = hosts.filter((h) => h.element === el.id);
      el.type = hostsOf.some((h) => h.slot !== null && h.slot !== undefined)
        ? "physical"
        : "logical";
    }
    // Singleton is ALWAYS derived from membership count (spec §2).
    el.singleton = el.hostIds.length === 1;
  }
  return [...byId.values()].sort((a, b) => a.id.localeCompare(b.id));
}

function normalizeSession(raw: SessionRecord, warnings: string[]): NormalizedSession {
  const hosts = raw.lab?.hosts ?? [];
  const links = raw.lab?.links ?? [];
  const explicitElements = raw.lab?.elements ?? [];
  const metrics = raw.metrics ?? [];
  const startMs = parseTs(raw.start);
  const lastSampleMs = metrics.length
    ? Math.max(...metrics.map((m) => parseTs(m.timestamp)))
    : null;
  const endMs = raw.end != null ? parseTs(raw.end) : (lastSampleMs ?? startMs);

  const hostIds = new Set<string>();
  for (const h of hosts) {
    if (hostIds.has(h.id)) warnings.push(`session ${raw.id}: duplicate host id ${h.id}`);
    hostIds.add(h.id);
  }
  const elements = deriveElements(hosts, explicitElements);

  return {
    id: raw.id,
    label: raw.label ?? null,
    note: raw.note ?? null,
    startMs,
    endMs,
    lab: { hosts, links, explicitElements },
    meta: raw.meta ?? { interval: null, charts: [], tabs: [] },
    metrics,
    events: raw.events ?? [],
    logEvents: raw.log_events ?? [],
    chartMap: (raw.chart_map ?? {}) as Record<string, string>,
    elements,
    hostIds,
    elementIds: new Set(elements.map((e) => e.id)),
  };
}

export function parseExportDocument(text: string): ParseResult {
  let doc: unknown;
  try {
    doc = JSON.parse(text);
  } catch {
    throw new ExportParseError("Not a JSON document.");
  }
  if (typeof doc !== "object" || doc === null) {
    throw new ExportParseError("Not a JSON object.");
  }
  const record = doc as Record<string, unknown>;
  if (!("format" in record)) {
    throw new ExportParseError(
      "No 'format' field — this looks like a legacy unversioned export. " +
        "Re-export from a current otto run.",
    );
  }
  if (record.format !== 1) {
    throw new ExportParseError(`Unsupported export format ${String(record.format)}.`);
  }
  if (!Array.isArray(record.sessions)) {
    throw new ExportParseError("Missing 'sessions' array.");
  }
  const typed = doc as MonitorHistoricalExportDocument;
  const warnings: string[] = [];
  const seen = new Set<string>();
  for (const s of typed.sessions) {
    if (seen.has(s.id)) warnings.push(`duplicate session id ${s.id}`);
    seen.add(s.id);
  }
  const sessions = typed.sessions.map((s) => normalizeSession(s, warnings));
  return { document: typed, sessions, warnings };
}

export function sessionBounds(session: NormalizedSession): TimeRange {
  return { from: session.startMs, to: session.endMs };
}

/** minutes=null means Full range → no filter (null). */
export function presetRange(bounds: TimeRange, minutes: number | null): TimeRange | null {
  if (minutes === null) return null;
  return { from: Math.max(bounds.from, bounds.to - minutes * 60_000), to: bounds.to };
}

export function clampRange(range: TimeRange, bounds: TimeRange): TimeRange {
  return {
    from: Math.max(range.from, bounds.from),
    to: Math.min(range.to, bounds.to),
  };
}

export function subjectKind(
  session: NormalizedSession,
  id: string,
): "host" | "element" | null {
  if (session.hostIds.has(id)) return "host";
  if (session.elementIds.has(id)) return "element";
  return null;
}

export function metricsForSubject(
  session: NormalizedSession,
  subjectId: string,
  range: TimeRange | null,
): MetricRecord[] {
  return session.metrics.filter((m) => {
    if (m.host !== subjectId) return false;
    if (range === null) return true;
    const ts = parseTs(m.timestamp);
    return ts >= range.from && ts <= range.to;
  });
}
```

`web/src/data/reviewStore.ts`:

```ts
// Review-mode state: the imported document, the active session, and the
// viewed time range. Deliberately separate from the legacy live store
// (store.ts) — that one keeps serving the SSE/live path and the two merge
// at the live-hookup phase, not before.
import { create } from "zustand";

import type { MonitorHistoricalExportDocument } from "../api/export.gen";
import {
  ExportParseError,
  type NormalizedSession,
  parseExportDocument,
  type TimeRange,
} from "./exportDoc";

interface ReviewActions {
  /** Parse + load an export document. Returns false (and sets importError,
   * keeping any previously loaded data) on failure. */
  importText: (text: string, sourceName: string) => boolean;
  selectSession: (id: string) => void;
  setRange: (range: TimeRange | null) => void;
  resetView: () => void;
  clearImportError: () => void;
}

export interface ReviewState {
  sessions: NormalizedSession[];
  rawDocument: MonitorHistoricalExportDocument | null;
  sourceName: string | null;
  warnings: string[];
  importError: string | null;
  activeSessionId: string | null;
  range: TimeRange | null;
  actions: ReviewActions;
}

export const useReviewStore = create<ReviewState>()((set, get) => ({
  sessions: [],
  rawDocument: null,
  sourceName: null,
  warnings: [],
  importError: null,
  activeSessionId: null,
  range: null,
  actions: {
    importText: (text, sourceName) => {
      try {
        const result = parseExportDocument(text);
        set({
          sessions: result.sessions,
          rawDocument: result.document,
          sourceName,
          warnings: result.warnings,
          importError: null,
          activeSessionId: result.sessions[0]?.id ?? null,
          range: null,
        });
        return true;
      } catch (err) {
        set({
          importError:
            err instanceof ExportParseError ? err.message : `Import failed: ${String(err)}`,
        });
        return false;
      }
    },
    selectSession: (id) => set({ activeSessionId: id, range: null }),
    setRange: (range) => set({ range }),
    resetView: () =>
      set({ activeSessionId: get().sessions[0]?.id ?? null, range: null }),
    clearImportError: () => set({ importError: null }),
  },
}));

export function useActiveSession(): NormalizedSession | null {
  return useReviewStore(
    (s) => s.sessions.find((sess) => sess.id === s.activeSessionId) ?? null,
  );
}
```

- [ ] **Step 4: Run tests, lint, typecheck**

```bash
cd web && npx vitest run src/__tests__/exportdoc.test.ts src/__tests__/reviewstore.test.ts
cd web && npm run check:fix && npm run check && npm run typecheck
```

Expected: green. Note: importing `fixtures/*.json` from tests requires `resolveJsonModule` behavior — TS `moduleResolution: "bundler"` handles it; if tsc complains, add `"resolveJsonModule": true` to `web/tsconfig.json` compilerOptions and say so in the report.

- [ ] **Step 5: Commit**

```bash
git add web/src/data web/src/__tests__/exportdoc.test.ts web/src/__tests__/reviewstore.test.ts
git commit -m "feat(web): review data layer — export-doc parser + reviewStore

Format-marker validation (legacy fails loud), boundary normalization of
lenient optionals, element derivation w/ explicit merge + empty-chassis,
duplicate-id warnings, session/range state. Tested against the committed
fixtures.

Assisted-by: Claude Fable 5"
```

---

### Task 4: New app shell — cutover

**Files:**
- Rewrite: `web/src/App.tsx`, `web/src/main.tsx`, `web/src/theme.ts`
- Create: `web/src/shell/AppBar.tsx`, `web/src/shell/ImportExport.tsx`, `web/src/shell/EmptyState.tsx`
- Rewrite test: `web/src/__tests__/theme.test.ts`; Create: `web/src/__tests__/shell.test.tsx`
- Delete: `web/src/components/ChartGrid.tsx`, `ChartPanel.tsx`, `EventPopover.tsx`, `EventTable.tsx`, `EventToolbar.tsx`, `Header.tsx`, `TabBar.tsx`, `web/src/dashboard.css`, `web/src/__tests__/app.test.tsx`, `web/src/__tests__/eventtable.test.tsx`
- Modify: `web/index.html` (title → `otto monitor`)

**Interfaces:**
- Consumes: Task 2 primitives, Task 3 store, Task 1 `app.css`.
- Produces: `App` = theme v2 + AppBar + import/export plumbing + (until Task 5) an EmptyState/loaded placeholder body. New `theme.ts` API: `loadTheme(): Theme` (storage else `prefers-color-scheme`), `saveTheme(t)` (persists + applies), `applyTheme(t)` (toggles `dark` class on `<html>`), same `"otto-theme"` storage key. `ImportExport` exposes a hidden file input `data-testid="import-input"` + window drag-drop, and `exportLoadedDocument()` (Blob download `otto-monitor-export.json`).
- **data-testid contract introduced here** (Task 6 depends on it): `app-bar`, `brand`, `status-text`, `status-dot`, `overflow-menu`, `menu-import`, `menu-export`, `menu-theme`, `import-input`, `import-error`, `empty-review`, `empty-import-btn`.
- **Known-broken window opens:** the five browser-marked Playwright specs fail from this commit until Task 6. Do not run `make dashboard` until then.

- [ ] **Step 1: Rewrite `web/src/theme.ts` + its test (TDD: test first)**

Replace `web/src/__tests__/theme.test.ts` wholesale:

```ts
// Theme v2 (UX spec §7): seed from prefers-color-scheme when nothing is
// stored; a 2-state toggle persists BOTH values (unlike v1, which only
// ever wrote "light"); applied as a `dark` class on <html> for Tailwind's
// @custom-variant.
import { afterEach, describe, expect, it, vi } from "vitest";

import { applyTheme, loadTheme, saveTheme } from "../theme";

function mockMedia(dark: boolean) {
  vi.stubGlobal("matchMedia", (query: string) => ({
    matches: dark && query.includes("dark"),
    addEventListener: () => {},
    removeEventListener: () => {},
  }));
}

afterEach(() => {
  localStorage.clear();
  vi.unstubAllGlobals();
  document.documentElement.classList.remove("dark");
});

describe("loadTheme", () => {
  it("honors stored values over the OS preference", () => {
    mockMedia(true);
    localStorage.setItem("otto-theme", "light");
    expect(loadTheme()).toBe("light");
    localStorage.setItem("otto-theme", "dark");
    expect(loadTheme()).toBe("dark");
  });

  it("seeds from prefers-color-scheme when nothing stored", () => {
    mockMedia(true);
    expect(loadTheme()).toBe("dark");
    mockMedia(false);
    expect(loadTheme()).toBe("light");
  });
});

describe("saveTheme / applyTheme", () => {
  it("persists and toggles the html dark class", () => {
    saveTheme("dark");
    expect(localStorage.getItem("otto-theme")).toBe("dark");
    expect(document.documentElement.classList.contains("dark")).toBe(true);
    saveTheme("light");
    expect(localStorage.getItem("otto-theme")).toBe("light");
    expect(document.documentElement.classList.contains("dark")).toBe(false);
  });

  it("applyTheme alone does not persist", () => {
    applyTheme("dark");
    expect(localStorage.getItem("otto-theme")).toBeNull();
  });
});
```

New `web/src/theme.ts`:

```ts
// Theme v2 (UX spec §7): initial theme seeds from the OS preference; the
// toggle is two-state light<->dark and persists the explicit choice. The
// storage key survives from v1, but v2 writes BOTH values (v1 only ever
// wrote "light" — dark was the implicit default; now absence = OS).
const STORAGE_KEY = "otto-theme";

export type Theme = "light" | "dark";

export function loadTheme(): Theme {
  const stored = localStorage.getItem(STORAGE_KEY);
  if (stored === "light" || stored === "dark") return stored;
  return window.matchMedia?.("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

/** Toggle the `dark` class on <html> — what app.css's @custom-variant reads. */
export function applyTheme(theme: Theme): void {
  document.documentElement.classList.toggle("dark", theme === "dark");
}

export function saveTheme(theme: Theme): void {
  localStorage.setItem(STORAGE_KEY, theme);
  applyTheme(theme);
}

// Apply before first paint (module side effect, same trick as v1).
applyTheme(loadTheme());
```

Run: `cd web && npx vitest run src/__tests__/theme.test.ts` — RED against old theme.ts first, GREEN after the rewrite. (Old `onThemeChange`/`applyBodyTheme` consumers are the components deleted in Step 3 — this compiles only once Step 3 lands; order your edits test → theme.ts → deletions → shell, then run.)

- [ ] **Step 2: Shell components**

`web/src/shell/ImportExport.tsx`:

```tsx
// The Import front door (UX spec §12): a hidden file input driven by the
// ⋯ menu / empty state, plus whole-window drag-drop. Export re-serializes
// the loaded raw document (client-side, no endpoint — spec §14).
import { type ReactNode, useCallback, useEffect, useRef } from "react";

import { useReviewStore } from "../data/reviewStore";

export function useImportFile(): (file: File) => void {
  const importText = useReviewStore((s) => s.actions.importText);
  return useCallback(
    (file: File) => {
      void file.text().then((text) => importText(text, file.name));
    },
    [importText],
  );
}

export function exportLoadedDocument(): void {
  const { rawDocument, sourceName } = useReviewStore.getState();
  if (!rawDocument) return;
  const blob = new Blob([JSON.stringify(rawDocument)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = sourceName ?? "otto-monitor-export.json";
  a.click();
  URL.revokeObjectURL(url);
}

/** Mounts the hidden input + drag-drop handlers; children get the picker via context-free ref registration. */
export function ImportProvider({ children }: { children: ReactNode }) {
  const inputRef = useRef<HTMLInputElement>(null);
  const importFile = useImportFile();

  useEffect(() => {
    registerPicker(() => inputRef.current?.click());
    const onDragOver = (e: DragEvent) => e.preventDefault();
    const onDrop = (e: DragEvent) => {
      e.preventDefault();
      const file = e.dataTransfer?.files?.[0];
      if (file) importFile(file);
    };
    window.addEventListener("dragover", onDragOver);
    window.addEventListener("drop", onDrop);
    return () => {
      registerPicker(null);
      window.removeEventListener("dragover", onDragOver);
      window.removeEventListener("drop", onDrop);
    };
  }, [importFile]);

  return (
    <>
      <input
        ref={inputRef}
        type="file"
        accept=".json,application/json"
        data-testid="import-input"
        className="hidden"
        onChange={(e) => {
          const file = e.target.files?.[0];
          if (file) importFile(file);
          e.target.value = ""; // re-importing the same file must re-fire
        }}
      />
      {children}
    </>
  );
}

let picker: (() => void) | null = null;
function registerPicker(fn: (() => void) | null): void {
  picker = fn;
}
/** Open the OS file dialog (called from the ⋯ menu and the empty state). */
export function openImportPicker(): void {
  picker?.();
}
```

`web/src/shell/AppBar.tsx`:

```tsx
// Global chrome (UX spec §7): brand fixed left; far right = status text ·
// status dot · ⋯ menu (Import/Export/theme — the infrequent actions).
// Pause appears here in live mode only — a later phase; review has none.
import { useState } from "react";

import { useReviewStore } from "../data/reviewStore";
import { loadTheme, saveTheme, type Theme } from "../theme";
import { OverflowMenu } from "../ui/Menu";
import { exportLoadedDocument, openImportPicker } from "./ImportExport";

export function AppBar() {
  const hasData = useReviewStore((s) => s.sessions.length > 0);
  const [theme, setTheme] = useState<Theme>(loadTheme);

  const toggleTheme = () => {
    const next: Theme = theme === "dark" ? "light" : "dark";
    saveTheme(next);
    setTheme(next);
  };

  return (
    <header
      data-testid="app-bar"
      className="flex h-12 items-center justify-between border-b border-gray-200 px-4
        dark:border-gray-800"
    >
      <div data-testid="brand" className="flex items-center gap-2 text-sm font-semibold">
        <span aria-hidden className="text-brand-500">
          ⬡
        </span>
        otto monitor
      </div>
      <div className="flex items-center gap-3">
        <span data-testid="status-text" className="text-sm text-gray-500 dark:text-gray-400">
          {hasData ? "Historical" : "No data"}
        </span>
        <span
          data-testid="status-dot"
          className={`h-2.5 w-2.5 rounded-full ${
            hasData ? "bg-status-historical" : "bg-gray-300 dark:bg-gray-600"
          }`}
        />
        <OverflowMenu
          items={[
            { id: "import", label: "Import…", onAction: openImportPicker, testId: "menu-import" },
            {
              id: "export",
              label: "Export",
              onAction: exportLoadedDocument,
              isDisabled: !hasData,
              testId: "menu-export",
            },
            {
              id: "theme",
              label: theme === "dark" ? "Switch to light mode" : "Switch to dark mode",
              onAction: toggleTheme,
              testId: "menu-theme",
            },
          ]}
        />
      </div>
    </header>
  );
}
```

`web/src/shell/EmptyState.tsx`:

```tsx
// Empty review state (UX spec §13): the import CTA is the whole page.
import { useReviewStore } from "../data/reviewStore";
import { Button } from "../ui/Button";
import { openImportPicker } from "./ImportExport";

export function EmptyState() {
  const importError = useReviewStore((s) => s.importError);
  return (
    <div
      data-testid="empty-review"
      className="flex flex-col items-center justify-center gap-4 py-24 text-center"
    >
      <p className="text-gray-500 dark:text-gray-400">
        No data loaded — import a collection to review.
      </p>
      {importError !== null && (
        <p data-testid="import-error" className="max-w-lg text-sm text-status-warn">
          {importError}
        </p>
      )}
      <Button variant="primary" onPress={openImportPicker} testId="empty-import-btn">
        Import…
      </Button>
    </div>
  );
}
```

Rewrite `web/src/App.tsx`:

```tsx
// The redesigned shell (plan 2026-07-11). Review-first: no backend fetch
// on boot — the Import front door hydrates the review store. Live mode
// (SSE, /api/meta) returns at the live-hookup phase; the legacy live data
// layer (store.ts/api/sse.ts) is intentionally kept, unreferenced, for it.
import { EmptyState } from "./shell/EmptyState";
import { AppBar } from "./shell/AppBar";
import { ImportProvider } from "./shell/ImportExport";
import { useReviewStore } from "./data/reviewStore";

function App() {
  const hasData = useReviewStore((s) => s.sessions.length > 0);
  return (
    <ImportProvider>
      <AppBar />
      {hasData ? <LoadedBody /> : <EmptyState />}
    </ImportProvider>
  );
}

/** Placeholder body — Task 5 replaces this with the router + review bar. */
function LoadedBody() {
  const sourceName = useReviewStore((s) => s.sourceName);
  return (
    <main className="p-4 text-sm text-gray-500 dark:text-gray-400">
      Loaded {sourceName} — views land with routing (next task).
    </main>
  );
}

export default App;
```

Rewrite `web/src/main.tsx`:

```tsx
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import App from "./App";
import "./app.css";

const container = document.getElementById("root");
if (!container) {
  throw new Error("#root element not found");
}

createRoot(container).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
```

- [ ] **Step 3: Delete the legacy components and their coupled tests**

```bash
git rm web/src/components/ChartGrid.tsx web/src/components/ChartPanel.tsx \
  web/src/components/EventPopover.tsx web/src/components/EventTable.tsx \
  web/src/components/EventToolbar.tsx web/src/components/Header.tsx \
  web/src/components/TabBar.tsx web/src/dashboard.css \
  web/src/__tests__/app.test.tsx web/src/__tests__/eventtable.test.tsx
```

Keep everything else (Global Constraints list). Update `web/index.html`'s `<title>` to `otto monitor`.

- [ ] **Step 4: Write the shell test**

Create `web/src/__tests__/shell.test.tsx`:

```tsx
// New-shell behavior: empty state -> import -> loaded chrome; theme menu
// toggles the html class; import errors surface without losing data.
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { readFileSync } from "node:fs";
import { afterEach, describe, expect, it } from "vitest";

import App from "../App";
import { useReviewStore } from "../data/reviewStore";

const MINIMAL = readFileSync(new URL("../../fixtures/minimal.json", import.meta.url), "utf-8");

function resetStore() {
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

afterEach(() => {
  resetStore();
  localStorage.clear();
  document.documentElement.classList.remove("dark");
});

async function importMinimal() {
  const file = new File([MINIMAL], "minimal.json", { type: "application/json" });
  fireEvent.change(screen.getByTestId("import-input"), { target: { files: [file] } });
  await waitFor(() => expect(screen.getByTestId("status-text").textContent).toBe("Historical"));
}

describe("App shell", () => {
  it("boots to the empty review state with no backend fetches", () => {
    render(<App />);
    expect(screen.getByTestId("empty-review")).toBeTruthy();
    expect(screen.getByTestId("status-text").textContent).toBe("No data");
  });

  it("imports a fixture through the hidden input", async () => {
    render(<App />);
    await importMinimal();
    expect(screen.queryByTestId("empty-review")).toBeNull();
  });

  it("surfaces an import error and keeps prior data", async () => {
    render(<App />);
    await importMinimal();
    const bad = new File(["{}"], "bad.json", { type: "application/json" });
    fireEvent.change(screen.getByTestId("import-input"), { target: { files: [bad] } });
    await waitFor(() => expect(useReviewStore.getState().importError).not.toBeNull());
    expect(screen.getByTestId("status-text").textContent).toBe("Historical");
  });

  it("theme menu item toggles the html dark class and persists", async () => {
    render(<App />);
    const before = document.documentElement.classList.contains("dark");
    fireEvent.click(screen.getByTestId("overflow-menu"));
    fireEvent.click(screen.getByTestId("menu-theme"));
    expect(document.documentElement.classList.contains("dark")).toBe(!before);
    expect(localStorage.getItem("otto-theme")).toBe(before ? "light" : "dark");
  });
});
```

- [ ] **Step 5: Run the full web verification**

```bash
cd web && npm run test && npm run check:fix && npm run check && npm run typecheck && npm run build
```

Expected: vitest green (7 kept pure suites + ui + exportdoc + reviewstore + theme + shell); build green. If the coverage thresholds now fail in either direction, leave them for Task 7's recalibration — `npm run test` (no --coverage) is the per-task gate.

- [ ] **Step 6: Commit**

```bash
git add -A web/src web/index.html
git commit -m "feat(web)!: redesigned monitor shell — review-first, Import front door

New AppBar/EmptyState/ImportExport on the ui/ primitives + reviewStore;
theme v2 (prefers-color-scheme seed, html.dark class); legacy components,
dashboard.css and their coupled tests deleted. Legacy pure data layer kept
(unreferenced) for live hookup. Browser Playwright specs are known-broken
until the Task 6 pivot.

Assisted-by: Claude Fable 5"
```

(`git add -A web/src` is safe here — the deletions in Step 3 went through `git rm` and this stages the new/modified files; verify with `git status` that nothing outside web/src + web/index.html is staged.)

---

### Task 5: Routing + review bar + placeholder pages

**Files:**
- Create: `web/src/shell/ReviewBar.tsx`, `web/src/pages/OverviewPage.tsx`, `web/src/pages/SubjectPage.tsx`, `web/src/pages/TopologyPage.tsx`
- Modify: `web/src/App.tsx` (router + review bar replace `LoadedBody`)
- Test: `web/src/__tests__/reviewbar.test.tsx`, `web/src/__tests__/pages.test.tsx`

**Interfaces:**
- Consumes: Tasks 2/3/4.
- Produces: hash routes `/#/`, `/#/host/:id`, `/#/topology` (wouter + `useHashLocation` — zero server changes, refresh-stable, deep-linkable). ReviewBar per UX spec §12: `HISTORICAL` Badge + source name · session Select (only when >1 session) · range ToggleGroup (Full / Last 15m / Last 1h) + custom from/to datetime-local inputs + Apply · Reset.
- **data-testid contract added:** `review-bar`, `historical-tag`, `source-name`, `session-picker`, `range-presets`, `range-from`, `range-to`, `range-apply`, `range-reset`, `overview-page`, `element-section-<id>`, `subject-link-<id>`, `subject-page`, `subject-title`, `series-summary`, `topology-page`, `not-found`.
- Pages are **deliberately minimal scaffolds** (lists + counts, no charts/tiles) — Plan 3 replaces their bodies with the fleet grid and the synced chart stack; the routes, data wiring, and testids are what this task establishes.

- [ ] **Step 1: Write the failing tests**

Create `web/src/__tests__/reviewbar.test.tsx`:

```tsx
// Review-bar behavior against the drift fixture (3 sessions, evolving lab)
// — the config-drift acceptance path: switching sessions re-renders under
// THAT session's lab.
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { readFileSync } from "node:fs";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import App from "../App";
import { useReviewStore } from "../data/reviewStore";

const DRIFT = readFileSync(new URL("../../fixtures/drift.json", import.meta.url), "utf-8");
const MINIMAL = readFileSync(new URL("../../fixtures/minimal.json", import.meta.url), "utf-8");

function resetStore() {
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

beforeEach(() => {
  window.location.hash = "#/";
});
afterEach(resetStore);

async function importText(text: string, name: string) {
  const file = new File([text], name, { type: "application/json" });
  fireEvent.change(screen.getByTestId("import-input"), { target: { files: [file] } });
  await waitFor(() => expect(screen.getByTestId("review-bar")).toBeTruthy());
}

describe("ReviewBar", () => {
  it("shows tag + source, hides the session picker for single-session files", async () => {
    render(<App />);
    await importText(MINIMAL, "minimal.json");
    expect(screen.getByTestId("historical-tag").textContent).toBe("HISTORICAL");
    expect(screen.getByTestId("source-name").textContent).toBe("minimal.json");
    expect(screen.queryByTestId("session-picker")).toBeNull();
  });

  it("switches sessions and re-renders that session's lab (drift)", async () => {
    render(<App />);
    await importText(DRIFT, "drift.json");
    expect(screen.getByTestId("session-picker")).toBeTruthy();
    // baseline lab: no workers_w1
    expect(screen.queryByTestId("subject-link-workers_w1")).toBeNull();
    fireEvent.click(screen.getByTestId("session-picker"));
    fireEvent.click(screen.getByText("expanded"));
    await waitFor(() => expect(screen.getByTestId("subject-link-workers_w1")).toBeTruthy());
    expect(screen.getByTestId("subject-link-workers_w2")).toBeTruthy();
    fireEvent.click(screen.getByTestId("session-picker"));
    fireEvent.click(screen.getByText("rewired"));
    await waitFor(() => expect(screen.queryByTestId("subject-link-workers_w2")).toBeNull());
    expect(screen.getByTestId("subject-link-edge-gw")).toBeTruthy();
  });

  it("reset restores the first session and full range", async () => {
    render(<App />);
    await importText(DRIFT, "drift.json");
    fireEvent.click(screen.getByTestId("session-picker"));
    fireEvent.click(screen.getByText("rewired"));
    fireEvent.click(screen.getByTestId("range-reset"));
    await waitFor(() =>
      expect(useReviewStore.getState().activeSessionId).toBe(
        useReviewStore.getState().sessions[0].id,
      ),
    );
    expect(useReviewStore.getState().range).toBeNull();
  });
});
```

Create `web/src/__tests__/pages.test.tsx`:

```tsx
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { readFileSync } from "node:fs";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import App from "../App";
import { presetRange, sessionBounds } from "../data/exportDoc";
import { useReviewStore } from "../data/reviewStore";

const KITCHEN = readFileSync(
  new URL("../../fixtures/kitchen-sink.json", import.meta.url),
  "utf-8",
);

function resetStore() {
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

beforeEach(() => {
  window.location.hash = "#/";
});
afterEach(resetStore);

async function importKitchen() {
  const file = new File([KITCHEN], "kitchen-sink.json", { type: "application/json" });
  fireEvent.change(screen.getByTestId("import-input"), { target: { files: [file] } });
  await waitFor(() => expect(screen.getByTestId("overview-page")).toBeTruthy());
}

describe("Overview page", () => {
  it("renders element sections incl. the empty chassis", async () => {
    render(<App />);
    await importKitchen();
    expect(screen.getByTestId("element-section-chassis-a")).toBeTruthy();
    expect(screen.getByTestId("element-section-spare-chassis")).toBeTruthy();
    expect(screen.getByTestId("subject-link-chassis-a_lc1")).toBeTruthy();
  });
});

describe("Subject page", () => {
  it("navigates by hash and shows range-scoped series counts", async () => {
    render(<App />);
    await importKitchen();
    fireEvent.click(screen.getByTestId("subject-link-workers_w2"));
    await waitFor(() => expect(screen.getByTestId("subject-page")).toBeTruthy());
    expect(window.location.hash).toBe("#/host/workers_w2");
    expect(screen.getByTestId("subject-title").textContent).toContain("workers_w2");
    const fullText = screen.getByTestId("series-summary").textContent ?? "";

    const session = useReviewStore.getState().sessions[0];
    useReviewStore.getState().actions.setRange(presetRange(sessionBounds(session), 15));
    await waitFor(() => {
      expect(screen.getByTestId("series-summary").textContent).not.toBe(fullText);
    });
  });

  it("unknown subjects render not-found, empty store renders empty state", async () => {
    render(<App />);
    await importKitchen();
    window.location.hash = "#/host/nope";
    await waitFor(() => expect(screen.getByTestId("not-found")).toBeTruthy());
  });
});
```

- [ ] **Step 2: Run to verify failure**

Run: `cd web && npx vitest run src/__tests__/reviewbar.test.tsx src/__tests__/pages.test.tsx`
Expected: FAIL — no `review-bar` testid, no router.

- [ ] **Step 3: Implement**

`web/src/shell/ReviewBar.tsx`:

```tsx
// The historical review bar (UX spec §12): HISTORICAL tag + source ·
// session picker (only >1) · range presets + custom from-to · Reset.
import { useEffect, useState } from "react";

import { presetRange, sessionBounds } from "../data/exportDoc";
import { useActiveSession, useReviewStore } from "../data/reviewStore";
import { localInputToMs, msToLocalInput } from "../data/time";
import { Badge } from "../ui/Badge";
import { Button } from "../ui/Button";
import { Select } from "../ui/Select";
import { TextInput } from "../ui/TextInput";
import { ToggleGroup } from "../ui/ToggleGroup";

const PRESETS = [
  { id: "full", label: "Full", minutes: null },
  { id: "15m", label: "Last 15m", minutes: 15 },
  { id: "1h", label: "Last 1h", minutes: 60 },
] as const;

export function ReviewBar() {
  const sessions = useReviewStore((s) => s.sessions);
  const sourceName = useReviewStore((s) => s.sourceName);
  const activeSessionId = useReviewStore((s) => s.activeSessionId);
  const range = useReviewStore((s) => s.range);
  const { selectSession, setRange, resetView } = useReviewStore((s) => s.actions);
  const session = useActiveSession();

  const bounds = session ? sessionBounds(session) : null;
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");
  useEffect(() => {
    if (!bounds) return;
    setFrom(msToLocalInput(range?.from ?? bounds.from));
    setTo(msToLocalInput(range?.to ?? bounds.to));
  }, [range, session?.id]); // eslint-disable-line -- bounds derives from session

  if (!session || !bounds) return null;

  const activePreset =
    range === null
      ? "full"
      : (PRESETS.find(
          (p) =>
            p.minutes !== null &&
            presetRange(bounds, p.minutes)?.from === range.from &&
            presetRange(bounds, p.minutes)?.to === range.to,
        )?.id ?? "custom");

  const applyCustom = () => {
    const fromMs = localInputToMs(from);
    const toMs = localInputToMs(to);
    if (fromMs !== null && toMs !== null && fromMs < toMs) {
      setRange({ from: fromMs, to: toMs });
    }
  };

  return (
    <div
      data-testid="review-bar"
      className="flex flex-wrap items-center gap-3 border-b border-gray-200 px-4 py-2
        dark:border-gray-800"
    >
      <Badge tone="historical" testId="historical-tag">
        HISTORICAL
      </Badge>
      <span data-testid="source-name" className="text-sm text-gray-500 dark:text-gray-400">
        {sourceName}
      </span>
      {sessions.length > 1 && (
        <Select
          label="Session"
          items={sessions.map((s) => ({ id: s.id, label: s.label ?? s.id }))}
          selectedKey={activeSessionId ?? ""}
          onSelectionChange={selectSession}
          testId="session-picker"
        />
      )}
      <ToggleGroup
        label="Range"
        options={PRESETS.map((p) => ({ id: p.id, label: p.label }))}
        selectedId={activePreset}
        onSelect={(id) => {
          const preset = PRESETS.find((p) => p.id === id);
          if (preset) setRange(presetRange(bounds, preset.minutes));
        }}
        testId="range-presets"
      />
      <TextInput label="From" type="datetime-local" value={from} onChange={setFrom} testId="range-from" />
      <TextInput label="To" type="datetime-local" value={to} onChange={setTo} testId="range-to" />
      <Button onPress={applyCustom} testId="range-apply">
        Apply
      </Button>
      <Button variant="ghost" onPress={resetView} testId="range-reset">
        Reset
      </Button>
    </div>
  );
}
```

(If biome's `useExhaustiveDependencies` rejects the effect's suppression comment form, restructure per its suggestion — e.g. include `bounds?.from`/`bounds?.to` in the deps — and note it in the report; the behavior contract is the tests.)

`web/src/pages/OverviewPage.tsx`:

```tsx
// SCAFFOLD (Plan 3 replaces this body with the fleet grid): element
// sections + plain subject links prove the data wiring, routing and the
// per-session lab rendering end-to-end.
import { Link } from "wouter";

import { useActiveSession } from "../data/reviewStore";

export function OverviewPage() {
  const session = useActiveSession();
  if (!session) return null;
  return (
    <main data-testid="overview-page" className="flex flex-col gap-6 p-4">
      {session.elements.map((el) => (
        <section key={el.id} data-testid={`element-section-${el.id}`}>
          <h2 className="mb-2 flex items-center gap-2 text-sm font-semibold">
            <span aria-hidden>{el.type === "physical" ? "▦" : "▤"}</span>
            {el.id}
            <span className="font-normal text-gray-400">
              {el.hostIds.length} host{el.hostIds.length === 1 ? "" : "s"}
              {el.description ? ` · ${el.description}` : ""}
            </span>
          </h2>
          <ul className="flex flex-wrap gap-2">
            {el.hostIds.map((hostId) => (
              <li key={hostId}>
                <Link
                  href={`/host/${hostId}`}
                  data-testid={`subject-link-${hostId}`}
                  className="inline-block rounded-lg border border-gray-200 px-3 py-2 text-sm
                    hover:border-brand-500 dark:border-gray-800 dark:hover:border-brand-500"
                >
                  {hostId}
                </Link>
              </li>
            ))}
            {el.hostIds.length === 0 && (
              <li className="text-sm text-gray-400">empty — no hosts fitted</li>
            )}
          </ul>
        </section>
      ))}
    </main>
  );
}
```

`web/src/pages/SubjectPage.tsx`:

```tsx
// SCAFFOLD (Plan 3 replaces this with the synced chart stack): proves
// subject resolution, deep links and range-scoped data selection.
import { Link, useParams } from "wouter";

import { metricsForSubject, subjectKind } from "../data/exportDoc";
import { useActiveSession, useReviewStore } from "../data/reviewStore";

export function SubjectPage() {
  const params = useParams<{ id: string }>();
  const session = useActiveSession();
  const range = useReviewStore((s) => s.range);
  if (!session) return null;

  const id = params.id;
  const kind = subjectKind(session, id);
  if (kind === null) {
    return (
      <main data-testid="not-found" className="p-4 text-sm text-gray-500">
        Unknown subject “{id}” in this session. <Link href="/">Back to overview</Link>
      </main>
    );
  }

  const host = session.lab.hosts.find((h) => h.id === id);
  const metrics = metricsForSubject(session, id, range);
  const labels = [...new Set(metrics.map((m) => m.label))].sort();

  return (
    <main data-testid="subject-page" className="flex flex-col gap-4 p-4">
      <nav className="text-sm text-gray-400">
        <Link href="/">Fleet</Link> / {id}
      </nav>
      <h1 data-testid="subject-title" className="flex items-center gap-2 text-lg font-semibold">
        {id}
        <span className="text-sm font-normal text-gray-400">
          {kind}
          {host?.board ? ` · ${host.board}` : ""}
          {host?.slot != null ? ` · slot ${host.slot}` : ""}
          {host?.hop ? ` · via ${host.hop}` : ""}
        </span>
      </h1>
      <p data-testid="series-summary" className="text-sm text-gray-500 dark:text-gray-400">
        {labels.length} series · {metrics.length} samples in range
      </p>
      <ul className="text-sm text-gray-600 dark:text-gray-300">
        {labels.map((label) => (
          <li key={label}>
            {label} ({metrics.filter((m) => m.label === label).length})
          </li>
        ))}
      </ul>
    </main>
  );
}
```

`web/src/pages/TopologyPage.tsx`:

```tsx
export function TopologyPage() {
  return (
    <main data-testid="topology-page" className="p-4 text-sm text-gray-500">
      Topology view lands in a later plan (UX spec §10).
    </main>
  );
}
```

Rewrite `web/src/App.tsx`'s body (replace `LoadedBody`):

```tsx
import { Route, Router, Switch } from "wouter";
import { useHashLocation } from "wouter/use-hash-location";

import { useReviewStore } from "./data/reviewStore";
import { OverviewPage } from "./pages/OverviewPage";
import { SubjectPage } from "./pages/SubjectPage";
import { TopologyPage } from "./pages/TopologyPage";
import { AppBar } from "./shell/AppBar";
import { EmptyState } from "./shell/EmptyState";
import { ImportProvider } from "./shell/ImportExport";
import { ReviewBar } from "./shell/ReviewBar";

function App() {
  const hasData = useReviewStore((s) => s.sessions.length > 0);
  return (
    <ImportProvider>
      <AppBar />
      {hasData ? (
        <Router hook={useHashLocation}>
          <ReviewBar />
          <Switch>
            <Route path="/" component={OverviewPage} />
            <Route path="/host/:id" component={SubjectPage} />
            <Route path="/topology" component={TopologyPage} />
            <Route>
              <main data-testid="not-found" className="p-4 text-sm text-gray-500">
                Not found.
              </main>
            </Route>
          </Switch>
        </Router>
      ) : (
        <EmptyState />
      )}
    </ImportProvider>
  );
}

export default App;
```

- [ ] **Step 4: Run tests, lint, typecheck, build**

```bash
cd web && npm run test && npm run check:fix && npm run check && npm run typecheck && npm run build
```

Expected: all green.

- [ ] **Step 5: Commit**

```bash
git add web/src/shell/ReviewBar.tsx web/src/pages web/src/App.tsx \
  web/src/__tests__/reviewbar.test.tsx web/src/__tests__/pages.test.tsx
git commit -m "feat(web): hash routing + review bar + scaffold pages

wouter hash routes (/, /host/:id, /topology); HISTORICAL tag · session
picker · range presets/custom · Reset per UX spec §12; overview/subject
scaffolds prove per-session lab rendering (config drift) and range-scoped
selection.

Assisted-by: Claude Fable 5"
```

---

### Task 6: Playwright pivot — behavior specs on the testid contract

**Files:**
- Delete: `tests/e2e/monitor/dashboard/test_dashboard_live.py`, `test_dashboard_events.py`, `test_dashboard_table.py`, `test_dashboard_regressions.py`, `test_dashboard_historical.py`, `tests/e2e/monitor/dashboard/data/historical.json`
- Modify: `tests/e2e/monitor/dashboard/conftest.py` (prune fixtures the deleted specs used; keep the harness/guard/timeout plumbing and anything `test_harness.py` uses)
- Create: `tests/e2e/monitor/dashboard/test_review_shell.py`
- Untouched: `tests/e2e/monitor/dashboard/test_harness.py` (verify at the end: `git diff --name-only` must not list it)

> **Amended 2026-07-11 (as delivered):** `data/historical.json` was NOT
> deleted — the Delete line above was a plan error. `test_harness.py`
> (untouchable) consumes the `live_dash` and `historical_dash` fixtures
> directly (12 of its tests take them as parameters; it does not build its
> own collectors), and `historical_dash` reads `data/historical.json`.
> Delivered prune: `table_dash`/`historical_table_dash`/`_preload_table`/
> `_table_parser`/`SYSLOG_PATTERN` removed (zero consumers); `live_dash`/
> `historical_dash`/`_preload`/`HISTORICAL_JSON` kept.

**Interfaces:**
- Consumes: the Task 4/5 `data-testid` contract; `web/fixtures/*.json` (repo-root-relative); `DashboardHarness` + `FakeCollector` (`tests/_fixtures/`) — the server still serves the dist; the new UI simply makes no API calls on boot.
- Produces: the new browser lane — `make dashboard` green again. Markers preserved exactly: `pytest.mark.hostless`, `pytest.mark.browser`, `pytest.mark.xdist_group("dashboard")` at module level (the Makefile/nox/CI invocations select on these and stay unchanged).

- [ ] **Step 1: Prune the old specs and conftest**

Delete the five spec files + `data/historical.json` via `git rm`. In `conftest.py`, remove fixtures and helpers that now have no consumers (`live_dash`, `table_dash`, `historical_dash`, `historical_table_dash`, `_preload*`, syslog-parser plumbing) — check each against `test_harness.py` before removing (it builds its own collectors; expect the prune to be clean, but verify with grep). Keep: the browser-guard `pytest_configure`, `_run_isolated`, `_generous_playwright_timeout`, and add:

```python
@pytest.fixture
def shell_dash():
    """A dist-serving harness with an empty collector — the review shell
    makes no boot-time API calls; data arrives via client-side Import."""
    harness = DashboardHarness(FakeCollector())
    harness.start()
    yield harness
    harness.stop()
```

(Match the exact start/stop idiom the deleted fixtures used — copy it from the current `live_dash` before deleting.)

- [ ] **Step 2: Write the new spec**

Create `tests/e2e/monitor/dashboard/test_review_shell.py`:

```python
"""Behavior specs for the redesigned review shell (plan 2026-07-11).

Contract: data-testid attributes only — styling and DOM structure are
free to change. Fixtures are the committed Plan-1 dummy-data documents
(web/fixtures/), imported through the client-side Import front door, so
every test here runs with zero backend data and zero external network.
"""

import json
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.hostless,
    pytest.mark.browser,
    pytest.mark.xdist_group("dashboard"),
]

FIXTURES = Path(__file__).resolve().parents[4] / "web" / "fixtures"


def _import_fixture(page, name: str) -> None:
    page.locator('[data-testid="import-input"]').set_input_files(FIXTURES / name)
    page.locator('[data-testid="review-bar"]').wait_for()


def test_empty_state_then_import(page, shell_dash):
    page.goto(shell_dash.url)
    page.locator('[data-testid="empty-review"]').wait_for()
    assert page.locator('[data-testid="status-text"]').inner_text() == "No data"
    _import_fixture(page, "kitchen-sink.json")
    assert page.locator('[data-testid="status-text"]').inner_text() == "Historical"
    assert page.locator('[data-testid="historical-tag"]').inner_text() == "HISTORICAL"
    page.locator('[data-testid="element-section-chassis-a"]').wait_for()
    page.locator('[data-testid="element-section-spare-chassis"]').wait_for()


def test_renders_fully_offline(page, shell_dash):
    """Air-gap runtime pin (successor of the deleted regression test):
    the shell + import + overview render with every non-local request and
    every WebSocket blocked."""
    blocked: list[str] = []

    def block(route):
        url = route.request.url
        if "127.0.0.1" in url or "localhost" in url:
            route.continue_()
        else:
            blocked.append(url)
            route.abort()

    page.route("**/*", block)
    page.route_web_socket("**/*", lambda ws: blocked.append(ws.url))
    page.goto(shell_dash.url)
    _import_fixture(page, "kitchen-sink.json")
    page.locator('[data-testid="subject-link-chassis-a_lc1"]').wait_for()
    assert blocked == []


def test_drift_session_picker_rerenders_lab(page, shell_dash):
    """The config-drift acceptance path (spec 2026-07-10 §1): each session
    renders under the lab config as it was at run time."""
    page.goto(shell_dash.url)
    _import_fixture(page, "drift.json")
    picker = page.locator('[data-testid="session-picker"]')
    picker.wait_for()
    assert page.locator('[data-testid="subject-link-workers_w2"]').count() == 0

    picker.click()
    page.get_by_text("expanded", exact=True).click()
    page.locator('[data-testid="subject-link-workers_w2"]').wait_for()

    picker.click()
    page.get_by_text("rewired", exact=True).click()
    page.locator('[data-testid="subject-link-edge-gw"]').wait_for()
    assert page.locator('[data-testid="subject-link-workers_w2"]').count() == 0


def test_single_session_hides_picker(page, shell_dash):
    page.goto(shell_dash.url)
    _import_fixture(page, "minimal.json")
    assert page.locator('[data-testid="session-picker"]').count() == 0


def test_range_presets_change_subject_summary(page, shell_dash):
    page.goto(shell_dash.url)
    _import_fixture(page, "kitchen-sink.json")
    page.locator('[data-testid="subject-link-workers_w1"]').click()
    page.locator('[data-testid="subject-page"]').wait_for()
    full = page.locator('[data-testid="series-summary"]').inner_text()

    page.get_by_role("radio", name="Last 15m").click()
    page.wait_for_function(
        "(prev) => document.querySelector('[data-testid=\"series-summary\"]')"
        ".innerText !== prev",
        arg=full,
    )
    page.locator('[data-testid="range-reset"]').click()
    # Reset returns to the overview state: first session + full range.
    page.wait_for_function(
        "(prev) => document.querySelector('[data-testid=\"series-summary\"]') === null"
        " || document.querySelector('[data-testid=\"series-summary\"]').innerText === prev",
        arg=full,
    )


def test_deep_link_and_reload(page, shell_dash):
    page.goto(shell_dash.url)
    _import_fixture(page, "kitchen-sink.json")
    page.locator('[data-testid="subject-link-db-01"]').click()
    page.locator('[data-testid="subject-page"]').wait_for()
    assert page.url.endswith("#/host/db-01")
    # Imported data is in-memory only: reload keeps the hash but shows the
    # empty state (honest current behavior — persistence is a later call).
    page.reload()
    page.locator('[data-testid="empty-review"]').wait_for()
    assert page.url.endswith("#/host/db-01")


def test_theme_toggle_persists_across_reload(page, shell_dash):
    page.goto(shell_dash.url)
    page.locator('[data-testid="overflow-menu"]').click()
    before = page.evaluate("document.documentElement.classList.contains('dark')")
    page.locator('[data-testid="menu-theme"]').click()
    assert page.evaluate("document.documentElement.classList.contains('dark')") is not before
    page.reload()
    assert page.evaluate("document.documentElement.classList.contains('dark')") is not before


def test_export_downloads_loaded_set(page, shell_dash):
    page.goto(shell_dash.url)
    _import_fixture(page, "minimal.json")
    page.locator('[data-testid="overflow-menu"]').click()
    with page.expect_download() as download_info:
        page.locator('[data-testid="menu-export"]').click()
    path = download_info.value.path()
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    assert doc["format"] == 1
    assert len(doc["sessions"]) == 1
```

> **Amended 2026-07-11 (as delivered):** two selector adaptations were
> needed against react-aria's rendered DOM — the session-picker items are
> targeted via `get_by_role("option", ...)` (the hidden native `<select>`
> duplicates the item text) and the range presets via `get_by_text(...)`
> scoped to `[data-testid="range-presets"]` (the clip-rect'd native radio
> input is click-intercepted by its own label). Additionally, review
> surfaced three behavior gaps in the list above; these specs were added
> post-delivery (`import` also gains `from datetime import datetime,
> timedelta`):
>
> - `test_custom_range_apply_and_reset` — custom from/to window (UX spec
>   §12): Apply narrows the subject's range-scoped selection, Reset
>   restores the full range.
> - `test_deep_link_back_forward` — browser back/forward walk the hash
>   history; the in-memory import survives (same-document navigations).
> - `test_not_found_routes` — both not-found render sites (router-level
>   catch-all and SubjectPage's unknown-subject branch) keep the chrome.

```python
def test_custom_range_apply_and_reset(page, shell_dash):
    """Custom from/to window (UX spec §12): Apply narrows the subject's
    range-scoped selection; Reset restores the full range."""
    page.goto(shell_dash.url)
    _import_fixture(page, "kitchen-sink.json")
    page.locator('[data-testid="subject-link-workers_w1"]').click()
    page.locator('[data-testid="subject-page"]').wait_for()
    full = page.locator('[data-testid="series-summary"]').inner_text()

    # datetime-local inputs hold LOCAL time (msToLocalInput), so derive the
    # narrow window from the pre-populated session-start value instead of
    # hardcoding strings off the fixture's UTC timestamps — on a non-UTC
    # host a UTC-derived window would land outside the session entirely.
    start_local = page.locator('[data-testid="range-from"]').input_value()
    # DTZ007 suppressed: deliberately naive — a datetime-local value is
    # wall-clock text with no timezone; it round-trips into the same input.
    t0 = datetime.strptime(start_local, "%Y-%m-%dT%H:%M")  # noqa: DTZ007
    page.locator('[data-testid="range-from"]').fill(
        (t0 + timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M")
    )
    page.locator('[data-testid="range-to"]').fill(
        (t0 + timedelta(minutes=20)).strftime("%Y-%m-%dT%H:%M")
    )
    page.locator('[data-testid="range-apply"]').click()
    page.wait_for_function(
        "(prev) => document.querySelector('[data-testid=\"series-summary\"]').innerText !== prev",
        arg=full,
    )
    page.locator('[data-testid="range-reset"]').click()
    page.wait_for_function(
        "(prev) => document.querySelector('[data-testid=\"series-summary\"]').innerText === prev",
        arg=full,
    )


def test_deep_link_back_forward(page, shell_dash):
    """Browser back/forward walk the hash history (UX spec: back/forward
    must work) — wouter's useHashLocation reacts to popstate/hashchange,
    and the in-memory import survives since these are same-document navs."""
    page.goto(shell_dash.url)
    _import_fixture(page, "kitchen-sink.json")
    page.locator('[data-testid="subject-link-db-01"]').click()
    page.locator('[data-testid="subject-page"]').wait_for()
    assert "db-01" in page.locator('[data-testid="subject-title"]').inner_text()

    page.go_back()
    page.locator('[data-testid="overview-page"]').wait_for()

    page.go_forward()
    page.locator('[data-testid="subject-page"]').wait_for()
    assert "db-01" in page.locator('[data-testid="subject-title"]').inner_text()


def test_not_found_routes(page, shell_dash):
    """Both not-found render sites (Task 5 ledger: there are two) keep the
    shell chrome. Same-document hash navigations, so the import survives."""
    page.goto(shell_dash.url)
    _import_fixture(page, "minimal.json")

    # Site 1: no route matches at all -> the router-level fallback
    # (App.tsx's Switch catch-all Route).
    page.goto(shell_dash.url + "/#/bogus")
    page.locator('[data-testid="not-found"]').wait_for()

    # Site 2: /host/:id matches but the id is unknown in this session ->
    # SubjectPage's own unknown-subject branch. The review bar staying
    # visible proves this render site keeps the chrome too.
    page.goto(shell_dash.url + "/#/host/ghost")
    page.locator('[data-testid="not-found"]').wait_for()
    assert page.locator('[data-testid="review-bar"]').is_visible()
```

- [ ] **Step 3: Build the dist and run the lane**

```bash
make web            # fresh dist with the new shell (also drift + air-gap gates)
make dashboard      # chromium behavior lane + test_harness wire pins
```

Expected: green. Debug selectors/timing here, not in Task 7. If a `wait_for_function` proves flaky, prefer asserting on a concrete post-state text instead — note any spec change in the report.

- [ ] **Step 4: Python lint + verify the untouchables**

```bash
uv run ruff format tests/e2e/monitor/dashboard/ && uv run ruff check tests/e2e/monitor/dashboard/
git diff --name-only | grep -v "web/\|conftest\|test_review_shell" ; git status --short
```

Expected: `test_harness.py` untouched; only the intended files changed.

- [ ] **Step 5: Commit**

```bash
git add tests/e2e/monitor/dashboard/
git commit -m "test(dashboard)!: behavior specs on the data-testid contract

DOM-parity specs (live/events/table/regressions/historical) retire with
the legacy UI (UX spec §16); the new lane pins import/review behavior:
offline air-gap render, drift session switching, range presets, theme
persistence, deep links, export download. test_harness wire pins
untouched.

Assisted-by: Claude Fable 5"
```

---

### Task 7: Gates + threshold recalibration

**Files:** possibly `web/vite.config.ts` (coverage thresholds) — otherwise verification only, fix-forward.

- [ ] **Step 1: Web quality gate**

```bash
make web-lint && make web-format-check && make web-typecheck && make web-coverage
```

If `web-coverage` fails its thresholds (the wipe removed big partially-covered files, so coverage likely MOVED — either direction): re-ratchet the four thresholds in `web/vite.config.ts` to ~2-3% below the new measured values, updating the comment above them with the new baseline numbers (same convention as the existing comment). Commit separately: `test(web): re-ratchet vitest coverage floor post-redesign`.

- [ ] **Step 2: Full web build + browser lane (again, clean)**

```bash
make web && make dashboard
```

Expected: drift gates, both vite builds, both air-gap greps, browser lane — all green.

- [ ] **Step 3: Python hostless gate**

```bash
make coverage-hostless
```

Expected: green — Python source untouched; only the dashboard-lane test files changed. (`test_harness.py` runs here via the hostless marker.)

- [ ] **Step 4: Lint/typecheck**

```bash
uv run nox -s lint typecheck
```

Expected: green (typecheck sees zero src changes; lint covers the new test file).

- [ ] **Step 5: Commit any gate fixes**

Each fix its own conventional commit, same trailer convention. No docs run needed: zero `src/otto/` changes (state this in the task report).

---

## Self-review notes (done at authoring time)

- **Spec coverage:** UX spec §7 chrome (brand/status/⋯ menu/theme) → Tasks 2/4; §12 review mode (Import client-side, review bar, session picker >1, range presets+custom, Reset) → Tasks 3/4/5; §13 states (empty-review, import CTA) → Task 4 (loading skeletons + disconnected are live-mode states — later phase); §15 keep/evolve/replace split honored (legacy data layer kept verbatim); §16 testing pivot (parity harness retired, behavior E2E, vitest for pure logic, air-gap gates) → Task 6/7. Data spec §8 build order steps 2 (scaffold) fully, step 3 partially (scaffold pages; grid/charts are Plan 3); §9 air-gap → font vendored, offline test rebuilt. `?fixture=` dev auto-import (data spec §8.5) deliberately DEFERRED to Plan 3 — Import-via-menu covers the dev loop until charts exist to iterate on.
- **Pause button** (§7) is live-only — correctly absent from the review-mode chrome.
- **Type consistency:** `NormalizedSession`/`TimeRange`/`presetRange`/`metricsForSubject` names match across Tasks 3/5 code and tests; testids in Tasks 4/5 components match Task 6 selectors one-for-one (checked by grep of this document).
- **Placeholder scan:** the scaffold pages are explicitly scoped as Plan-3-replaceable, with their data wiring (not their markup) as the deliverable; no TBDs remain.
- **Known risk, stated:** exact `react-aria-components`/Tailwind v4 API details may drift from this plan's code (both evolve quickly). The tests are the contract; implementers adapt minimally and report deviations — same protocol that worked for Plan 1's ruff adjustments.

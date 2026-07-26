// Shared chrome for every covapp page (Task 3 brief). DOM/anatomy
// reference: docs/superpowers/specs/assets/2026-07-24-coverage-ui/
// file-page.html lines 178-197 (⋮ menu with keyboard-shortcuts item +
// coverage-key sections) — recreated with React + Tailwind semantic tokens,
// not the mockup's literal CSS. Menu usage pattern mirrors
// web/src/shell/AppBar.tsx (Dropdown.Root > ButtonUtility trigger >
// Dropdown.Popover > Dropdown.Menu > Dropdown.Section/Item/Separator).
//
// AppShell reads tier/state legend data + project_name straight from
// getIndex() rather than via props: App.tsx only ever mounts AppShell once
// dataGuard() === "ok", so a non-null payload is always available here, and
// every page gluing AppShell in would otherwise have to thread the same
// getIndex() read through as props for no benefit.
import { Check, Command, DotsVertical, Moon01, Sun } from "@untitledui/icons";
import { type ReactNode, useEffect, useState } from "react";

import { ButtonUtility } from "@/components/base/buttons/button-utility";
import { Dropdown } from "@/components/base/dropdown/dropdown";
import { loadTheme, saveTheme, type Theme } from "@/theme";
import { cx } from "@/utils/cx";
import { Breadcrumbs, type Crumb } from "../../ui/Breadcrumbs";
import { groupContexts } from "../contexts";
import { getIndex } from "../data";
import { useFocus } from "../focus";
import { ShortcutsDialog } from "./ShortcutsDialog";
import { StatsCard, type StatsCardProps } from "./StatsCard";

export interface AppShellProps {
  crumbs: Crumb[];
  title: ReactNode;
  meta: ReactNode;
  stats: StatsCardProps | null;
  children: ReactNode;
}

/** Informational, non-actionable menu row (tier/state/branch legend swatch
 * + label). Dropdown's Item is the only content-bearing element `Menu`'s
 * collection API accepts as a child (a bare `<div>` isn't a recognized
 * collection node) — so key rows render as `isDisabled` Items instead of a
 * dedicated "static row" component, which Dropdown doesn't expose. Passing
 * no `label` prop makes DropdownItem fall through to rendering `children`
 * (see dropdown.tsx), which is how the swatch gets in there at all.
 *
 * `className="opacity-100 cursor-default"` (Task 7, ledger-sanctioned
 * adjacent fix): the vendored `DropdownItem` applies
 * `state.isDisabled && "cursor-not-allowed opacity-50"` ahead of any
 * caller `className` in its own `cx()` call (see dropdown.tsx) — `cx` is
 * `tailwind-merge`, which keeps the LAST conflicting utility in a class
 * list, so passing these two here wins over the vendored disabled
 * treatment and restores full-opacity swatches for what are informational
 * rows, not actually-disabled actions. */
function KeyRow({ id, color, children }: { id: string; color?: string; children: ReactNode }) {
  return (
    <Dropdown.Item id={id} isDisabled textValue={id} className="opacity-100 cursor-default">
      <span className="flex items-center gap-2">
        {color && (
          <span
            aria-hidden
            className="inline-block size-2 shrink-0 rounded-sm"
            style={{ backgroundColor: color }}
          />
        )}
        <span className="text-sm font-medium text-secondary">{children}</span>
      </span>
    </Dropdown.Item>
  );
}

/** App-bar pin (Task 7 spec §4, `contexts-page.html`'s `.focus-chip`
 * anatomy): tier dot + label + ✕. Brand-tinted (`bg-brand-primary_alt`/
 * `border-fg-brand-primary_alt`/`text-brand-secondary`) — the same violet
 * tokens `RunsPage.tsx`'s active tier chip and "Focus this context" button
 * already use, so this reads as one consistent "brand pin" treatment
 * rather than a one-off. */
function FocusChip({
  label,
  tierColor,
  onClear,
}: {
  label: string;
  tierColor?: string;
  onClear: () => void;
}) {
  return (
    <span
      data-testid="focus-chip"
      className="inline-flex max-w-52 items-center gap-1.5 rounded-full border
        border-fg-brand-primary_alt bg-brand-primary_alt px-2.5 py-1 text-xs font-medium
        text-brand-secondary"
    >
      <span
        aria-hidden
        className={cx("size-2 shrink-0 rounded-sm", !tierColor && "bg-fg-quaternary")}
        style={tierColor ? { backgroundColor: tierColor } : undefined}
      />
      <span className="truncate">{label}</span>
      <button
        type="button"
        data-testid="focus-clear"
        aria-label="Clear focus"
        title="Clear focus"
        onClick={onClear}
        className="shrink-0 opacity-70 outline-none hover:opacity-100"
      >
        ✕
      </button>
    </span>
  );
}

/** One row of the ⋮ menu's "Focus context" section — "All contexts" (no
 * `dotColor`, per `contexts-page.html`'s `buildMenuFocus`: even that row
 * gets a (neutral) dot, mirrored here via `bg-fg-quaternary`) or a
 * per-context row, ✓ marking whichever is active. A real `onAction`
 * (Dropdown.Item's activation handler), not a raw `onClick` — same as
 * every other actionable item in this menu. */
function FocusMenuItem({
  active,
  dotColor,
  label,
  testId,
  onAction,
}: {
  active: boolean;
  dotColor?: string;
  label: string;
  testId: string;
  onAction: () => void;
}) {
  return (
    <Dropdown.Item id={testId} textValue={label} onAction={onAction} data-testid={testId}>
      <span className="flex w-full min-w-0 items-center gap-2">
        <span
          aria-hidden
          className={cx("size-2 shrink-0 rounded-sm", !dotColor && "bg-fg-quaternary")}
          style={dotColor ? { backgroundColor: dotColor } : undefined}
        />
        <span className="truncate text-sm font-medium text-secondary">{label}</span>
        {active && <Check aria-hidden className="ml-auto size-4 shrink-0 text-fg-brand-primary" />}
      </span>
    </Dropdown.Item>
  );
}

function BranchPill({ tone }: { tone: "high" | "low" | "na" }) {
  return (
    <span
      className={
        tone === "high"
          ? "rounded px-1 py-0.5 font-mono text-xs font-semibold text-success-primary"
          : tone === "low"
            ? "rounded px-1 py-0.5 font-mono text-xs font-semibold text-error-primary"
            : "rounded px-1 py-0.5 font-mono text-xs font-semibold text-quaternary line-through"
      }
    >
      B{tone === "high" ? 0 : tone === "low" ? 1 : 2}
    </span>
  );
}

export function AppShell({ crumbs, title, meta, stats, children }: AppShellProps) {
  const index = getIndex();
  const { focus, setFocus } = useFocus();
  const [theme, setTheme] = useState<Theme>(() => loadTheme());
  const [shortcutsOpen, setShortcutsOpen] = useState(false);

  // Wired here (not ShortcutsDialog) because it's the app-wide "?" binding,
  // not something scoped to the dialog itself.
  useEffect(() => {
    function onKeyDown(event: KeyboardEvent) {
      if (event.key !== "?") return;
      const target = event.target as HTMLElement | null;
      const tag = target?.tagName;
      if (tag === "INPUT" || tag === "TEXTAREA" || target?.isContentEditable) return;
      setShortcutsOpen(true);
    }
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, []);

  function toggleTheme() {
    const next: Theme = theme === "dark" ? "light" : "dark";
    saveTheme(next); // applies the .dark-mode class as a side effect
    setTheme(next);
  }

  const tierOrder = index?.tier_order ?? [];
  const tierLabels = index?.tier_labels ?? {};
  const tierColors = index?.tier_colors ?? {};
  const stateColors = index?.state_colors ?? {};

  // Contexts for the chip's tier dot + the ⋮ menu's "Focus context"
  // switcher — derived here (not read off `focus`, which is only ever a
  // label string) via the same `groupContexts` every other page uses.
  const contexts = index ? groupContexts(index) : [];
  const focusedContext = contexts.find((ctx) => ctx.label === focus) ?? null;

  return (
    <div className="flex min-h-screen flex-col">
      <header
        data-testid="app-bar"
        className="flex h-12 shrink-0 items-center justify-between gap-3 border-b border-secondary px-4"
      >
        <div
          data-testid="brand"
          className="flex items-center gap-2 text-sm font-semibold text-secondary"
        >
          <span aria-hidden className="text-brand-500">
            ⬡
          </span>
          <span>otto coverage</span>
          {index && <span className="text-quaternary">· {index.project_name}</span>}
        </div>
        <div className="flex items-center gap-2">
          {focus !== null && (
            <FocusChip
              label={focus}
              tierColor={focusedContext ? tierColors[focusedContext.tier] : undefined}
              onClear={() => setFocus(null)}
            />
          )}
          <ButtonUtility
            aria-label={theme === "dark" ? "Switch to light mode" : "Switch to dark mode"}
            tooltip={theme === "dark" ? "Switch to light mode" : "Switch to dark mode"}
            data-testid="theme-toggle"
            icon={theme === "dark" ? Sun : Moon01}
            color="tertiary"
            size="sm"
            onClick={toggleTheme}
          />
          <Dropdown.Root>
            <ButtonUtility
              aria-label="More actions"
              data-testid="appbar-menu"
              icon={DotsVertical}
              color="tertiary"
              size="sm"
            />
            <Dropdown.Popover className="w-72">
              <Dropdown.Menu>
                <Dropdown.Section>
                  <Dropdown.Item
                    id="shortcuts"
                    label="Keyboard shortcuts"
                    icon={Command}
                    onAction={() => setShortcutsOpen(true)}
                    data-testid="menu-shortcuts"
                  />
                </Dropdown.Section>
                <Dropdown.Separator />
                <Dropdown.Section>
                  <Dropdown.SectionHeader className="px-2.5 pt-2 pb-1 text-xs font-medium text-quaternary">
                    Focus context
                  </Dropdown.SectionHeader>
                  <FocusMenuItem
                    active={focus === null}
                    label="All contexts"
                    testId="menu-focus-all"
                    onAction={() => setFocus(null)}
                  />
                  {contexts.map((ctx) => (
                    <FocusMenuItem
                      key={ctx.label}
                      active={focus === ctx.label}
                      dotColor={tierColors[ctx.tier]}
                      label={ctx.label}
                      testId={`menu-focus-${ctx.label}`}
                      onAction={() => setFocus(ctx.label)}
                    />
                  ))}
                </Dropdown.Section>
                <Dropdown.Separator />
                {tierOrder.length > 0 && (
                  <Dropdown.Section>
                    <Dropdown.SectionHeader className="px-2.5 pt-2 pb-1 text-xs font-medium text-quaternary">
                      Coverage key — tiers
                    </Dropdown.SectionHeader>
                    {tierOrder.map((tier) => (
                      <KeyRow key={tier} id={`key-tier-${tier}`} color={tierColors[tier]}>
                        {tierLabels[tier] ?? tier}
                      </KeyRow>
                    ))}
                  </Dropdown.Section>
                )}
                <Dropdown.Section>
                  <Dropdown.SectionHeader className="px-2.5 pt-2 pb-1 text-xs font-medium text-quaternary">
                    States
                  </Dropdown.SectionHeader>
                  <KeyRow id="key-state-uncovered" color={stateColors.uncovered}>
                    uncovered
                  </KeyRow>
                  <KeyRow id="key-state-excluded" color={stateColors.excluded}>
                    excluded
                  </KeyRow>
                  <KeyRow id="key-state-stale" color={stateColors.stale}>
                    stale (revoked)
                  </KeyRow>
                  <KeyRow id="key-state-aging" color={stateColors.aging}>
                    aging
                  </KeyRow>
                </Dropdown.Section>
                <Dropdown.Section>
                  <Dropdown.SectionHeader className="px-2.5 pt-2 pb-1 text-xs font-medium text-quaternary">
                    Branches
                  </Dropdown.SectionHeader>
                  <KeyRow id="key-branch-taken">
                    <BranchPill tone="high" /> taken
                  </KeyRow>
                  <KeyRow id="key-branch-not-taken">
                    <BranchPill tone="low" /> not taken
                  </KeyRow>
                  <KeyRow id="key-branch-unreachable">
                    <BranchPill tone="na" /> unreachable
                  </KeyRow>
                </Dropdown.Section>
              </Dropdown.Menu>
            </Dropdown.Popover>
          </Dropdown.Root>
        </div>
      </header>

      <div className="flex flex-1 flex-col gap-4 p-4">
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div className="min-w-0">
            <Breadcrumbs items={crumbs} />
            <h1 className="mt-2 text-lg font-semibold text-primary">{title}</h1>
            <div data-testid="page-meta" className="mt-1 text-sm text-tertiary">
              {meta}
            </div>
          </div>
          {stats && <StatsCard {...stats} />}
        </div>
        {children}
      </div>

      <ShortcutsDialog isOpen={shortcutsOpen} onClose={() => setShortcutsOpen(false)} />
    </div>
  );
}

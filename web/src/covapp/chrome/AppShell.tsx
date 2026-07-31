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
import type { IndexPayload } from "../types";
import { ShortcutsDialog } from "./ShortcutsDialog";
import { StatsCard, type StatsCardProps } from "./StatsCard";
import { TicketSearch } from "./TicketSearch";

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
// `color?: string | undefined`, not `color?: string`: under
// `exactOptionalPropertyTypes` the two differ, and every caller below reads a
// `Partial<IndexPayload["state_colors"]>` entry, which IS `string | undefined`
// whenever no index has loaded. "No colour" and "colour omitted" render
// identically here (the `{color && …}` guard), so accepting the explicit
// undefined once at this declaration is honest and beats four conditional
// spreads at the call sites.
function KeyRow({
  id,
  color,
  children,
}: {
  // A react-aria collection Key, forwarded straight to Dropdown.Item — NOT a
  // DOM id (react-aria mints the real element id itself). That is why KeyRow
  // is listed in biome.json's useUniqueElementIds `excludedComponents`; if
  // this ever renders a real element with id={id}, take it back off that list.
  id: string;
  color?: string | undefined;
  children: ReactNode;
}) {
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

/** `FocusChip`'s ticket-context counterpart (Task 12) — same tier-dot +
 * label + ✕ anatomy, but never tier-colored: a ticket has no single tier of
 * its own (its lines can span every tier), so its dot is always the neutral
 * `bg-fg-quaternary` swatch `FocusChip` only falls back to when a context's
 * OWN tier color is unavailable. */
function TicketChip({ id, onClear }: { id: string; onClear: () => void }) {
  return (
    <span
      data-testid="ticket-chip"
      className="inline-flex max-w-52 items-center gap-1.5 rounded-full border
        border-fg-brand-primary_alt bg-brand-primary_alt px-2.5 py-1 text-xs font-medium
        text-brand-secondary"
    >
      <span aria-hidden className="size-2 shrink-0 rounded-sm bg-fg-quaternary" />
      <span className="truncate font-mono">{id}</span>
      <button
        type="button"
        data-testid="ticket-clear"
        aria-label="Clear ticket pin"
        title="Clear ticket pin"
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
 * every other actionable item in this menu. Reused as-is for the "Pin
 * ticket" section below (Task 12) — the shape (active/dotColor/label/
 * testId/onAction) is generic; a ticket row just never supplies a
 * `dotColor`. */
function FocusMenuItem({
  active,
  dotColor,
  label,
  testId,
  onAction,
}: {
  active: boolean;
  /** `| undefined` explicitly — see TierStatRow.dotColor. A tier with no
   * wire colour renders the neutral `bg-fg-quaternary` dot below, which is
   * this component already treating `undefined` as a value it understands. */
  dotColor?: string | undefined;
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

/** One disabled row per manual-testing override entry (Task 11, ⋮ menu
 * "Overrides" section) — informational, same `isDisabled` `Dropdown.Item`
 * pattern `KeyRow` above uses, but shaped for an `OverrideJson` rather than
 * a fixed tier/state swatch: a DASHED tier dot (never solid — mirrors
 * `FilePage.tsx`'s `AssertedChip`, "declared", never "recorded") plus the
 * override's own `key` and a truncated `reason` (full text in `title`, for
 * a reason too long to fit the menu's fixed width). */
function OverrideEntryRow({
  entry,
  tierColor,
}: {
  entry: IndexPayload["overrides"][number];
  tierColor?: string | undefined;
}) {
  return (
    <Dropdown.Item
      id={`override-${entry.id}`}
      isDisabled
      textValue={entry.key}
      data-testid="override-entry"
      className="opacity-100 cursor-default"
    >
      <span title={entry.reason} className="flex min-w-0 items-center gap-2">
        <span
          aria-hidden
          className="size-2 shrink-0 rounded-sm border border-dashed border-current"
          style={tierColor ? { borderColor: tierColor } : undefined}
        />
        <span className="truncate font-mono text-xs font-medium text-secondary">{entry.key}</span>
        <span className="truncate text-xs text-quaternary">{entry.reason}</span>
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
  const { focus, setFocus, ticket, setTicket, hideAsserted, setHideAsserted } = useFocus();
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
  // Partial, unlike IndexPayload's own (closed, always-populated) field: with
  // no index loaded there is no palette, and colors are never hard-coded
  // here (Global Constraints). Each KeyRow below simply renders no swatch —
  // the type now says that can happen instead of claiming a `string`.
  const stateColors: Partial<IndexPayload["state_colors"]> = index?.state_colors ?? {};

  // Contexts for the chip's tier dot + the ⋮ menu's "Focus context"
  // switcher — derived here (not read off `focus`, which is only ever a
  // label string) via the same `groupContexts` every other page uses.
  const contexts = index ? groupContexts(index) : [];
  const focusedContext = contexts.find((ctx) => ctx.label === focus) ?? null;
  // Task 12: the ticket pin switcher, which lives in its own app-bar search
  // box (TicketSearch) rather than the ⋮ menu — an empty `tickets.json` (no
  // `[coverage.tickets]` attribution anywhere in this report) hides the box
  // rather than showing an empty, useless one.
  const tickets = index?.tickets ?? [];
  // Task 11 (manual-overrides spec §6): the ⋮ menu's "Overrides" section and
  // the toggle row both only exist when the report actually carries at
  // least one asserted entry — an empty `overrides` list means nothing to
  // hide, so both stay absent rather than showing a useless empty section
  // (same "no data, no control" pattern `tickets.length > 0` above already
  // uses for `TicketSearch`).
  const overrides = index?.overrides ?? [];

  return (
    <div className="flex min-h-screen flex-col">
      <header
        data-testid="app-bar"
        className="flex h-12 shrink-0 items-center justify-between gap-3 border-b border-secondary px-4"
      >
        <div className="flex min-w-0 items-center gap-5">
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
          {/* Top-level page nav (Task 10: neither this nor the Runs link
              existed before — both are added together here). `#/tickets`
              sits outside the `#/coverage/...` namespace deliberately (see
              App.tsx) so it can never collide with a real directory path. */}
          <nav data-testid="app-nav" className="flex items-center gap-1 text-xs font-medium">
            <a
              href="#/runs"
              data-testid="nav-runs"
              className="rounded-md px-2 py-1 text-tertiary hover:bg-tertiary hover:text-primary"
            >
              Runs
            </a>
            <a
              href="#/tickets"
              data-testid="nav-tickets"
              className="rounded-md px-2 py-1 text-tertiary hover:bg-tertiary hover:text-primary"
            >
              Tickets
            </a>
          </nav>
          {overrides.length > 0 && (
            // Always-visible indicator (Task 11, spec §6) — the ⋮ menu's
            // "Overrides" section (below) holds the actual listing; this is
            // just "something is being asserted here", visible without
            // opening the menu at all. Dashed border echoes `AssertedChip`/
            // `OverrideEntryRow`'s "declared, not recorded" treatment.
            <span
              data-testid="overrides-badge"
              title="manual-testing overrides are active — open the ⋮ menu for the list"
              className="rounded-full border border-dashed border-secondary px-2 text-xs text-tertiary"
            >
              {overrides.length} override{overrides.length === 1 ? "" : "s"}
            </span>
          )}
        </div>
        <div className="flex items-center gap-2">
          {focus !== null && (
            <FocusChip
              label={focus}
              {...(focusedContext && { tierColor: tierColors[focusedContext.tier] })}
              onClear={() => setFocus(null)}
            />
          )}
          {ticket !== null && <TicketChip id={ticket} onClear={() => setTicket(null)} />}
          <ButtonUtility
            aria-label={theme === "dark" ? "Switch to light mode" : "Switch to dark mode"}
            tooltip={theme === "dark" ? "Switch to light mode" : "Switch to dark mode"}
            data-testid="theme-toggle"
            icon={theme === "dark" ? Sun : Moon01}
            color="tertiary"
            size="sm"
            onClick={toggleTheme}
          />
          {tickets.length > 0 && (
            <TicketSearch tickets={tickets} ticket={ticket} onPin={setTicket} />
          )}
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
                {(overrides.length > 0 || hideAsserted) && (
                  // Task 11 (manual-overrides spec §6): the row shows when
                  // the report carries at least one asserted entry — see
                  // `overrides`' doc comment above — OR when `hideAsserted`
                  // is already true (F3 fix): a hand-typed `?asserted=1`
                  // deep link on a no-overrides report must not blank the
                  // tickets/coverage data with no visible control to clear
                  // it, so the toggle stays reachable even though the
                  // report itself has nothing to hide. The always-visible
                  // badge above stays gated on `overrides.length > 0` only —
                  // it announces "overrides are active", which isn't true
                  // here. The toggle row reuses `FocusMenuItem` (no
                  // `dotColor`, same as "All contexts") rather than a
                  // bespoke checkbox component, so it reads as one more menu
                  // action among the others instead of a one-off control.
                  <>
                    <Dropdown.Separator />
                    <Dropdown.Section>
                      <Dropdown.SectionHeader className="px-2.5 pt-2 pb-1 text-xs font-medium text-quaternary">
                        Overrides
                      </Dropdown.SectionHeader>
                      <FocusMenuItem
                        active={hideAsserted}
                        label="Hide asserted coverage"
                        testId="toggle-hide-asserted"
                        onAction={() => setHideAsserted(!hideAsserted)}
                      />
                      {overrides.map((entry) => (
                        <OverrideEntryRow
                          key={entry.id}
                          entry={entry}
                          tierColor={tierColors[entry.tier]}
                        />
                      ))}
                    </Dropdown.Section>
                  </>
                )}
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

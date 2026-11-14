// The source card's Columns menu (spec "The Columns menu"): the vendored
// Dropdown, the first in the repo to use selection. Each selection section
// carries its own `selectionMode`, so react-aria gives it its own selection
// manager, and `shouldCloseOnSelect={false}` keeps the menu open while the
// reader changes several settings; Reset columns is a plain action and
// closes. Every checked state is read from, and every change written to, the
// one source-columns store (`useSourceColumns`), never held here.
import { Columns03 } from "@untitledui/icons";
import { Button as AriaButton, type Key, type Selection } from "react-aria-components";
import { Dropdown } from "@/components/base/dropdown/dropdown";
import { cx } from "@/utils/cx";
import { type ColumnChoices, type HitMode, useSourceColumns } from "../sourceColumns";

/** The source card header's button look, shared by Columns and Expand
 * contexts so the two cannot drift apart. */
export const CARD_HEADER_BUTTON_CLASS =
  "inline-flex shrink-0 items-center gap-1.5 rounded-lg border border-secondary px-2.5 py-1 " +
  "text-xs font-medium text-tertiary outline-focus-ring hover:bg-tertiary hover:text-primary " +
  "focus-visible:outline-2 focus-visible:outline-offset-2";

type ColumnKey = "tickets" | "lineNumbers" | "branches" | "hitCounts";

const SHOW_ITEMS: { key: ColumnKey; label: string }[] = [
  { key: "tickets", label: "Tickets" },
  { key: "lineNumbers", label: "Line numbers" },
  { key: "branches", label: "Branches" },
  { key: "hitCounts", label: "Hit counts" },
];

const MODE_ITEMS: { key: HitMode; label: string }[] = [
  { key: "perTier", label: "Per tier" },
  { key: "total", label: "Total" },
];

const HEADER_CLASS = "px-2.5 pt-2 pb-1 text-xs font-medium text-quaternary";

/** The keys a section's selection holds; react-aria's `"all"` means every
 * key the section offers. */
export const keysOf = (selection: Selection, offered: readonly Key[]): Set<Key> =>
  new Set(selection === "all" ? offered : selection);

export function ColumnsMenu({
  tierCount,
  fileHasTickets,
}: {
  tierCount: number;
  fileHasTickets: boolean;
}) {
  const { columns, choose, reset } = useSourceColumns(tierCount);
  // The Tickets item exists when the file has tickets, before visibility
  // applies, so hiding Tickets never removes its own item.
  const showItems = SHOW_ITEMS.filter((it) => it.key !== "tickets" || fileHasTickets);
  const modeDisabled = !columns.hitCounts || !columns.modeSelectable;

  const onShowChange = (selection: Selection) => {
    const next = keysOf(
      selection,
      showItems.map((it) => it.key),
    );
    const patch: ColumnChoices = {};
    for (const { key } of showItems) {
      if (next.has(key) !== columns[key]) patch[key] = next.has(key);
    }
    choose(patch);
  };

  const onModeChange = (selection: Selection) => {
    const [mode] = keysOf(
      selection,
      MODE_ITEMS.map((it) => it.key),
    );
    if (mode === "perTier" || mode === "total") choose({ hitMode: mode });
  };

  return (
    <Dropdown.Root>
      <AriaButton data-testid="columns-menu" className={CARD_HEADER_BUTTON_CLASS}>
        <Columns03 aria-hidden className="size-3" />
        Columns
      </AriaButton>
      <Dropdown.Popover className="w-56">
        <Dropdown.Menu aria-label="Columns">
          <Dropdown.Section
            selectionMode="multiple"
            selectedKeys={showItems.filter((it) => columns[it.key]).map((it) => it.key)}
            onSelectionChange={onShowChange}
            shouldCloseOnSelect={false}
          >
            <Dropdown.SectionHeader className={HEADER_CLASS}>Show columns</Dropdown.SectionHeader>
            {showItems.map((it) => (
              <Dropdown.Item
                key={it.key}
                id={it.key}
                label={it.label}
                textValue={it.label}
                selectionIndicator="checkbox"
              />
            ))}
          </Dropdown.Section>
          {/* Per tier / Total look like one two-segment switch (Chris,
              2026-11-14) but stay real radio items, so arrow keys and screen
              readers treat them as menu items. The grid lays them side by
              side; collection order still runs Per tier then Total. */}
          <Dropdown.Section
            selectionMode="single"
            disallowEmptySelection
            selectedKeys={[columns.hitMode]}
            onSelectionChange={onModeChange}
            shouldCloseOnSelect={false}
            className="grid grid-cols-2 px-2.5 pb-2"
          >
            <Dropdown.SectionHeader className={cx(HEADER_CLASS, "col-span-2 px-0")}>
              Hit counts as
            </Dropdown.SectionHeader>
            {MODE_ITEMS.map((it) => (
              <Dropdown.Item
                key={it.key}
                id={it.key}
                textValue={it.label}
                isDisabled={modeDisabled}
                selectionIndicator="none"
                className="px-0 py-0.5 ring-1 ring-primary ring-inset first-of-type:rounded-l-lg
                  last-of-type:rounded-r-lg"
              >
                {({ isSelected }) => (
                  <span
                    className={cx(
                      "block rounded-md py-0.5 text-center",
                      isSelected && "bg-brand-solid text-white",
                    )}
                  >
                    {it.label}
                  </span>
                )}
              </Dropdown.Item>
            ))}
          </Dropdown.Section>
          <Dropdown.Separator />
          <Dropdown.Section>
            <Dropdown.Item
              id="reset"
              label="Reset columns"
              textValue="Reset columns"
              onAction={reset}
            />
          </Dropdown.Section>
        </Dropdown.Menu>
      </Dropdown.Popover>
    </Dropdown.Root>
  );
}

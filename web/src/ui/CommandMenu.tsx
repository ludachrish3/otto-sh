// web/src/ui/CommandMenu.tsx
// The in-house command palette (spec §Palette). Untitled UI's own command
// menu is PRO-tier, so this is the same react-aria stack it uses —
// ModalOverlay + Dialog + Autocomplete + Menu — styled with the vendored
// theme tokens only. Enter/click runs and closes; Esc closes; the filter
// is a plain case/diacritic-insensitive contains over label + sublabel.
import { Check, SearchLg } from "@untitledui/icons";
import {
  Autocomplete,
  Dialog,
  Header,
  Input,
  Menu,
  MenuItem,
  MenuSection,
  Modal,
  ModalOverlay,
  SearchField,
  useFilter,
} from "react-aria-components";

import type { Command, CommandSection } from "./commands";
import { Kbd } from "./Kbd";
import { formatBinding } from "./shortcuts";
import { useUiStore } from "./uiStore";

const SECTION_ORDER: CommandSection[] = ["Navigation", "Actions", "Live window"];

export function CommandMenu({ commands }: { commands: Command[] }) {
  const open = useUiStore((s) => s.paletteOpen);
  const { openPalette, closePalette } = useUiStore((s) => s.actions);
  const { contains } = useFilter({ sensitivity: "base" });

  const sections = SECTION_ORDER.map((section) => ({
    section,
    items: commands.filter((c) => c.section === section),
  })).filter((s) => s.items.length > 0);

  const byId = new Map(commands.map((c) => [c.id, c]));

  return (
    <ModalOverlay
      isOpen={open}
      onOpenChange={(next) => (next ? openPalette() : closePalette())}
      isDismissable
      className="fixed inset-0 z-50 flex justify-center bg-overlay/70 pt-[20vh]"
    >
      <Modal className="w-full max-w-140 px-4">
        <Dialog
          aria-label="Command menu"
          data-testid="command-menu"
          className="overflow-hidden rounded-xl bg-primary shadow-2xl ring-1 ring-secondary_alt
            outline-hidden"
        >
          <Autocomplete filter={(textValue, inputValue) => contains(textValue, inputValue)}>
            <SearchField aria-label="Search commands" autoFocus className="group flex">
              <div className="flex w-full items-center gap-2.5 border-b border-secondary px-4">
                <SearchLg aria-hidden className="size-4 shrink-0 text-fg-quaternary" />
                <Input
                  data-testid="command-input"
                  placeholder="Type a command or search hosts…"
                  className="h-12 w-full bg-transparent text-md text-primary outline-hidden
                    placeholder:text-placeholder"
                />
              </div>
            </SearchField>
            <Menu
              className="max-h-80 overflow-y-auto py-1.5 outline-hidden"
              renderEmptyState={() => (
                <div data-testid="command-empty" className="px-4 py-6 text-sm text-quaternary">
                  No results
                </div>
              )}
              onAction={(key) => {
                const command = byId.get(String(key));
                if (!command?.enabled) return;
                closePalette();
                command.run();
              }}
            >
              {sections.map(({ section, items }) => (
                <MenuSection key={section} className="pb-1">
                  <Header className="px-4 pt-2 pb-1 text-xs font-medium text-quaternary">
                    {section}
                  </Header>
                  {items.map((command) => {
                    const Icon = command.icon;
                    return (
                      <MenuItem
                        key={command.id}
                        id={command.id}
                        textValue={`${command.label} ${command.sublabel ?? ""}`}
                        isDisabled={!command.enabled}
                        data-testid={`command-item-${command.id}`}
                        className="group mx-1.5 flex cursor-pointer items-center rounded-md px-2.5
                          py-2 outline-hidden transition duration-100 ease-linear
                          data-focused:bg-primary_hover data-disabled:cursor-not-allowed
                          data-disabled:opacity-50"
                      >
                        <Icon
                          aria-hidden
                          className="mr-2.5 size-4 shrink-0 stroke-[2.25px] text-fg-quaternary"
                        />
                        <span className="truncate text-sm font-medium text-secondary">
                          {command.label}
                        </span>
                        {command.sublabel && (
                          <span className="ml-2 truncate text-xs text-quaternary">
                            {command.sublabel}
                          </span>
                        )}
                        <span className="ml-auto flex items-center pl-3">
                          {command.checked && (
                            <Check
                              aria-hidden
                              className="size-4 stroke-[2.25px] text-fg-brand-primary"
                            />
                          )}
                          {command.binding && <Kbd>{formatBinding(command.binding)}</Kbd>}
                        </span>
                      </MenuItem>
                    );
                  })}
                </MenuSection>
              ))}
            </Menu>
          </Autocomplete>
        </Dialog>
      </Modal>
    </ModalOverlay>
  );
}

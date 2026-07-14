// Collapsible section. react-aria supplies the button semantics and
// aria-expanded; the visual is a compact header row + panel, matching the
// other ui/ primitives (open-code Untitled UI: React Aria + Tailwind).
//
// No free-tier Untitled UI component covers this shape (a disclosure/
// accordion section), so this stays hand-authored — Task 8 only restyles it
// onto Untitled UI's semantic tokens (bg-primary/text-quaternary/
// border-secondary/hover:bg-primary_hover, same vocabulary the vendored
// components use) in place of the old gray-N + dark: pairs. Untitled UI's
// theme.css keys those tokens off a single `.dark-mode` class swap (see
// web/README.md), so a `dark:` variant is redundant with them, not
// additive — the constraint every migrated file in this task follows.
import type { ReactNode } from "react";
import { Disclosure as AriaDisclosure, Button, DisclosurePanel } from "react-aria-components";

export function Disclosure(props: {
  title: string;
  defaultExpanded?: boolean;
  testId?: string;
  toggleTestId?: string;
  children: ReactNode;
}) {
  const { title, defaultExpanded = true, testId, toggleTestId, children } = props;
  return (
    <AriaDisclosure
      defaultExpanded={defaultExpanded}
      data-testid={testId}
      className="overflow-hidden rounded-lg border border-secondary bg-primary shadow-xs"
    >
      <Button
        slot="trigger"
        data-testid={toggleTestId}
        className="flex w-full cursor-pointer items-center gap-2 px-2.5 py-1.5 text-[11px]
          font-semibold tracking-wide text-quaternary uppercase outline-none
          hover:bg-primary_hover"
      >
        {title}
        <span aria-hidden className="ml-auto text-[9px] text-quaternary">
          ▾
        </span>
      </Button>
      <DisclosurePanel className="border-t border-secondary">{children}</DisclosurePanel>
    </AriaDisclosure>
  );
}

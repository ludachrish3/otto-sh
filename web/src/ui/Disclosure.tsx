// Collapsible section. react-aria supplies the button semantics and
// aria-expanded; the visual is a compact header row + panel, matching the
// other ui/ primitives (open-code Untitled UI: React Aria + Tailwind).
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
      className="overflow-hidden rounded-lg border border-gray-200 bg-white shadow-sm
        dark:border-gray-800 dark:bg-gray-950"
    >
      <Button
        slot="trigger"
        data-testid={toggleTestId}
        className="flex w-full cursor-pointer items-center gap-2 px-2.5 py-1.5 text-[11px]
          font-semibold tracking-wide text-gray-500 uppercase outline-none hover:bg-gray-50
          dark:text-gray-400 dark:hover:bg-gray-900"
      >
        {title}
        <span aria-hidden className="ml-auto text-[9px] text-gray-400">
          ▾
        </span>
      </Button>
      <DisclosurePanel className="border-t border-gray-100 dark:border-gray-800">
        {children}
      </DisclosurePanel>
    </AriaDisclosure>
  );
}

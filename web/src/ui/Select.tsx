import type { ComponentPropsWithRef } from "react";
import {
  Button as AriaButton,
  Select as AriaSelect,
  type Key,
  ListBox,
  ListBoxItem,
  Popover,
  SelectValue,
} from "react-aria-components";

export interface SelectItem {
  id: string;
  label: string;
  /** Rendered as the `title` attribute on the option's DOM element (a
   * hover tooltip) — e.g. a session's note. */
  title?: string;
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
      onSelectionChange={(key: Key | null) => {
        if (key !== null) onSelectionChange(String(key));
      }}
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
                outline-none focus:bg-gray-100 data-[selected]:font-semibold dark:text-gray-200
                dark:focus:bg-gray-800"
              // react-aria-components' `filterDOMProps` only forwards a
              // fixed global-attribute allowlist (dir/lang/hidden/inert/
              // translate) onto the rendered option — `title` isn't one of
              // them, so setting it directly as a prop above would be
              // silently dropped before it ever reaches the DOM. `render`
              // hands back the fully-computed option props (role, a11y
              // attrs, the resolved children, the merged ref) so we can add
              // `title` on the actual element that becomes the DOM option.
              // The cast is safe: `render`'s prop type is a link/div union
              // because ListBoxItem *could* render an `<a>` (when given an
              // `href`, which this component never does), so it's always
              // the div-shaped member at runtime here.
              render={(optionProps) => (
                <div {...(optionProps as ComponentPropsWithRef<"div">)} title={item.title} />
              )}
            >
              {item.label}
            </ListBoxItem>
          ))}
        </ListBox>
      </Popover>
    </AriaSelect>
  );
}

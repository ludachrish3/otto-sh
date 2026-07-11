import {
  Button as AriaButton,
  Menu as AriaMenu,
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
                outline-none focus:bg-gray-100 data-[disabled]:cursor-not-allowed
                data-[disabled]:opacity-50 dark:text-gray-200 dark:focus:bg-gray-800"
            >
              {item.label}
            </MenuItem>
          ))}
        </AriaMenu>
      </Popover>
    </MenuTrigger>
  );
}

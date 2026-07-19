// web/src/ui/Breadcrumbs.tsx
// Untitled UI's breadcrumbs is PRO-tier (same situation as the command
// palette — see CommandMenu.tsx), so this composes the react-aria stack it
// wraps — Breadcrumbs/Breadcrumb/Link — styled per its "button" variant,
// then deliberately borrowed depth from the vendored tabs' button-border
// type (tabs.tsx): the same ring-1 bg-secondary_alt container and the same
// hover/current bg-primary_alt + shadow item treatment, per Chris's ask
// that the breadcrumbs stop blending into the page.
import { ChevronRight } from "@untitledui/icons";
import { Breadcrumbs as AriaBreadcrumbs, Breadcrumb, Link } from "react-aria-components";

import { cx } from "@/utils/cx";

export interface Crumb {
  label: string;
  /** Absent on the current (last) crumb. Hash-router path, e.g. "#/hosts". */
  href?: string;
}

const ITEM =
  "flex items-center gap-1 rounded-md px-2.5 py-1 text-sm font-semibold whitespace-nowrap transition duration-100 ease-linear outline-focus-ring focus-visible:outline-2 focus-visible:-outline-offset-2";

export function Breadcrumbs({ items }: { items: Crumb[] }) {
  return (
    <AriaBreadcrumbs
      data-testid="breadcrumbs"
      className="flex w-max items-center gap-0.5 rounded-[10px] bg-secondary_alt p-1 ring-1
        ring-secondary ring-inset"
    >
      {items.map((item, i) => {
        const current = i === items.length - 1;
        return (
          <Breadcrumb key={item.label} className="flex items-center gap-0.5">
            {current ? (
              <span
                aria-current="page"
                className={cx(ITEM, "bg-primary_alt text-secondary shadow-sm")}
              >
                {item.label}
              </span>
            ) : (
              <Link
                href={item.href}
                className={cx(
                  ITEM,
                  "cursor-pointer text-quaternary hover:bg-primary_alt hover:text-secondary hover:shadow-sm",
                )}
              >
                {item.label}
              </Link>
            )}
            {!current && (
              <ChevronRight aria-hidden className="size-4 shrink-0 text-fg-quaternary" />
            )}
          </Breadcrumb>
        );
      })}
    </AriaBreadcrumbs>
  );
}

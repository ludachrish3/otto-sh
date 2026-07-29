import { SearchMd } from "@untitledui/icons";
import type { HTMLAttributes } from "react";

// Adapters between `@untitledui/icons` and the vendored Untitled UI
// components' `icon` props.
//
// The vendored input declares `icon?: ComponentType<HTMLAttributes<
// HTMLOrSVGElement>>` (components/base/input/input.tsx). Passing an icon
// straight through — `icon={SearchMd}` — type-checks fine WITHOUT
// `exactOptionalPropertyTypes` and fails with it, for a reason that is
// neither end's fault and neither end's to fix:
//
//   @untitledui/icons declares each icon as `FC<Props>` where
//   `Props extends SVGProps<SVGSVGElement>` and REDECLARES two inherited
//   members without `| undefined`: `color?: string; size?: number`. React's
//   own attribute types spell every optional as `?: T | undefined`. Checking
//   `FC<Props>` against `ComponentType<HTMLAttributes<…>>` is contravariant
//   in the props, so it asks whether `HTMLAttributes<…>` is assignable to
//   `Props` — and `color?: string | undefined` is not assignable to
//   `color?: string` once the flag is on.
//
// Both ends are out of reach: the icon package is a dependency, and
// input.tsx is vendored (never hand-edit — check_untitledui_hash.sh fails on
// any byte change). `skipLibCheck` does not help; it suppresses errors
// INSIDE a .d.ts, not assignability at a use site.
//
// So the icon is wrapped rather than cast. This is a real component that
// accepts exactly what the vendored prop promises to pass and forwards the
// one member the vendored component actually uses (input.tsx renders
// `<Icon className={cx(…)} />` and passes nothing else). The incompatible
// members are DROPPED, not asserted away — an `as` here would silence the
// checker while leaving `color`/`size` claiming a type React can violate.

/** `SearchMd`, usable as a vendored component's `icon`. */
export function SearchIcon({ className }: HTMLAttributes<HTMLOrSVGElement>) {
  return <SearchMd className={className} />;
}

// The one place a data colour becomes a pill. A colour the project configures
// (tier colours, state colours: any CSS colour string) can never be assumed
// legible as text on the page background, so the rule is: the data colour is
// NEVER text ink. It appears only as a dot (`TintDot`) and as a `color-mix`
// tint behind the label; the label itself uses the theme's own ink. `color-mix`
// keeps hex and named colours working uniformly. One tint strength for every
// pill (14%, the tier chip's).
import type { CSSProperties } from "react";
import { cx } from "@/utils/cx";

/** The hue marker: a small square in the data colour. Use it directly where
 * the thing being coloured is not a pill (a table cell's leading dot). */
export function TintDot({ color, testId }: { color: string; testId?: string }) {
  return (
    <span
      aria-hidden
      data-testid={testId}
      className="size-2 shrink-0 rounded-sm"
      style={{ backgroundColor: color }}
    />
  );
}

/** The tint behind a pill's label; never applied to text. */
export function tintStyle(color: string): CSSProperties {
  return { backgroundColor: `color-mix(in srgb, ${color} 14%, transparent)` };
}

const SIZES = {
  md: "px-2.5 py-0.5 text-xs",
  sm: "px-2 py-0.5 text-[10.5px] whitespace-nowrap",
} as const;

export function TintedPill({
  color,
  label,
  size = "md",
  testId,
  labelTestId,
}: {
  color: string;
  label: string;
  size?: keyof typeof SIZES;
  testId?: string;
  labelTestId?: string;
}) {
  return (
    <span
      data-testid={testId}
      className={cx(
        "inline-flex w-fit max-w-full items-center gap-1.5 rounded-full font-medium text-secondary",
        SIZES[size],
      )}
      style={tintStyle(color)}
    >
      <TintDot color={color} />
      <span data-testid={labelTestId} className="min-w-0 break-words">
        {label}
      </span>
    </span>
  );
}

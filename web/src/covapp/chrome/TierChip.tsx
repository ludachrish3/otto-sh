// A tier's label pill. The tier colour is data (any CSS colour the project
// configures), so it can never be assumed legible as text on the page
// background: the hue appears only in the dot and the tint behind the label,
// and the label itself uses the theme's own text ink.
import type { IndexPayload } from "../types";

export function TierChip({ tier, index }: { tier: string; index: IndexPayload }) {
  const color = index.tier_colors[tier] ?? "currentColor";
  return (
    <span
      data-testid="tier-chip"
      className="inline-flex w-fit max-w-full items-center gap-1.5 rounded-full px-2.5 py-0.5 text-xs font-medium text-secondary"
      style={{ backgroundColor: `color-mix(in srgb, ${color} 14%, transparent)` }}
    >
      <span aria-hidden className="size-2 shrink-0 rounded-sm" style={{ backgroundColor: color }} />
      <span data-testid="tier-chip-label" className="min-w-0 break-words">
        {index.tier_labels[tier] ?? tier}
      </span>
    </span>
  );
}

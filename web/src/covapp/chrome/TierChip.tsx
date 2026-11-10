// A tier's label pill. The tier colour is data: see `TintedPill` for why it
// only ever tints and dots, never inks the label.
import type { IndexPayload } from "../types";
import { TintedPill } from "./TintedPill";

export function TierChip({ tier, index }: { tier: string; index: IndexPayload }) {
  return (
    <TintedPill
      testId="tier-chip"
      labelTestId="tier-chip-label"
      color={index.tier_colors[tier] ?? "currentColor"}
      label={index.tier_labels[tier] ?? tier}
    />
  );
}

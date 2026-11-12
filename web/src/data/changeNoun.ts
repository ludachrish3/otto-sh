/** "change" for exactly one, "changes" otherwise: the one pluralisation of
 * the unsaved-edit count (the review bar marker and the switch prompt). */
export function changeNoun(count: number): string {
  return count === 1 ? "change" : "changes";
}

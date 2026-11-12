// The source generation: how async work knows the tab has moved onto
// another source since it started (data/source.ts owns every move). Its own
// module so the fetches source.ts makes (data/snapshot.ts) and the event
// writes (data/eventApi.ts) can check it without importing source.ts back.

let generation = 0;

export function sourceGeneration(): number {
  return generation;
}

export function isCurrentSource(gen: number): boolean {
  return gen === generation;
}

/** Moves the generation on: everything in flight is stale from now. Called
 * only by data/source.ts, as the first step of every source change. */
export function nextGeneration(): number {
  generation += 1;
  return generation;
}

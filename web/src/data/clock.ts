// A `now` that ticks at the COLLECTION INTERVAL, in its own store.
//
// Unreachable dimming needs a clock, not events: a host that goes silent emits
// nothing, so without a tick nothing would ever re-render it and a dead host would
// stay green forever. But the clock must not wake the world — hence its own store,
// so only health consumers re-render (pinned by the tier-2 guard in the tests).
//
// The rate is the collection interval because the down threshold IS
// HEALTH_K x cadence: polling faster than the collector cannot learn anything, since
// no new information can arrive between polls.
import { useEffect } from "react";
import { create } from "zustand";

interface ClockState {
  now: number;
  tick: () => void;
}

export const useClockStore = create<ClockState>()((set) => ({
  now: Date.now(),
  tick: () => set({ now: Date.now() }),
}));

/** Subscribe to a `now` that advances every *intervalMs*. Null = never tick. */
export function useNow(intervalMs: number | null): number {
  const now = useClockStore((s) => s.now);
  useEffect(() => {
    if (intervalMs === null || intervalMs <= 0) return;
    const id = setInterval(() => useClockStore.getState().tick(), intervalMs);
    return () => clearInterval(id);
  }, [intervalMs]);
  return now;
}

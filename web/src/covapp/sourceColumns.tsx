// Source-view column settings: ONE provider holds what the reader has
// *chosen* (per setting), and `resolveColumns` turns choices + window width
// into the one effective visibility every consumer reads. Memory only — no
// localStorage, sessionStorage or URL — so choices last exactly as long as
// the open tab's page (spec: "How long choices last"). Deliberately NOT part
// of focus.tsx, which owns pinned/shareable values with the opposite
// lifecycle. Mounted outside the data guard, so it reads no index.
import {
  createContext,
  type ReactNode,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from "react";

/** The report is "narrow" below this window width (px). The single home of
 * the threshold; the media query below derives from it. */
export const NARROW_MAX_WIDTH = 820;

export type HitMode = "perTier" | "total";

/** A key that is present has been chosen by the reader; an absent key
 * follows its default (which depends on the window width). */
export interface ColumnChoices {
  tickets?: boolean;
  lineNumbers?: boolean;
  branches?: boolean;
  hitCounts?: boolean;
  hitMode?: HitMode;
}

export interface EffectiveColumns {
  tickets: boolean;
  lineNumbers: boolean;
  branches: boolean;
  hitCounts: boolean;
  hitMode: HitMode;
  /** Whether Per tier / Total is a real choice (needs more than one tier). */
  modeSelectable: boolean;
}

/** Resolve choices against the width: each setting independently, so a
 * reader who touched one item still gets the narrow defaults for the rest. */
export function resolveColumns(
  chosen: ColumnChoices,
  narrow: boolean,
  tierCount: number,
): EffectiveColumns {
  const modeSelectable = tierCount > 1;
  const defaultMode: HitMode = narrow ? "total" : "perTier";
  return {
    tickets: chosen.tickets ?? !narrow,
    lineNumbers: chosen.lineNumbers ?? true,
    branches: chosen.branches ?? !narrow,
    hitCounts: chosen.hitCounts ?? true,
    hitMode: modeSelectable ? (chosen.hitMode ?? defaultMode) : "perTier",
    modeSelectable,
  };
}

interface SourceColumnsState {
  chosen: ColumnChoices;
  narrow: boolean;
  setChosen: (update: (prev: ColumnChoices) => ColumnChoices) => void;
}

const SourceColumnsContext = createContext<SourceColumnsState | null>(null);

// Narrow is width < NARROW_MAX_WIDTH, exact at fractional (zoomed) widths,
// which `(max-width: 819px)` would leave wide at 819.5 px.
const NARROW_QUERY = `not all and (min-width: ${NARROW_MAX_WIDTH}px)`;

export function SourceColumnsProvider({ children }: { children: ReactNode }) {
  const [chosen, setChosen] = useState<ColumnChoices>({});
  const [narrow, setNarrow] = useState(() => window.matchMedia(NARROW_QUERY).matches);

  useEffect(() => {
    const query = window.matchMedia(NARROW_QUERY);
    setNarrow(query.matches);
    const onChange = (event: MediaQueryListEvent) => setNarrow(event.matches);
    query.addEventListener("change", onChange);
    return () => query.removeEventListener("change", onChange);
  }, []);

  const value = useMemo(() => ({ chosen, narrow, setChosen }), [chosen, narrow]);
  return <SourceColumnsContext.Provider value={value}>{children}</SourceColumnsContext.Provider>;
}

export function useSourceColumns(tierCount: number): {
  columns: EffectiveColumns;
  choose: (patch: ColumnChoices) => void;
  reset: () => void;
} {
  const ctx = useContext(SourceColumnsContext);
  if (!ctx) throw new Error("useSourceColumns must be used within a SourceColumnsProvider");
  const { chosen, narrow, setChosen } = ctx;
  const columns = useMemo(
    () => resolveColumns(chosen, narrow, tierCount),
    [chosen, narrow, tierCount],
  );

  const choose = useCallback(
    (patch: ColumnChoices) =>
      setChosen((prev) => {
        const next = { ...prev, ...patch };
        // Turning Hit counts off fixes the mode in effect at that moment.
        if (patch.hitCounts === false && patch.hitMode === undefined) {
          next.hitMode = resolveColumns(prev, narrow, tierCount).hitMode;
        }
        return next;
      }),
    [setChosen, narrow, tierCount],
  );
  const reset = useCallback(() => setChosen(() => ({})), [setChosen]);

  return { columns, choose, reset };
}

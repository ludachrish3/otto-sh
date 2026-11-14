import { act, render, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { type HitMode, NARROW_MAX_WIDTH, resolveColumns, useSourceColumns } from "./sourceColumns";
import { Providers, setViewportWidth } from "./testUtils";

afterEach(() => {
  vi.restoreAllMocks();
});

const probes: Record<string, ReturnType<typeof useSourceColumns> | undefined> = {};

function Probe({ slot }: { slot: string }) {
  probes[slot] = useSourceColumns(4);
  return null;
}

describe("resolveColumns", () => {
  it("wide defaults show everything, per tier", () => {
    expect(resolveColumns({}, false, 4)).toEqual({
      tickets: true,
      lineNumbers: true,
      branches: true,
      hitCounts: true,
      hitMode: "perTier",
      modeSelectable: true,
    });
  });

  it("narrow defaults hide tickets and branches and total the hits", () => {
    expect(resolveColumns({}, true, 4)).toEqual({
      tickets: false,
      lineNumbers: true,
      branches: false,
      hitCounts: true,
      hitMode: "total",
      modeSelectable: true,
    });
  });

  it("tracks each setting separately, not as a snapshot", () => {
    expect(resolveColumns({ branches: false }, true, 4).hitMode).toBe("total");
    expect(resolveColumns({ branches: false }, false, 4).tickets).toBe(true);
  });

  it("a chosen mode holds when narrow", () => {
    const chosen: HitMode = "perTier";
    expect(resolveColumns({ hitMode: chosen }, true, 4).hitMode).toBe(chosen);
  });

  it("one tier or none: per tier and not selectable", () => {
    for (const tiers of [1, 0]) {
      const cols = resolveColumns({}, true, tiers);
      expect(cols.hitMode).toBe("perTier");
      expect(cols.modeSelectable).toBe(false);
    }
  });
});

describe("useSourceColumns", () => {
  it("turning hit counts off fixes the mode across resizes until reset", () => {
    setViewportWidth(1000);
    const { result } = renderHook(() => useSourceColumns(4), { wrapper: Providers });
    act(() => result.current.choose({ hitCounts: false }));
    act(() => setViewportWidth(700));
    act(() => result.current.choose({ hitCounts: true }));
    expect(result.current.columns.hitMode).toBe("perTier");
    act(() => setViewportWidth(1000));
    expect(result.current.columns.hitMode).toBe("perTier");
    act(() => setViewportWidth(700));
    act(() => result.current.reset());
    expect(result.current.columns.hitMode).toBe("total");
  });

  it("819 px is narrow and 820 px is wide", () => {
    setViewportWidth(NARROW_MAX_WIDTH - 1);
    const { result } = renderHook(() => useSourceColumns(4), { wrapper: Providers });
    expect(result.current.columns.tickets).toBe(false);
    act(() => setViewportWidth(NARROW_MAX_WIDTH));
    expect(result.current.columns.tickets).toBe(true);
  });

  it("a fractional width just under 820 px (a zoomed window) is narrow", () => {
    setViewportWidth(NARROW_MAX_WIDTH - 0.5);
    const { result } = renderHook(() => useSourceColumns(4), { wrapper: Providers });
    expect(result.current.columns.tickets).toBe(false);
  });

  it("two components under one provider share the choices", () => {
    render(
      <>
        <Probe slot="a" />
        <Probe slot="b" />
      </>,
      { wrapper: Providers },
    );
    act(() => probes["a"]?.choose({ lineNumbers: false }));
    expect(probes["b"]?.columns.lineNumbers).toBe(false);
  });

  it("writes nothing to web storage", () => {
    const spy = vi.spyOn(Storage.prototype, "setItem");
    const { result } = renderHook(() => useSourceColumns(4), { wrapper: Providers });
    act(() => result.current.choose({ hitCounts: false, tickets: false }));
    act(() => result.current.reset());
    expect(spy).not.toHaveBeenCalled();
  });
});

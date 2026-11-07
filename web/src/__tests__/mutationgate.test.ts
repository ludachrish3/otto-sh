// web/src/__tests__/mutationgate.test.ts
import { act, renderHook } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import {
  anyMutationPending,
  HISTORY_GATE_KEY,
  runGate,
  useMutationGate,
} from "../shell/useMutationGate";

function deferred() {
  let resolve!: () => void;
  const promise = new Promise<void>((res) => {
    resolve = res;
  });
  return { promise, resolve };
}

describe("useMutationGate", () => {
  it("two runs in one tick call fn once", async () => {
    const { result } = renderHook(() => useMutationGate("k"));
    const d = deferred();
    const fn = vi.fn(() => d.promise);
    let first!: Promise<void>;
    let second!: Promise<void>;
    act(() => {
      first = result.current.run(fn);
      second = result.current.run(fn);
    });
    expect(fn).toHaveBeenCalledTimes(1);
    await act(async () => {
      d.resolve();
      await Promise.all([first, second]);
    });
  });

  it("pending is true until fn settles, then false", async () => {
    const { result } = renderHook(() => useMutationGate("k"));
    const d = deferred();
    let p!: Promise<void>;
    act(() => {
      p = result.current.run(() => d.promise);
    });
    expect(result.current.pending).toBe(true);
    await act(async () => {
      d.resolve();
      await p;
    });
    expect(result.current.pending).toBe(false);
    const fn = vi.fn(async () => {});
    await act(async () => {
      await result.current.run(fn);
    });
    expect(fn).toHaveBeenCalledTimes(1);
  });

  it("a pending run outlives the component, and a remount on the same key is still shut", async () => {
    const first = renderHook(() => useMutationGate("k"));
    const d = deferred();
    let p!: Promise<void>;
    act(() => {
      p = first.result.current.run(() => d.promise, "mark");
    });
    first.unmount();
    const again = renderHook(() => useMutationGate("k"));
    expect(again.result.current.pending).toBe(true);
    expect(again.result.current.pendingAction).toBe("mark");
    const fn = vi.fn(async () => {});
    await act(async () => {
      await again.result.current.run(fn);
    });
    expect(fn).not.toHaveBeenCalled();
    await act(async () => {
      d.resolve();
      await p;
    });
    expect(again.result.current.pending).toBe(false);
    expect(again.result.current.pendingAction).toBeNull();
  });

  it("any pending key shuts every gate's pending and run; pendingAction names only the pressed key's action", async () => {
    const a = renderHook(() => useMutationGate("a"));
    const b = renderHook(() => useMutationGate("b"));
    const d = deferred();
    let p!: Promise<void>;
    act(() => {
      p = a.result.current.run(() => d.promise, "stop");
    });
    expect(a.result.current.pendingAction).toBe("stop");
    expect(b.result.current.pendingAction).toBeNull();
    expect(a.result.current.pending).toBe(true);
    expect(b.result.current.pending).toBe(true);
    expect(anyMutationPending()).toBe(true);
    const fn = vi.fn(async () => {});
    await act(async () => {
      await b.result.current.run(fn);
      await runGate("c", "x", fn);
    });
    expect(fn).not.toHaveBeenCalled();
    await act(async () => {
      d.resolve();
      await p;
    });
    expect(b.result.current.pending).toBe(false);
    expect(anyMutationPending()).toBe(false);
    await act(async () => {
      await runGate("c", "x", fn);
    });
    expect(fn).toHaveBeenCalledTimes(1);
  });

  it("HISTORY_GATE_KEY is the history's own key", () => {
    expect(HISTORY_GATE_KEY).toBe("history");
  });

  it("a rejection propagates and releases the gate", async () => {
    const { result } = renderHook(() => useMutationGate("k"));
    await act(async () => {
      await expect(result.current.run(() => Promise.reject(new Error("boom")))).rejects.toThrow(
        "boom",
      );
    });
    expect(result.current.pending).toBe(false);
    const fn = vi.fn(async () => {});
    await act(async () => {
      await result.current.run(fn);
    });
    expect(fn).toHaveBeenCalledTimes(1);
  });
});

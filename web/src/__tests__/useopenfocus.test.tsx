// web/src/__tests__/useopenfocus.test.tsx
// ui/useOpenFocus.ts's contract (#957): a caller must render the hook's
// `scope` inside the overlay's Dialog, or the disabled-field fallback has
// no focus manager. The dev-time assertion makes leaving it out loud.
import { act, cleanup, render } from "@testing-library/react";
import { FocusScope } from "react-aria";
import { Dialog, Modal } from "react-aria-components";
import { afterEach, describe, expect, it } from "vitest";
import { allowConsoleOutput } from "../../vitest.setup";
import { useOpenFocus } from "../ui/useOpenFocus";

afterEach(() => {
  cleanup();
});

function Overlay({ withScope }: { withScope: boolean }) {
  const focus = useOpenFocus<HTMLInputElement>(true);
  return (
    <Modal isOpen>
      <Dialog aria-label="Probe">
        {withScope && focus.scope}
        <input aria-label="Field" ref={focus.ref} />
      </Dialog>
    </Modal>
  );
}

describe("useOpenFocus", () => {
  it("a caller that renders `scope` opens with focus in its field", () => {
    const { getByLabelText } = render(<Overlay withScope />);
    expect(document.activeElement).toBe(getByLabelText("Field"));
  });

  it("a caller that omits `scope` trips the dev-time assertion", () => {
    allowConsoleOutput(); // React logs the layout-effect throw to console.error
    expect(() => render(<Overlay withScope={false} />)).toThrow(
      "`scope` must render inside the overlay's Dialog",
    );
  });
});

// #1032: when a nested overlay closes, the Dialog takes focus only if the
// control that opened it is gone and focus sits on <body>. A plain
// FocusScope, not a Modal: jsdom's react-aria containment pulls focus back
// to the field on its own, which the browsers do not do in this case (the
// browser suite's test_delete_from_the_events_panel_keeps_focus_in_the_panel
// is the end-to-end proof).
function Reclaiming({ opener, nestedOpen }: { opener: boolean; nestedOpen: boolean }) {
  const focus = useOpenFocus<HTMLInputElement>(true, { nestedOpen });
  return (
    <FocusScope>
      {focus.scope}
      <div role="dialog" aria-label="Probe" tabIndex={-1}>
        <input aria-label="Field" ref={focus.ref} />
        {opener && <button type="button">Opener</button>}
      </div>
    </FocusScope>
  );
}

const nextFrame = () =>
  act(() => new Promise<void>((resolve) => requestAnimationFrame(() => resolve())));

describe("useOpenFocus nested close (#1032)", () => {
  it("focuses the Dialog when the opener is gone and focus is on <body>", async () => {
    const { getByRole, rerender } = render(<Reclaiming opener nestedOpen={false} />);
    getByRole("button", { name: "Opener" }).focus();
    rerender(<Reclaiming opener nestedOpen />);
    rerender(<Reclaiming opener={false} nestedOpen={false} />);
    expect(document.activeElement).toBe(document.body);
    await nextFrame();
    expect(document.activeElement).toBe(getByRole("dialog", { name: "Probe" }));
  });

  it("leaves focus to react-aria when the opener is still there", async () => {
    const { getByRole, rerender } = render(<Reclaiming opener nestedOpen={false} />);
    getByRole("button", { name: "Opener" }).focus();
    rerender(<Reclaiming opener nestedOpen />);
    (document.activeElement as HTMLElement).blur();
    rerender(<Reclaiming opener nestedOpen={false} />);
    await nextFrame();
    expect(document.activeElement).toBe(document.body);
  });

  it("leaves focus that moved elsewhere on purpose", async () => {
    const { getByRole, getByLabelText, rerender } = render(
      <Reclaiming opener nestedOpen={false} />,
    );
    getByRole("button", { name: "Opener" }).focus();
    rerender(<Reclaiming opener nestedOpen />);
    rerender(<Reclaiming opener={false} nestedOpen={false} />);
    getByLabelText("Field").focus();
    await nextFrame();
    expect(document.activeElement).toBe(getByLabelText("Field"));
  });

  it("does nothing when the nested flag does not change", async () => {
    const { getByRole, rerender } = render(<Reclaiming opener nestedOpen={false} />);
    getByRole("button", { name: "Opener" }).focus();
    rerender(<Reclaiming opener={false} nestedOpen={false} />);
    await nextFrame();
    expect(document.activeElement).toBe(document.body);
  });
});

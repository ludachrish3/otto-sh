// web/src/__tests__/useopenfocus.test.tsx
// ui/useOpenFocus.ts's contract (#957): a caller must render the hook's
// `scope` inside the overlay's Dialog, or the disabled-field fallback has
// no focus manager. The dev-time assertion makes leaving it out loud.
import { cleanup, render } from "@testing-library/react";
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

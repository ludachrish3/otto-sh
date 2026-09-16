import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { FileIcon } from "./fileIconStub";

describe("FileIcon stub", () => {
  it("renders nothing, whatever props the vendored slot forwards", () => {
    const { container } = render(<FileIcon type="folder" variant="solid" className="size-10" />);
    expect(container.innerHTML).toBe("");
  });
});

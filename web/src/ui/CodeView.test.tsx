// CodeView's contract (Task 5 brief) — generic, presentation-only, no
// covapp imports (mirrors TreeView.tsx's "reusable outside covapp" stance).
// Expansion is fully controlled: the caller (FilePage) owns `openLines` +
// `onToggleLine`, so these tests drive that state externally rather than
// relying on any internal toggle state.
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";
import { afterEach, describe, expect, it } from "vitest";

import { type CodeLine, CodeView, type GutterCol } from "./CodeView";

afterEach(() => {
  cleanup();
});

const COLUMNS: GutterCol[] = [
  { id: "num", width: "46px", header: "#" },
  { id: "tier:sys", width: "40px", header: <span data-testid="col-sys">sys</span> },
  { id: "branches", width: "96px", header: "Branches" },
];

function makeLines(overrides: Partial<CodeLine>[] = []): CodeLine[] {
  const base: CodeLine[] = [
    {
      number: 1,
      html: '<span style="color:red">int</span> main() {',
      rowClass: "t-sys",
      cells: [<span key="n">1</span>, <span key="s">3</span>, <span key="b" />],
      expandable: true,
    },
    {
      number: 2,
      html: "}",
      rowClass: "",
      cells: [<span key="n">2</span>, <span key="s">·</span>, <span key="b" />],
    },
  ];
  return base.map((line, i) => ({ ...line, ...overrides[i] }));
}

function Harness({
  lines,
  renderExpansion,
  ticketGutterWidth,
}: {
  lines: CodeLine[];
  renderExpansion?: (line: CodeLine) => React.ReactNode;
  ticketGutterWidth?: string;
}) {
  const [openLines, setOpenLines] = useState<Set<number>>(new Set());
  function onToggleLine(n: number) {
    setOpenLines((prev) => {
      const next = new Set(prev);
      if (next.has(n)) next.delete(n);
      else next.add(n);
      return next;
    });
  }
  return (
    <CodeView
      lines={lines}
      header={<div data-testid="my-header">header content</div>}
      columns={COLUMNS}
      renderExpansion={renderExpansion}
      openLines={openLines}
      onToggleLine={onToggleLine}
      ticketGutterWidth={ticketGutterWidth}
    />
  );
}

describe("CodeView", () => {
  it("renders the caller's header ReactNode verbatim", () => {
    render(<Harness lines={makeLines()} />);
    expect(screen.getByTestId("my-header").textContent).toBe("header content");
  });

  it("renders one column header cell per GutterCol, in order, plus a sticky row", () => {
    render(<Harness lines={makeLines()} />);
    const colsRow = screen.getByTestId("code-columns");
    expect(colsRow.className).toContain("sticky");
    expect(colsRow.textContent).toContain("#");
    expect(colsRow.textContent).toContain("Branches");
    expect(screen.getByTestId("col-sys")).toBeTruthy();
  });

  it("composes the grid template as '0px <col widths...> 1fr 66px' (ticket gutter + source + expander)", () => {
    render(<Harness lines={makeLines()} />);
    const colsRow = screen.getByTestId("code-columns");
    expect(colsRow.style.gridTemplateColumns).toBe("0px 46px 40px 96px 1fr 66px");
    const row1 = screen.getByTestId("code-row-1");
    expect(row1.style.gridTemplateColumns).toBe("0px 46px 40px 96px 1fr 66px");
  });

  it("renders line.cells in order and applies rowClass to the row", () => {
    render(<Harness lines={makeLines()} />);
    const row1 = screen.getByTestId("code-row-1");
    expect(row1.className).toContain("t-sys");
    expect(row1.textContent).toContain("1");
    expect(row1.textContent).toContain("3");
  });

  it("renders line.html via dangerouslySetInnerHTML in the source cell", () => {
    render(<Harness lines={makeLines()} />);
    const row1 = screen.getByTestId("code-row-1");
    expect(row1.querySelector("span[style]")?.textContent).toBe("int");
  });

  it("shows an expander button only for expandable lines with a renderExpansion result", () => {
    render(
      <Harness
        lines={makeLines()}
        renderExpansion={(line) => (line.number === 1 ? <span>chip</span> : null)}
      />,
    );
    expect(screen.getByTestId("code-expander-1")).toBeTruthy();
    expect(screen.queryByTestId("code-expander-2")).toBeNull();
  });

  it("does not render an expander for a non-expandable line even if renderExpansion is provided", () => {
    render(<Harness lines={makeLines()} renderExpansion={() => <span>chip</span>} />);
    expect(screen.queryByTestId("code-expander-2")).toBeNull();
  });

  it("panel is closed by default (openLines empty) and opens on click, controlled via onToggleLine", () => {
    render(<Harness lines={makeLines()} renderExpansion={() => <span>chip content</span>} />);
    expect(screen.queryByTestId("ctx-panel-1")).toBeNull();

    fireEvent.click(screen.getByTestId("code-expander-1"));
    const panel = screen.getByTestId("ctx-panel-1");
    expect(panel.textContent).toContain("chip content");
    expect(panel.style.gridColumn).toBe("1 / -1");

    fireEvent.click(screen.getByTestId("code-expander-1"));
    expect(screen.queryByTestId("ctx-panel-1")).toBeNull();
  });

  it("reflects the open state via aria-expanded on the expander button", () => {
    render(<Harness lines={makeLines()} renderExpansion={() => <span>chip</span>} />);
    const btn = screen.getByTestId("code-expander-1");
    expect(btn.getAttribute("aria-expanded")).toBe("false");
    fireEvent.click(btn);
    expect(btn.getAttribute("aria-expanded")).toBe("true");
  });

  it("shows the number of top-level renderExpansion children as the expander's count", () => {
    render(
      <Harness
        lines={makeLines()}
        renderExpansion={(line) =>
          line.number === 1 ? (
            <>
              <span key="a">chip a</span>
              <span key="b">chip b</span>
              <span key="c">chip c</span>
            </>
          ) : null
        }
      />,
    );
    expect(screen.getByTestId("code-expander-1").textContent).toContain("3");
  });

  it("renders a distinct row for every line, keyed by line number", () => {
    render(<Harness lines={makeLines()} />);
    expect(screen.getByTestId("code-row-1")).toBeTruthy();
    expect(screen.getByTestId("code-row-2")).toBeTruthy();
  });

  describe("ticket gutter + line highlighting (Task 11)", () => {
    it("defaults ticketGutterWidth to 0px and keeps the leading cell a bare aria-hidden placeholder", () => {
      render(<Harness lines={makeLines()} />);
      const colsRow = screen.getByTestId("code-columns");
      expect(colsRow.style.gridTemplateColumns).toBe("0px 46px 40px 96px 1fr 66px");
      const leading = screen.getByTestId("code-row-1").firstElementChild;
      expect(leading?.getAttribute("aria-hidden")).toBe("true");
      expect(leading?.textContent).toBe("");
    });

    it("widens the leading column via ticketGutterWidth and renders a line's ticketGutter content", () => {
      render(
        <Harness
          lines={makeLines([{ ticketGutter: <a href="/x">PROJ-1</a> }])}
          ticketGutterWidth="72px"
        />,
      );
      const colsRow = screen.getByTestId("code-columns");
      expect(colsRow.style.gridTemplateColumns).toBe("72px 46px 40px 96px 1fr 66px");
      const row1Leading = screen.getByTestId("code-row-1").firstElementChild;
      // Real (potentially interactive) content must NOT be hidden from the
      // accessibility tree — unlike the empty/decorative case above.
      expect(row1Leading?.getAttribute("aria-hidden")).toBeNull();
      expect(row1Leading?.querySelector("a")?.textContent).toBe("PROJ-1");
      // Line 2 has no ticketGutter override — stays the bare placeholder
      // even while the column itself is widened for line 1's sake.
      const row2Leading = screen.getByTestId("code-row-2").firstElementChild;
      expect(row2Leading?.getAttribute("aria-hidden")).toBe("true");
      expect(row2Leading?.textContent).toBe("");
    });

    it("stamps data-highlighted=true only on rows whose CodeLine.highlighted is true", () => {
      render(<Harness lines={makeLines([{ highlighted: true }])} />);
      expect(screen.getByTestId("code-row-1").getAttribute("data-highlighted")).toBe("true");
      expect(screen.getByTestId("code-row-2").getAttribute("data-highlighted")).toBeNull();
    });
  });
});

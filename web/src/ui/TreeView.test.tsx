// TreeView is a generic, covapp-agnostic component (Task 4 brief) — these
// fixtures are deliberately NOT coverage types (DirNode/FileNode etc.),
// proving the component has no coverage-domain knowledge baked in.
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { type TreeColumn, TreeView } from "./TreeView";

interface Node {
  id: string;
  name: string;
  value: number;
  children: Node[] | null;
}

const COLUMNS: TreeColumn[] = [
  { id: "name", label: "Name", width: "minmax(120px,1fr)", align: "left" },
  { id: "value", label: "Value", width: "80px" },
];

function getChildren(item: Node): Node[] | null {
  return item.children;
}
function getRowId(item: Node): string {
  return item.id;
}
function renderName(item: Node) {
  return item.name;
}
function renderCells(item: Node) {
  return [String(item.value)];
}
function sortValue(item: Node, columnId: string): string | number {
  return columnId === "name" ? item.name.toLowerCase() : item.value;
}

afterEach(() => {
  cleanup();
});

describe("TreeView", () => {
  it('keeps the sortable header buttons OUTSIDE role="tree" (tree contains only treeitem/group content)', () => {
    const roots: Node[] = [{ id: "a", name: "Alpha", value: 10, children: null }];
    render(
      <TreeView
        roots={roots}
        getChildren={getChildren}
        getRowId={getRowId}
        columns={COLUMNS}
        renderName={renderName}
        renderCells={renderCells}
        sortValue={sortValue}
        onNavigate={vi.fn()}
      />,
    );

    const tree = screen.getByRole("tree");
    // role="tree" permits only treeitem/group descendants — the eight
    // interactive header buttons (one per column here: two) must sit
    // outside it, or assistive tech's item count/structure is wrong.
    expect(tree.contains(screen.getByTestId("tree-col-name"))).toBe(false);
    expect(tree.contains(screen.getByTestId("tree-col-value"))).toBe(false);
    expect(tree.contains(screen.getByTestId("tree-row-a"))).toBe(true);
  });

  it("expands and collapses a non-leaf row via its chevron", () => {
    const roots: Node[] = [
      {
        id: "a",
        name: "Alpha",
        value: 10,
        children: [{ id: "a1", name: "One", value: 1, children: null }],
      },
    ];
    const onNavigate = vi.fn();
    render(
      <TreeView
        roots={roots}
        getChildren={getChildren}
        getRowId={getRowId}
        columns={COLUMNS}
        renderName={renderName}
        renderCells={renderCells}
        sortValue={sortValue}
        onNavigate={onNavigate}
      />,
    );

    expect(screen.queryByTestId("tree-row-a1")).toBeNull();
    const row = screen.getByTestId("tree-row-a");
    expect(row.getAttribute("aria-expanded")).toBe("false");

    fireEvent.click(screen.getByTestId("chevron-a"));
    expect(screen.getByTestId("tree-row-a1")).toBeTruthy();
    expect(row.getAttribute("aria-expanded")).toBe("true");

    fireEvent.click(screen.getByTestId("chevron-a"));
    expect(screen.queryByTestId("tree-row-a1")).toBeNull();
    expect(row.getAttribute("aria-expanded")).toBe("false");
  });

  it("starts every row expanded when defaultExpanded is true", () => {
    const roots: Node[] = [
      {
        id: "a",
        name: "Alpha",
        value: 10,
        children: [{ id: "a1", name: "One", value: 1, children: null }],
      },
    ];
    render(
      <TreeView
        roots={roots}
        getChildren={getChildren}
        getRowId={getRowId}
        columns={COLUMNS}
        renderName={renderName}
        renderCells={renderCells}
        sortValue={sortValue}
        onNavigate={vi.fn()}
        defaultExpanded
      />,
    );
    expect(screen.getByTestId("tree-row-a1")).toBeTruthy();
  });

  it("calls onNavigate only for a name click, never for a chevron or data-cell click", () => {
    const roots: Node[] = [
      {
        id: "a",
        name: "Alpha",
        value: 10,
        children: [{ id: "a1", name: "One", value: 1, children: null }],
      },
    ];
    const onNavigate = vi.fn();
    render(
      <TreeView
        roots={roots}
        getChildren={getChildren}
        getRowId={getRowId}
        columns={COLUMNS}
        renderName={renderName}
        renderCells={renderCells}
        sortValue={sortValue}
        onNavigate={onNavigate}
      />,
    );

    // chevron click: toggles only, never navigates
    fireEvent.click(screen.getByTestId("chevron-a"));
    expect(onNavigate).not.toHaveBeenCalled();
    expect(screen.getByTestId("tree-row-a1")).toBeTruthy();

    // data-cell click: not the name, does nothing
    const row = screen.getByTestId("tree-row-a");
    fireEvent.click(within(row).getByText("10"));
    expect(onNavigate).not.toHaveBeenCalled();

    // name click: navigates, does not also toggle (row stays expanded)
    fireEvent.click(screen.getByTestId("name-a"));
    expect(onNavigate).toHaveBeenCalledTimes(1);
    expect(onNavigate).toHaveBeenCalledWith(roots[0]);
    expect(screen.getByTestId("tree-row-a1")).toBeTruthy();
  });

  it("supports the WAI-ARIA treeitem keyboard pattern: Enter navigates, ArrowRight expands, ArrowLeft collapses", () => {
    const roots: Node[] = [
      {
        id: "a",
        name: "Alpha",
        value: 10,
        children: [{ id: "a1", name: "One", value: 1, children: null }],
      },
    ];
    const onNavigate = vi.fn();
    render(
      <TreeView
        roots={roots}
        getChildren={getChildren}
        getRowId={getRowId}
        columns={COLUMNS}
        renderName={renderName}
        renderCells={renderCells}
        sortValue={sortValue}
        onNavigate={onNavigate}
      />,
    );

    const row = screen.getByTestId("tree-row-a");

    fireEvent.keyDown(row, { key: "ArrowRight" });
    expect(screen.getByTestId("tree-row-a1")).toBeTruthy();
    expect(row.getAttribute("aria-expanded")).toBe("true");

    fireEvent.keyDown(row, { key: "ArrowLeft" });
    expect(screen.queryByTestId("tree-row-a1")).toBeNull();
    expect(row.getAttribute("aria-expanded")).toBe("false");

    fireEvent.keyDown(row, { key: "Enter" });
    expect(onNavigate).toHaveBeenCalledTimes(1);
    expect(onNavigate).toHaveBeenCalledWith(roots[0]);
  });

  it("renders a header arrow and cycles column sort none -> asc -> desc -> none for a flat sibling group", () => {
    const roots: Node[] = [
      { id: "c", name: "Charlie", value: 15, children: null },
      { id: "a", name: "Alpha", value: 10, children: null },
      { id: "b", name: "Bravo", value: 20, children: null },
    ];
    render(
      <TreeView
        roots={roots}
        getChildren={getChildren}
        getRowId={getRowId}
        columns={COLUMNS}
        renderName={renderName}
        renderCells={renderCells}
        sortValue={sortValue}
        onNavigate={vi.fn()}
      />,
    );

    const order = () =>
      screen
        .getAllByRole("treeitem")
        .map((el) => el.getAttribute("data-testid")?.replace("tree-row-", ""));

    expect(order()).toEqual(["c", "a", "b"]); // given order, unsorted

    const valueHeader = screen.getByTestId("tree-col-value");
    fireEvent.click(valueHeader);
    expect(order()).toEqual(["a", "c", "b"]); // ascending by value
    expect(within(valueHeader).getByText("▲")).toBeTruthy();

    fireEvent.click(valueHeader);
    expect(order()).toEqual(["b", "c", "a"]); // descending by value
    expect(within(valueHeader).getByText("▼")).toBeTruthy();

    fireEvent.click(valueHeader);
    expect(order()).toEqual(["c", "a", "b"]); // back to given order
    expect(screen.queryByText("▲", { selector: '[data-testid="tree-col-value"] *' })).toBeNull();
    expect(screen.queryByText("▼", { selector: '[data-testid="tree-col-value"] *' })).toBeNull();
  });

  it("sorts a child sibling group independently of the root sibling group", () => {
    // Root order [b, a] and a's children order [a2, a1] are BOTH
    // deliberately non-ascending, on a different value scale (tens vs
    // hundreds) — a bug that flattened the tree before sorting, or shared
    // state across levels, would produce a visibly wrong interleaving here.
    const roots: Node[] = [
      { id: "b", name: "Bravo", value: 20, children: null },
      {
        id: "a",
        name: "Alpha",
        value: 10,
        children: [
          { id: "a2", name: "Two", value: 200, children: null },
          { id: "a1", name: "One", value: 100, children: null },
        ],
      },
    ];
    render(
      <TreeView
        roots={roots}
        getChildren={getChildren}
        getRowId={getRowId}
        columns={COLUMNS}
        renderName={renderName}
        renderCells={renderCells}
        sortValue={sortValue}
        onNavigate={vi.fn()}
        defaultExpanded
      />,
    );

    const order = () =>
      screen
        .getAllByRole("treeitem")
        .map((el) => el.getAttribute("data-testid")?.replace("tree-row-", ""));

    expect(order()).toEqual(["b", "a", "a2", "a1"]); // given order

    fireEvent.click(screen.getByTestId("tree-col-value"));
    // root group ascending: a(10) before b(20); a's children ascending
    // independently: a1(100) before a2(200) — proves each sibling group is
    // sorted on its own values, not flattened together.
    expect(order()).toEqual(["a", "a1", "a2", "b"]);
  });
});

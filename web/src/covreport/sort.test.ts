// Pins the exact behavior of the legacy static/report.js sorter that this
// module replaces: class-name contract (sortable/num/sort-asc/sort-desc),
// data-sort precedence over cell text, and NaN -> -Infinity numeric fallback.
import { describe, expect, it } from "vitest";

import { attachSort, cellValue, initReportPage, makeComparator } from "./sort";

function renderTable(rowsHtml: string, tableClass = "files-table"): HTMLTableElement {
  document.body.innerHTML = `
    <table class="${tableClass}">
      <thead><tr>
        <th class="sortable">File</th>
        <th class="sortable num">Line %</th>
      </tr></thead>
      <tbody>${rowsHtml}</tbody>
    </table>`;
  return document.querySelector("table") as HTMLTableElement;
}

const ROWS = `
  <tr><td>b.c</td><td data-sort="9.5">9.5%</td></tr>
  <tr><td>a.c</td><td data-sort="100.0">100.0%</td></tr>
  <tr><td>c.c</td><td>&mdash;</td></tr>`;

function column(table: HTMLTableElement, idx: number): string[] {
  return Array.from(table.tBodies[0].rows).map((r) => (r.cells[idx].textContent ?? "").trim());
}

function header(table: HTMLTableElement, idx: number): HTMLTableCellElement {
  return table.querySelectorAll<HTMLTableCellElement>("thead th")[idx];
}

describe("cellValue", () => {
  it("prefers data-sort over cell text", () => {
    const table = renderTable(ROWS);
    expect(cellValue(table.tBodies[0].rows[0], 1)).toBe("9.5");
  });

  it("falls back to trimmed textContent without data-sort", () => {
    const table = renderTable(ROWS);
    expect(cellValue(table.tBodies[0].rows[0], 0)).toBe("b.c");
  });

  it("returns empty string for a missing cell", () => {
    const table = renderTable(ROWS);
    expect(cellValue(table.tBodies[0].rows[0], 99)).toBe("");
  });
});

describe("makeComparator", () => {
  it("treats non-numeric values as -Infinity in numeric mode", () => {
    const table = renderTable(ROWS);
    const rows = Array.from(table.tBodies[0].rows);
    rows.sort(makeComparator(1, true, true));
    // ascending: the dash row (no data-sort, NaN) sorts first
    expect(rows.map((r) => r.cells[0].textContent)).toEqual(["c.c", "b.c", "a.c"]);
  });
});

describe("attachSort", () => {
  it("sorts text columns ascending on first click and marks sort-asc", () => {
    const table = renderTable(ROWS);
    attachSort(table);
    header(table, 0).click();
    expect(column(table, 0)).toEqual(["a.c", "b.c", "c.c"]);
    expect(header(table, 0).classList.contains("sort-asc")).toBe(true);
  });

  it("re-clicking flips to descending and swaps the marker class", () => {
    const table = renderTable(ROWS);
    attachSort(table);
    header(table, 0).click();
    header(table, 0).click();
    expect(column(table, 0)).toEqual(["c.c", "b.c", "a.c"]);
    expect(header(table, 0).classList.contains("sort-desc")).toBe(true);
    expect(header(table, 0).classList.contains("sort-asc")).toBe(false);
  });

  it("sorts num columns numerically via data-sort (9.5 before 100.0)", () => {
    const table = renderTable(ROWS);
    attachSort(table);
    header(table, 1).click();
    expect(column(table, 0)).toEqual(["c.c", "b.c", "a.c"]);
    header(table, 1).click();
    expect(column(table, 0)).toEqual(["a.c", "b.c", "c.c"]);
  });

  it("clicking a second column clears the first column's marker", () => {
    const table = renderTable(ROWS);
    attachSort(table);
    header(table, 0).click();
    header(table, 1).click();
    expect(header(table, 0).classList.contains("sort-asc")).toBe(false);
    expect(header(table, 1).classList.contains("sort-asc")).toBe(true);
  });
});

describe("initReportPage", () => {
  it("wires only .files-table tables", () => {
    document.body.innerHTML = `
      <table class="summary-table">
        <thead><tr><th class="sortable">X</th></tr></thead>
        <tbody><tr><td>2</td></tr><tr><td>1</td></tr></tbody>
      </table>`;
    initReportPage(document);
    const table = document.querySelector("table") as HTMLTableElement;
    (table.querySelector("th") as HTMLTableCellElement).click();
    // untouched: no sort marker, original row order preserved
    expect(table.querySelector("th.sort-asc")).toBeNull();
    expect(column(table, 0)).toEqual(["2", "1"]);
  });
});

// Otto coverage report — click-to-sort for the index's files table.
//
// Faithful TypeScript port of the legacy static/report.js: the class-name
// contract (sortable/num on <th>, sort-asc/sort-desc as the active marker)
// and the data-sort-over-textContent precedence are pinned by sort.test.ts
// and by the Playwright suite in tests/e2e/cov/report_browser/.

export function cellValue(row: HTMLTableRowElement, idx: number): string {
  const cell = row.cells[idx];
  if (!cell) return "";
  const raw = cell.getAttribute("data-sort");
  if (raw !== null) return raw;
  return (cell.textContent ?? "").trim();
}

export function makeComparator(
  idx: number,
  asc: boolean,
  numeric: boolean,
): (a: HTMLTableRowElement, b: HTMLTableRowElement) => number {
  return (a, b) => {
    const av = cellValue(a, idx);
    const bv = cellValue(b, idx);
    if (numeric) {
      let an = Number.parseFloat(av);
      let bn = Number.parseFloat(bv);
      if (Number.isNaN(an)) an = Number.NEGATIVE_INFINITY;
      if (Number.isNaN(bn)) bn = Number.NEGATIVE_INFINITY;
      return asc ? an - bn : bn - an;
    }
    return asc ? av.localeCompare(bv) : bv.localeCompare(av);
  };
}

export function attachSort(table: HTMLTableElement): void {
  const headers = table.querySelectorAll<HTMLTableCellElement>("thead th.sortable");
  headers.forEach((th, idx) => {
    th.addEventListener("click", () => {
      const tbody = table.tBodies[0];
      if (!tbody) return;
      const rows = Array.from(tbody.rows);
      const asc = !th.classList.contains("sort-asc");
      headers.forEach((h) => {
        h.classList.remove("sort-asc");
        h.classList.remove("sort-desc");
      });
      th.classList.add(asc ? "sort-asc" : "sort-desc");
      const numeric = th.classList.contains("num");
      rows.sort(makeComparator(idx, asc, numeric));
      rows.forEach((r) => tbody.appendChild(r));
    });
  });
}

export function initReportPage(root: Document = document): void {
  root.querySelectorAll<HTMLTableElement>("table.files-table").forEach(attachSort);
}

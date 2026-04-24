// Otto coverage report — simple click-to-sort for the files table.
//
// No dependencies, no build step. Loaded once per page.
(function () {
  "use strict";

  function cellValue(row, idx) {
    var cell = row.cells[idx];
    if (!cell) return "";
    var raw = cell.getAttribute("data-sort");
    if (raw !== null) return raw;
    return cell.textContent.trim();
  }

  function makeComparator(idx, asc, numeric) {
    return function (a, b) {
      var av = cellValue(a, idx);
      var bv = cellValue(b, idx);
      if (numeric) {
        var an = parseFloat(av);
        var bn = parseFloat(bv);
        if (isNaN(an)) an = -Infinity;
        if (isNaN(bn)) bn = -Infinity;
        return asc ? an - bn : bn - an;
      }
      return asc ? av.localeCompare(bv) : bv.localeCompare(av);
    };
  }

  function attachSort(table) {
    var headers = table.querySelectorAll("thead th.sortable");
    headers.forEach(function (th, idx) {
      th.addEventListener("click", function () {
        var tbody = table.tBodies[0];
        if (!tbody) return;
        var rows = Array.prototype.slice.call(tbody.rows);
        var asc = !th.classList.contains("sort-asc");
        headers.forEach(function (h) {
          h.classList.remove("sort-asc");
          h.classList.remove("sort-desc");
        });
        th.classList.add(asc ? "sort-asc" : "sort-desc");
        var numeric = th.classList.contains("num");
        rows.sort(makeComparator(idx, asc, numeric));
        rows.forEach(function (r) { tbody.appendChild(r); });
      });
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll("table.files-table").forEach(attachSort);
  });
})();

// Client-side sorting and filtering for tables marked .table-sortable,
// filter inputs with data-filter="#tableId".
(function () {
  function cellValue(row, idx) {
    const cell = row.children[idx];
    if (!cell) return "";
    const v = cell.dataset.value !== undefined ? cell.dataset.value : cell.textContent.trim();
    const n = Number(v.replace(/,/g, ""));
    return v !== "" && !isNaN(n) ? n : v.toLowerCase();
  }
  document.querySelectorAll("table.table-sortable").forEach(function (table) {
    table.querySelectorAll("th[data-sort]").forEach(function (th) {
      th.addEventListener("click", function () {
        const idx = Array.prototype.indexOf.call(th.parentNode.children, th);
        const asc = !th.classList.contains("sorted-asc");
        table.querySelectorAll("th").forEach(h => h.classList.remove("sorted-asc", "sorted-desc"));
        th.classList.add(asc ? "sorted-asc" : "sorted-desc");
        const body = table.tBodies[0];
        Array.from(body.rows).sort(function (a, b) {
          const x = cellValue(a, idx), y = cellValue(b, idx);
          if (x === y) return 0;
          return (x > y ? 1 : -1) * (asc ? 1 : -1);
        }).forEach(r => body.appendChild(r));
      });
    });
  });
  document.querySelectorAll("input[data-filter]").forEach(function (input) {
    const table = document.querySelector(input.dataset.filter);
    if (!table) return;
    input.addEventListener("input", function () {
      const q = input.value.toLowerCase();
      Array.from(table.tBodies[0].rows).forEach(function (r) {
        r.style.display = r.textContent.toLowerCase().includes(q) ? "" : "none";
      });
    });
  });
  // Confirm destructive actions
  document.querySelectorAll("form[data-confirm]").forEach(function (f) {
    f.addEventListener("submit", function (e) { if (!confirm(f.dataset.confirm)) e.preventDefault(); });
  });
})();

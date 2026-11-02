// RailMon UI: station/train autocomplete, swap, quick dates, result filters.
(function () {
  "use strict";
  const MIN_CHARS = 2;
  const cache = new Map();

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }
  function highlight(text, q) {
    const t = String(text || ""), i = t.toLowerCase().indexOf(q.toLowerCase());
    if (!q || i < 0) return esc(t);
    return esc(t.slice(0, i)) + "<mark>" + esc(t.slice(i, i + q.length)) + "</mark>" + esc(t.slice(i + q.length));
  }

  function setup(input) {
    const kind = input.dataset.ac; // "station" | "train"
    const field = input.closest(".rm-field") || input.parentElement;
    field.classList.add("rm-ac-host");
    const list = document.createElement("ul");
    list.className = "rm-ac";
    list.setAttribute("role", "listbox");
    list.hidden = true;
    field.appendChild(list);
    input.setAttribute("role", "combobox");
    input.setAttribute("aria-autocomplete", "list");
    input.setAttribute("autocomplete", "off");
    input.setAttribute("spellcheck", "false");

    let items = [], active = -1, timer = null, ctrl = null, lastQ = "";

    function close() { list.hidden = true; active = -1; input.setAttribute("aria-expanded", "false"); }
    function open() { list.hidden = false; input.setAttribute("aria-expanded", "true"); }

    function render(q) {
      if (!items.length) {
        list.innerHTML = '<li class="rm-ac-empty">No matches for “' + esc(q) + '”</li>';
        open();
        return;
      }
      list.innerHTML = items.map(function (it, i) {
        if (kind === "station") {
          return '<li role="option" data-i="' + i + '" class="' + (i === active ? "active" : "") + '">' +
            '<span class="rm-ac-code">' + highlight(it.code, q) + '</span>' +
            '<span class="rm-ac-main"><b>' + highlight(it.name, q) + '</b><small>' + esc(it.state || "") +
            (it.trains ? (it.state ? " · " : "") + it.trains + " trains" : "") + '</small></span>' +
            '<i class="bi bi-geo-alt rm-ac-icon"></i></li>';
        }
        return '<li role="option" data-i="' + i + '" class="' + (i === active ? "active" : "") + '">' +
          '<span class="rm-ac-code rm-ac-num">' + highlight(it.number, q) + '</span>' +
          '<span class="rm-ac-main"><b>' + highlight(it.name, q) + '</b><small>' + esc(it.from || "") + " → " + esc(it.to || "") + '</small></span>' +
          '<i class="bi bi-train-front rm-ac-icon"></i></li>';
      }).join("");
      open();
    }

    function choose(i) {
      const it = items[i];
      if (!it) return;
      if (input.dataset.acFill === "code") input.value = kind === "station" ? it.code : it.number;
      else input.value = kind === "station" ? it.name + " (" + it.code + ")" : it.number + " · " + it.name;
      input.dataset.code = kind === "station" ? it.code : it.number;
      close();
      input.dispatchEvent(new Event("change", { bubbles: true }));
      // move to the next empty field (e.g. From -> To)
      const form = input.form;
      if (form) {
        const next = Array.from(form.querySelectorAll("input[data-ac]")).find(el => el !== input && !el.value);
        if (next) next.focus();
      }
    }

    function search(q) {
      const key = kind + ":" + q.toLowerCase();
      if (cache.has(key)) { items = cache.get(key); active = items.length ? 0 : -1; render(q); return; }
      if (ctrl) ctrl.abort();
      ctrl = new AbortController();
      const url = "/api/v1/suggest/" + (kind === "station" ? "stations" : "trains") + "?q=" + encodeURIComponent(q);
      fetch(url, { signal: ctrl.signal, headers: { Accept: "application/json" } })
        .then(r => r.ok ? r.json() : { items: [] })
        .then(function (data) {
          if (q !== lastQ) return; // stale response
          items = data.items || [];
          cache.set(key, items);
          active = items.length ? 0 : -1;
          render(q);
        })
        .catch(function () { /* aborted or offline: keep quiet */ });
    }

    input.addEventListener("input", function () {
      delete input.dataset.code;
      const q = input.value.trim();
      lastQ = q;
      clearTimeout(timer);
      if (q.length < MIN_CHARS) { close(); return; }
      timer = setTimeout(function () { search(q); }, 120);
    });
    input.addEventListener("focus", function () { if (input.value.trim().length >= MIN_CHARS && items.length && !input.dataset.code) render(input.value.trim()); });
    input.addEventListener("keydown", function (e) {
      if (list.hidden) { if (e.key === "ArrowDown" && input.value.trim().length >= MIN_CHARS) { search(input.value.trim()); e.preventDefault(); } return; }
      if (e.key === "ArrowDown") { active = Math.min(active + 1, items.length - 1); render(input.value.trim()); e.preventDefault(); }
      else if (e.key === "ArrowUp") { active = Math.max(active - 1, 0); render(input.value.trim()); e.preventDefault(); }
      else if (e.key === "Enter") { if (active >= 0 && items.length) { choose(active); e.preventDefault(); } }
      else if (e.key === "Tab") { if (active >= 0 && items.length) choose(active); }
      else if (e.key === "Escape") { close(); }
    });
    list.addEventListener("mousedown", function (e) {
      const li = e.target.closest("li[data-i]");
      if (li) { e.preventDefault(); choose(+li.dataset.i); }
    });
    input.addEventListener("blur", function () { setTimeout(close, 120); });
  }

  document.querySelectorAll("input[data-ac]").forEach(setup);

  // Swap From/To
  document.querySelectorAll("[data-swap]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      const [a, b] = btn.dataset.swap.split(",").map(s => document.querySelector(s));
      if (!a || !b) return;
      [a.value, b.value] = [b.value, a.value];
      btn.classList.add("spin");
      setTimeout(() => btn.classList.remove("spin"), 300);
    });
  });

  // Quick date chips
  document.querySelectorAll("[data-date-offset]").forEach(function (chip) {
    chip.addEventListener("click", function () {
      const input = document.querySelector("#date");
      if (!input) return;
      const d = new Date();
      d.setDate(d.getDate() + parseInt(chip.dataset.dateOffset, 10));
      input.value = d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0") + "-" + String(d.getDate()).padStart(2, "0");
      document.querySelectorAll("[data-date-offset]").forEach(c => c.classList.toggle("active", c === chip));
    });
  });

  // Search results: sort + filters
  const results = document.getElementById("results");
  if (results) {
    const cards = Array.from(results.querySelectorAll(".rm-train"));
    const sortSel = document.getElementById("sortby");
    function apply() {
      const slots = Array.from(document.querySelectorAll("[data-filter-slot].active")).map(b => b.dataset.filterSlot);
      const types = Array.from(document.querySelectorAll("[data-filter-type].active")).map(b => b.dataset.filterType);
      let shown = 0;
      cards.forEach(function (c) {
        const ok = (!slots.length || slots.includes(c.dataset.slot)) && (!types.length || types.includes(c.dataset.type));
        c.classList.toggle("d-none", !ok);
        if (ok) shown++;
      });
      const by = sortSel ? sortSel.value : "dep";
      const key = c => by === "dur" ? +c.dataset.dur : by === "arr" ? +c.dataset.arr : c.dataset.dep;
      cards.slice().sort((x, y) => key(x) > key(y) ? 1 : key(x) < key(y) ? -1 : 0).forEach(c => results.appendChild(c));
      const count = document.getElementById("rcount");
      if (count) count.textContent = shown;
      document.getElementById("noresults").classList.toggle("d-none", shown > 0);
    }
    document.querySelectorAll(".rm-toggle").forEach(b => b.addEventListener("click", function () { b.classList.toggle("active"); apply(); }));
    if (sortSel) sortSel.addEventListener("change", apply);
  }
})();

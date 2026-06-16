// TrueSight — page Inventaire logiciel : agrégat du parc.
// Données : GET /api/v1/inventory/software?q=<filtre> →
//   [{name,version,publisher,agent_count}]
// La barre de recherche globale (topbar) pilote le filtre `q` côté serveur (debounce).
(function () {
  "use strict";

  var DEBOUNCE_MS = 250;
  var search = document.getElementById("global-search");
  var debounceTimer = null;

  function esc(s) {
    if (s === null || s === undefined) return "";
    return String(s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function render(rows) {
    var body = document.getElementById("inventory-body");
    if (!body) return;

    var countEl = document.getElementById("inventory-count");
    if (countEl) {
      var n = rows.length;
      countEl.textContent = n + (n === 1 ? " entrée" : " entrées") + (n >= 500 ? " (max)" : "");
    }

    if (!rows.length) {
      body.innerHTML = '<tr><td colspan="4" class="empty-cell">Aucun logiciel — déployez un agent ou ajustez la recherche.</td></tr>';
      return;
    }

    body.innerHTML = rows
      .map(function (r) {
        var count = r.agent_count || 0;
        return (
          "<tr>" +
          '<td class="mono">' + esc(r.name || "—") + "</td>" +
          '<td class="mono text-dim">' + esc(r.version || "—") + "</td>" +
          '<td class="text-dim">' + esc(r.publisher || "—") + "</td>" +
          '<td class="num"><span class="chip" title="' + count + (count === 1 ? " poste" : " postes") + '">' + count + "</span></td>" +
          "</tr>"
        );
      })
      .join("");
  }

  async function load() {
    var body = document.getElementById("inventory-body");
    var q = (search && search.value) || "";
    try {
      var resp = await fetch("/api/v1/inventory/software?q=" + encodeURIComponent(q), {
        headers: { Accept: "application/json" },
      });
      if (resp.status === 401) { window.location.href = "/login"; return; }
      if (!resp.ok) throw new Error("HTTP " + resp.status);
      render(await resp.json());
    } catch (e) {
      if (body) body.innerHTML = '<tr><td colspan="4" class="empty-cell err-cell">Erreur de chargement</td></tr>';
    }
  }

  // Recherche : debounce sur la barre globale (filtrage serveur).
  if (search) {
    search.setAttribute("placeholder", "Rechercher un logiciel, un éditeur…");
    search.addEventListener("input", function () {
      clearTimeout(debounceTimer);
      debounceTimer = setTimeout(load, DEBOUNCE_MS);
    });
  }

  // Raccourci "/" pour focaliser la recherche.
  document.addEventListener("keydown", function (e) {
    if (e.key === "/" && document.activeElement !== search &&
        !/^(INPUT|TEXTAREA|SELECT)$/.test((document.activeElement || {}).tagName || "")) {
      if (search) { e.preventDefault(); search.focus(); }
    }
  });

  var refresh = document.getElementById("refresh");
  if (refresh) refresh.addEventListener("click", load);

  load();
})();

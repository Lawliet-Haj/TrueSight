// Rendu du journal d'audit (page admin). Données : GET /api/v1/audit?limit=200.
// Externalisé depuis audit.html pour permettre une CSP stricte (script-src 'self').
(function () {
  "use strict";

  // Classe de badge par type d'action.
  var actionBadge = {
    "login.success": "ok",
    "login.fail": "danger",
    "logout": "off",
    "command.create": "warn",
    "agent.revoke": "danger",
    "remote.start": "info",
    "remote.end": "off",
  };

  function esc(s) {
    if (s === null || s === undefined) return "";
    return String(s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  async function loadAudit() {
    var body = document.getElementById("audit-body");
    try {
      var resp = await fetch("/api/v1/audit?limit=200", { headers: { Accept: "application/json" } });
      if (resp.status === 401) { window.location.href = "/login"; return; }
      if (!resp.ok) throw new Error("HTTP " + resp.status);
      var rows = await resp.json();

      var countEl = document.getElementById("audit-count");
      if (countEl) countEl.textContent = rows.length + (rows.length === 1 ? " entrée" : " entrées");

      if (!rows.length) {
        body.innerHTML = '<tr><td colspan="6" class="empty-cell">Aucune entrée</td></tr>';
        return;
      }
      body.innerHTML = rows.map(function (r) {
        var cls = actionBadge[r.action] || "";
        var details = r.details && Object.keys(r.details).length ? esc(JSON.stringify(r.details)) : "";
        return '<tr>' +
          '<td class="mono text-faint nowrap">' + esc((r.ts || "").replace("T", " ").replace("Z", "")) + "</td>" +
          '<td><span class="badge ' + cls + '">' + esc(r.action) + "</span></td>" +
          '<td class="text-dim">' + esc(r.user_email || (r.user_id || "—")) + "</td>" +
          '<td class="mono text-faint">' + esc(r.target_agent || "—") + "</td>" +
          '<td class="mono text-faint">' + esc(r.ip || "—") + "</td>" +
          '<td class="mono text-faint" style="max-width:340px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="' + details + '">' + details + "</td>" +
          "</tr>";
      }).join("");
    } catch (e) {
      body.innerHTML = '<tr><td colspan="6" class="empty-cell err-cell">Erreur de chargement</td></tr>';
    }
  }

  document.getElementById("refresh").addEventListener("click", loadAudit);
  loadAudit();
})();
